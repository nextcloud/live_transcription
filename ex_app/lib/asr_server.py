#
# SPDX-FileCopyrightText: 2025 Nextcloud GmbH and Nextcloud contributors
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Streaming ASR websocket server backed by sherpa-onnx.

Speaks the same websocket protocol the previous Vosk server did, so the wire
contract with ``transcriber.py`` is unchanged:

    C->S  {"config": {"language": "en", "sample_rate": 48000}}
    S->C  {"success": true}                       (or false for a bad language)
    C->S  <binary s16le mono PCM>
    S->C  exactly one of {"partial": "..."} | {"text": "..."}
    C->S  {"eof": 1}
    S->C  {"text": "..."}                         then the connection closes

The server must send exactly one reply for each binary frame. The client waits
for a reply after each frame it sends, so a server that stays silent stops the
call.

All the connections share one ``OnlineRecognizer``. Per-speaker state lives in the
``OnlineStream``, which costs approximately 1 MiB, but a second recognizer reloads
the weights and costs approximately 730 MiB. Decoding runs in a bounded thread
pool, because ``decode_stream`` releases the GIL and would otherwise block the
event loop for every other speaker in the call.

A silero VAD gates each stream. Without it, a silent participant costs 85 % to
95 % of the CPU time of a speaking one, because the encoder runs regardless of
content. A four-person call is then slower than realtime on a 4-core host.
"""

import asyncio
import json
import logging
import os
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from contextlib import suppress
from urllib.parse import urlparse

import numpy as np
import sherpa_onnx
import websockets as ws
from dotenv import load_dotenv
from models import LANGUAGE_PROMPTS, MODEL_DIR_NAME, VAD_MODEL_NAME
from nc_py_api.ex_app import persistent_storage

load_dotenv()

LOGGER = logging.getLogger("lt.asr_server")

# The sample rate the model's feature extractor expects. sherpa-onnx resamples
# audio that arrives at another rate. The first accept_waveform() call builds a
# stateful Kaldi LinearResample that is reused for the whole stream, so there are
# no per-chunk boundary artefacts and no resampling dependency here. The rate
# must never change mid-stream, because sherpa-onnx then calls exit(-1).
ENGINE_RATE = 16000

VAD_WINDOW = 512  # silero v5 frame size at 16 kHz; sherpa requires exactly this

# How many gated chunks are replayed when the gate opens. See Session.__init__.
PRE_SPEECH_CHUNKS = 2

# Low-pass FIR that downsamples to ENGINE_RATE for the VAD. The recognizer
# resamples internally, but the VAD does not. It reads every 512-sample window as
# 32 ms at ENGINE_RATE, thus feeding it 48 kHz audio makes each window cover a
# third of the intended span. Measured on an 11 s clip, that finds speech in 5 %
# of windows instead of 68 %, which silences almost everything.
#
# A chunk is therefore downsampled twice, once here for the VAD and again inside
# sherpa-onnx for the recognizer. Feeding this decimated copy to both would avoid
# that, but it is the worse trade:
# worse. Measured over 90 s of speech it saves 0.14 s of CPU, which is 0.15 % of
# realtime against approximately 100 s spent decoding, and it costs 0.40 points of
# WER. The cause is that Kaldi's LinearResample has a longer filter than the short
# one below. Cheap and approximate is right for a speech or silence decision and
# wrong for the model's log-mel features, so each consumer keeps the resampler
# that suits it.
FIR_TAPS = 97


def _design_lowpass(decimation: int) -> np.ndarray:
	"""Windowed-sinc low pass at the decimated Nyquist frequency."""
	n = np.arange(FIR_TAPS) - (FIR_TAPS - 1) / 2
	cutoff = 0.5 / decimation
	taps = 2 * cutoff * np.sinc(2 * cutoff * n) * np.hamming(FIR_TAPS)
	return (taps / taps.sum()).astype(np.float32)


class Decimator:
	"""Integer-factor downsampler that keeps filter and phase state between chunks.

	A stateless per-chunk resample restarts the filter on every chunk and leaves a
	discontinuity at each boundary, which happens 2.5 times a second.
	"""

	def __init__(self, factor: int):
		self.__factor = factor
		self.__taps = _design_lowpass(factor)
		self.__tail = np.zeros(FIR_TAPS - 1, dtype=np.float32)
		self.__phase = 0

	def __call__(self, pcm: np.ndarray) -> np.ndarray:
		buf = np.concatenate((self.__tail, pcm))
		filtered = np.convolve(buf, self.__taps, mode="valid")
		out = filtered[self.__phase::self.__factor]
		self.__phase = (self.__phase - len(filtered)) % self.__factor
		self.__tail = buf[len(buf) - (FIR_TAPS - 1):]
		return np.ascontiguousarray(out, dtype=np.float32)


DEFAULT_SERVER_URL = "ws://localhost:2702"
DEFAULT_LANGUAGE = "en"


def _env_float(name: str, default: float) -> float:
	raw = os.environ.get(name)
	if not raw:
		return default
	try:
		return float(raw)
	except ValueError:
		LOGGER.warning("%s=%r is not a number, using %s", name, raw, default)
		return default


def _env_int(name: str, default: int) -> int:
	raw = os.environ.get(name)
	if not raw:
		return default
	try:
		return int(raw)
	except ValueError:
		LOGGER.warning("%s=%r is not an integer, using %s", name, raw, default)
		return default


def model_suffix() -> str:
	"""int8 by default. The fp32 export trades disk space for accuracy."""
	return ".int8.onnx" if os.environ.get("LT_ASR_INT8", "true").lower() in ("1", "true") else ".onnx"


async def wait_for_model(model_dir: str):
	"""Block until the model files exist and have stopped growing.

	supervisord starts this server at the same time as the main app, but the app
	only downloads the model in its `/init` handler, which Nextcloud calls after
	the container reports healthy. Exiting here would burn the whole download
	window in restarts, thus it waits for the files instead.
	"""  # noqa
	suffix = model_suffix()
	needed = [
		os.path.join(model_dir, "tokens.txt"),
		os.path.join(model_dir, f"encoder{suffix}"),
		os.path.join(model_dir, f"decoder{suffix}"),
		os.path.join(model_dir, f"joiner{suffix}"),
	]
	announced = False
	sizes: dict[str, int] = {}
	while True:
		missing = [p for p in needed if not os.path.exists(p)]
		if not missing:
			# huggingface_hub writes in place, thus require two consecutive
			# identical sizes before treating a file as complete.
			current = {p: os.path.getsize(p) for p in needed}
			if current == sizes and all(current.values()):
				return
			sizes = current
		elif not announced:
			LOGGER.info(
				"Waiting for the ASR model to be downloaded to %s",
				model_dir, extra={"tag": "asr"},
			)
			announced = True
		await asyncio.sleep(2)


def build_recognizer(model_dir: str) -> sherpa_onnx.OnlineRecognizer:
	"""Build the shared recognizer.

	Raises
	------
		FileNotFoundError: when a model file is missing
	"""  # noqa
	suffix = model_suffix()
	paths = {
		"tokens": os.path.join(model_dir, "tokens.txt"),
		"encoder": os.path.join(model_dir, f"encoder{suffix}"),
		"decoder": os.path.join(model_dir, f"decoder{suffix}"),
		"joiner": os.path.join(model_dir, f"joiner{suffix}"),
	}
	for name, path in paths.items():
		if not os.path.exists(path):
			raise FileNotFoundError(f"ASR model file missing ({name}): {path}")

	provider = "cuda" if os.environ.get("COMPUTE_DEVICE", "cpu").lower() == "cuda" else "cpu"
	if provider == "cuda":
		# The PyPI wheel is CPU-only and falls back to the CPU silently. Thus log
		# which provider was requested and let the sherpa-onnx fallback line show
		# in the output.
		LOGGER.info("COMPUTE_DEVICE=cuda: requesting the CUDA provider")

	return sherpa_onnx.OnlineRecognizer.from_transducer(
		# One thread per decode, and concurrency comes from the pool instead.
		# A 320 ms chunk is too small to split across cores: the parallel region
		# is short, and onnxruntime keeps its intra-op threads spinning between
		# operators, which bills idle spin as busy CPU. Raising this costs CPU
		# without lowering latency. Measured with 4 speakers on a 4-core host,
		# 1 thread against 4: 12.8 against 28.1 CPU seconds, and p95 210 ms
		# against 384 ms. The default scales with the core count, so a 16-thread
		# host was worse again at 81.0 CPU seconds and p95 1315 ms, where 103 of
		# 112 chunks missed the 400 ms budget and the stream never caught up.
		num_threads=_env_int("LT_ASR_THREADS", 1),
		provider=provider,
		decoding_method="greedy_search",
		enable_endpoint_detection=True,
		# The sherpa-onnx defaults (2.4 s and 1.2 s) fire mid-sentence on natural
		# speech pauses, and they split an utterance where a word straddles the
		# boundary. Measured on an 11 s clip, the defaults dropped "ask" twice and
		# 3.0/2.0 loses nothing. Note that reset() keeps undecoded audio, because
		# it only advances counters, thus the loss came from where the split
		# landed.
		rule1_min_trailing_silence=_env_float("LT_ASR_RULE1_SILENCE", 3.0),
		rule2_min_trailing_silence=_env_float("LT_ASR_RULE2_SILENCE", 2.0),
		**paths,  # type: ignore[arg-type]
	)


def build_vad(vad_model: str) -> sherpa_onnx.VoiceActivityDetector | None:
	if os.environ.get("LT_ASR_DISABLE_VAD", "false").lower() in ("1", "true"):
		LOGGER.warning(
			"VAD disabled: every silent participant will cost nearly as much CPU as a speaking one"
		)
		return None
	if not os.path.exists(vad_model):
		LOGGER.warning("VAD model not found at %s, continuing without it", vad_model)
		return None

	config = sherpa_onnx.VadModelConfig()
	config.silero_vad.model = vad_model
	# silero's own defaults are 0.5 s of silence and 0.25 s of speech. The silence
	# is shortened here because this gate only decides whether to spend CPU on a
	# chunk, and the endpoint rules decide where an utterance ends. Holding the
	# gate open for half a second after every word would spend CPU on silence.
	config.silero_vad.min_silence_duration = _env_float("LT_ASR_VAD_MIN_SILENCE", 0.25)
	config.silero_vad.threshold = _env_float("LT_ASR_VAD_THRESHOLD", 0.5)
	# Left at silero's defaults: min_speech_duration 0.25 s and max_speech_duration
	# 20 s. Both only bound the segments the VAD queues up, and this server never
	# reads that queue, because the recognizer is fed the full-rate stream instead.
	# window_size must stay 512, which is what silero v5 was exported with.
	config.sample_rate = ENGINE_RATE
	config.provider = "cpu"
	config.num_threads = 1
	# Keep the buffer larger than silero's own max_speech_duration (20 s). If you
	# do not, a long utterance makes sherpa-onnx log an overflow and reallocate
	# before its adaptive threshold can kick in. The sherpa-onnx ASR examples use
	# 30 to 100.
	return sherpa_onnx.VoiceActivityDetector(config, buffer_size_in_seconds=30)


class Session:
	"""Per-connection decoding state: one speaker, one language."""

	def __init__(
		self,
		recognizer: sherpa_onnx.OnlineRecognizer,
		pool: ThreadPoolExecutor,
		vad_model: str,
	):
		self.__recognizer = recognizer
		self.__pool = pool
		self.__vad_model = vad_model
		self.__stream: sherpa_onnx.OnlineStream | None = None
		# The VAD holds speech and silence state, thus it cannot be shared between
		# speakers. The model is 628 kB and onnxruntime mmaps it.
		self.__vad: sherpa_onnx.VoiceActivityDetector | None = None
		self.__vad_tail = np.empty(0, dtype=np.float32)
		self.__vad_decimator: Decimator | None = None
		# True once an endpoint has closed an utterance and nothing new has been
		# decoded since. Only in that state is it safe to drop silent audio.
		self.__between_utterances = True
		# Audio held back while gated. silero is causal and needs roughly one and
		# a half windows of speech before it triggers, thus the chunk that first
		# tests positive already starts mid-word. silero's own VADIterator
		# compensates by back-dating the segment start by speech_pad_ms, and the
		# equivalent here is to replay the gated chunks ahead of the first decoded
		# one.
		#
		# Two chunks, not one. Measured by logging the gate against chunk energy,
		# the trigger lands one chunk after audible speech begins, and where the
		# onset straddles a chunk boundary it lands two chunks late: a silence of
		# 3.5 s before the utterance had energy in the two chunks preceding the
		# trigger, and one chunk of replay left "And so my fellow Americans" as
		# "Oh my fellow Americans". The cost of being generous is one extra chunk
		# of silence decoded per utterance.
		self.__pre_speech: deque[np.ndarray] = deque(maxlen=PRE_SPEECH_CHUNKS)
		self.__language = DEFAULT_LANGUAGE
		self.__sample_rate = 48000

	@property
	def language(self) -> str:
		return self.__language

	async def __run(self, fn, *args):
		return await asyncio.get_running_loop().run_in_executor(self.__pool, fn, *args)

	def __create_stream(self) -> sherpa_onnx.OnlineStream:
		stream = self.__recognizer.create_stream()
		# The language prompt conditions the model on every chunk. set_option()
		# accepts any string silently and degrades an unknown value to
		# auto-detect. Thus set_language() validates the value before it reaches
		# here.
		stream.set_option("language", LANGUAGE_PROMPTS[self.__language])
		return stream

	def __reset_stream(self):
		self.__stream = self.__create_stream()
		self.__vad_tail = np.empty(0, dtype=np.float32)
		self.__between_utterances = True
		self.__pre_speech.clear()

		# The VAD reads every 512-sample window as 32 ms at ENGINE_RATE, thus audio
		# must arrive at exactly that rate. An integer ratio covers what WebRTC
		# delivers in practice, which is 48 kHz, or 16 kHz already native.
		factor, remainder = divmod(self.__sample_rate, ENGINE_RATE)
		if remainder or factor < 1:
			LOGGER.warning(
				"Sample rate %d is not a whole multiple of %d, running without a VAD, "
				"so silent participants will cost CPU time",
				self.__sample_rate, ENGINE_RATE, extra={"tag": "asr"},
			)
			self.__vad = None
			self.__vad_decimator = None
			return

		self.__vad_decimator = Decimator(factor) if factor > 1 else None
		if self.__vad is None:
			self.__vad = build_vad(self.__vad_model)
		else:
			self.__vad.reset()

	async def set_language(self, language: str, sample_rate: float | None) -> bool:
		if language not in LANGUAGE_PROMPTS:
			LOGGER.error("Unsupported language: %s", language, extra={"tag": "asr"})
			return False

		if sample_rate:
			rate = int(sample_rate)
			if self.__stream is not None and rate != self.__sample_rate:
				# sherpa-onnx builds its resampler from the first chunk's rate and
				# exits the process if a later chunk disagrees. A new stream gets
				# a new resampler, so rebuild rather than risk the hard exit.
				LOGGER.info("Sample rate changed %d -> %d, rebuilding the stream",
							self.__sample_rate, rate, extra={"tag": "asr"})
				self.__stream = None
			self.__sample_rate = rate

		self.__language = language
		# Rebuild so the new prompt applies from the next chunk onwards. Any
		# audio already buffered belongs to the previous language.
		await self.__run(self.__reset_stream)
		LOGGER.info("Language set to %s (prompt %s) at %d Hz",
					language, LANGUAGE_PROMPTS[language], self.__sample_rate,
					extra={"tag": "asr"})
		return True

	def __has_speech(self, pcm: np.ndarray) -> bool:
		"""Gate on the VAD, which only accepts exact 512-sample windows."""
		if self.__vad is None:
			return True

		narrow = self.__vad_decimator(pcm) if self.__vad_decimator else pcm
		buf = np.concatenate((self.__vad_tail, narrow)) if self.__vad_tail.size else narrow
		usable = (buf.size // VAD_WINDOW) * VAD_WINDOW
		for start in range(0, usable, VAD_WINDOW):
			self.__vad.accept_waveform(buf[start:start + VAD_WINDOW])
		self.__vad_tail = buf[usable:].copy()

		# The VAD also queues completed speech segments, and each one holds a copy
		# of its audio. Nothing here consumes them, because the recognizer is fed
		# the full-rate stream directly, and the queue is unbounded. Thus a queue
		# that is never drained leaks for the length of the call.
		while not self.__vad.empty():
			self.__vad.pop()

		return self.__vad.is_speech_detected()

	def __decode(self, pcm: np.ndarray) -> tuple[str, bool]:
		if self.__stream is None:
			LOGGER.warning("Empty recognizer stream encountered while trying to decode incoming frames")
			return "", False

		# The endpoint timer runs on DECODED frames, because only decode_stream
		# advances `num_trailing_blanks` and `num_processed_frames`. Thus an open
		# utterance must keep decoding through a pause, or it never closes and
		# its final is never sent. It is safe to drop silence only after an
		# endpoint, and then the audio is dropped outright. Feeding audio that is
		# never decoded builds an unbounded backlog, which is decoded in a burst
		# at the next word, so the cost is deferred and not avoided.
		if self.__between_utterances and not self.__has_speech(pcm):
			self.__pre_speech.append(pcm)
			return "", False

		while self.__pre_speech:
			self.__stream.accept_waveform(self.__sample_rate, self.__pre_speech.popleft())
		self.__stream.accept_waveform(self.__sample_rate, pcm)
		while self.__recognizer.is_ready(self.__stream):
			self.__recognizer.decode_stream(self.__stream)

		# Read the result BEFORE reset(), which discards the segment's text.
		text = self.__recognizer.get_result(self.__stream)
		if self.__recognizer.is_endpoint(self.__stream):
			# reset() and not a new stream. It restores the encoder and decoder to
			# their initial states but deliberately keeps the feature extractor,
			# which is what the cache-aware model needs to carry acoustic context
			# across the boundary. Measured over 90 s of conversational speech
			# against a single uninterrupted stream, reset() diverged by 4.90 %
			# and a fresh stream per utterance by 6.53 %, so rebuilding the stream
			# costs accuracy exactly where the model is strongest.
			self.__recognizer.reset(self.__stream)
			self.__between_utterances = True
			return text, True
		self.__between_utterances = False
		return text, False

	async def feed(self, frame: bytes) -> str:
		if self.__stream is None:
			await self.__run(self.__reset_stream)

		pcm = np.frombuffer(frame, dtype=np.int16).astype(np.float32) / 32768.0
		text, is_final = await self.__run(self.__decode, np.ascontiguousarray(pcm))

		if is_final:
			if text.strip():
				return json.dumps({"text": text.strip()})
			# An endpoint with no text is plain silence. An empty final would close
			# an utterance in the UI that never had any content.
			return json.dumps({"partial": ""})
		return json.dumps({"partial": text.strip()})

	def __finalise(self) -> str:
		if self.__stream is None:
			return ""
		# Pad with silence so the encoder can flush the frames still inside its
		# lookahead window, or the last word is truncated.
		self.__stream.accept_waveform(
			self.__sample_rate,
			np.zeros(self.__sample_rate // 2, dtype=np.float32),
		)
		self.__stream.input_finished()
		while self.__recognizer.is_ready(self.__stream):
			self.__recognizer.decode_stream(self.__stream)
		return self.__recognizer.get_result(self.__stream)

	async def flush(self) -> str:
		text = await self.__run(self.__finalise)
		self.__stream = None
		self.__vad_tail = np.empty(0, dtype=np.float32)
		return json.dumps({"text": text.strip()})

	def close(self):
		self.__stream = None
		self.__vad = None
		self.__vad_tail = np.empty(0, dtype=np.float32)


async def recognize(
	websocket: ws.ServerConnection,
	recognizer: sherpa_onnx.OnlineRecognizer,
	pool: ThreadPoolExecutor,
	vad_model: str,
):
	session = Session(recognizer, pool, vad_model)
	LOGGER.debug("Connection from %s", websocket.remote_address, extra={"tag": "asr"})

	try:
		while True:
			message = await websocket.recv()

			if isinstance(message, str):
				try:
					request = json.loads(message)
				except json.JSONDecodeError:
					LOGGER.error("Invalid JSON from the client: %s", message, extra={"tag": "asr"})
					continue

				if "config" in request:
					config = request.get("config") or {}
					success = True
					if "language" in config:
						success = await session.set_language(
							config["language"], config.get("sample_rate")
						)
					elif "sample_rate" in config:
						success = await session.set_language(session.language, config["sample_rate"])
					await websocket.send(json.dumps({"success": success}))
					continue

				if request.get("eof") == 1:
					await websocket.send(await session.flush())
					break

				if request.get("reset") == 1:
					await websocket.send(await session.flush())
					continue

				LOGGER.warning("Unhandled control message: %s", message, extra={"tag": "asr"})
				continue

			await websocket.send(await session.feed(message))
	except ws.exceptions.ConnectionClosedOK:
		LOGGER.debug("Client closed the connection: %s", websocket.remote_address, extra={"tag": "asr"})
	except ws.exceptions.WebSocketException as e:
		LOGGER.error("Websocket error: %s", e, extra={"tag": "asr"})
	except Exception as e:
		LOGGER.exception("ASR session failed", exc_info=e, extra={"tag": "asr"})
	finally:
		session.close()


async def start():
	logging.basicConfig(
		level=os.environ.get("LT_LOG_LEVEL", "INFO"),
		format="%(asctime)s %(levelname)s %(name)s: %(message)s",
	)

	if os.environ.get("LT_DISABLE_INTERNAL_ASR", "false").lower() in ("true", "1"):
		LOGGER.info("LT_DISABLE_INTERNAL_ASR is set, not starting the internal ASR server")
		return

	server_url = os.environ.get("LT_ASR_SERVER_URL", DEFAULT_SERVER_URL)
	parsed = urlparse(server_url)
	interface = parsed.hostname
	if not interface:
		raise ValueError("LT_ASR_SERVER_URL must contain a valid hostname or IP address")
	# don't catch the exception here, it should fail if the port is not an integer
	port = parsed.port or 2702

	model_dir = os.environ.get("LT_ASR_MODEL_DIR") or os.path.join(persistent_storage(), MODEL_DIR_NAME)
	vad_model = os.environ.get("LT_ASR_VAD_MODEL") or os.path.join(persistent_storage(), VAD_MODEL_NAME)

	# The app's /init handler fetches both, and it runs after this process starts.
	# Thus waiting here keeps the VAD from being skipped on a cold deploy.
	await wait_for_model(model_dir)
	if os.environ.get("LT_ASR_DISABLE_VAD", "false").lower() not in ("1", "true"):
		if not os.path.exists(vad_model):
			LOGGER.info("Waiting for the VAD model at %s", vad_model, extra={"tag": "asr"})
		while not os.path.exists(vad_model):
			await asyncio.sleep(2)
	recognizer = build_recognizer(model_dir)
	LOGGER.info("Loaded the ASR model from %s", model_dir, extra={"tag": "asr"})

	# This bounds how many decodes overlap and not how many speakers may connect.
	# A single decode uses one thread, so the workers are what fills the cores.
	# Above the core count the decodes only take turns, which adds latency without
	# adding throughput. The value is still honoured, because the admin may know
	# something this process cannot see, such as the container being one of
	# several sharing the host.
	cores = os.cpu_count() or 4
	workers = max(1, _env_int("LT_MAX_WORKERS", cores))
	if workers > cores:
		LOGGER.warning(
			"LT_MAX_WORKERS=%d is above the %d cores available. One decode uses one "
			"thread, so the extra workers only take turns and every speaker gets "
			"slower rather than the server serving more of them.",
			workers, cores, extra={"tag": "asr"},
		)
	pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="asr")

	async def handler(websocket: ws.ServerConnection):
		await recognize(websocket, recognizer, pool, vad_model)

	async with ws.serve(handler, interface, port, max_size=None):
		LOGGER.info(
			"ASR server listening on ws://%s:%d (%d workers, %d threads per decode)",
			interface, port, workers, _env_int("LT_ASR_THREADS", 1),
			extra={"tag": "asr"},
		)
		await asyncio.Future()


if __name__ == "__main__":
	with suppress(KeyboardInterrupt):
		asyncio.run(start())
