#
# SPDX-FileCopyrightText: 2025 Nextcloud GmbH and Nextcloud contributors
# SPDX-License-Identifier: AGPL-3.0-or-later
#

import asyncio
import json
import logging
import os
import queue
import threading
from contextlib import suppress
from functools import partial
from time import perf_counter
from urllib.parse import urlparse

from aiortc.mediastreams import MediaStreamError
from audio_stream import AudioStream
from av.audio.resampler import AudioResampler
from constants import MAX_AUDIO_FRAMES, MAX_CONNECT_TRIES, MIN_TRANSCRIPT_SEND_INTERVAL, VOSK_CONNECT_TIMEOUT
from livetypes import StreamEndedException, Transcript, TranslateInputOutput, VoskException
from models import LANGUAGE_MAP
from utils import get_ssl_context
from websockets import ClientConnection, connect

LOGGER = logging.getLogger("lt.transcriber")


class VoskTranscriber:
	def __init__(
		self,
		session_id: str,
		language: str,
		transcript_queue: asyncio.Queue,
		should_translate: threading.Event,
		translate_queue_input: queue.Queue,
	):
		self.__voskcon: ClientConnection | None = None
		self.__voskcon_lock = asyncio.Lock()
		self.audio_task: asyncio.Task | None = None
		self.__audio_task_lock = threading.RLock()

		self.__resampler = AudioResampler(format="s16", layout="mono", rate=48000)
		self.__server_url = os.environ.get("LT_VOSK_SERVER_URL", "ws://localhost:2702")

		self.__session_id = session_id
		self.__language = language
		self.__transcript_queue = transcript_queue
		self.__should_translate = should_translate
		self.__translate_queue_input = translate_queue_input

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

		# extend lock over the whole language switch process to avoid
		# sending transcripts with the old language attached
		async with self.__voskcon_lock:
			await self.__voskcon.send(
				json.dumps({
					"config": {
						"language": language,
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
			self.__language = language


	async def __run_audio_xfer(self, stream: AudioStream):  # noqa: C901
		frames = []
		last_sent = 0.0

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

				if "partial" in json_msg:
					message = json_msg["partial"]
				elif "text" in json_msg:
					message = json_msg["text"]
				else:
					message = ""

				# the english model outputs "the" periodically even in silent audio streams
				# no similar case was seen with any other language model so VAD seems unnecessary?
				if message == "" or message == "the":
					continue

				is_final = "text" in json_msg
				if not is_final and (perf_counter() - last_sent) < MIN_TRANSCRIPT_SEND_INTERVAL:
					# skip partial messages sent within MIN_TRANSCRIPT_SEND_INTERVAL (300ms)
					continue

				self.__transcript_queue.put_nowait(Transcript(
					final=is_final,
					lang_id=self.__language,
					message=message,
					speaker_session_id=self.__session_id,
				))
				last_sent = perf_counter()

				if is_final and self.__should_translate.is_set():
					# todo
					LOGGER.debug("Queuing message for translation", extra={
						"origin_language": self.__language,
						"transcript": message,
						"session_id": self.__session_id,
						"tag": "translate",
					})
					self.__translate_queue_input.put_nowait(TranslateInputOutput(
						origin_language=self.__language,
						target_language="",  # to be filled by MetaTranslator
						message=message,
						speaker_session_id=self.__session_id,
						target_nc_session_ids=set(),  # to be filled by MetaTranslator
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
