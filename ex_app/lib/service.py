#
# SPDX-FileCopyrightText: 2025 Nextcloud GmbH and Nextcloud contributors
# SPDX-License-Identifier: AGPL-3.0-or-later
#

import asyncio
import dataclasses
import hashlib
import hmac
import json
import logging
import os
import ssl
import threading
from collections.abc import Callable
from contextlib import suppress
from enum import IntEnum
from functools import partial
from secrets import token_urlsafe
from urllib.parse import urlparse

from aiortc import AudioStreamTrack, RTCPeerConnection, RTCSessionDescription
from aiortc.mediastreams import MediaStreamError
from aiortc.rtcconfiguration import RTCConfiguration, RTCIceServer
from aiortc.sdp import candidate_from_sdp
from av.audio.frame import AudioFrame
from av.audio.resampler import AudioResampler
from dotenv import load_dotenv
from livetypes import (
	HPBSettings,
	LanguageSetRequest,
	SpreedClientException,
	StreamEndedException,
	Target,
	TranscribeRequest,
	VoskException,
)
from models import LANGUAGE_MAP
from nc_py_api import NextcloudApp
from websockets import ClientConnection
from websockets import State as WsState
from websockets import connect
from websockets.exceptions import WebSocketException

load_dotenv()

MSG_RECEIVE_TIMEOUT = 10  # seconds
MAX_CONNECT_TRIES = 5  # maximum number of connection attempts
MAX_AUDIO_FRAMES = 20  # maximum number of audio frames to collect before sending to Vosk
HPB_SHUTDOWN_TIMEOUT = 30  # seconds to wait for the ws connectino to shut down gracefully
CALL_LEAVE_TIMEOUT = 60  # seconds to wait before leaving the call if there are no targets
# wait VOSK_CONNECT_TIMEOUT seconds for the Vosk server handshake to complete,
# this includes the language load time in the Vosk server
VOSK_CONNECT_TIMEOUT = 60
LOGGER = logging.getLogger("lt.service")


class CALL_FLAG(IntEnum):
	DISCONNECTED = 0
	IN_CALL      = 1
	WITH_AUDIO   = 2
	WITH_VIDEO   = 4
	WITH_PHONE   = 8


class ConnectResult(IntEnum):
	SUCCESS = 0
	FAILURE = 1  # do not retry
	RETRY   = 2


def hmac_sha256(key, message):
	return hmac.new(
		key.encode("utf-8"),
		message.encode("utf-8"),
		hashlib.sha256
	).hexdigest()


def get_ssl_context(server_addr: str) -> ssl.SSLContext | None:
	nc = NextcloudApp()

	if server_addr.startswith(("ws://", "http://")):
		LOGGER.info("Using default SSL context for insecure WebSocket connection (ws://)", extra={
			"server_addr": server_addr,
			"tag": "connection",
		})
		return None

	cert_verify = os.environ.get("SKIP_CERT_VERIFY", "false").lower()
	if cert_verify in ("true", "1"):
		LOGGER.info("Skipping certificate verification for WebSocket connection", extra={
			"server_addr": server_addr,
			"SKIP_CERT_VERIFY": cert_verify,
			"tag": "connection",
		})
		ssl_ctx = ssl.SSLContext()
		ssl_ctx.check_hostname = False
		ssl_ctx.verify_mode = ssl.CERT_NONE
		return ssl_ctx

	if nc.app_cfg.options.nc_cert and isinstance(nc.app_cfg.options.nc_cert, ssl.SSLContext):
		# Use the SSL context provided by nc_py_api
		LOGGER.info("Using SSL context provided by nc_py_api", extra={
			"server_addr": server_addr,
			"tag": "connection",
		})
		return nc.app_cfg.options.nc_cert

	# verify certificate normally and don't use SSLContext from nc_py_api
	LOGGER.info("Using default SSL context for WebSocket connection", extra={
		"server_addr": server_addr,
		"tag": "connection",
	})
	return None


class PeerConnection:
	def __init__(self, session_id: str, pc: RTCPeerConnection):
		self.session_id = session_id
		self.pc = pc


class AudioStream:
	def __init__(self, track: AudioStreamTrack):
		self.track = track
		self._ended = False

		@track.on("ended")
		async def on_ended():
			LOGGER.debug("Track ended", extra={"tag": "track"})
			self._ended = True

	async def receive(self) -> AudioFrame:
		"""Receive the next audio frame."""
		if self._ended:
			raise StreamEndedException("Track has ended")
		return await self.track.recv()  # type: ignore[return-value]

	def stop(self):
		"""Stop the audio stream."""
		self.track.stop()


# data carrier in the transcript_queue
@dataclasses.dataclass
class Transcript:
	message: str
	speaker_session_id: str


