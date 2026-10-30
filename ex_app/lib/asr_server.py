#
# SPDX-FileCopyrightText: 2025 Nextcloud GmbH and Nextcloud contributors
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Streaming ASR websocket server that uses sherpa-onnx.

It uses the same websocket protocol as the previous Vosk server, so
``transcriber.py`` did not change:

    C->S  {"config": {"language": "en", "sample_rate": 48000}}
    S->C  {"success": true}                       ("detect_language", for an unknown language too)
    C->S  <binary s16le mono PCM>
    S->C  exactly one of {"partial": "..."} | {"text": "..."}
    C->S  {"eof": 1}
    S->C  {"text": "..."}                         then the connection closes

The server must send exactly one reply for each binary frame, because the
client waits for that reply before it sends the next frame.

All connections share one ``OnlineRecognizer``, because each recognizer loads
its own copy of the weights (approximately 730 MiB), while the ``OnlineStream``
of each speaker costs approximately 1 MiB. Decoding runs on ``Lanes``, a set of
decode threads which releases the GIL and does not block the event loop.

A silero VAD pre-filters each stream, because the encoder uses 85 % to 95 % of
the CPU time of speech also for silence. Without the VAD, a four-person call is
slower than realtime on a 4-core host, for instance.
"""

import asyncio
import json
import logging
import math
import os
import queue
import threading
from collections import deque
from collections.abc import Callable
from contextlib import suppress
from pathlib import Path
from urllib.parse import urlparse

import numpy as np
import sherpa_onnx
import websockets as ws
from dotenv import load_dotenv
from models import (
	DETECT_LANGUAGE,
	LANGUAGE_PROMPTS,
	MODEL_DIR_NAME,
	MODEL_FILES_PATTERNS,
	VAD_MODEL_NAME,
	supported_language,
)
from nc_py_api.ex_app import persistent_storage
from utils import asr_server_url, internal_asr_disabled

load_dotenv()

LOGGER = logging.getLogger("lt.asr_server")

# Sample rate of the model's feature extractor. sherpa-onnx resamples other
# rates itself, with one stateful resampler for each stream. The rate must not
# change in a stream, because sherpa-onnx then calls exit(-1).
ENGINE_RATE = 16000

# silero v5 frame size at 16 kHz
VAD_WINDOW = 512

# Number of audio chunks to prepend for sherpa from the silent zone when VAD
# detects audio. See Session.__init__.
PRE_SPEECH_CHUNKS = 4

# todo: test how often partials and finals arrive, are partials even generated?
# Send a final for the settled part of an utterance after this much audio
# without a final. Translation uses only finals, and the endpoint rules wait for
# a pause, so a speaker who does not pause is otherwise not translated until
# MAX_UTTERANCE_SECONDS, if ever.
#
# This does not reset the stream, so no word is cut. A lower
# MAX_UTTERANCE_SECONDS resets the decoder at any point: at 10 s it cut
# "country" to "coun" in an 11 s clip.
EARLY_FINAL_SECONDS = 10.0

# The hard limit on an utterance, or -1 for none. Reaching it resets the decoder
# and the encoder caches wherever it lands, so the words around the cut lose their
# context: on 90 s of speech the sherpa-onnx default of 20 s cut "humanity" to
# "hu". Early finals already deliver text during long speech, thus this only
# guards a monologue. Not resetting is cheap: one stream decoded for 10 minutes
# without a reset held memory at 1030 MiB and decode time at 86 ms per chunk,
# and only re-reading the text grew, from 0.1 ms to 0.4 ms per chunk.
MAX_UTTERANCE_SECONDS = 600.0

# Tokens that end a sentence: includes the CJK (Chinese, Japanese, Korean) full stop,
# fullwidth ? and !, the Devanagari danda and the Arabic question mark.
SENTENCE_ENDS = (".", "?", "!", "\u3002", "\uff1f", "\uff01", "\u0964", "\u061f")


def settled_end(tokens: list[str], start: int) -> int:
	"""Index after the last token in tokens[start:] that later decoding cannot change.

	Greedy search only appends tokens, so a word is complete when a later token
	starts a new word with a space. The last word is never complete. A sentence
	end is preferred, so that an early final is a full sentence. Scripts without
	spaces between words have only the sentence ends. Returns start if no token
	is settled.
	"""
	for i in range(len(tokens) - 2, start - 1, -1):
		# A following digit means a decimal point, as in "3.5".
		if tokens[i].rstrip().endswith(SENTENCE_ENDS) and not tokens[i + 1].lstrip()[:1].isdigit():
			return i + 1
	for i in range(len(tokens) - 1, start, -1):
		if tokens[i].startswith(" "):
			return i
	return start


def available_cores() -> int:
	"""Number of cores that this process can use.

	Read the cgroup CPU quota first, because the Python APIs do not see it: in a
	container started with ``--cpus 2`` on a 4-core host, cpu_count(),
	process_cpu_count() and sched_getaffinity() all give 4. Deployments limit
	ExApp containers in this way.
	"""
	try:
		quota, period = Path("/sys/fs/cgroup/cpu.max").read_text().split()
		if quota != "max" and int(period) > 0:
			# A fractional quota still gets one full worker.
			return max(1, math.ceil(int(quota) / int(period)))
	except (OSError, ValueError):
		pass
	# process_cpu_count() is new in Python 3.13 and uses the CPU affinity.
	count = getattr(os, "process_cpu_count", os.cpu_count)
	return count() or 1


def default_max_workers() -> int:
	"""Number of decode lanes if LT_MAX_WORKERS is not set: one for each core.

	More lanes than cores do not help: the queue then seldom holds more jobs
	than lanes, so no batches form and the decodes only take turns on the cores.
	With 16 speakers on a 4-core host for 20 s, 8 lanes fell 6.7 s behind
	realtime, and 4 lanes stayed within 0.7 s.
	"""
	return available_cores()


# Low-pass FIR filter to downsample the audio to ENGINE_RATE for the VAD. The
# recognizer resamples internally, but the VAD does not, and it reads each
# 512-sample window as 32 ms. With 48 kHz audio, it found speech in 5 % of the
# windows of an 11 s clip instead of 68 %.
#
# The recognizer does not get this downsampled audio, because this short filter
# is less accurate than the Kaldi resampler in sherpa-onnx. On 90 s of speech it
# saved only 0.15 % of realtime in CPU time and increased the WER by 0.40
# points. It is sufficient for a speech or silence decision, but not for the
# model's features.
FIR_TAPS = 97


def _design_lowpass(decimation: int) -> np.ndarray:
	"""Windowed-sinc low pass at the decimated Nyquist frequency."""
	n = np.arange(FIR_TAPS) - (FIR_TAPS - 1) / 2
	cutoff = 0.5 / decimation
	taps = 2 * cutoff * np.sinc(2 * cutoff * n) * np.hamming(FIR_TAPS)
	return (taps / taps.sum()).astype(np.float32)


class Decimator:
	"""Integer-factor downsampler that keeps the filter and phase state between chunks.

	A new filter for each chunk causes a discontinuity at each chunk boundary,
	which occurs 2.5 times a second.
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


