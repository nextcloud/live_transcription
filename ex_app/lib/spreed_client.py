#
# SPDX-FileCopyrightText: 2025 Nextcloud GmbH and Nextcloud contributors
# SPDX-License-Identifier: AGPL-3.0-or-later
#

import asyncio
import dataclasses
import json
import logging
import os
import threading
from collections.abc import Awaitable, Callable
from contextlib import suppress
from secrets import token_urlsafe
from urllib.parse import urlparse

from aiortc import RTCPeerConnection, RTCSessionDescription
from aiortc.rtcconfiguration import RTCConfiguration, RTCIceServer
from aiortc.sdp import candidate_from_sdp
from audio_stream import AudioStream
from constants import CALL_LEAVE_TIMEOUT, MSG_RECEIVE_TIMEOUT
from livetypes import CallFlag, HPBSettings, SigConnectResult, Target, Transcript, VoskException
from models import LANGUAGE_MAP
from nc_py_api import NextcloudApp
from transcriber import VoskTranscriber
from utils import get_ssl_context, hmac_sha256, sanitize_websocket_url
from websockets import ClientConnection
from websockets import State as WsState
from websockets import connect
from websockets.exceptions import WebSocketException

LOGGER = logging.getLogger("lt.spreed_client")


@dataclasses.dataclass
class PeerConnection:
	session_id: str
	pc: RTCPeerConnection