class SpreedClient:
	def __init__(
		self,
		room_token: str,
		hpb_settings: HPBSettings,
		lang_id: str,
		leave_call_cb: Callable[[str], None],  # room_token
	) -> None:
		self.id = 0
		self._server: ClientConnection | None = None
		self._monitor: asyncio.Task | None = None
		self.peer_connections: dict[str, PeerConnection] = {}
		self.peer_connection_lock = threading.Lock()
		self.targets: dict[str, Target] = {}
		self.target_lock = threading.Lock()
		self.transcript_queue: asyncio.Queue = asyncio.Queue()
		self._transcript_sender: asyncio.Task | None = None
		self.transcribers: dict[str, VoskTranscriber] = {}
		self.transcriber_lock = threading.Lock()
		self.defunct = threading.Event()
		self._close_task: asyncio.Task | None = None
		self._deferred_close_task: asyncio.Task | None = None

		self.resumeid = None
		self.sessionid = None

		nc = NextcloudApp()
		self._websopcket_url = os.environ["LT_HPB_URL"]
		self._backendURL = nc.app_cfg.endpoint + "/ocs/v2.php/apps/spreed/api/v3/signaling/backend"
		self.secret = os.environ["LT_INTERNAL_SECRET"]

		self.room_token = room_token
		self.hpb_settings = hpb_settings
		self.lang_id = lang_id
		self.leave_call_cb = leave_call_cb


	async def connect(self) -> ConnectResult:
		websopcket_host = urlparse(self._websopcket_url).hostname
		ssl_ctx = get_ssl_context(self._websopcket_url)
		self._server = await connect(
			self._websopcket_url,
			**({
				"server_hostname": websopcket_host,
				"ssl": ssl_ctx,  # type: ignore[arg-type]
			} if ssl_ctx else {}),
		)

		await self.send_hello()

		msg_counter = 0
		while True:
			message = await self.receive(MSG_RECEIVE_TIMEOUT)
			if message is None:
				LOGGER.error("No message received for %s secs, aborting...", MSG_RECEIVE_TIMEOUT, extra={
					"room_token": self.room_token,
					"msg_counter": msg_counter,
					"tag": "connection",
				})
				return ConnectResult.FAILURE

			if message.get("type") == "error":
				LOGGER.error(
					"Error message received: %s\nDetails: %s", message.get("error", {}).get("message"),
					message.get("error", {}).get("details"), extra={
						"room_token": self.room_token,
						"msg_counter": msg_counter,
						"tag": "connection",
					},
				)
				return ConnectResult.FAILURE

			if message.get("type") == "bye":
				LOGGER.info("Received bye message, closing connection", extra={
					"room_token": self.room_token,
					"msg_counter": msg_counter,
					"tag": "connection",
				})
				return ConnectResult.FAILURE

			if message.get("type") == "welcome":
				LOGGER.debug("Welcome message received", extra={
					"room_token": self.room_token,
					"msg_counter": msg_counter,
					"tag": "connection",
				})
				continue

			if message.get("type") == "hello":
				self.sessionid = message["hello"]["sessionid"]
				self.resumeid = message["hello"]["resumeid"]
				LOGGER.debug("Hello message received", extra={
					"sessionid": self.sessionid,
					"resumeid": self.resumeid,
					"room_token": self.room_token,
					"msg_counter": msg_counter,
					"tag": "connection",
				})
				break

			if msg_counter > 10:
				LOGGER.error(
					"Too many messages received without 'welcome', reconnecting...",
					extra={
						"room_token": self.room_token,
						"msg_counter": msg_counter,
						"tag": "connection",
					},
				)
				return ConnectResult.RETRY

		self._monitor = asyncio.create_task(self.signalling_monitor())
		self._transcript_sender = asyncio.create_task(self.transcipt_queue_consumer())
		# leave the call if there are no targets after some time
		self._deferred_close_task = asyncio.create_task(self.maybe_leave_call())

		await self.send_incall()
		await self.send_join()
		self.defunct.clear()
		LOGGER.info("Connected to signaling server", extra={
			"room_token": self.room_token,
			"tag": "connection",
		})
		return ConnectResult.SUCCESS

	async def send_message(self, message: dict):
		if not self._server:
			LOGGER.error("No server connection, cannot send message", extra={
				"room_token": self.room_token,
				"send_message": message,
				"tag": "send_message",
			})
			return

		self.id += 1
		message["id"] = str(self.id)
		await self._server.send(json.dumps(message))
		LOGGER.debug("Message sent: %s", message, extra={
			"id": self.id,
			"room_token": self.room_token,
			"tag": "send_message",
		})

	async def send_hello(self):
		nonce = token_urlsafe(64)
		await self.send_message({
			"type": "hello",
			"hello": {
				"version": "2.0",
				"auth": {
					"type": "internal",
					"params": {
						"random": nonce,
						"token": hmac_sha256(self.secret, nonce),
						"backend": self._backendURL,
					}
				},
			},
		})

	async def send_incall(self):
		await self.send_message({
			"type": "internal",
			"internal": {
				"type": "incall",
				"incall": {
					"incall": CALL_FLAG.IN_CALL,
				},
			},
		})

	async def send_join(self):
		await self.send_message({
			"type": "room",
			"room": {
				"roomid": self.room_token,
				"sessionid": self.sessionid
			}
		})

	async def send_offer_request(self, publisher_session_id):
		await self.send_message({
			"type": "message",
			"message": {
				"recipient": {
					"type": "session",
					"sessionid": publisher_session_id
				},
				"data": {
					"type": "requestoffer",
					"roomType": "video"
				}
			}
		})

	async def send_offer_answer(self, publisher_session_id, offer_sid, sdp):
		await self.send_message({
			"type": "message",
			"message": {
				"recipient": {
					"type": "session",
					"sessionid": publisher_session_id
				},
				"data": {
					"to": publisher_session_id,
					"type": "answer",
					"roomType": "video",
					"sid": offer_sid,
					"payload": {
						"nick": "I am the big transcriber",
						"type": "answer",
						"sdp": sdp
					}
				}
			}
		})

	async def send_candidate(self, sender, offer_sid, candidate_str):
		await self.send_message({
			"type": "message",
			"message": {
				"recipient": {
					"type": "session",
					"sessionid": sender,
				},
				"data": {
					"to": sender,
					"type": "candidate",
					"sid": offer_sid,
					"roomType": "video",
					"payload": {
						"candidate": {
							"candidate": candidate_str,
							"sdpMLineIndex": 0,
							"sdpMid": "0",
						}
					}
				}
			}
		})

	async def send_bye(self):
		await self.send_message({
			"type": "bye",
			"bye": {}
		})

	async def send_transcript(self, message: str, spk_session_id: str):
		with self.target_lock:
			sids = list(self.targets.keys())
		for sid in sids:
			await self.send_message({
				"type": "message",
				"message": {
					"recipient": {
						"type": "session",
						"sessionid": sid,
					},
					"data": {
						"message": message,
						"type": "transcript",
						"speakerSessionId": spk_session_id,
					}
				}
			})

	# todo: add function to reconnect to hpb, full SpreedClient lifecycle
	async def close(self, using_resume: bool = False):
		if self.defunct.is_set():
			LOGGER.debug("SpreedClient is already defunct, skipping close", extra={
				"room_token": self.room_token,
				"using_resume": using_resume,
				"tag": "client",
			})
			return

		app_closing = self._monitor.cancelled() if self._monitor else False

		with suppress(Exception):
			if self._monitor and not self._monitor.done():
				LOGGER.debug("Cancelling monitor task", extra={
					"room_token": self.room_token,
					"using_resume": using_resume,
					"tag": "monitor",
				})
				# Cancel the monitor task if it's still running
				self._monitor.cancel()
			self._monitor = None

		with suppress(Exception):
			await self.send_bye()

		with suppress(Exception):
			if not using_resume:
				for pc in self.peer_connections.values():
					if pc.pc.connectionState != "closed" and pc.pc.connectionState != "failed":
						LOGGER.debug("Closing peer connection", extra={
							"session_id": pc.session_id,
							"room_token": self.room_token,
							"using_resume": using_resume,
							"tag": "peer_connection",
						})
						with suppress(Exception):
							await pc.pc.close()
				with self.peer_connection_lock:
					self.peer_connections.clear()
				self.resumeid = None
				self.sessionid = None

		with suppress(Exception):
			LOGGER.debug("Shutting down all transcribers", extra={
				"room_token": self.room_token,
				"using_resume": using_resume,
				"tag": "transcriber",
			})
			for transcriber in self.transcribers.values():
				transcriber.shutdown()
			with self.transcriber_lock:
				self.transcribers.clear()

		with suppress(Exception):
			if self._transcript_sender and not self._transcript_sender.done():
				LOGGER.debug("Cancelling transcript sender task", extra={
					"room_token": self.room_token,
					"using_resume": using_resume,
					"tag": "transcript",
				})
				self._transcript_sender.cancel()
				self._transcript_sender = None

		with suppress(Exception):
			if self._server and self._server.state == WsState.OPEN:
				LOGGER.debug("Closing WebSocket connection", extra={
					"room_token": self.room_token,
					"using_resume": using_resume,
					"tag": "connection",
				})
				# Close the WebSocket connection if it's still open
				await self._server.close()
			self._server = None

		if not using_resume:
			self.defunct.set()
			if not app_closing:
				self.leave_call_cb(self.room_token)

	async def receive(self, timeout: int = 0) -> dict | None:
		if not self._server:
			LOGGER.debug("No server connection, cannot receive message", extra={
				"room_token": self.room_token,
				"tag": "receive",
			})
			return None

		# caller handles the exceptions
		if timeout > 0:
			received_msg = await asyncio.wait_for(self._server.recv(), timeout)
		else:
			received_msg = await self._server.recv()

		message = json.loads(received_msg)
		LOGGER.debug("Message received", extra={
			"recv_message": message,
			"room_token": self.room_token,
			"tag": "receive",
		})
		return message

	def add_target(self, session_id: str):
		with self.target_lock:
			if session_id not in self.targets:
				self.targets[session_id] = Target()
				LOGGER.debug("Added target session id: %s", session_id, extra={
					"session_id": session_id,
					"room_token": self.room_token,
					"tag": "target",
				})
			else:
				LOGGER.debug("Target '%s' already exists", session_id, extra={
					"session_id": session_id,
					"room_token": self.room_token,
					"tag": "target",
				})
			if self._deferred_close_task:
				self._deferred_close_task.cancel()
				self._deferred_close_task = None

	def remove_target(self, session_id: str):
		with self.target_lock:
			if session_id in self.targets:
				LOGGER.debug("Removed target", extra={
					"session_id": session_id,
					"room_token": self.room_token,
					"tag": "target",
				})
				del self.targets[session_id]
				if len(self.targets) == 0:
					if self._deferred_close_task:
						self._deferred_close_task.cancel()
					self._deferred_close_task = asyncio.create_task(self.maybe_leave_call())
			else:
				LOGGER.debug("Target does not exist", extra={
					"session_id": session_id,
					"room_token": self.room_token,
					"tag": "target",
				})

	async def signalling_monitor(self):  # noqa: C901
		"""Monitor the signaling server for incoming messages."""
		while True:
			if self.defunct.is_set():
				LOGGER.debug("SpreedClient is defunct, stopping monitor", extra={
					"room_token": self.room_token,
					"tag": "monitor",
				})
				break

			try:
				message = await self.receive()
			except WebSocketException as e:
				LOGGER.exception("HPB websocket error", exc_info=e, extra={
					"room_token": self.room_token,
					"tag": "monitor",
				})
				# todo: retry connection?
				if not self._close_task:
					self._close_task = asyncio.create_task(self.close())
				break
			except asyncio.CancelledError:
				LOGGER.debug("Signalling monitor task cancelled", extra={
					"room_token": self.room_token,
					"tag": "monitor",
				})
				if not self._close_task:
					self._close_task = asyncio.create_task(self.close())
				raise
			except Exception as e:
				LOGGER.exception("Unexpected error in signalling monitor", exc_info=e, extra={
					"room_token": self.room_token,
					"tag": "monitor",
				})
				if not self._close_task:
					self._close_task = asyncio.create_task(self.close())
				break

			if message.get("type") == "error":
				LOGGER.error(
					"Error message received: %s\nDetails: %s",
					message.get("error", {}).get("message"),
					message.get("error", {}).get("details"),
					extra={
						"room_token": self.room_token,
						"recv_message": message,
						"tag": "monitor",
					},
				)
				# todo: only close if the error is not recoverable
				if not self._close_task:
					self._close_task = asyncio.create_task(self.close())
				return

			if (
				message["type"] == "event"
				and message["event"]["target"] == "participants"
				and message["event"]["type"] == "update"
			):
				LOGGER.debug("Participants update received", extra={
					"room_token": self.room_token,
					"recv_message": message,
					"tag": "participants",
				})

				if message["event"]["update"].get("all") and message["event"]["update"].get("incall") == 0:
					LOGGER.debug("Call ended for everyone, closing connection", extra={
						"room_token": self.room_token,
						"recv_message": message,
						"tag": "participants",
					})
					if not self._close_task:
						self._close_task = asyncio.create_task(self.close())
					return

				users_update = message["event"]["update"].get("users", [])
				if not users_update:
					continue

				for user_desc in users_update:
					if user_desc.get("internal", False):
						continue

					if user_desc["inCall"] == CALL_FLAG.DISCONNECTED:
						LOGGER.debug("User disconnected", extra={
							"user_desc": user_desc,
							"room_token": self.room_token,
							"tag": "participants",
						})
						# the transcription should automatically stop when the audio track is closed
						# cleaning it up in this class
						if user_desc["sessionId"] in self.transcribers:
							with self.transcriber_lock:
								self.transcribers[user_desc["sessionId"]].shutdown()
								del self.transcribers[user_desc["sessionId"]]
						self.remove_target(user_desc["sessionId"])
						continue

					if (user_desc["inCall"] & CALL_FLAG.IN_CALL and user_desc["inCall"] & CALL_FLAG.WITH_AUDIO):
						LOGGER.debug("User joined with audio", extra={
							"user_desc": user_desc,
							"room_token": self.room_token,
							"tag": "participants",
						})
						await self.send_offer_request(user_desc["sessionId"])
						continue

				# the last user just left the call, live_transcription is the only one left
				if (len(users_update) == 2):
					if (
						users_update[0].get("sessionId") != self.sessionid
						and users_update[1].get("sessionId") != self.sessionid
					):
						# false alarm, we are not the only one left
						continue

					# if we are the only one left, close the connection
					transcriber_index = 0 if users_update[0].get("sessionId") == self.sessionid else 1
					if (
						users_update[transcriber_index].get("inCall") & CALL_FLAG.IN_CALL
						and users_update[transcriber_index^1].get("inCall") == CALL_FLAG.DISCONNECTED
					):
						LOGGER.debug("Last user left the call, closing connection", extra={
							"room_token": self.room_token,
							"transcriber_session_id": users_update[transcriber_index].get("sessionId"),
							"tag": "participants",
						})
						if not self._close_task:
							self._close_task = asyncio.create_task(self.close())
						return

			if message["type"] == "message" and message["message"]["data"]["type"] == "offer":
				LOGGER.debug("Received offer message", extra={
					"recv_message": message,
					"room_token": self.room_token,
					"tag": "offer",
				})
				await self.handle_offer(message)
				continue

			if message["type"] == "message" and message["message"]["data"]["type"] == "candidate":
				LOGGER.debug("Received candidate message", extra={
					"recv_message": message,
					"peer_session_id": message["message"]["sender"]["sessionid"],
					"room_token": self.room_token,
					"tag": "candidate",
				})
				candidate = candidate_from_sdp(message["message"]["data"]["payload"]["candidate"]["candidate"])
				candidate.sdpMid = message["message"]["data"]["payload"]["candidate"]["sdpMid"]
				candidate.sdpMLineIndex = message["message"]["data"]["payload"]["candidate"]["sdpMLineIndex"]
				with self.peer_connection_lock:
					if message["message"]["sender"]["sessionid"] not in self.peer_connections:
						continue
					await self.peer_connections[message["message"]["sender"]["sessionid"]].pc.addIceCandidate(candidate)
				continue

			if message["type"] == "bye":
				LOGGER.debug("Received bye message, closing connection", extra={
					"room_token": self.room_token,
					"recv_message": message,
					"tag": "bye",
				})
				if not self._close_task:
					self._close_task = asyncio.create_task(self.close())

	async def maybe_leave_call(self):
		"""Leave the call if there are no targets."""
		LOGGER.debug("Waiting to leave call if there are no targets", extra={
			"room_token": self.room_token,
			"tag": "maybe_leave_call",
		})
		await asyncio.sleep(CALL_LEAVE_TIMEOUT)

		if self.defunct.is_set():
			LOGGER.debug("SpreedClient is already defunct, clearing deferred close task and returning", extra={
				"room_token": self.room_token,
				"tag": "maybe_leave_call",
			})
			self._deferred_close_task = None
			return

		with self.target_lock:
			len_targets = len(self.targets)
		if len_targets == 0:
			LOGGER.debug("No transcript receivers for %s secs, leaving the call", CALL_LEAVE_TIMEOUT, extra={
				"room_token": self.room_token,
				"tag": "maybe_leave_call",
			})
			if not self._close_task:
				self._close_task = asyncio.create_task(self.close())
		self._deferred_close_task = None

	async def handle_offer(self, message):  # noqa: C901
		"""Handle incoming offer messages."""
		spkr_sid = message["message"]["sender"]["sessionid"]
		with self.peer_connection_lock:
			if spkr_sid in self.peer_connections:
				LOGGER.debug("Peer connection for %s already exists, skipping offer handling", spkr_sid, extra={
					"room_token": self.room_token,
					"spkr_sid": spkr_sid,
					"recv_message": message,
					"tag": "offer",
				})
				return

		ice_servers = []
		for stunserver in self.hpb_settings.stunservers:
			ice_servers.append(
				RTCIceServer(urls=stunserver.urls)
			)
		for turnserver in self.hpb_settings.turnservers:
			ice_servers.append(
				RTCIceServer(
					urls=turnserver.urls,
					username=turnserver.username,
					credential=turnserver.credential,
				)
			)
		if len(ice_servers) == 0:
			ice_servers = None
		rtc_config = RTCConfiguration(iceServers=ice_servers)
		pc = RTCPeerConnection(configuration=rtc_config)

		@pc.on("connectionstatechange")
		async def on_connectionstatechange():
			LOGGER.debug("Peer connection state changed", extra={
				"session_id": spkr_sid,
				"connection_state": pc.connectionState,
				"room_token": self.room_token,
				"tag": "peer_connection",
			})
			if pc.connectionState in ("failed", "closed"):
				LOGGER.debug("Peer connection for %s is %s", spkr_sid, pc.connectionState, extra={
					"session_id": spkr_sid,
					"connection_state": pc.connectionState,
					"room_token": self.room_token,
					"tag": "peer_connection",
				})
				with self.peer_connection_lock:
					if spkr_sid in self.peer_connections:
						del self.peer_connections[spkr_sid]

		pc.addTransceiver("audio", direction="recvonly")
		@pc.on("track")
		async def on_track(track):
			if track.kind == "audio":
				LOGGER.debug("Receiving %s track from %s", track.kind, spkr_sid, extra={
					"session_id": spkr_sid,
					"room_token": self.room_token,
					"tag": "track",
				})
				stream = AudioStream(track)
				with self.transcriber_lock:
					self.transcribers[spkr_sid] = VoskTranscriber(spkr_sid, self.lang_id, self.transcript_queue)

					try:
						await self.transcribers[spkr_sid].connect()
					except Exception:
						LOGGER.exception("Error connecting to Vosk server. Cannot continue further.", extra={
							"server_url": os.getenv("LT_VOSK_SERVER_URL", "ws://localhost:2702"),
							"session_id": spkr_sid,
							"room_token": self.room_token,
							"tag": "vosk",
						})
						if not self._close_task:
							self._close_task = asyncio.create_task(self.close())
							return

					await self.transcribers[spkr_sid].start(stream=stream)
					LOGGER.debug("Started transcriber for %s in %s", spkr_sid, LANGUAGE_MAP.get(self.lang_id).name,
						extra={
							"session_id": spkr_sid,
							"language": self.lang_id,
							"room_token": self.room_token,
							"tag": "transcriber",
						},
					)

		with self.peer_connection_lock:
			self.peer_connections[spkr_sid] = PeerConnection(session_id=spkr_sid, pc=pc)

		await pc.setRemoteDescription(
			RTCSessionDescription(type="offer", sdp=message["message"]["data"]["payload"]["sdp"])
		)

		answer = await pc.createAnswer()
		await pc.setLocalDescription(answer)
		await self.send_offer_answer(message["message"]["data"]["from"], message["message"]["data"]["sid"], answer.sdp)
		LOGGER.debug("Sent answer for offer from %s", spkr_sid, extra={
			"session_id": spkr_sid,
			"room_token": self.room_token,
			"tag": "offer",
		})

		local_sdp = pc.localDescription.sdp
		LOGGER.debug("Local SDP for %s:", spkr_sid, extra={
			"session_id": spkr_sid,
			"room_token": self.room_token,
			"local_sdp": local_sdp,
			"tag": "offer",
		})

		for line in local_sdp.splitlines():
			if line.startswith("a=candidate:"):
				await self.send_candidate(
					message["message"]["sender"]["sessionid"],
					message["message"]["data"]["sid"],
					line[2:],
				)

	async def set_language(self, lang_id: str):
		excs = []
		with self.transcriber_lock:
			transcribers = list(self.transcribers.values())
		try:
			for transcriber in transcribers:
				await transcriber.set_language(lang_id)
		except Exception as e:
			excs.append(e)
		if len(excs) > 1:
			raise VoskException("Failed to set language for multiple transcribers", retcode=500)
		if len(excs) == 1:
			raise VoskException(f"Failed to set language for one transcriber: {excs[0]}", retcode=500)
		self.lang_id = lang_id

	async def transcipt_queue_consumer(self):
		"""Consume transcripts from the queue and send them to the server."""
		LOGGER.debug("Starting the transcript queue consumer", extra={
			"room_token": self.room_token,
			"tag": "transcript",
		})
		while True:
			transcript: Transcript = await self.transcript_queue.get()  # type: ignore[annotation-unchecked]

			try:
				await asyncio.wait_for(
					self.send_transcript(
						message=transcript.message,
						spk_session_id=transcript.speaker_session_id,
					),
					timeout=10,
				)
			except TimeoutError:
				LOGGER.error("Timeout while sending a transcript", extra={
					"speaker_session_id": transcript.speaker_session_id,
					"room_token": self.room_token,
					"tag": "transcript",
				})
				continue
			except asyncio.CancelledError:
				LOGGER.debug("Transcript consumer task cancelled", extra={
					"room_token": self.room_token,
					"tag": "transcript",
				})
				raise
			except Exception as e:
				LOGGER.exception("Error while sending transcript", exc_info=e, extra={
					"speaker_session_id": transcript.speaker_session_id,
					"room_token": self.room_token,
					"tag": "transcript",
				})
				continue