def model_paths(model_dir: str) -> dict[str, str] | None:
	"""Paths of the first export in MODEL_FILES_PATTERNS whose files all exist."""
	for files in MODEL_FILES_PATTERNS.values():
		paths = {role: os.path.join(model_dir, name) for role, name in files.items()}
		if all(os.path.exists(p) for p in paths.values()):
			return paths
	return None


async def wait_for_model(model_dir: str):
	"""Wait until the model files exist and their sizes stop changing.

	supervisord starts this server together with the main app, but the app
	downloads the model only in `/init`, which Nextcloud calls after the
	container is healthy. If this server exits, it only restarts again and again
	during the download, so wait for the files.
	"""  # noqa
	announced = False
	sizes: dict[str, int] = {}
	while True:
		paths = model_paths(model_dir)
		if paths:
			# huggingface_hub writes the files in place, so a file is complete only
			# when two consecutive checks give the same size.
			current = {p: os.path.getsize(p) for p in paths.values()}
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


# Interval between log lines while waiting for the VAD model. The healthcheck
# only checks that this process is alive, so a stalled download shows only in
# the log.
VAD_WAIT_LOG_INTERVAL = 60.0


async def wait_for_vad(vad_model: str, explicit: bool):
	"""Wait until the VAD model exists.

	/init downloads only the default path. A path set with LT_ASR_VAD_MODEL that
	does not exist is a configuration error, and waiting does not correct it.

	Raises
	------
		FileNotFoundError: when an explicit path does not exist
	"""  # noqa
	if os.path.exists(vad_model):
		return
	if explicit:
		raise FileNotFoundError(f"LT_ASR_VAD_MODEL points at {vad_model}, which does not exist")
	loop = asyncio.get_running_loop()
	next_log = loop.time()
	while not os.path.exists(vad_model):
		if loop.time() >= next_log:
			LOGGER.info("Waiting for the VAD model to be downloaded to %s", vad_model, extra={"tag": "asr"})
			next_log = loop.time() + VAD_WAIT_LOG_INTERVAL
		await asyncio.sleep(2)


