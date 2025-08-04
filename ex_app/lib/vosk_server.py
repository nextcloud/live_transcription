#!/usr/bin/env python3

import asyncio
import concurrent.futures
import json
import logging
import os
from contextlib import suppress
from urllib.parse import urlparse

import websockets as ws
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


def process_chunk(rec: KaldiRecognizer, message: ws.Data) -> tuple[str, bool]:
	if isinstance(message, str):
		if message == '{"eof" : 1}':
			return rec.FinalResult(), True
		if message == '{"reset" : 1}':
			return rec.FinalResult(), False
		return rec.PartialResult(), False

	if rec.AcceptWaveform(message):
		return rec.Result(), False
	return rec.PartialResult(), False


async def recognize(websocket: ws.ServerConnection):
	global pool

	loop = asyncio.get_running_loop()
	model = None
	rec = None
	lang = DEFAULT_LANGUAGE
	sample_rate = SAMPLE_RATE

	logging.info("Connection from %s", websocket.remote_address)

	while True:
		try:
			message = await websocket.recv()

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

			if not rec or model_changed:
				model_changed = False
				rec = KaldiRecognizer(model, sample_rate)

			response, stop = await loop.run_in_executor(pool, process_chunk, rec, message)
			await websocket.send(response)
			if stop:
				# sending {"eof" : 1} ends the stream and tries to release the model
				logging.info("Stream finished for %s", websocket.remote_address)
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