class VoskTranscriber:
	def __init__(self, session_id: str, language: str, transcript_queue: asyncio.Queue):
		self.__voskcon: ClientConnection | None = None
		self.__voskcon_lock = asyncio.Lock()
		self.audio_task: asyncio.Task | None = None
		self.__audio_task_lock = threading.RLock()

		self.__resampler = AudioResampler(format="s16", layout="mono", rate=48000)
		self.__server_url = os.environ.get("LT_VOSK_SERVER_URL", "ws://localhost:2702")

		self.__session_id = session_id
		self.__language = language
		self.__transcript_queue = transcript_queue

	async def connect(self):
		ssl_ctx = get_ssl_context(self.__server_url)
		async with self.__voskcon_lock:
			self.__voskcon: ClientConnection | None = await connect(  # type: ignore[annotation-unchecked]
				self.__server_url,
				*({
					"server_hostname": urlparse(self.__server_url).hostname,
					"ssl": ssl_ctx,
				} if ssl_ctx else {}),
				open_timeout=VOSK_CONNECT_TIMEOUT,
			)
			await self.__voskcon.send(
				json.dumps({
					"config": {
						"sample_rate": 48000,
						"language": self.__language,
						# "show_words": True,
					}
				})
			)
			# todo: wait for language set confirmation

	async def start(self, stream: AudioStream):
		with self.__audio_task_lock:
			if self.audio_task and not self.audio_task.done():
				LOGGER.debug("Audio task already running for session_id: %s", self.__session_id, extra={
					"session_id": self.__session_id,
					"tag": "vosk",
				})
				return
			self.audio_task = asyncio.create_task(self.__run_audio_xfer(stream))
			self.audio_task.add_done_callback(partial(self.__done_cb, stream))

	def shutdown(self):
		with self.__audio_task_lock:
			if self.audio_task and not self.audio_task.done():
				LOGGER.debug("Cancelling audio task for session_id: %s", self.__session_id, extra={
					"session_id": self.__session_id,
					"tag": "vosk",
				})
				self.audio_task.cancel()

	def __done_cb(self, stream: AudioStream, future: asyncio.Future):
		with self.__audio_task_lock:
			if not self.audio_task or self.audio_task.done():
				return
			LOGGER.debug("Stopping audio task for session_id: %s", self.__session_id, extra={
				"session_id": self.__session_id,
				"tag": "vosk",
			})
			stream.stop()
			future.cancel("Cancelling audio task in VoskTranscriber for session_id: " + self.__session_id)
			self.audio_task = None

	async def set_language(self, language: str) -> None:
		if not self.__voskcon:
			raise VoskException("Vosk connection is not established, cannot switch language")
		if self.__language == language:
			LOGGER.debug("Language is already set to %s, no need to switch", LANGUAGE_MAP[language].name, extra={
				"language": language,
				"session_id": self.__session_id,
				"tag": "vosk",
			})
			return

		LOGGER.debug("Switching Vosk language from %s to %s",
			LANGUAGE_MAP[self.__language].name, LANGUAGE_MAP[language].name,
			extra={
				"session_id": self.__session_id,
				"tag": "vosk",
			},
		)
		self.__language = language
		async with self.__voskcon_lock:
			await self.__voskcon.send(
				json.dumps({
					"config": {
						"language": self.__language,
					}
				})
			)
			response = None
			max_received_msgs = MAX_CONNECT_TRIES
			while (not response or "success" not in response) and max_received_msgs > 0:
				response = await self.__voskcon.recv()
				max_received_msgs -= 1
		if not response or "success" not in response:
			LOGGER.error(
				"Max received messages limit reached while waiting for Vosk server response. "
				"Expected 'success' in response from Vosk server after switching language.",
				response, extra={
					"recv_response": response,
					"session_id": self.__session_id,
					"tag": "vosk",
				},
			)
			raise VoskException("Vosk server did not respond in an expected fashion after switching language")

		LOGGER.debug("Response from Vosk server after switching language", extra={
			"recv_response": response,
			"session_id": self.__session_id,
			"tag": "vosk",
		})
		try:
			json_res = json.loads(response)
		except json.JSONDecodeError as e:
			raise VoskException("Error decoding JSON response from Vosk server after switching language") from e
		ret = json_res.get("success", False)
		if not ret:
			raise VoskException("Vosk server did not confirm language switch successfully")
		LOGGER.debug("Vosk language switched to %s successfully", LANGUAGE_MAP[language].name, extra={
			"language": language,
			"session_id": self.__session_id,
			"tag": "vosk",
		})


	async def __run_audio_xfer(self, stream: AudioStream):  # noqa: C901
		frames = []
		try:
			while True:
				fr = await stream.receive()
				frames.append(fr)

				# We need to collect frames so we don't send partial results too often
				if len(frames) < MAX_AUDIO_FRAMES:
					continue

				dataframes = bytearray(b"")
				for fr in frames:
					for rfr in self.__resampler.resample(fr):
						dataframes += bytes(rfr.planes[0])[:rfr.samples * 2]
				frames.clear()

				async with self.__voskcon_lock:
					if not self.__voskcon:
						raise Exception("Vosk connection is not established, cannot send audio data")
					await self.__voskcon.send(bytes(dataframes))
					result = await self.__voskcon.recv()

				if not result:
					continue

				try:
					json_msg = json.loads(result)
				except json.JSONDecodeError:
					LOGGER.error("Error decoding JSON message from Vosk server", extra={
						"result": result,
						"session_id": self.__session_id,
						"tag": "vosk",
					})
					continue

				message = json_msg.get("text", "")
				if message == "":
					continue

				self.__transcript_queue.put_nowait(Transcript(
					message=message,
					speaker_session_id=self.__session_id,
				))
		except (StreamEndedException, MediaStreamError):
			LOGGER.debug("Audio stream ended for session id: %s", self.__session_id, extra={
				"session_id": self.__session_id,
				"tag": "vosk",
			})
		except asyncio.CancelledError:
			LOGGER.debug("Audio task cancelled for session id: %s", self.__session_id, extra={
				"session_id": self.__session_id,
				"tag": "vosk",
			})
			raise
		except Exception as e:
			LOGGER.exception("Error in VoskTranscriber for session id: %s", self.__session_id, exc_info=e, extra={
				"session_id": self.__session_id,
				"tag": "vosk",
			})
		finally:
			async with self.__voskcon_lock:
				if self.__voskcon:
					LOGGER.debug("Closing Vosk server connection for session id: %s", self.__session_id, extra={
						"session_id": self.__session_id,
						"tag": "vosk",
					})
					with suppress(Exception):
						await self.__voskcon.send('{"eof" : 1}')
					with suppress(Exception):
						await self.__voskcon.close()
						self.__voskcon = None