class SpreedClient:
	def __init__(
		self,
		room_token: str,
		hpb_settings: HPBSettings,
		lang_id: str,
		leave_call_cb: Callable[[str], Awaitable[None]],  # room_token
	) -> None:
		self.id = 0
		self._server: ClientConnection | None = None
		self._monitor: asyncio.Task | None = None
		self.peer_connections: dict[str, PeerConnection] = {}
		self.peer_connection_lock = asyncio.Lock()
		self.targets: dict[str, Target] = {}
		self.target_lock = asyncio.Lock()
		self.nc_sid_map: dict[str, str] = {}  # {"nc_session_id": "session_id"}, use the same target lock
		# the first target's Nextcloud session ID, used to defer the first target add until we join the call
		self._nc_sid_wait_stash: dict[str, None] = {}
		self.transcript_queue: asyncio.Queue = asyncio.Queue()
		self._transcript_sender: asyncio.Task | None = None
		self.transcribers: dict[str, VoskTranscriber] = {}
		self.transcriber_lock = asyncio.Lock()
		self.defunct = threading.Event()
		self._close_task: asyncio.Task | None = None
		self._deferred_close_task: asyncio.Task | None = None

		self.resumeid = None
		self.sessionid = None

		nc = NextcloudApp()
		self._websocket_url = sanitize_websocket_url(os.environ["LT_HPB_URL"])
		self._backendURL = nc.app_cfg.endpoint + "/ocs/v2.php/apps/spreed/api/v3/signaling/backend"
		self.secret = os.environ["LT_INTERNAL_SECRET"]

		self.room_token = room_token
		self.hpb_settings = hpb_settings
		self.lang_id = lang_id
		self.leave_call_cb = leave_call_cb


	async def connect(self) -> SigConnectResult:
		websocket_host = urlparse(self._websocket_url).hostname
		ssl_ctx = get_ssl_context(self._websocket_url)
		self._server = await connect(
			self._websocket_url,
			**({
				"server_hostname": websocket_host,
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
				return SigConnectResult.FAILURE

			if message.get("type") == "error":
				LOGGER.error(
					"Signaling error message received: %s\nDetails: %s", message.get("error", {}).get("message"),
					message.get("error", {}).get("details"), extra={
						"room_token": self.room_token,
						"msg_counter": msg_counter,
						"tag": "connection",
					},
				)

				message_code = message.get("error", {}).get("code")
				if message_code == "duplicate_session":
					LOGGER.error("Duplicate session found, aborting connection", extra={
						"room_token": self.room_token,
						"msg_counter": msg_counter,
						"tag": "connection",
					})
					return SigConnectResult.FAILURE
				if message_code == "room_join_failed":
					LOGGER.error("Room join failed, retrying...", extra={
						"room_token": self.room_token,
						"msg_counter": msg_counter,
						"tag": "connection",
					})
					return SigConnectResult.RETRY

				return SigConnectResult.FAILURE

			if message.get("type") == "bye":
				LOGGER.info("Received bye message, closing connection", extra={
					"room_token": self.room_token,
					"msg_counter": msg_counter,
					"tag": "connection",
				})
				return SigConnectResult.FAILURE

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
				return SigConnectResult.RETRY

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
		return SigConnectResult.SUCCESS

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
					"incall": CallFlag.IN_CALL,
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

	async def send_transcript(self, transcript: Transcript):
		async with self.target_lock:
			if not self.targets:
				LOGGER.debug("No targets to send transcript to, skipping", extra={
					"room_token": self.room_token,
					"transcript": transcript,
					"tag": "transcript",
				})
				return
			sids = list(self.targets.keys())

		send_tasks = [
			self.send_message({
				"type": "message",
				"message": {
					"recipient": {
						"type": "session",
						"sessionid": sid,
					},
					"data": {
						"final": transcript.final,
						"langId": transcript.lang_id,
						"message": transcript.message,
						"speakerSessionId": transcript.speaker_session_id,
						"type": "transcript",
					}
				}
			})
			for sid in sids
		]
		await asyncio.gather(*send_tasks)

	# todo: add function to reconnect to hpb, full SpreedClient lifecycle
	async def close(self, using_resume: bool = False):  # noqa: C901
		if self.defunct.is_set():
			LOGGER.debug("SpreedClient is already defunct, skipping close", extra={
				"room_token": self.room_token,
				"using_resume": using_resume,
				"tag": "client",
			})
			return

		if self._deferred_close_task and not self._deferred_close_task.done():
			LOGGER.debug("Cancelling deferred close task", extra={
				"room_token": self.room_token,
				"using_resume": using_resume,
				"tag": "deferred_close",
			})
			self._deferred_close_task.cancel()
			self._deferred_close_task = None

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
			LOGGER.debug("Shutting down all transcribers", extra={
				"room_token": self.room_token,
				"using_resume": using_resume,
				"tag": "transcriber",
			})
			for transcriber in self.transcribers.values():
				transcriber.shutdown()
			async with self.transcriber_lock:
				self.transcribers.clear()

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
				async with self.peer_connection_lock:
					self.peer_connections.clear()
				self.resumeid = None
				self.sessionid = None

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
				await self.leave_call_cb(self.room_token)

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

	async def add_target(self, nc_session_id: str):
		async with self.target_lock:
			if nc_session_id not in self.nc_sid_map:
				# stash the NC session IDs until we receive the participants update
				self._nc_sid_wait_stash[nc_session_id] = None
				LOGGER.debug("HPB session ID corresponding to Nextcloud session ID '%s' not found, deferring add",
					extra={
						"nc_session_id": nc_session_id,
						"room_token": self.room_token,
						"tag": "target",
					})
				return

			self._nc_sid_wait_stash.pop(nc_session_id, None)
			session_id = self.nc_sid_map[nc_session_id]
			if session_id not in self.targets:
				self.targets[session_id] = Target()
				LOGGER.debug("Added target", extra={
					"session_id": session_id,
					"nc_session_id": nc_session_id,
					"targets": self.targets,
					"lang_id": self.lang_id,
					"room_token": self.room_token,
					"tag": "target",
				})
			else:
				LOGGER.debug("Target already exists", extra={
					"session_id": session_id,
					"nc_session_id": nc_session_id,
					"targets": self.targets,
					"lang_id": self.lang_id,
					"room_token": self.room_token,
					"tag": "target",
				})
			if self._deferred_close_task:
				self._deferred_close_task.cancel()
				self._deferred_close_task = None

	async def remove_target(self, nc_session_id: str):
		async with self.target_lock:
			self._nc_sid_wait_stash.pop(nc_session_id, None)
			if nc_session_id not in self.nc_sid_map:
				LOGGER.debug("HPB session ID corresponding to Nextcloud session ID '%s' not found",
					nc_session_id,
					extra={
						"nc_session_id": nc_session_id,
						"room_token": self.room_token,
						"tag": "target",
					},
				)
				return

			session_id = self.nc_sid_map[nc_session_id]
			if session_id in self.targets:
				LOGGER.debug("Removed target", extra={
					"session_id": session_id,
					"nc_session_id": nc_session_id,
					"targets": self.targets,
					"lang_id": self.lang_id,
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
					"nc_session_id": nc_session_id,
					"targets": self.targets,
					"lang_id": self.lang_id,
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
				LOGGER.exception("HPB websocket error, reconnecting...", exc_info=e, extra={
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
				if message.get("error", {}).get("code") == "processing_failed":
					# this is most probably related to a transcript reception failure on HPB side
					# we can try to continue
					continue

				# only close if the error is not recoverable
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

					if user_desc["inCall"] == CallFlag.DISCONNECTED:
						LOGGER.debug("User disconnected", extra={
							"user_desc": user_desc,
							"room_token": self.room_token,
							"tag": "participants",
						})
						# the transcription should automatically stop when the audio track is closed
						# cleaning it up in this class
						async with self.transcriber_lock:
							if user_desc["sessionId"] in self.transcribers:
								self.transcribers[user_desc["sessionId"]].shutdown()
								del self.transcribers[user_desc["sessionId"]]
						await self.remove_target(user_desc["sessionId"])
						async with self.target_lock:
							self.nc_sid_map.pop(user_desc["nextcloudSessionId"], None)
						continue

					# user connected, keep a map of Nextcloud session IDs to HPB session IDs
					async with self.target_lock:
						self.nc_sid_map[user_desc["nextcloudSessionId"]] = user_desc["sessionId"]

					# if this is one of the deferred targets, add it to the targets
					if (user_desc["nextcloudSessionId"] in self._nc_sid_wait_stash):
						LOGGER.debug("Adding one of the deferred targets to the target dict", extra={
							"nc_session_id": user_desc["nextcloudSessionId"],
							"session_id": user_desc["sessionId"],
							"room_token": self.room_token,
							"tag": "target",
						})
						await self.add_target(user_desc["nextcloudSessionId"])
						async with self.target_lock:
							self._nc_sid_wait_stash.pop(user_desc["nextcloudSessionId"], None)

					# user connected with audio
					if (user_desc["inCall"] & CallFlag.IN_CALL and user_desc["inCall"] & CallFlag.WITH_AUDIO):
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
						users_update[transcriber_index].get("inCall") & CallFlag.IN_CALL
						and users_update[transcriber_index^1].get("inCall") == CallFlag.DISCONNECTED
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
				async with self.peer_connection_lock:
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

		async with self.target_lock:
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
		async with self.peer_connection_lock:
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
				async with self.peer_connection_lock:
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
				async with self.transcriber_lock:
					self.transcribers[spkr_sid] = VoskTranscriber(spkr_sid, self.lang_id, self.transcript_queue)

					try:
						await self.transcribers[spkr_sid].connect()
						await self.transcribers[spkr_sid].start(stream=stream)
					except Exception:
						LOGGER.exception("Error in connection and start of the Vosk server. Cannot continue further.",
							extra={
								"server_url": os.getenv("LT_VOSK_SERVER_URL", "ws://localhost:2702"),
								"session_id": spkr_sid,
								"room_token": self.room_token,
								"tag": "vosk",
							},
						)
						if not self._close_task:
							self._close_task = asyncio.create_task(self.close())
						return

					LOGGER.debug("Started transcriber for %s in %s", spkr_sid, LANGUAGE_MAP.get(self.lang_id).name,
						extra={
							"session_id": spkr_sid,
							"language": self.lang_id,
							"room_token": self.room_token,
							"tag": "transcriber",
						},
					)

		async with self.peer_connection_lock:
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
		async with self.transcriber_lock:
			transcribers = list(self.transcribers.values())
		try:
			for transcriber in transcribers:
				await transcriber.set_language(lang_id)
		except Exception as e:
			excs.append(e)
		if len(excs) > 1:
			LOGGER.error("Failed to set language for multiple transcribers", extra={
				"lang_id": lang_id,
				"room_token": self.room_token,
				"excs": excs,
				"tag": "transcriber",
			})
			raise VoskException(
				f"Failed to set language for multiple transcribers, first of which is: {excs[0]}",
				retcode=500,
			)
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
					self.send_transcript(transcript),
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
