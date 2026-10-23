#
# SPDX-FileCopyrightText: 2025 Nextcloud GmbH and Nextcloud contributors
# SPDX-License-Identifier: AGPL-3.0-or-later
#

import asyncio
import json
import logging
import os
from contextlib import suppress
from functools import partial
from time import perf_counter
from urllib.parse import urlparse

import numpy as np
from aiortc.mediastreams import MediaStreamError
from audio_stream import AudioStream
from constants import ASR_CONNECT_TIMEOUT, MAX_AUDIO_FRAMES, MAX_CONNECT_TRIES, MIN_TRANSCRIPT_SEND_INTERVAL
from livetypes import ASRException, StreamEndedException, Transcript, TranslateInputOutput
from models import LANGUAGE_MAP
from utils import get_ssl_context
from websockets import ClientConnection, connect

LOGGER = logging.getLogger("lt.transcriber")


class Transcriber:
	def __init__(
		self,
		session_id: str,
		language: str,
		transcript_queue: asyncio.Queue,
		should_translate: asyncio.Event,
		translate_queue_input: asyncio.Queue,
	):
		self.__asrcon: ClientConnection | None = None
		self.__asrcon_lock = asyncio.Lock()
		self.audio_task: asyncio.Task | None = None

		self.__server_url = os.environ.get("LT_ASR_SERVER_URL", "ws://localhost:2702")

		self.__session_id = session_id
		self.__language = language
		self.__transcript_queue = transcript_queue
		self.__should_translate = should_translate
		self.__translate_queue_input = translate_queue_input

	async def connect(self):
		ssl_ctx = get_ssl_context(self.__server_url)
		async with self.__asrcon_lock:
			self.__asrcon: ClientConnection | None = await connect(  # type: ignore[annotation-unchecked]
				self.__server_url,
				**({
					"server_hostname": urlparse(self.__server_url).hostname,
					"ssl": ssl_ctx,
				} if ssl_ctx else {}),
				open_timeout=ASR_CONNECT_TIMEOUT,
				# PCM barely compresses (to 0.76 of its size) and deflating it costs
				# CPU on both ends, so do not offer permessage-deflate.
				compression=None,
			)
			await self.__asrcon.send(
				json.dumps({
					"config": {
						"sample_rate": 48000,
						"language": self.__language,
					}
				})
			)
			# Read the reply here. Left unread, it answers the first audio frame and
			# every later reply arrives one frame late.
			try:
				await self.__await_config_ack(self.__language)
			except BaseException:
				with suppress(Exception):
					await self.__asrcon.close()
				self.__asrcon = None
				raise

	async def __await_config_ack(self, language: str) -> None:
		"""Read replies until the one to a config message, and raise unless it reports success.

		Replies to audio frames already in flight may arrive first, thus up to
		MAX_CONNECT_TRIES of them are skipped. Each reply is parsed as JSON before
		the key is looked up, because a partial transcript can contain the word
		"success". The caller holds __asrcon_lock.
		"""
		if not self.__asrcon:
			raise ASRException("ASR connection is not established")
		for _ in range(MAX_CONNECT_TRIES):
			response = await self.__asrcon.recv()
			try:
				json_res = json.loads(response)
			except json.JSONDecodeError as e:
				raise ASRException("Error decoding JSON response from ASR server after a config message") from e
			if not isinstance(json_res, dict) or "success" not in json_res:
				continue
			LOGGER.debug("Response from ASR server to a config message", extra={
				"recv_response": response,
				"session_id": self.__session_id,
				"tag": "asr",
			})
			if not json_res["success"]:
				raise ASRException(f"ASR server rejected the configuration for language {language!r}")
			return
		LOGGER.error(
			"Max received messages limit reached while waiting for ASR server response. "
			"Expected 'success' in response from ASR server after a config message.",
			extra={
				"session_id": self.__session_id,
				"tag": "asr",
			},
		)
		raise ASRException("ASR server did not respond in an expected fashion after a config message")

	async def start(self, stream: AudioStream):
		if self.audio_task and not self.audio_task.done():
			LOGGER.debug("Audio task already running for session_id: %s", self.__session_id, extra={
				"session_id": self.__session_id,
				"tag": "asr",
			})
			return
		self.audio_task = asyncio.create_task(self.__run_audio_xfer(stream))
		self.audio_task.add_done_callback(partial(self.__done_cb, stream))

	async def shutdown(self):
		if self.audio_task and not self.audio_task.done():
			LOGGER.debug("Cancelling audio task for session_id: %s", self.__session_id, extra={
				"session_id": self.__session_id,
				"tag": "asr",
			})
			self.audio_task.cancel()
		self.audio_task = None

	def __done_cb(self, stream: AudioStream, future: asyncio.Future):
		# the audio task is done, clean up
		LOGGER.debug("Stopping audio task for session_id: %s", self.__session_id, extra={
			"session_id": self.__session_id,
			"tag": "asr",
		})
		stream.stop()
		future.cancel("Cancelling audio task in Transcriber for session_id: " + self.__session_id)
		self.audio_task = None

	async def set_language(self, language: str) -> None:
		if not self.__asrcon:
			raise ASRException("ASR connection is not established, cannot switch language")
		if self.__language == language:
			LOGGER.debug("Language is already set to %s, no need to switch", LANGUAGE_MAP[language].name, extra={
				"language": language,
				"session_id": self.__session_id,
				"tag": "asr",
			})
			return

		LOGGER.info("Switching ASR language from %s to %s",
			LANGUAGE_MAP[self.__language].name, LANGUAGE_MAP[language].name,
			extra={
				"session_id": self.__session_id,
				"tag": "asr",
			},
		)

		# extend lock over the whole language switch process to avoid
		# sending transcripts with the old language attached
		async with self.__asrcon_lock:
			await self.__asrcon.send(
				json.dumps({
					"config": {
						"language": language,
					}
				})
			)
			await self.__await_config_ack(language)
			LOGGER.debug("ASR language switched to %s successfully", LANGUAGE_MAP[language].name, extra={
				"language": language,
				"session_id": self.__session_id,
				"tag": "asr",
			})
			self.__language = language


	async def __run_audio_xfer(self, stream: AudioStream):  # noqa: C901
		frames = []
		# pre-allocate buffer to avoid repeated heap allocation
		# 960 samples * 2 bytes * MAX_AUDIO_FRAMES is the typical size.
		dataframes = bytearray(960 * 2 * MAX_AUDIO_FRAMES)
		dataframes_len = 0
		last_sent = 0.0

		try:
			while True:
				fr = await stream.receive()
				frames.append(fr)

				# collect frames so we don't send partial results too often
				if len(frames) < MAX_AUDIO_FRAMES:
					continue

				dataframes_len = 0
				for fr in frames:
					# we only need to downmix to mono as the input stream is already s16 and 48kHz,
					# which is what the ASR server expects
					n_channels = len(fr.layout.channels)
					# only the first fr.samples * n_channels samples are valid.
					valid_bytes = fr.samples * n_channels * 2  # 2 bytes per s16 sample
					raw = bytes(fr.planes[0])[:valid_bytes]
					if n_channels >= 2:
						# interleaved s16 stereo
						samples_s16 = np.frombuffer(raw, dtype=np.int16)
						stereo = samples_s16.reshape(-1, n_channels)
						mono = stereo.mean(axis=1, dtype=np.float32).astype(np.int16)
						mono_bytes = mono.tobytes()
					else:
						mono_bytes = raw

					chunk_size = len(mono_bytes)
					new_len = dataframes_len + chunk_size

					if new_len > len(dataframes):
						dataframes.extend(b"\x00" * (new_len - len(dataframes)))
					dataframes[dataframes_len:new_len] = mono_bytes
					dataframes_len = new_len

				frames.clear()
				del fr

				async with self.__asrcon_lock:
					if not self.__asrcon:
						raise Exception("ASR connection is not established, cannot send audio data")
					await self.__asrcon.send(memoryview(dataframes)[:dataframes_len])
					result = await self.__asrcon.recv()
				if not result:
					continue

				try:
					json_msg = json.loads(result)
				except json.JSONDecodeError:
					LOGGER.error("Error decoding JSON message from ASR server", extra={
						"result": result,
						"session_id": self.__session_id,
						"tag": "asr",
					})
					continue

				if "partial" in json_msg:
					message = json_msg["partial"]
				elif "text" in json_msg:
					message = json_msg["text"]
				else:
					message = ""

				# the server gates every stream behind a VAD and answers silence with an
				# empty partial, which carries no transcript to show
				if message == "":
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
				"tag": "asr",
			})
		except asyncio.CancelledError:
			LOGGER.debug("Audio task cancelled for session id: %s", self.__session_id, extra={
				"session_id": self.__session_id,
				"tag": "asr",
			})
			raise
		except Exception as e:
			LOGGER.exception("Error in Transcriber for session id: %s", self.__session_id, exc_info=e, extra={
				"session_id": self.__session_id,
				"tag": "asr",
			})
		finally:
			async with self.__asrcon_lock:
				if self.__asrcon:
					LOGGER.debug("Closing ASR server connection for session id: %s", self.__session_id, extra={
						"session_id": self.__session_id,
						"tag": "asr",
					})
					with suppress(Exception):
						await self.__asrcon.send('{"eof" : 1}')
					with suppress(Exception):
						await self.__asrcon.close()
						self.__asrcon = None