def build_recognizer(model_dir: str) -> sherpa_onnx.OnlineRecognizer:
	"""Build the shared recognizer.

	Raises
	------
		FileNotFoundError: when a model file is missing
	"""  # noqa
	paths = model_paths(model_dir)
	if paths is None:
		raise FileNotFoundError(f"No complete ASR model export in {model_dir}")
	# onnxruntime finds encoder.data next to encoder.onnx.
	paths.pop("encoder_data", None)

	provider = "cuda" if os.environ.get("COMPUTE_DEVICE", "cpu").lower() == "cuda" else "cpu"
	if provider == "cuda":
		# The PyPI wheel is CPU-only and silently uses the CPU. Log the request,
		# so that a sherpa-onnx fallback message in the log has context.
		LOGGER.info("COMPUTE_DEVICE=cuda: requesting the CUDA provider")

	return sherpa_onnx.OnlineRecognizer.from_transducer(
		# One thread for each decode; the lanes give the concurrency. A 320 ms
		# chunk is too small to divide across cores, and the onnxruntime
		# threads spin between operators, which uses CPU for no work. With 4
		# speakers on a 4-core host, 1 thread against 4 used 12.8 against 28.1
		# CPU seconds, with p95 latency 210 ms against 384 ms.
		num_threads=_env_int("LT_ASR_THREADS", 1),
		provider=provider,
		decoding_method="greedy_search",
		enable_endpoint_detection=True,
		# The sherpa-onnx defaults (2.4 s and 1.2 s) end an utterance at a
		# natural pause in a sentence, and a split in a word loses that word.
		# On an 11 s clip the defaults lost "ask" two times, and 3.0 and 2.0
		# lost nothing.
		rule1_min_trailing_silence=_env_float("LT_ASR_RULE1_SILENCE", 3.0),
		rule2_min_trailing_silence=_env_float("LT_ASR_RULE2_SILENCE", 2.0),
		# sherpa-onnx fires rule 3 once the utterance length reaches this, and has
		# no switch to turn the rule off, thus infinity is how -1 disables it.
		rule3_min_utterance_length=MAX_UTTERANCE_SECONDS if MAX_UTTERANCE_SECONDS != -1 else math.inf,
		**paths,  # type: ignore[arg-type]
	)