class Application:
	def __init__(self) -> None:
		self.hpb_settings = get_hpb_settings()
		self.spreed_clients: dict[str, SpreedClient] = {}
		self.spreed_clients_lock = threading.RLock()

	async def transcript_req(self, req: TranscribeRequest) -> None:
		with self.spreed_clients_lock:
			if req.roomToken in self.spreed_clients:
				LOGGER.info("Already in call for room token %s, adding target %s", req.roomToken, req.sessionId, extra={
					"room_token": req.roomToken,
					"session_id": req.sessionId,
					"tag": "application",
				})
				if req.enable:
					self.spreed_clients[req.roomToken].add_target(req.sessionId)
				else:
					self.spreed_clients[req.roomToken].remove_target(req.sessionId)
				return

		if not req.enable:
			LOGGER.info(
				"Received request to turn off transcription for room token: %s, "
				"session id: %s but no call is active. Ignoring request.",
				req.roomToken, req.sessionId, extra={
					"room_token": req.roomToken,
					"session_id": req.sessionId,
					"tag": "application",
				})
			return

		LOGGER.info("Joining call for room token: %s, session id: %s", req.roomToken, req.sessionId, extra={
			"room_token": req.roomToken,
			"session_id": req.sessionId,
			"tag": "application",
		})
		with self.spreed_clients_lock:
			self.spreed_clients[req.roomToken] = SpreedClient(
				req.roomToken,
				self.hpb_settings,
				req.langId,
				self.__leave_call_cb,
			)
			self.spreed_clients[req.roomToken].add_target(req.sessionId)

		tries = MAX_CONNECT_TRIES
		last_exc = None
		while tries > 0:
			try:
				self.spreed_clients_lock.acquire()
				conn_result = await self.spreed_clients[req.roomToken].connect()
				match conn_result:
					case ConnectResult.SUCCESS:
						LOGGER.info("Connected to signaling server for room token: %s", req.roomToken, extra={
							"room_token": req.roomToken,
							"tag": "connection",
						})
						return
					case ConnectResult.FAILURE:
						# do not retry
						LOGGER.error("Failed to connect to signaling server for room token: %s", req.roomToken, extra={
							"room_token": req.roomToken,
							"try": MAX_CONNECT_TRIES + 1 - tries,
							"tag": "connection",
						})
						await self.spreed_clients[req.roomToken].close()
						del self.spreed_clients[req.roomToken]
						return
					case ConnectResult.RETRY:
						LOGGER.warning("Retrying connection to signaling server for room token: %s", req.roomToken,
							extra={
								"room_token": req.roomToken,
								"try": MAX_CONNECT_TRIES + 1 - tries,
								"tag": "connection",
							},
						)
						await self.spreed_clients[req.roomToken].close()
				tries -= 1
				await asyncio.sleep(2)
			except Exception as e:
				LOGGER.warning("Error connecting to signaling server", exc_info=e, extra={
					"room_token": req.roomToken,
					"try": MAX_CONNECT_TRIES + 1 - tries,
					"tag": "connection",
				})
				tries -= 1
				last_exc = e
				await asyncio.sleep(2)
			finally:
				self.spreed_clients_lock.release()

		LOGGER.error("Failed to connect to signaling server for room token %s after %d attempts",
			req.roomToken, MAX_CONNECT_TRIES,
			extra={
				"room_token": req.roomToken,
				"tag": "connection",
			},
		)
		raise SpreedClientException(
			f"Failed to connect to signaling server for room token {req.roomToken} after {MAX_CONNECT_TRIES} attempts"
		) from last_exc

	async def set_call_language(self, req: LanguageSetRequest) -> None:
		if req.roomToken not in self.spreed_clients:
			raise SpreedClientException(
				f"No SpreedClient for room token {req.roomToken}, cannot set language"
			)

		with self.spreed_clients_lock:
			spreed_client = self.spreed_clients[req.roomToken]
			await spreed_client.set_language(req.langId)

	async def leave_call(self, room_token: str):
		"""Leave the call for the given room token. Called from an API endpoint."""
		if room_token not in self.spreed_clients:
			LOGGER.info("No SpreedClient for room token %s active, cannot leave call", room_token, extra={
				"room_token": room_token,
				"tag": "connection",
			})
			return

		with self.spreed_clients_lock:
			spreed_client = self.spreed_clients[room_token]
			if spreed_client.defunct.is_set():
				LOGGER.info("SpreedClient for room token %s is already closed", room_token, extra={
					"room_token": room_token,
					"tag": "connection",
				})
				return

			await spreed_client.close(using_resume=False)
			LOGGER.info("Left call for room token %s", room_token, extra={
				"room_token": room_token,
				"tag": "connection",
			})

	def __leave_call_cb(self, room_token: str):
		with self.spreed_clients_lock:
			if room_token not in self.spreed_clients:
				LOGGER.debug("No SpreedClient for room token %s active, cannot leave call", room_token, extra={
					"room_token": room_token,
					"tag": "connection",
				})
				return

			spreed_client = self.spreed_clients[room_token]
			if spreed_client.defunct.is_set():
				del self.spreed_clients[room_token]
				return

			LOGGER.info("Leaving call for room token %s", room_token, extra={
				"room_token": room_token,
				"tag": "connection",
			})
			asyncio.get_running_loop().call_soon_threadsafe(spreed_client.close)
			ret = self.spreed_clients[room_token].defunct.wait(
				timeout=HPB_SHUTDOWN_TIMEOUT
			)  # wait for the client to close
			if not ret:
				LOGGER.error("Timeout while waiting for SpreedClient to close for room token %s", room_token, extra={
					"room_token": room_token,
					"tag": "connection",
				})
				return

			LOGGER.info("Closed SpreedClient for room token %s", room_token, extra={
				"room_token": room_token,
				"tag": "connection",
			})
			del self.spreed_clients[room_token]


