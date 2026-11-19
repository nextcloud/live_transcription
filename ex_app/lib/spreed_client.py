#
# SPDX-FileCopyrightText: 2025 Nextcloud GmbH and Nextcloud contributors
# SPDX-License-Identifier: AGPL-3.0-or-later
#

import asyncio
import dataclasses
import json
import logging
import os
import time
import weakref
from collections.abc import Awaitable, Callable
from contextlib import suppress
from secrets import token_urlsafe
from urllib.parse import urlparse

from aiortc import RTCPeerConnection, RTCSessionDescription
from aiortc.rtcconfiguration import RTCConfiguration, RTCIceServer
from aiortc.sdp import candidate_from_sdp
from audio_stream import AudioStream
from constants import (
	CALL_LEAVE_TIMEOUT,
	HPB_PING_TIMEOUT,
	HPB_SHUTDOWN_TIMEOUT,
	ICE_GATHERING_TIMEOUT,
	MAX_TRANSCRIPT_SEND_TIMEOUT,
	MAX_TRANSLATION_SEND_TIMEOUT,
	MSG_RECEIVE_TIMEOUT,
	SEND_TIMEOUT,
	TIMEOUT_INCREASE_FACTOR,
)
from livetypes import (
	ASRException,
	CallFlag,
	HPBSettings,
	ReconnectMethod,
	SigConnectResult,
	SpreedRateLimitedException,
	Target,
	Transcript,
	TranscriptTargetNotFoundException,
	TranslateInputOutput,
	TranslateLangPairException,
)
from meta_translator import MetaTranslator
from models import LANGUAGE_MAP
from nc_py_api import AsyncNextcloudApp
from transcriber import Transcriber
from utils import asr_server_url, get_ssl_context, hmac_sha256, sanitize_websocket_url
from websockets import ClientConnection
from websockets import State as WsState
from websockets import connect
from websockets.exceptions import WebSocketException

LOGGER = logging.getLogger("lt.spreed_client")


@dataclasses.dataclass
class PeerConnection:
	session_id: str
	pc: RTCPeerConnection


def is_live(pc: RTCPeerConnection) -> bool:
	"""Whether pc can still carry audio: aiortc does not leave "failed" or "closed".

	A "failed" connection still holds its receivers, so code that frees one checks
	for "closed" only.
	"""
	return pc.connectionState not in ("failed", "closed")


# How long to wait for an offer after sending a "requestoffer" before sending
# another request for the same session id. In seconds.
OFFER_REQUEST_COOLDOWN = 10
# How many times to request an offer from a speaker whose transcriber ended.
OFFER_REQUEST_TRIES = 5


