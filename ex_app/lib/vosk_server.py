#
# SPDX-FileCopyrightText: 2025 Nextcloud GmbH and Nextcloud contributors
# SPDX-FileCopyrightText: 2020 Alpha Cephei Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-or-later
#

import asyncio
import concurrent.futures
import json
import logging
import os
from contextlib import suppress
from urllib.parse import urlparse

import numpy as np
import websockets as ws
from av.audio import AudioFrame
from av.audio.resampler import AudioResampler
from dotenv import load_dotenv
from models import MODELS_LIST
from nc_py_api.ex_app import persistent_storage
from vosk import KaldiRecognizer, Model

load_dotenv()

THREAD_POOL_WORKERS = min(32, (os.cpu_count() or 1) + 4)
if os.getenv("LT_MAX_WORKERS", "invalid").isnumeric():
	THREAD_POOL_WORKERS = max(5, int(os.environ["LT_MAX_WORKERS"]))
SAMPLE_RATE = 48000.0
DEFAULT_LANGUAGE = "en"
TARGET_SAMPLE_RATE = int(SAMPLE_RATE)
TARGET_LAYOUT = "mono"
TARGET_FORMAT = "s16"


models: dict[str, Model] = {}
model_lock = asyncio.Lock()
model_counter: dict[str, int] = {}
pool: concurrent.futures.ThreadPoolExecutor


async def get_model(lang: str) -> Model | None:
	global models
	global model_lock
	global model_counter

	async with model_lock:
		if lang not in MODELS_LIST:
			logging.error("Unsupported language: %s", lang)
			return None

		if lang not in models:
			models[lang] = Model(os.path.join(persistent_storage(), MODELS_LIST[lang]))

			if models[lang] is None:
				logging.error("Failed to load model for language: %s", lang)
				del models[lang]
				return None

			model_counter[lang] = 1
			logging.info("Loaded model for language: %s", lang)
			return models[lang]

		model_counter[lang] += 1
		logging.info("Increased model counter for language: %s", lang)
		return models[lang]


async def maybe_release_model(lang: str):
	global models
	global model_lock
	global model_counter

	async with model_lock:
		if lang in model_counter and model_counter[lang] > 1:
			model_counter[lang] -= 1
			logging.info("Decreased model counter for language: %s", lang)
			return
		if lang in models:
			del models[lang]
			del model_counter[lang]
			logging.info("Released the model for language: %s", lang)
			return
		logging.warning("Attempted to release a non-existent model: %s", lang)


class AudioProcessor:
	"""Reusable audio processor with cached resampler for efficiency."""

	def __init__(self, src_format: str, src_layout: str, src_rate: int):
		self.src_format = src_format
		self.src_layout = src_layout
		self.src_rate = src_rate
		self.resampler = AudioResampler(
			format=TARGET_FORMAT,
			layout=TARGET_LAYOUT,
			rate=TARGET_SAMPLE_RATE
		)
		# Cache dtype based on format
		self.dtype = self._get_dtype(src_format)

	@staticmethod
	def _get_dtype(fmt: str) -> np.dtype:
		"""Map PyAV audio format to numpy dtype."""
		dtype_map = {
			"s16": np.int16,
			"s16p": np.int16,  # planar
			"s32": np.int32,
			"s32p": np.int32,  # planar
			"flt": np.float32,
			"fltp": np.float32,  # planar
			"dbl": np.float64,
			"dblp": np.float64,  # planar
			"u8": np.uint8,
			"u8p": np.uint8,  # planar
		}
		if fmt not in dtype_map:
			logging.warning("Unknown audio format '%s', defaulting to int16", fmt)
		return np.dtype(dtype_map.get(fmt, np.int16))

	def resample_to_vosk_bytes(self, raw: bytes) -> bytes:
		"""Resample raw audio bytes to Vosk format (s16 mono at TARGET_SAMPLE_RATE)."""
		arr = np.frombuffer(raw, dtype=self.dtype)

		# Reshape for AudioFrame: [channels, samples]
		if arr.ndim == 1:
			arr = arr.reshape(1, -1)

		in_frame = AudioFrame.from_ndarray(arr, format=self.src_format, layout=self.src_layout)
		in_frame.sample_rate = self.src_rate

		# Resample and collect output
		out = bytearray()
		for of in self.resampler.resample(in_frame):
			# s16 mono => 2 bytes/sample, use memoryview for efficiency
			out.extend(memoryview(of.planes[0])[: of.samples * 2])
		return bytes(out)


def process_chunk(rec: KaldiRecognizer, message: bytes) -> tuple[str, bool]:
	"""Process audio chunk with Vosk recognizer."""
	if rec.AcceptWaveform(message):
		return rec.Result(), False
	return rec.PartialResult(), False