def check_hpb_env_vars():
	# Check if the required environment variables are set
	required_vars = ("LT_HPB_URL", "LT_INTERNAL_SECRET")
	missing_vars = [var for var in required_vars if not os.getenv(var)]
	if missing_vars:
		raise ValueError(f"Missing environment variables: {', '.join(missing_vars)}")

	hpb_url = os.environ["LT_HPB_URL"]
	hpb_url_host = urlparse(hpb_url).hostname
	if not hpb_url_host:
		raise ValueError(
			f"Could not detect hostname in LT_HPB_URL env var: {hpb_url}. "
			"Verify that it is a valid URL with a protocol and hostname."
		)

	vosk_url = os.environ.get("LT_VOSK_SERVER_URL")
	if vosk_url:
		vosk_url_parsed = urlparse(vosk_url)
		vosk_host = vosk_url_parsed.hostname
		if not vosk_host:
			raise ValueError(
				f"Could not detect hostname in LT_VOSK_SERVER_URL: {vosk_url}. "
				"Verify that it is a valid URL with a protocol and hostname."
			)
		try:
			_ = vosk_url_parsed.port
		except ValueError as e:
			raise ValueError(f"Invalid VOSK server URL: {vosk_url}") from e


def get_hpb_settings() -> HPBSettings:
	check_hpb_env_vars()
	try:
		nc = NextcloudApp(npa_nc_cert=get_ssl_context(f"${os.environ['NEXTCLOUD_URL']}"))
		settings = nc.ocs("GET", "/ocs/v2.php/apps/spreed/api/v3/signaling/settings")
		hpb_settings = HPBSettings(**settings)
		LOGGER.debug("HPB settings retrieved successfully", extra={
			"stun_servers": [s.urls for s in hpb_settings.stunservers],
			"turn_servers": [t.urls for t in hpb_settings.turnservers],
			"server": hpb_settings.server,
			"tag": "hpb_settings",
		})
		return hpb_settings
	except Exception as e:
		raise Exception("Error getting HPB settings") from e
