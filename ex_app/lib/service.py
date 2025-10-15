#
# SPDX-FileCopyrightText: 2025 Nextcloud GmbH and Nextcloud contributors
# SPDX-License-Identifier: AGPL-3.0-or-later
#

import asyncio
import logging

from constants import HPB_SHUTDOWN_TIMEOUT, MAX_CONNECT_TRIES
from livetypes import (
	RoomLanguageSetRequest,
	SigConnectResult,
	SpreedClientException,
	TargetLanguageSetRequest,
	TranscribeRequest,
)
from spreed_client import SpreedClient
from utils import get_hpb_settings

LOGGER = logging.getLogger("lt.service")


class Application:
	def __init__(self) -> None:
		self.hpb_settings = get_hpb_settings()
		self.spreed_clients: dict[str, SpreedClient] = {}
		self.spreed_clients_lock = asyncio.Lock()
		self.__task_bin: set[asyncio.Task] = set()

	async def __defer_start_client(self, req: TranscribeRequest) -> None:
		t = 0
		# defer starting a new client to give time for cleanup of the previous one
		# wait up to HPB_SHUTDOWN_TIMEOUT seconds
		while t <= HPB_SHUTDOWN_TIMEOUT:
			t += 5
			await asyncio.sleep(t)
			async with self.spreed_clients_lock:
				if req.roomToken not in self.spreed_clients:
					break
		await self.transcript_req(req, deferred=True)

	async def transcript_req(self, req: TranscribeRequest, deferred: bool = False) -> None:
		async with self.spreed_clients_lock:
			if req.roomToken in self.spreed_clients:
				if self.spreed_clients[req.roomToken].defunct.is_set():
					if req.enable:
						# defer start of a new client giving time for cleanup
						LOGGER.info("SpreedClient for room token: %s is defunct, defering start of new client",
							req.roomToken,
							extra={
								"room_token": req.roomToken,
								"nc_session_id": req.ncSessionId,
								"tag": "application",
							},
						)
						task = asyncio.create_task(self.__defer_start_client(req))
						self.__task_bin.add(task)
						task.add_done_callback(self.__task_bin.discard)
					else:
						LOGGER.info("SpreedClient for room token: %s is already defunct,"
							" ignoring request to disable transcription",
							req.roomToken,
							extra={
								"room_token": req.roomToken,
								"nc_session_id": req.ncSessionId,
								"tag": "application",
							},
						)
					return

				# already in a call with a valid client
				if req.enable:
					LOGGER.info("Already in call for room token: %s, adding NC sessiond id: %s",
						req.roomToken,
						req.ncSessionId,
						extra={
							"room_token": req.roomToken,
							"nc_session_id": req.ncSessionId,
							"tag": "application",
						},
					)
					await self.spreed_clients[req.roomToken].add_target(req.ncSessionId)
				else:
					await self.spreed_clients[req.roomToken].remove_target(req.ncSessionId)
					# todo: await?
					self.spreed_clients[req.roomToken].remove_translation(req.ncSessionId)
				return

		if not req.enable:
			LOGGER.info(
				"Received request to turn off transcription for room token: %s, "
				"NC session id: %s but no call is active. Ignoring request.",
				req.roomToken, req.ncSessionId, extra={
					"room_token": req.roomToken,
					"nc_session_id": req.ncSessionId,
					"tag": "application",
				})
			return

		LOGGER.info("Joining call for room token: %s, NC session id: %s", req.roomToken, req.ncSessionId, extra={
			"room_token": req.roomToken,
			"nc_session_id": req.ncSessionId,
			"deferred": deferred,
			"tag": "application",
		})
		async with self.spreed_clients_lock:
			self.spreed_clients[req.roomToken] = SpreedClient(
				req.roomToken,
				self.hpb_settings,
				req.langId,
				self.__leave_call_cb,
			)

		tries = MAX_CONNECT_TRIES
		last_exc = None
		while tries > 0:
			try:
				await self.spreed_clients_lock.acquire()
				conn_result = await self.spreed_clients[req.roomToken].connect()
				match conn_result:
					case SigConnectResult.SUCCESS:
						LOGGER.info("Connected to signaling server for room token: %s", req.roomToken, extra={
							"room_token": req.roomToken,
							"tag": "connection",
						})
						await self.spreed_clients[req.roomToken].add_target(req.ncSessionId)
						return
					case SigConnectResult.FAILURE:
						# do not retry
						LOGGER.error("Failed to connect to signaling server for room token: %s", req.roomToken, extra={
							"room_token": req.roomToken,
							"try": MAX_CONNECT_TRIES + 1 - tries,
							"tag": "connection",
						})
						await self.spreed_clients[req.roomToken].close()
						if req.roomToken in self.spreed_clients:
							del self.spreed_clients[req.roomToken]
						return
					case SigConnectResult.RETRY:
						LOGGER.warning("Retrying connection to signaling server for room token: %s", req.roomToken,
							extra={
								"room_token": req.roomToken,
								"try": MAX_CONNECT_TRIES + 1 - tries,
								"tag": "connection",
							},
						)
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

	async def set_call_language(self, req: RoomLanguageSetRequest) -> None:
		# todo: re-check all locks if they're asyncio
		async with self.spreed_clients_lock:
			if req.roomToken not in self.spreed_clients:
				raise SpreedClientException(
					f"No SpreedClient for room token {req.roomToken}, cannot set language"
				)

			spreed_client = self.spreed_clients[req.roomToken]
			await spreed_client.set_language(req.langId)

	async def set_target_language(self, req: TargetLanguageSetRequest) -> None:
		if req.roomToken not in self.spreed_clients:
			raise SpreedClientException(
				f"No SpreedClient for room token {req.roomToken}, cannot set target language"
			)

		with self.spreed_clients_lock:
			spreed_client = self.spreed_clients[req.roomToken]
			await spreed_client.set_target_language(req.ncSessionId, req.langId)

	async def leave_call(self, room_token: str):
		"""Leave the call for the given room token. Called from an API endpoint."""
		async with self.spreed_clients_lock:
			if room_token not in self.spreed_clients:
				LOGGER.info("No SpreedClient for room token %s active, cannot leave call", room_token, extra={
					"room_token": room_token,
					"tag": "connection",
				})
				return

			spreed_client = self.spreed_clients[room_token]
			if spreed_client.defunct.is_set():
				LOGGER.info("SpreedClient for room token %s is already closed", room_token, extra={
					"room_token": room_token,
					"tag": "connection",
				})
				return

			await spreed_client.close(keep_peers=False)
			LOGGER.info("Left call for room token %s", room_token, extra={
				"room_token": room_token,
				"tag": "connection",
			})

	async def __leave_call_cb(self, room_token: str):
		async with self.spreed_clients_lock:
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