async def recognize(websocket: ws.ServerConnection):  # noqa: C901
	global pool

	loop = asyncio.get_running_loop()
	model = None
	rec = None
	lang = DEFAULT_LANGUAGE
	sample_rate = SAMPLE_RATE
	model_changed = False
	audio_processor: AudioProcessor | None = None

	logging.info("Connection from %s", websocket.remote_address)

	while True:
		try:
			message = await websocket.recv()

			# Handle EOF message
			if isinstance(message, str) and message == '{"eof" : 1}':
				if rec:
					await websocket.send(rec.FinalResult())
				logging.info("Stream finished for %s", websocket.remote_address)
				await maybe_release_model(lang)
				break

			# Load configuration if provided
			if isinstance(message, str) and "config" in message:
				try:
					config = json.loads(message).get("config")
				except json.JSONDecodeError:
					logging.error("Invalid JSON in config message: %s", message)
					continue
				if not config:
					logging.error("Invalid config message: %s", message)
					continue
				logging.info("Config: %s", config)
				if "sample_rate" in config:
					sample_rate = float(config["sample_rate"])

				# Handle audio format configuration
				if "audio_format" in config and "audio_layout" in config and "audio_sample_rate" in config:
					audio_processor = AudioProcessor(
						src_format=config["audio_format"],
						src_layout=config["audio_layout"],
						src_rate=int(config["audio_sample_rate"])
					)
					logging.info("Audio processor initialized with format=%s, layout=%s, rate=%d",
						config["audio_format"], config["audio_layout"], config["audio_sample_rate"])

				if "language" in config:
					# try to release the model if it was already loaded
					if rec:
						# check inside to ensure this is not the first message
						if lang == config["language"]:
							logging.info("Language already set to %s", lang)
							await websocket.send('{"success" : true}')
							continue
						await maybe_release_model(lang)
					lang = config["language"]
					logging.info("Language set to %s", lang)
					model = await get_model(lang)
					if model is None:
						await websocket.send('{"success" : false}')
						continue
					await websocket.send('{"success" : true}')
					model_changed = True
				continue

			# Handle binary audio payload - assume client sends raw audio in known format
			if isinstance(message, (bytes, bytearray)):
				# Audio processor should be initialized by audio format config
				if audio_processor is None:
					logging.error("Received audio data before audio format configuration")
					continue

				if not rec or model_changed:
					model_changed = False
					rec = KaldiRecognizer(model, sample_rate)

				# Resample using cached processor
				vosk_bytes = audio_processor.resample_to_vosk_bytes(bytes(message))

				response, stop = await loop.run_in_executor(pool, process_chunk, rec, vosk_bytes)
				await websocket.send(response)
				if stop:
					await maybe_release_model(lang)
					break
		except ws.exceptions.ConnectionClosedOK:
			logging.info("Connection closed normally by client: %s", websocket.remote_address)
			await maybe_release_model(lang)
			break
		except ws.exceptions.WebSocketException as e:
			logging.error("WebSocket error, closing this connection and cleaning up: %s", e)
			await maybe_release_model(lang)
			with suppress(Exception):
				await websocket.close()
			break


async def start():
	global pool

	# Enable loging if needed
	#
	# logger = logging.getLogger('websockets')
	# logger.setLevel(logging.INFO)
	# logger.addHandler(logging.StreamHandler())
	logging.basicConfig(level=logging.INFO)

	disable_vosk = os.environ.get("LT_DISABLE_INTERNAL_VOSK", "false").lower()
	if disable_vosk in ("true", "1"):
		logging.info("LT_DISABLE_INTERNAL_VOSK is set, not starting internal Vosk server")
		return

	vosk_url = os.environ.get("LT_VOSK_SERVER_URL", "ws://localhost:2702")
	vosk_parsed_url = urlparse(vosk_url)
	interface = vosk_parsed_url.hostname
	if not interface:
		raise ValueError("LT_VOSK_SERVER_URL must contain a valid hostname or IP address")
	# don't catch the exception here, it should fail if the port is not an integer
	port = vosk_parsed_url.port or 2702

	if os.environ.get("COMPUTE_DEVICE", "cpu").lower() == "cuda":
		from vosk import GpuInit, GpuThreadInit
		GpuInit()
		pool = concurrent.futures.ThreadPoolExecutor(
			max_workers=THREAD_POOL_WORKERS,
			initializer=lambda: GpuThreadInit(),
		)
	else:
		pool = concurrent.futures.ThreadPoolExecutor(max_workers=THREAD_POOL_WORKERS)

	async with ws.serve(recognize, interface, port):
		await asyncio.Future()


if __name__ == "__main__":
	asyncio.run(start())