def build_vad(vad_model: str) -> sherpa_onnx.VoiceActivityDetector | None:
	"""Build one speaker's VAD. start() has already waited for the model file."""
	if os.environ.get("LT_ASR_DISABLE_VAD", "false").lower() in ("1", "true"):
		LOGGER.warning(
			"VAD disabled: every silent participant will cost nearly as much CPU as a speaking one"
		)
		return None

	config = sherpa_onnx.VadModelConfig()
	config.silero_vad.model = vad_model
	# silero's default minimum silence is 0.5 s. This gate only decides if a
	# chunk gets decoded, and the endpoint rules decide where an utterance ends,
	# so a shorter silence saves CPU time.
	config.silero_vad.min_silence_duration = _env_float("LT_ASR_VAD_MIN_SILENCE", 0.25)
	config.silero_vad.threshold = _env_float("LT_ASR_VAD_THRESHOLD", 0.5)
	# min_speech_duration and max_speech_duration stay at silero's defaults
	# (0.25 s and 20 s), because they only limit the queued speech segments,
	# which this server does not read. window_size must stay 512 for silero v5.
	config.sample_rate = ENGINE_RATE
	config.provider = "cpu"
	config.num_threads = 1
	# Keep the buffer longer than max_speech_duration (20 s). Otherwise a long
	# utterance causes an overflow log and a reallocation.
	return sherpa_onnx.VoiceActivityDetector(config, buffer_size_in_seconds=30)


class Lanes:
	"""Decode threads that batch the streams that wait for them.

	Each lane takes the waiting jobs from the shared queue and decodes their
	streams in one decode_streams() call. A batch shares one encoder run, which
	costs less than separate decodes. With 16 speakers on a 4-core host for
	20 s, a pool of 8 threads used 100.8 CPU seconds and fell 7.4 s behind, and
	4 lanes used 76.2 and stayed within 0.7 s. Batches form only when more
	streams wait than there are lanes.

	A stream decoded in a batch can give a different result than alone, because
	the int8 encoder uses one quantisation scale for each batch. The WER changed
	from 7.69 % to 8.60 % with 8 speakers, and did not change with 4.
	"""

	def __init__(self, recognizer: sherpa_onnx.OnlineRecognizer, count: int):
		self.__recognizer = recognizer
		self.__count = count
		self.__jobs: queue.SimpleQueue = queue.SimpleQueue()
		for i in range(count):
			threading.Thread(target=self.__work, name=f"asr-{i}", daemon=True).start()

	async def submit(
		self,
		ingest: Callable[[], sherpa_onnx.OnlineStream | None] | None,
		collect: Callable[[bool], object],
	):
		"""Run ingest on a lane, decode the stream that it returns, then return collect(decoded).

		ingest gives audio to a stream and returns that stream, or None if no
		decode is necessary. If ingest is None, only collect runs on a lane: use
		this for state changes that must not occur during a decode of the stream.
		"""
		loop = asyncio.get_running_loop()
		future = loop.create_future()
		self.__jobs.put((ingest, collect, loop, future))
		return await future

	def __work(self):
		while True:
			jobs = [self.__jobs.get()]
			# Take only this lane's share of the waiting jobs, so that the other
			# lanes also get work. With 8 streams, one batch of 8 took 782 ms at
			# p95 for each 400 ms chunk, and 4 batches of 2 took 316 ms.
			share = math.ceil((1 + self.__jobs.qsize()) / self.__count)
			with suppress(queue.Empty):
				while len(jobs) < share:
					jobs.append(self.__jobs.get_nowait())

			streams: list[sherpa_onnx.OnlineStream | None] = []
			errors: list[BaseException | None] = []
			for ingest, _, _, _ in jobs:
				try:
					streams.append(ingest() if ingest else None)
					errors.append(None)
				except BaseException as e:  # noqa: BLE001
					streams.append(None)
					errors.append(e)

			batch_error: BaseException | None = None
			try:
				ready = [s for s in streams if s is not None and self.__recognizer.is_ready(s)]
				while ready:
					if len(ready) == 1:
						self.__recognizer.decode_stream(ready[0])
					else:
						self.__recognizer.decode_streams(ready)
					ready = [s for s in ready if self.__recognizer.is_ready(s)]
			except BaseException as e:  # noqa: BLE001
				batch_error = e

			for (_, collect, loop, future), stream, error in zip(jobs, streams, errors, strict=True):
				error = error or batch_error
				if error is None:
					try:
						result = collect(stream is not None)
					except BaseException as e:  # noqa: BLE001
						error = e
				if error is None:
					loop.call_soon_threadsafe(_resolve, future, result, None)
				else:
					loop.call_soon_threadsafe(_resolve, future, None, error)


