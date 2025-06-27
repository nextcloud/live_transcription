#!/usr/bin/env python3

# isort: off
import os
from nc_py_api.ex_app import persistent_storage

def override_env():
	# os.environ["VOSK_MODEL_PATH"] = os.environ.get("VOSK_MODEL_PATH", persistent_storage())
	# todo: let's see how the model switching plays out
	os.environ["VOSK_MODEL_PATH"] = os.path.join(persistent_storage(), "vosk-model-en-us-0.22")
	os.environ["VOSK_SPK_MODEL_PATH"] = os.environ.get("VOSK_SPK_MODEL_PATH", persistent_storage())

override_env()
# isort: on

import asyncio
import concurrent.futures
import json
import logging
from urllib.parse import urlparse

import websockets as ws
from vosk import GpuInit, GpuThreadInit, KaldiRecognizer, Model

THREAD_POOL_WORKERS = min(32, (os.cpu_count() or 1) + 4)
SAMPLE_RATE = 48000.0

model: Model
pool: concurrent.futures.ThreadPoolExecutor


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
	global model
	global args
	global pool

	loop = asyncio.get_running_loop()
	rec = None
	sample_rate = SAMPLE_RATE
	# show_words = args.show_words
	# max_alternatives = args.max_alternatives

	logging.info("Connection from %s", websocket.remote_address)

	while True:

		message = await websocket.recv()

		# Load configuration if provided
		if isinstance(message, str) and "config" in message:
			config = json.loads(message).get("config")
			if not config:
				logging.error("Invalid config message: %s", message)
				continue
			logging.info("Config: %s", config)
			if "sample_rate" in config:
				sample_rate = float(config["sample_rate"])
			# todo: dynamic model loading
			if "model" in config:
				model = Model(config["model"])
				model_changed = True
			# todo: language
			# if "words" in config:
			# 	show_words = bool(config["words"])
			# if "max_alternatives" in config:
			# 	max_alternatives = int(config["max_alternatives"])
			continue

		# Create the recognizer, word list is temporary disabled since not every model supports it
		if not rec or model_changed:
			model_changed = False
			rec = KaldiRecognizer(model, sample_rate)
			# rec.SetWords(show_words)
			# rec.SetMaxAlternatives(max_alternatives)

		response, stop = await loop.run_in_executor(pool, process_chunk, rec, message)
		await websocket.send(response)
		if stop:
			logging.info("Stream finished for %s", websocket.remote_address)
			break



async def start():

	global model
	global args
	global pool

	# Enable loging if needed
	#
	# logger = logging.getLogger('websockets')
	# logger.setLevel(logging.INFO)
	# logger.addHandler(logging.StreamHandler())
	logging.basicConfig(level=logging.INFO)

	args = type("", (), {})()

	vosk_url = os.environ.get("LT_VOSK_SERVER_URL", "ws://localhost:2702")
	vosk_parsed_url = urlparse(vosk_url)
	args.interface = vosk_parsed_url.hostname
	if not args.interface:
		raise ValueError("LT_VOSK_SERVER_URL must contain a valid hostname or IP address")
	# don't catch the exception here, it should fail if the port is not an integer
	args.port = vosk_parsed_url.port or 2702

	# args.max_alternatives = int(os.environ.get("VOSK_ALTERNATIVES", 0))
	# args.show_words = bool(os.environ.get("VOSK_SHOW_WORDS", True))

	# todo: dynamic model loading
	model = Model(os.environ["VOSK_MODEL_PATH"])

	# todo: fallback to cpu if gpu is fully occupied
	if os.environ.get("COMPUTE_DEVICE", "cpu").lower() == "cuda":
		GpuInit()
		pool = concurrent.futures.ThreadPoolExecutor(
			max_workers=THREAD_POOL_WORKERS,
			initializer=lambda: GpuThreadInit(),
		)
	else:
		pool = concurrent.futures.ThreadPoolExecutor(max_workers=THREAD_POOL_WORKERS)

	async with ws.serve(recognize, args.interface, args.port):
		await asyncio.Future()


# todo: semaphore for model loading
def load_model(lang: str):
	...


if __name__ == "__main__":
	asyncio.run(start())