class SpreedClient:
	def __init__(
		self,
		room_token: str,
		hpb_settings: HPBSettings,
		room_lang_id: str,
		leave_call_cb: Callable[[str], Awaitable[None]] | None,  # room_token
	) -> None:
		self.id = 0
		self._server: ClientConnection | None = None
		self._monitor: asyncio.Task | None = None
		self.peer_connections: dict[str, PeerConnection] = {}
		self.peer_connection_lock = asyncio.Lock()
		# sessions with an in-flight "requestoffer": {session_id: request time}
		self._offer_requested: dict[str, float] = {}
		self.targets: dict[str, Target] = {}
		self.target_lock = asyncio.Lock()
		self.nc_sid_map: dict[str, str] = {}  # {"nc_session_id": "session_id"}, use the same target lock
		# the first target's Nextcloud session ID, used to defer the first target add until we join the call
		self._nc_sid_wait_stash: dict[str, None] = {}
		self.transcript_queue: asyncio.Queue = asyncio.Queue()
		self._transcript_sender: asyncio.Task | None = None
		self.transcribers: dict[str, Transcriber] = {}
		self.transcriber_lock = asyncio.Lock()
		self.defunct = asyncio.Event()
		self._close_task: asyncio.Task | None = None
		self._deferred_close_task: asyncio.Task | None = None
		self._reconnect_task: asyncio.Task | None = None
		# releases started from a done callback, kept here until they finish
		self._release_tasks: set[asyncio.Task] = set()
		self.translate_queue_input: asyncio.Queue = asyncio.Queue()
		self.translate_queue_output: asyncio.Queue = asyncio.Queue()
		self.translated_text_sender: asyncio.Task | None = None
		self.should_translate = asyncio.Event()  # set if at least one target has translation enabled
		self.meta_translator = MetaTranslator(
			room_token,
			room_lang_id,
			self.translate_queue_input,
			self.translate_queue_output,
			self.should_translate,
		)

		self.resumeid = None
		self.sessionid = None

		self._websocket_url = sanitize_websocket_url(os.environ["LT_HPB_URL"])
		self.secret = os.environ["LT_INTERNAL_SECRET"]

		self.room_token = room_token
		self.hpb_settings = hpb_settings
		self.room_lang_id = room_lang_id
		self.leave_call_cb = leave_call_cb


	async def _resume_connection(self) -> bool:
		"""
		Raises
		------
		SpreedRateLimitedException: when the HPB server rate limits the client during resume
		"""  # noqa
		try:
			await self.send_message({
				"type": "hello",
				"hello": {
					"version": "2.0",
					"resumeid": self.resumeid,
				}
			})
		except Exception as e:
			LOGGER.exception("Error resuming connection to HPB with short hello", exc_info=e, extra={
				"room_token": self.room_token,
				"tag": "short_resume",
			})
			return False

		msg_counter = 0
		# wait for the hello response with the new session ID
		while msg_counter < 10:
			message = await self.receive(MSG_RECEIVE_TIMEOUT)
			if message is None:
				LOGGER.error("No message received for %s secs while resuming, aborting...", MSG_RECEIVE_TIMEOUT, extra={
					"room_token": self.room_token,
					"tag": "short_resume",
				})
				return False

			if message.get("type") == "hello":
				self.sessionid = message["hello"]["sessionid"]
				LOGGER.info("Resumed connection with new session ID", extra={
					"sessionid": self.sessionid,
					"room_token": self.room_token,
					"tag": "short_resume",
				})
				return True

			if message.get("type") == "error":
				LOGGER.error(
					"Signaling error message received during a short resume", extra={
						"room_token": self.room_token,
						"msg_counter": msg_counter,
						"error_received": message,
						"tag": "short_resume",
					},
				)

				err_code = message.get("error", {}).get("code")
				if err_code == "no_such_session":
					LOGGER.info("Performing a full reconnect since the previous session expired", extra={
						"room_token": self.room_token,
						"msg_counter": msg_counter,
						"tag": "short_resume",
					})
					return False

				if err_code == "too_many_requests":
					LOGGER.error("Rate limited by the HPB during short resume, giving up", extra={
						"room_token": self.room_token,
						"msg_counter": msg_counter,
						"tag": "short_resume",
					})
					raise SpreedRateLimitedException()

				# some other error, do not retry
				return False

			msg_counter += 1

		# we did not receive the hello message for 10 messages
		return False

	async def connect(self, reconnect: ReconnectMethod = ReconnectMethod.NO_RECONNECT) -> SigConnectResult:  # noqa: C901
		if self._server and self._server.state == WsState.OPEN and reconnect != ReconnectMethod.FULL_RECONNECT:
			LOGGER.debug("Already connected to signaling server, skipping connect", extra={
				"room_token": self.room_token,
				"reconnect": reconnect,
				"tag": "connection",
			})
			return SigConnectResult.SUCCESS

		websocket_host = urlparse(self._websocket_url).hostname
		ssl_ctx = get_ssl_context(self._websocket_url)
		try:
			self._server = await connect(
				self._websocket_url,
				**({
					"server_hostname": websocket_host,
					"ssl": ssl_ctx,  # type: ignore[arg-type]
				} if ssl_ctx else {}),
				ping_timeout=HPB_PING_TIMEOUT,
			)
		except Exception as e:
			LOGGER.exception("Error connecting to signaling server, retrying...", exc_info=e, extra={
				"room_token": self.room_token,
				"reconnect": reconnect,
				"tag": "connection",
			})
			if reconnect != ReconnectMethod.NO_RECONNECT:
				await asyncio.sleep(2)
				self._reconnect_task = asyncio.create_task(self.connect(reconnect=ReconnectMethod.FULL_RECONNECT))
			return SigConnectResult.RETRY

		if reconnect == ReconnectMethod.SHORT_RESUME:
			self._reconnect_task = None
			try:
				res = await self._resume_connection()
			except SpreedRateLimitedException:
				if not self._close_task:
					self._close_task = asyncio.create_task(self.close())
				return SigConnectResult.FAILURE
			except Exception as e:
				LOGGER.exception("Unexpected error during short resume, retrying connection", exc_info=e, extra={
					"room_token": self.room_token,
					"tag": "connection",
				})
				if reconnect != ReconnectMethod.NO_RECONNECT:
					self._reconnect_task = asyncio.create_task(self.connect(reconnect=ReconnectMethod.SHORT_RESUME))
				return SigConnectResult.RETRY

			if res:
				LOGGER.info("Resumed connection to signaling server for room token: %s", self.room_token, extra={
					"room_token": self.room_token,
					"tag": "connection",
				})
				await self.send_incall()
				await self.send_join()
				return SigConnectResult.SUCCESS

			LOGGER.info("Short resume failed, performing full reconnect for room token: %s", self.room_token, extra={
				"room_token": self.room_token,
				"tag": "connection",
			})
			if reconnect != ReconnectMethod.NO_RECONNECT:
				await asyncio.sleep(2)
				self._reconnect_task = asyncio.create_task(self.connect(reconnect=ReconnectMethod.FULL_RECONNECT))
			return SigConnectResult.RETRY

		if reconnect == ReconnectMethod.FULL_RECONNECT:
			self._reconnect_task = None
			LOGGER.info("Performing full reconnect for room token: %s", self.room_token, extra={
				"room_token": self.room_token,
				"tag": "connection",
			})
			try:
				await asyncio.wait_for(self.close(), HPB_SHUTDOWN_TIMEOUT)
			except TimeoutError:
				LOGGER.warning("Timeout while closing SpreedClient during full reconnect, proceeding anyway", extra={
					"room_token": self.room_token,
					"tag": "connection",
				})
			finally:
				self.defunct.set()
				self._deferred_close_task = None
				self._monitor = None
				self.resumeid = None
				self.sessionid = None
				self._server = None

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
					if reconnect != ReconnectMethod.NO_RECONNECT:
						await asyncio.sleep(2)
						self._reconnect_task = asyncio.create_task(
							self.connect(reconnect=ReconnectMethod.FULL_RECONNECT),
						)
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
				if reconnect != ReconnectMethod.NO_RECONNECT:
					await asyncio.sleep(2)
					self._reconnect_task = asyncio.create_task(self.connect(reconnect=ReconnectMethod.FULL_RECONNECT))
				return SigConnectResult.RETRY

		self.defunct.clear()
		self._monitor = asyncio.create_task(self.signalling_monitor(), name=f"signalling_monitor-{self.room_token}")

		# just to be safe, start the transcript sender if not already running, even in reconnects
		if self._transcript_sender is None or self._transcript_sender.done():
			self._transcript_sender = asyncio.create_task(
				self.transcipt_queue_consumer(),
				name=f"transcript_sender-{self.room_token}",
			)

		# just to be safe, start the translate sender if not already running, even in reconnects
		if self.translated_text_sender is None or self.translated_text_sender.done():
			self.translated_text_sender = asyncio.create_task(
				self.translated_text_consumer(),
				name=f"translated_text_sender-{self.room_token}",
			)

		if reconnect == ReconnectMethod.NO_RECONNECT:
			# leave the call if there are no targets after some time
			self._deferred_close_task = asyncio.create_task(
				self.maybe_leave_call(),
				name=f"deferred_close-{self.room_token}",
			)

		await self.send_incall()
		await self.send_join()
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
		try:
			await self._server.send(json.dumps(message))
		except WebSocketException as e:
			LOGGER.exception("HPB websocket error, reconnecting...", exc_info=e, extra={
				"room_token": self.room_token,
				"send_message": message,
				"tag": "send_message",
			})
			if not self._reconnect_task or self._reconnect_task.done():
				self._reconnect_task = asyncio.create_task(self.connect(reconnect=ReconnectMethod.SHORT_RESUME))
			return
		except Exception as e:
			LOGGER.exception("Unexpected error send a message to HPB, ignoring", exc_info=e, extra={
				"room_token": self.room_token,
				"send_message": message,
				"tag": "send_message",
			})
			# ignore the exception, most probably TypeError which is not expected to happen anyway
			return

		LOGGER.debug("Message sent", extra={
			"id": self.id,
			"room_token": self.room_token,
			"sent_message": message,
			"tag": "send_message",
		})

	async def send_hello(self):
		nonce = token_urlsafe(64)
		nc = AsyncNextcloudApp()
		abs_url_res = await nc.ocs(
			"GET",
			"/ocs/v1.php/apps/app_api/api/v1/info/nextcloud_url/absolute",
			params={"url": ""},
		)
		nc_public_url = abs_url_res.get("absolute_url") or nc.app_cfg.endpoint

		await self.send_message({
			"type": "hello",
			"hello": {
				"version": "2.0",
				"auth": {
					"type": "internal",
					"params": {
						"random": nonce,
						"token": hmac_sha256(self.secret, nonce),
						"backend": nc_public_url,
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

		for sid in sids:
			await self.send_message({
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
					},
				}
			})

	async def send_translated_text(self, segment: TranslateInputOutput):
		for nc_sid in segment.target_nc_session_ids:
			if nc_sid not in self.nc_sid_map:
				continue
			await self.send_message({
				"type": "message",
				"message": {
					"recipient": {
						"type": "session",
						"sessionid": self.nc_sid_map[nc_sid],
					},
					"data": {
						"langId": segment.target_language,
						"message": segment.message,
						"speakerSessionId": segment.speaker_session_id,
						"final": True,
						# todo: change to "translate"?
						"type": "transcript",
					},
				}
			})

	async def close(self):  # noqa: C901
		if self.defunct.is_set():
			LOGGER.debug("SpreedClient is already defunct, skipping close", extra={
				"room_token": self.room_token,
				"tag": "client",
			})
			return

		if self._deferred_close_task and not self._deferred_close_task.done():
			LOGGER.debug("Cancelling deferred close task", extra={
				"room_token": self.room_token,
				"tag": "deferred_close",
			})
			self._deferred_close_task.cancel()
		self._deferred_close_task = None

		if self._reconnect_task and not self._reconnect_task.done():
			LOGGER.debug("Cancelling reconnect task", extra={
				"room_token": self.room_token,
				"tag": "reconnect",
			})
			self._reconnect_task.cancel()
		self._reconnect_task = None

		# A cancelled restart still finishes the release of its connection, which is
		# no longer in peer_connections. Wait for it, so that close() returns with
		# every connection closed.
		release_tasks = list(self._release_tasks)
		for task in release_tasks:
			task.cancel()
		await asyncio.gather(*release_tasks, return_exceptions=True)

		app_closing = self._monitor.cancelled() if self._monitor else False

		with suppress(Exception):
			if self._monitor and not self._monitor.done():
				LOGGER.debug("Cancelling monitor task", extra={
					"room_token": self.room_token,
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
				"tag": "transcriber",
			})
			async with self.transcriber_lock:
				transcribers_snap = list(self.transcribers.values())

			coros = [t.shutdown() for t in transcribers_snap]
			await asyncio.gather(*coros, return_exceptions=True)

			async with self.transcriber_lock:
				self.transcribers.clear()

		with suppress(Exception):
			# Take the entries out first: each close() fires the state change
			# handler, which removes its entry from the same dict.
			async with self.peer_connection_lock:
				peer_connections = list(self.peer_connections.values())
				self.peer_connections.clear()
				self._offer_requested.clear()

			# a "failed" connection still holds its receivers until it is closed
			to_close_coros = []
			for pc in peer_connections:
				if pc.pc.connectionState != "closed":
					LOGGER.debug("Closing peer connection", extra={
						"session_id": pc.session_id,
						"room_token": self.room_token,
						"tag": "peer_connection",
					})
					to_close_coros.append(pc.pc.close())

			await asyncio.gather(*to_close_coros, return_exceptions=True)
			self.resumeid = None
			self.sessionid = None

		with suppress(Exception):
			if self._transcript_sender and not self._transcript_sender.done():
				LOGGER.debug("Cancelling transcript sender task", extra={
					"room_token": self.room_token,
					"tag": "transcript",
				})
				self._transcript_sender.cancel()
			self._transcript_sender = None

		with suppress(Exception):
			self.should_translate.clear()
			await self.meta_translator.shutdown()
			if self.translated_text_sender and not self.translated_text_sender.done():
				LOGGER.debug("Cancelling translated text sender task", extra={
					"room_token": self.room_token,
					"tag": "translate",
				})
				self.translated_text_sender.cancel()
			self.translated_text_sender = None

		while not self.transcript_queue.empty():
			with suppress(Exception):
				self.transcript_queue.get_nowait()

		while not self.translate_queue_input.empty():
			with suppress(Exception):
				self.translate_queue_input.get_nowait()

		while not self.translate_queue_output.empty():
			with suppress(Exception):
				self.translate_queue_output.get_nowait()

		with suppress(Exception):
			if self._server and self._server.state == WsState.OPEN:
				LOGGER.info("Closing WebSocket connection for room", extra={
					"room_token": self.room_token,
					"tag": "connection",
				})
				# Close the WebSocket connection if it's still open
				await self._server.close()
			self._server = None

		self.defunct.set()
		if not app_closing and self.leave_call_cb:
			await self.leave_call_cb(self.room_token)
		self.leave_call_cb = None

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
				LOGGER.info("Added transcript target", extra={
					"session_id": session_id,
					"nc_session_id": nc_session_id,
					"targets": self.targets,
					"room_lang_id": self.room_lang_id,
					"room_token": self.room_token,
					"tag": "target",
				})
			else:
				LOGGER.info("Transcript target already exists", extra={
					"session_id": session_id,
					"nc_session_id": nc_session_id,
					"targets": self.targets,
					"room_lang_id": self.room_lang_id,
					"room_token": self.room_token,
					"tag": "target",
				})
			if self._deferred_close_task:
				self._deferred_close_task.cancel()
				self._deferred_close_task = None

	async def is_target(self, nc_session_id: str):
		"""Check if the given Nextcloud session ID corresponds to an active transcript target."""
		async with self.target_lock:
			return nc_session_id in self.nc_sid_map or nc_session_id in self._nc_sid_wait_stash

	async def remove_target(self, nc_session_id: str):
		async with self.target_lock:
			self._nc_sid_wait_stash.pop(nc_session_id, None)
			if nc_session_id not in self.nc_sid_map:
				LOGGER.info("HPB session ID corresponding to Nextcloud session ID '%s' not found",
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
				LOGGER.info("Removed transcript target", extra={
					"session_id": session_id,
					"nc_session_id": nc_session_id,
					"targets": self.targets,
					"room_lang_id": self.room_lang_id,
					"room_token": self.room_token,
					"tag": "target",
				})
				del self.targets[session_id]
				if len(self.targets) == 0:
					if self._deferred_close_task:
						self._deferred_close_task.cancel()
					self._deferred_close_task = asyncio.create_task(self.maybe_leave_call())
			else:
				LOGGER.info("Transcript target does not exist", extra={
					"session_id": session_id,
					"nc_session_id": nc_session_id,
					"targets": self.targets,
					"room_lang_id": self.room_lang_id,
					"room_token": self.room_token,
					"tag": "target",
				})

	async def remove_target_hpb_sid(self, session_id: str):
		async with self.target_lock:
			if session_id in self.targets:
				LOGGER.info("Removed transcript target", extra={
					"session_id": session_id,
					"targets": self.targets,
					"room_lang_id": self.room_lang_id,
					"room_token": self.room_token,
					"tag": "target",
				})
				del self.targets[session_id]
				if len(self.targets) == 0:
					if self._deferred_close_task:
						self._deferred_close_task.cancel()
					self._deferred_close_task = asyncio.create_task(self.maybe_leave_call())
			else:
				LOGGER.info("Transcript target does not exist", extra={
					"session_id": session_id,
					"targets": self.targets,
					"room_lang_id": self.room_lang_id,
					"room_token": self.room_token,
					"tag": "target",
				})

	async def signalling_monitor(self):  # noqa: C901
		"""Monitor the signaling server for incoming messages."""
		while True:
			try:
				message = await self.receive()
			except WebSocketException as e:
				LOGGER.exception("HPB websocket error, reconnecting...", exc_info=e, extra={
					"room_token": self.room_token,
					"tag": "monitor",
				})
				if not self._reconnect_task or self._reconnect_task.done():
					self._reconnect_task = asyncio.create_task(
						self.connect(reconnect=ReconnectMethod.SHORT_RESUME),
						name=f"close-{self.room_token}"
					)
				await asyncio.sleep(2)
				continue
			except asyncio.CancelledError:
				LOGGER.debug("Signalling monitor task cancelled", extra={
					"room_token": self.room_token,
					"tag": "monitor",
				})
				if not self._close_task:
					self._close_task = asyncio.create_task(self.close(), name=f"close-{self.room_token}")
				raise
			except Exception as e:
				LOGGER.exception("Unexpected error in signalling monitor", exc_info=e, extra={
					"room_token": self.room_token,
					"tag": "monitor",
				})
				if not self._close_task:
					self._close_task = asyncio.create_task(self.close(), name=f"close-{self.room_token}")
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
				if message.get("error", {}).get("code") == "client_not_found":
					# "No MCU client found to send message to."
					# transcript sent to a participant who is not in the call anymore
					continue

				# only close if the error is not recoverable
				if not self._close_task:
					self._close_task = asyncio.create_task(self.close(), name=f"close-{self.room_token}")
				return

			if (
				message["type"] == "event"
				and message["event"]["target"] == "participants"
				and message["event"]["type"] == "update"
			):
				LOGGER.info("Participants update received", extra={
					"room_token": self.room_token,
					"recv_message": message,
					"tag": "participants",
				})

				if message["event"]["update"].get("all") and message["event"]["update"].get("incall") == 0:
					LOGGER.info("Call ended for everyone, closing connection", extra={
						"room_token": self.room_token,
						"recv_message": message,
						"tag": "participants",
					})
					if not self._close_task:
						self._close_task = asyncio.create_task(self.close(), name=f"close-{self.room_token}")
					return

				users_update = message["event"]["update"].get("users", [])
				if not users_update:
					continue

				for user_desc in users_update:
					if user_desc.get("internal", False):
						continue

					if user_desc["inCall"] == CallFlag.DISCONNECTED:
						LOGGER.info("User disconnected", extra={
							"user_desc": user_desc,
							"room_token": self.room_token,
							"tag": "participants",
						})
						await self.release_peer_connection(user_desc["sessionId"])
						await self.remove_target_hpb_sid(user_desc["sessionId"])
						async with self.target_lock:
							# "nextcloudSessionId" may not be present in the user_desc in call disconnects
							self.nc_sid_map.pop(user_desc.get("nextcloudSessionId", ""), None)
						continue

					# user connected, keep a map of Nextcloud session IDs to HPB session IDs
					async with self.target_lock:
						# not sure why a KeyError is hit when the monitor task is cancelled,
						# adding a guard to prevent error logs
						if "nextcloudSessionId" in user_desc:
							# todo: get the userid for the session ids and maintain a map in "sid_translation_lang_map"
							# todo: use the ownerId of the room for the OCP translations, using talk api, or something.
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

					# user connected with audio
					if (user_desc["inCall"] & CallFlag.IN_CALL and user_desc["inCall"] & CallFlag.WITH_AUDIO):
						LOGGER.info("User joined with audio", extra={
							"user_desc": user_desc,
							"room_token": self.room_token,
							"tag": "participants",
						})
						async with self.peer_connection_lock:
							if (
								user_desc["sessionId"] in self.peer_connections
								and is_live(self.peer_connections[user_desc["sessionId"]].pc)
							):
								LOGGER.info("Peer connection for user already exists, skipping offer request", extra={
									"user_desc": user_desc,
									"room_token": self.room_token,
									"tag": "participants",
								})
								continue
							last_request = self._offer_requested.get(user_desc["sessionId"])
							if (
								last_request is not None
								and time.monotonic() - last_request < OFFER_REQUEST_COOLDOWN
							):
								LOGGER.info("Offer request already in flight, skipping duplicate", extra={
									"user_desc": user_desc,
									"room_token": self.room_token,
									"tag": "participants",
								})
								continue
							self._offer_requested[user_desc["sessionId"]] = time.monotonic()
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
						LOGGER.info("Last user left the call, closing connection", extra={
							"room_token": self.room_token,
							"transcriber_session_id": users_update[transcriber_index].get("sessionId"),
							"tag": "participants",
						})
						if not self._close_task:
							self._close_task = asyncio.create_task(self.close(), name=f"close-{self.room_token}")
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
				LOGGER.info("Received bye message, closing connection", extra={
					"room_token": self.room_token,
					"recv_message": message,
					"tag": "bye",
				})
				if not self._close_task:
					self._close_task = asyncio.create_task(self.close(), name=f"close-{self.room_token}")

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
		if (
			len_targets == 0
			and not (self.should_translate.is_set() and await self.meta_translator.is_translating())
		):
			LOGGER.warning("No transcript/translation receivers for %s secs, leaving the call",
				CALL_LEAVE_TIMEOUT,
				extra={
					"room_token": self.room_token,
					"tag": "maybe_leave_call",
				}
			)
			if not self._close_task:
				self._close_task = asyncio.create_task(self.close(), name=f"close-{self.room_token}")
		self._deferred_close_task = None

	async def release_peer_connection(self, session_id: str, pc: RTCPeerConnection | None = None) -> None:
		"""Release a speaker's peer connection, transcriber and pending offer request.

		Cleanup when peer connection does not exist.
		When it does exist and has not been replaced from a newer offer, close it.
		If it's a different pc object, the transcriber and the offer request then
		belong to the new connection. pc itself is closed in either case.
		"""
		async with self.peer_connection_lock:
			entry = self.peer_connections.get(session_id)
			# when pc is None, the user disconnected so pc from `peer_connections` should be cleaned and stopped
			owned = pc is None or (entry is not None and entry.pc is pc)
			if owned:
				self._offer_requested.pop(session_id, None)
				if entry is not None:
					del self.peer_connections[session_id]
					pc = entry.pc

		if owned:
			async with self.transcriber_lock:
				if (transcriber := self.transcribers.pop(session_id, None)) is not None:
					await transcriber.shutdown()

		# a "failed" connection still holds its receivers until it is closed
		if pc is not None and pc.connectionState != "closed":
			with suppress(Exception):
				await pc.close()

	async def restart_speaker(self, session_id: str, pc: RTCPeerConnection) -> None:
		"""Release a speaker whose transcriber ended by itself, and ask for a new offer.

		The speaker is still in the call, and no participant update follows that
		would request the offer, so without the request the speaker stays without
		transcripts until the call ends. The signaling server can leave a request
		unanswered: in a test it did so for one sent right after the old
		connection closed. So request again until an offer arrives.
		"""
		release = asyncio.create_task(self.release_peer_connection(session_id, pc))
		try:
			await asyncio.shield(release)
		except asyncio.CancelledError:
			# close() cancelled this task, and it no longer has the connection in
			# peer_connections. Finish the release: a cancelled pc.close() leaves the
			# transports open, and a second pc.close() waits for the first forever.
			await release
			raise
		for _ in range(OFFER_REQUEST_TRIES):
			async with self.peer_connection_lock:
				if self.defunct.is_set() or session_id in self.peer_connections:
					return
				requested = time.monotonic()
				self._offer_requested[session_id] = requested
			await self.send_offer_request(session_id)
			await asyncio.sleep(OFFER_REQUEST_COOLDOWN)
			async with self.peer_connection_lock:
				# An offer and the speaker's leave remove the entry, and the monitor replaces it.
				if self._offer_requested.get(session_id) != requested:
					return
		LOGGER.error("No offer from %s after %d requests, the speaker stays without transcripts",
			session_id, OFFER_REQUEST_TRIES, extra={
				"session_id": session_id,
				"room_token": self.room_token,
				"tag": "offer",
			})

	async def handle_offer(self, message):  # noqa: C901
		"""Handle incoming offer messages."""
		spkr_sid = message["message"]["sender"]["sessionid"]
		async with self.peer_connection_lock:
			self._offer_requested.pop(spkr_sid, None)
			if spkr_sid in self.peer_connections and is_live(self.peer_connections[spkr_sid].pc):
				LOGGER.debug("Peer connection for user already exists, skipping offer request", extra={
					"session_id": spkr_sid,
					"room_token": self.room_token,
					"tag": "participants",
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
		weakself = weakref.ref(self)

		@pc.on("connectionstatechange")
		async def on_connectionstatechange():
			LOGGER.info("Peer connection state changed", extra={
				"session_id": spkr_sid,
				"connection_state": pc.connectionState,
				"room_token": weakself().room_token if weakself() else None,
				"tag": "peer_connection",
			})

			if not weakself():
				LOGGER.error("SpreedClient instance is gone, cannot handle connection state change", extra={
					"session_id": spkr_sid,
					"connection_state": pc.connectionState,
					"room_token": None,
					"tag": "peer_connection",
				})
				return

			if not is_live(pc):
				# log "closed" as INFO
				log = LOGGER.warning if pc.connectionState == "failed" else LOGGER.info
				log("Peer connection state for '%s' is '%s'", spkr_sid, pc.connectionState, extra={
					"session_id": spkr_sid,
					"connection_state": pc.connectionState,
					"room_token": weakself().room_token,
					"tag": "peer_connection",
				})
				await weakself().release_peer_connection(spkr_sid, pc)

		pc.addTransceiver("audio", direction="recvonly")
		@pc.on("track")
		async def on_track(track):  # noqa: C901
			if track.kind == "audio":
				LOGGER.debug("Receiving %s track from %s", track.kind, spkr_sid, extra={
					"session_id": spkr_sid,
					"room_token": weakself().room_token if weakself() else None,
					"tag": "track",
				})

				if not weakself():
					LOGGER.warning("SpreedClient instance is gone, cannot handle track", extra={
						"session_id": spkr_sid,
						"room_token": None,
						"tag": "track",
					})
					return

				stream = AudioStream(track)
				language = weakself().room_lang_id
				transcriber = Transcriber(
					spkr_sid,
					language,
					weakself().transcript_queue,
					weakself().should_translate,
					weakself().translate_queue_input,
				)
				# Connect without transcriber_lock: connect() waits up to ASR_CONNECT_TIMEOUT for
				# an ASR server that restarts, and the releases of other speakers, which the
				# signaling monitor awaits, need the lock.
				try:
					await transcriber.connect()
				except Exception:
					LOGGER.exception("Error in connection and start of the ASR server. Cannot continue further.",
						extra={
							"server_url": asr_server_url(),
							"session_id": spkr_sid,
							"room_token": weakself().room_token if weakself() else None,
							"tag": "asr",
						},
					)
					if weakself() and not weakself()._close_task:
						weakself()._close_task = asyncio.create_task(
							weakself().close(),
							name=f"close-{weakself().room_token}",
						)
					return

				def on_audio_task_done(task: asyncio.Task):
					# The audio task ends by itself when the ASR connection is lost. The peer
					# connection is still open then, and aiortc keeps decoding the speaker's audio
					# into the queue of the track, which nothing reads any more: about 1.2 MB each
					# second for each speaker, until the speaker leaves. A cancelled task was shut
					# down by a release or by close(), and a newer track replaces the transcriber.
					# A task that ended with its track needs no restart: the connection closed,
					# and its state change handler releases it.
					if (
						task.cancelled()
						or transcriber.stream_ended
						or not weakself()
						or weakself().defunct.is_set()
						or weakself().transcribers.get(spkr_sid) is not transcriber
					):
						return
					LOGGER.warning("Transcriber for %s ended, restarting its peer connection", spkr_sid, extra={
						"session_id": spkr_sid,
						"room_token": weakself().room_token,
						"tag": "transcriber",
					})
					release = asyncio.create_task(weakself().restart_speaker(spkr_sid, pc))
					weakself()._release_tasks.add(release)
					release.add_done_callback(weakself()._release_tasks.discard)

				is_current_pc = False
				if weakself():
					async with weakself().peer_connection_lock:
						entry = weakself().peer_connections.get(spkr_sid)

					async with weakself().transcriber_lock:
						# Register only for the speaker's current peer connection. The speaker can be
						# released, or close() can run, while this transcriber connects, and a
						# transcriber of a peer connection that is no longer the speaker's would never be
						# shut down. A release removes the entry before it takes this lock, so either
						# it finds this transcriber here, or this check fails.
						is_current_pc = entry is not None and entry.pc is pc and is_live(pc)
						if is_current_pc:
							# A renegotiated track replaces the speaker's transcriber. Stop the old one,
							# or its audio task and ASR connection keep running.
							if (previous := weakself().transcribers.get(spkr_sid)) is not None:
								await previous.shutdown()
							weakself().transcribers[spkr_sid] = transcriber
							await transcriber.start(stream=stream)
							if transcriber.audio_task is not None:
								transcriber.audio_task.add_done_callback(on_audio_task_done)

				if not is_current_pc:
					LOGGER.debug("Track of a released connection, not transcribing it", extra={
						"session_id": spkr_sid,
						"room_token": weakself().room_token if weakself() else None,
						"tag": "track",
					})
					await transcriber.shutdown()
					return

				# A language switch that ran while this transcriber connected did not see it.
				if weakself() and weakself().room_lang_id != language:
					try:
						await transcriber.set_language(weakself().room_lang_id)
					except Exception:
						LOGGER.warning("Could not switch the new transcriber of %s to the room language",
							spkr_sid, extra={
								"session_id": spkr_sid,
								"room_lang_id": weakself().room_lang_id,
								"room_token": weakself().room_token,
								"tag": "transcriber",
							},
						)

				LOGGER.info(
					"Started transcriber for %s in %s",
					spkr_sid,
					LANGUAGE_MAP.get(weakself().room_lang_id).name,
					extra={
						"session_id": spkr_sid,
						"room_lang_id": weakself().room_lang_id,
						"room_token": weakself().room_token,
						"tag": "transcriber",
					},
				)

		async with self.peer_connection_lock:
			self.peer_connections[spkr_sid] = PeerConnection(session_id=spkr_sid, pc=pc)

		try:
			await pc.setRemoteDescription(
				RTCSessionDescription(type="offer", sdp=message["message"]["data"]["payload"]["sdp"])
			)
			answer = await pc.createAnswer()
			await pc.setLocalDescription(answer)
			await self.send_offer_answer(
				message["message"]["data"]["from"],
				message["message"]["data"]["sid"],
				answer.sdp,
			)
		except Exception:
			# release the peer connection so the next offer is not considered a duplicate
			LOGGER.exception("Negotiation with %s failed, releasing its peer connection", spkr_sid, extra={
				"session_id": spkr_sid,
				"room_token": self.room_token,
				"tag": "offer",
			})
			await self.release_peer_connection(spkr_sid, pc)
			return

		LOGGER.debug("Sent answer for offer from %s", spkr_sid, extra={
			"session_id": spkr_sid,
			"answer": answer,
			"room_token": self.room_token,
			"tag": "offer",
		})

		# @pc.on("icecandidate") may not work as well so we wait for the gathering to complete
		# https://github.com/aiortc/aiortc/issues/1344
		waited_for = 0
		while pc.iceGatheringState != "complete" and waited_for <= ICE_GATHERING_TIMEOUT:
			await asyncio.sleep(0.2)
			waited_for += 0.2

		if waited_for > ICE_GATHERING_TIMEOUT and pc.iceGatheringState != "complete":
			LOGGER.warning("Timed out waiting for ice gathering to complete, continuing with the peer connection still")

		local_sdp = pc.localDescription.sdp
		LOGGER.debug("Local SDP for %s:", spkr_sid, extra={
			"session_id": spkr_sid,
			"room_token": self.room_token,
			"local_sdp": local_sdp,
			"tag": "offer",
		})

		candidates = []
		for line in local_sdp.splitlines():
			if line.startswith("a=candidate:"):
				candidates.append(line[2:])
				await self.send_candidate(
					message["message"]["sender"]["sessionid"],
					message["message"]["data"]["sid"],
					line[2:],
				)

		LOGGER.info("Sent candidates to the peer", extra={ "candidates": candidates })

	async def set_language(self, lang_id: str):
		excs = []
		async with self.transcriber_lock:
			# Set first, so that a transcriber created from now on, such as the replacement
			# of one that lost its ASR connection, starts in the new language.
			self.room_lang_id = lang_id
			transcribers = list(self.transcribers.values())
		# one failing transcriber must not keep the others in the old language
		for transcriber in transcribers:
			# Without an ASR connection there is nothing to switch: its audio task ended, and
			# its replacement reads room_lang_id. Counting it as a failure would report an
			# error for a switch that reached every speaker.
			if not transcriber.connected:
				continue
			try:
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
			raise ASRException(
				f"Failed to set language for multiple transcribers, first of which is: {excs[0]}",
				retcode=500,
			)
		if len(excs) == 1:
			raise ASRException(f"Failed to set language for one transcriber: {excs[0]}", retcode=500)

	async def transcipt_queue_consumer(self):
		"""Consume transcripts from the queue and send them to the server."""
		LOGGER.debug("Starting the transcript queue consumer", extra={
			"room_token": self.room_token,
			"tag": "transcript",
		})
		timeout = SEND_TIMEOUT
		timeout_count = 0

		while True:
			try:
				transcript: Transcript = await self.transcript_queue.get()  # type: ignore[annotation-unchecked]

				await asyncio.wait_for(
					self.send_transcript(transcript),
					timeout=timeout,
				)
				timeout_count -= 1 if timeout_count > 0 else 0
				if timeout_count == 0 and timeout > SEND_TIMEOUT:
					timeout = max(SEND_TIMEOUT, int(timeout / TIMEOUT_INCREASE_FACTOR))
					LOGGER.debug("Decreased transcript send timeout to %d seconds", timeout, extra={
						"speaker_session_id": transcript.speaker_session_id,
						"room_token": self.room_token,
						"tag": "translate",
					})
			except TimeoutError:
				LOGGER.error("Timeout while sending a transcript", extra={
					"speaker_session_id": transcript.speaker_session_id,
					"room_token": self.room_token,
					"tag": "transcript",
				})

				if timeout > MAX_TRANSCRIPT_SEND_TIMEOUT:
					LOGGER.warning("Transcription timeout too high (%d seconds), not increasing further",
						timeout,
						extra={
						"speaker_session_id": transcript.speaker_session_id,
						"room_token": self.room_token,
						"tag": "translate",
						},
					)
					continue

				timeout_count += 1
				if timeout_count >= 5:
					timeout = int(timeout * TIMEOUT_INCREASE_FACTOR)
					LOGGER.error("Multiple timeouts while sending transcripts, increasing to %d", timeout, extra={
						"speaker_session_id": transcript.speaker_session_id,
						"room_token": self.room_token,
						"tag": "transcript",
					})
					timeout_count = 0
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

	async def set_target_language(self, nc_session_id: str, target_lang_id: str, new_call: bool = False):
		"""
		Raises
		------
			TranslateFatalException: If a fatal error occurs and all translators should be removed
			TranslateLangPairException: If the language pair is not supported
			TranslateException: If any other translation error occurs
			TranscriptTargetNotFoundException: If the transcript target is not found and new_call is False
		"""  # noqa

		if target_lang_id == self.room_lang_id:
			LOGGER.debug("Target language is the same as the original language, doing nothing", extra={
				"nc_session_id": nc_session_id,
				"target_lang_id": target_lang_id,
				"original_lang_id": self.room_lang_id,
				"room_token": self.room_token,
				"tag": "translate",
			})
			raise TranslateLangPairException(
				f"Target language '{target_lang_id}' is the same as the original language '{self.room_lang_id}'",
			)

		if (
			not new_call
			and not await self.is_target(nc_session_id)
			and not await self.meta_translator.is_translation_target(nc_session_id)
		):
			raise TranscriptTargetNotFoundException(
				f"Transcript target with Nextcloud session ID '{nc_session_id}' not found."
				" Transcription must be enabled for the target before setting the translation language.",
			)

		if not await self.meta_translator.is_target_lang_supported(target_lang_id):
			raise TranslateLangPairException(
				f"Target language '{target_lang_id}' is not supported",
			)

		await self.meta_translator.add_translator(target_lang_id, nc_session_id)
		# prevent original language transcripts from being sent to this target
		await self.remove_target(nc_session_id)

	async def remove_translation(self, nc_session_id: str, add_target_back: bool = False):
		await self.meta_translator.remove_translator(nc_session_id)
		if add_target_back:
			# add the target back to receive original language transcripts
			await self.add_target(nc_session_id)

	async def translated_text_consumer(self):
		"""Consume translated text segments from the queue and send them to the server."""
		LOGGER.debug("Starting the translated text queue consumer", extra={
			"room_token": self.room_token,
			"tag": "translate",
		})
		timeout = SEND_TIMEOUT
		timeout_count = 0
		while True:
			try:
				segment: TranslateInputOutput = await self.translate_queue_output.get()  # type: ignore[annotation-unchecked]

				await asyncio.wait_for(
					self.send_translated_text(segment),
					timeout=timeout,
				)
				timeout_count -= 1 if timeout_count > 0 else 0
				if timeout_count == 0 and timeout > SEND_TIMEOUT:
					timeout = max(SEND_TIMEOUT, int(timeout / TIMEOUT_INCREASE_FACTOR))
					LOGGER.debug("Decreased translated text send timeout to %d seconds", timeout, extra={
						"origin_language": segment.origin_language,
						"target_language": segment.target_language,
						"speaker_session_id": segment.speaker_session_id,
						"room_token": self.room_token,
						"tag": "translate",
					})
			except TimeoutError:
				LOGGER.debug("Timeout while sending a translated text", extra={
					"origin_language": segment.origin_language,
					"target_language": segment.target_language,
					"speaker_session_id": segment.speaker_session_id,
					"room_token": self.room_token,
					"tag": "translate",
				})

				if timeout > MAX_TRANSLATION_SEND_TIMEOUT:
					LOGGER.warning("Translation timeout too high (%d seconds), not increasing further", timeout, extra={
						"origin_language": segment.origin_language,
						"target_language": segment.target_language,
						"speaker_session_id": segment.speaker_session_id,
						"room_token": self.room_token,
						"tag": "translate",
					})
					continue

				timeout_count += 1
				if timeout_count >= 5:
					timeout = int(timeout * TIMEOUT_INCREASE_FACTOR)
					LOGGER.error("Multiple timeouts while sending translated texts, increasing to %d", timeout, extra={
						"origin_language": segment.origin_language,
						"target_language": segment.target_language,
						"speaker_session_id": segment.speaker_session_id,
						"room_token": self.room_token,
						"tag": "translate",
					})
					timeout_count = 0
				continue

			except asyncio.CancelledError:
				LOGGER.debug("Translated text consumer task cancelled", extra={
					"room_token": self.room_token,
					"tag": "translate",
				})
				raise
			except Exception as e:
				LOGGER.exception("Error while sending a translated text", exc_info=e, extra={
					"origin_language": segment.origin_language,
					"target_language": segment.target_language,
					"speaker_session_id": segment.speaker_session_id,
					"room_token": self.room_token,
					"tag": "translate",
				})
				continue