def _resolve(future: asyncio.Future, result, error: BaseException | None):
	# A closed connection cancels its future.
	if future.done():
		return
	if error is None:
		future.set_result(result)
	else:
		future.set_exception(error)


class Session:
	"""Per-connection decoding state: one speaker, one language."""

	def __init__(
		self,
		recognizer: sherpa_onnx.OnlineRecognizer,
		lanes: "Lanes",
		vad_model: str,
	):
		self.__recognizer = recognizer
		self.__lanes = lanes
		self.__vad_model = vad_model
		self.__stream: sherpa_onnx.OnlineStream | None = None
		# The VAD keeps speech and silence state, so each speaker needs one. The
		# model is 628 kB and onnxruntime mmaps it.
		self.__vad: sherpa_onnx.VoiceActivityDetector | None = None
		self.__vad_tail = np.empty(0, dtype=np.float32)
		self.__vad_decimator: Decimator | None = None
		# True after an endpoint, until the next decode. Only then is it safe to
		# drop silent audio.
		self.__between_utterances = True
		# Audio kept while the gate is closed, to replay before the first
		# decoded chunk. silero triggers only after approximately 1.5 windows of
		# speech, so the chunk that opens the gate starts in a word. The trigger
		# can come two chunks after the speech starts: with one chunk of replay,
		# "And so my fellow Americans" became "Oh my fellow Americans".
		self.__pre_speech: deque[np.ndarray] = deque(maxlen=PRE_SPEECH_CHUNKS)
		# Tokens of the current utterance already sent in an early final, and
		# the seconds of audio since the last final.
		self.__sent_tokens = 0
		self.__since_final = 0.0
		self.__language = DETECT_LANGUAGE
		self.__sample_rate = 48000

	@property
	def language(self) -> str:
		return self.__language

	async def __run_on_lane(self, fn, *args):
		return await self.__lanes.submit(None, lambda _decoded: fn(*args))

	def __create_stream(self) -> sherpa_onnx.OnlineStream:
		stream = self.__recognizer.create_stream()
		# set_language() already changed an id without a prompt to DETECT_LANGUAGE.
		stream.set_option("language", LANGUAGE_PROMPTS[self.__language])
		return stream

	def __reset_stream(self):
		self.__stream = self.__create_stream()
		self.__vad_tail = np.empty(0, dtype=np.float32)
		self.__between_utterances = True
		self.__pre_speech.clear()
		self.__sent_tokens = 0
		self.__since_final = 0.0

		# The VAD needs audio at exactly ENGINE_RATE. An integer ratio covers
		# what WebRTC sends: 48 kHz, or 16 kHz.
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

	async def set_language(self, requested: str, sample_rate: float | None) -> None:
		language = supported_language(requested)
		if language != requested:
			LOGGER.info("Unsupported language %r, detecting the spoken language instead",
						requested, extra={"tag": "asr"})

		if sample_rate:
			rate = int(sample_rate)
			if self.__stream is not None and rate != self.__sample_rate:
				# sherpa-onnx exits the process if the rate changes in a stream,
				# so start a new stream.
				LOGGER.info("Sample rate changed %d -> %d, rebuilding the stream",
							self.__sample_rate, rate, extra={"tag": "asr"})
				self.__stream = None
			self.__sample_rate = rate

		self.__language = language
		# A new stream uses the new prompt from the next chunk, and discards the
		# buffered audio of the previous language.
		await self.__run_on_lane(self.__reset_stream)
		LOGGER.info("Language set to %s (prompt %s) at %d Hz",
					language, LANGUAGE_PROMPTS[language], self.__sample_rate,
					extra={"tag": "asr"})

	def __has_speech(self, pcm: np.ndarray) -> bool:
		"""Gate on the VAD, which only accepts exact 512-sample windows."""
		if self.__vad is None:
			return True

		vad_pcm = self.__vad_decimator(pcm) if self.__vad_decimator else pcm
		buf = np.concatenate((self.__vad_tail, vad_pcm)) if self.__vad_tail.size else vad_pcm
		usable = (buf.size // VAD_WINDOW) * VAD_WINDOW
		for start in range(0, usable, VAD_WINDOW):
			self.__vad.accept_waveform(buf[start:start + VAD_WINDOW])
		self.__vad_tail = buf[usable:].copy()

		# The VAD also keeps each completed speech segment, with a copy of its
		# audio, in an unbounded queue. This server does not use the segments,
		# so discard them, or they use memory for the full call.
		while not self.__vad.empty():
			self.__vad.pop()

		return self.__vad.is_speech_detected()

	def __ingest(self, pcm: np.ndarray) -> sherpa_onnx.OnlineStream | None:
		"""Feed a chunk, and return the stream if it now needs decoding."""
		if self.__stream is None:
			return None

		# The endpoint rules count only decoded frames, so an open utterance must
		# also be decoded during a pause, or it does not end and its final is not
		# sent. Drop silence only after an endpoint. Audio given to the stream
		# but not decoded only makes a backlog, which is then decoded at the
		# next word.
		if self.__between_utterances and not self.__has_speech(pcm):
			self.__pre_speech.append(pcm)
			return None

		while self.__pre_speech:
			replay = self.__pre_speech.popleft()
			self.__since_final += replay.size / self.__sample_rate
			self.__stream.accept_waveform(self.__sample_rate, replay)
		self.__stream.accept_waveform(self.__sample_rate, pcm)
		self.__since_final += pcm.size / self.__sample_rate
		return self.__stream

	def __collect(self, decoded: bool) -> tuple[str, bool]:
		"""Read the result of the chunk that __ingest fed."""
		if not decoded or self.__stream is None:
			return "", False

		# Read the result before reset(), because reset() clears it. Join the
		# tokens instead of reading .text, because an early final cuts at a token
		# index. Both give the same text, because no inverse text normalisation
		# or homophone replacer is configured.
		tokens = self.__recognizer.get_result_all(self.__stream).tokens
		if self.__recognizer.is_endpoint(self.__stream):
			# reset() and not a new stream, because reset() keeps the feature
			# extractor, which carries the acoustic context across the endpoint.
			# On 90 s of speech, compared with one continuous stream, reset()
			# diverged by 4.90 % and a new stream for each utterance by 6.53 %.
			self.__recognizer.reset(self.__stream)
			self.__between_utterances = True
			text = "".join(tokens[self.__sent_tokens:])
			self.__sent_tokens = 0
			self.__since_final = 0.0
			return text, True
		self.__between_utterances = False

		if self.__since_final >= EARLY_FINAL_SECONDS:
			end = settled_end(tokens, self.__sent_tokens)
			if end > self.__sent_tokens:
				text = "".join(tokens[self.__sent_tokens:end])
				self.__sent_tokens = end
				self.__since_final = 0.0
				return text, True
		return "".join(tokens[self.__sent_tokens:]), False

	async def feed(self, frame: bytes) -> str:
		if self.__stream is None:
			await self.__run_on_lane(self.__reset_stream)

		pcm = np.frombuffer(frame, dtype=np.int16).astype(np.float32) / 32768.0
		pcm = np.ascontiguousarray(pcm)
		text, is_final = await self.__lanes.submit(lambda: self.__ingest(pcm), self.__collect)

		if is_final:
			if text.strip():
				return json.dumps({"text": text.strip()})
			# An endpoint with no text is silence. An empty final closes an
			# utterance in the UI that has no text.
			return json.dumps({"partial": ""})
		return json.dumps({"partial": text.strip()})

	def __finalise(self) -> str:
		if self.__stream is None:
			return ""
		# Add silence so that the encoder processes the frames in its lookahead
		# window. Otherwise the last word is cut.
		self.__stream.accept_waveform(
			self.__sample_rate,
			np.zeros(self.__sample_rate // 2, dtype=np.float32),
		)
		self.__stream.input_finished()
		while self.__recognizer.is_ready(self.__stream):
			self.__recognizer.decode_stream(self.__stream)
		tokens = self.__recognizer.get_result_all(self.__stream).tokens
		return "".join(tokens[self.__sent_tokens:])

	async def flush(self) -> str:
		text = await self.__run_on_lane(self.__finalise)
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
	lanes: "Lanes",
	vad_model: str,
):
	session = Session(recognizer, lanes, vad_model)
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
					if "language" in config:
						await session.set_language(config["language"], config.get("sample_rate"))
					elif "sample_rate" in config:
						await session.set_language(session.language, config["sample_rate"])
					# An unsupported language changes to automatic detection, so a
					# config message always succeeds.
					await websocket.send(json.dumps({"success": True}))
					continue

				if request.get("eof") == 1:
					await websocket.send(await session.flush())
					break

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

	if internal_asr_disabled():
		LOGGER.info("LT_DISABLE_INTERNAL_ASR is set, not starting the internal ASR server")
		return

	server_url = asr_server_url()
	parsed = urlparse(server_url)
	interface = parsed.hostname
	if not interface:
		raise ValueError("LT_ASR_SERVER_URL must contain a valid hostname or IP address")
	# don't catch the exception here, it should fail if the port is not an integer
	port = parsed.port or 2702

	model_dir = os.environ.get("LT_ASR_MODEL_DIR") or os.path.join(persistent_storage(), MODEL_DIR_NAME)
	vad_override = os.environ.get("LT_ASR_VAD_MODEL")
	vad_model = vad_override or os.path.join(persistent_storage(), VAD_MODEL_NAME)

	# The app's /init handler downloads both after this process starts. Wait for
	# them, or the server starts without a VAD on a new installation.
	await wait_for_model(model_dir)
	if os.environ.get("LT_ASR_DISABLE_VAD", "false").lower() not in ("1", "true"):
		await wait_for_vad(vad_model, explicit=bool(vad_override))
	recognizer = build_recognizer(model_dir)
	LOGGER.info("Loaded the ASR model from %s", model_dir, extra={"tag": "asr"})

	# _env_int also uses the default when the variable is empty.
	workers = max(1, _env_int("LT_MAX_WORKERS", default_max_workers()))
	cores = available_cores()
	if workers > cores:
		LOGGER.warning(
			"LT_MAX_WORKERS=%d is above the %d cores available. Extra lanes take "
			"turns on the cores instead of batching speakers, which lowers how "
			"many people can speak at once before captions fall behind.",
			workers, cores, extra={"tag": "asr"},
		)
	lanes = Lanes(recognizer, workers)

	async def handler(websocket: ws.ServerConnection):
		await recognize(websocket, recognizer, lanes, vad_model)

	# PCM compresses only to 0.76 of its size, and deflate uses CPU on the event
	# loop. An external client can offer permessage-deflate, so refuse it here.
	async with ws.serve(handler, interface, port, max_size=None, compression=None):
		LOGGER.info(
			"ASR server listening on ws://%s:%d (%d workers, %d threads per decode)",
			interface, port, workers, _env_int("LT_ASR_THREADS", 1),
			extra={"tag": "asr"},
		)
		await asyncio.Future()


if __name__ == "__main__":
	with suppress(KeyboardInterrupt):
		asyncio.run(start())
