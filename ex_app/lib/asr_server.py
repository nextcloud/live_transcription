#
# SPDX-FileCopyrightText: 2025 Nextcloud GmbH and Nextcloud contributors
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Streaming ASR websocket server backed by sherpa-onnx.

Speaks the same websocket protocol the previous Vosk server did, so the wire
contract with ``transcriber.py`` is unchanged:

    C->S  {"config": {"language": "en", "sample_rate": 48000}}
    S->C  {"success": true}                       ("auto" or an unknown language detects it)
    C->S  <binary s16le mono PCM>
    S->C  exactly one of {"partial": "..."} | {"text": "..."}
    C->S  {"eof": 1}
    S->C  {"text": "..."}                         then the connection closes

The server must send exactly one reply for each binary frame. The client waits
for a reply after each frame it sends, so a server that stays silent stops the
call.

All the connections share one ``OnlineRecognizer``. Per-speaker state lives in the
``OnlineStream``, which costs approximately 1 MiB, but a second recognizer reloads
the weights and costs approximately 730 MiB. Decoding runs on ``Lanes``, a fixed
set of threads that batch the speakers waiting for them, because decoding
releases the GIL and would otherwise block the event loop for every other
speaker in the call.

A silero VAD gates each stream. Without it, a silent participant costs 85 % to
95 % of the CPU time of a speaking one, because the encoder runs regardless of
content. A four-person call is then slower than realtime on a 4-core host.
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
	AUTO_LANGUAGE,
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

# The sample rate the model's feature extractor expects. sherpa-onnx resamples
# audio that arrives at another rate. The first accept_waveform() call builds a
# stateful Kaldi LinearResample that is reused for the whole stream, so there are
# no per-chunk boundary artefacts and no resampling dependency here. The rate
# must never change mid-stream, because sherpa-onnx then calls exit(-1).
ENGINE_RATE = 16000

VAD_WINDOW = 512  # silero v5 frame size at 16 kHz; sherpa requires exactly this

# How many gated chunks are replayed when the gate opens. See Session.__init__.
PRE_SPEECH_CHUNKS = 4

# Send a final for the settled part of an utterance after this much audio
# without one. Translation runs only on finals, and the endpoint rules wait for
# a pause, thus a speaker who does not pause is otherwise not translated until
# MAX_UTTERANCE_SECONDS.
#
# This does not reset the stream. Greedy transducer search only appends tokens,
# thus every word that a later token has started a new word after is settled,
# and sending it as a final costs no accuracy. Shortening MAX_UTTERANCE_SECONDS
# instead resets the decoder wherever the limit lands, which at 10 s cut
# "country" to "coun" in an 11 s clip and lost the rest of the word.
EARLY_FINAL_SECONDS = 10.0

# The hard limit on an utterance. It resets the decoder wherever it lands, thus
# it only bounds the per-utterance decoder result in a monologue. On 90 s of
# speech the sherpa-onnx default of 20 s cut "humanity" to "hu", and 60 s lost
# nothing.
MAX_UTTERANCE_SECONDS = 60.0

# Tokens that end a sentence. An early final prefers to stop after one of these.
# The escapes are the CJK full stop, fullwidth ? and !, the Devanagari danda and
# the Arabic question mark.
SENTENCE_ENDS = (".", "?", "!", "\u3002", "\uff1f", "\uff01", "\u0964", "\u061f")


def settled_end(tokens: list[str], start: int) -> int:
	"""Index after the last token in tokens[start:] that later decoding cannot extend.

	A word is complete once a following token starts a new one with a space. The
	last word is never complete, because its next token may still continue it.
	A sentence end is preferred, so that an early final reads as a sentence.
	Scripts without spaces between words have only the sentence ends. Returns
	start when nothing is settled.
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
	"""Cores this process may actually use.

	None of the Python APIs see a CPU quota: in a container started with
	``--cpus 2`` on a 4-core host, cpu_count(), process_cpu_count() and
	sched_getaffinity() all still report 4, while the cgroup holds the process to
	two cores' worth of runtime. Deployments pin ExApp containers that way, so the
	quota is read first and the interpreter is only the fallback.
	"""
	try:
		quota, period = Path("/sys/fs/cgroup/cpu.max").read_text().split()
		if quota != "max" and int(period) > 0:
			# A fractional quota still gets one whole worker to run on.
			return max(1, math.ceil(int(quota) / int(period)))
	except (OSError, ValueError):
		pass
	# process_cpu_count() landed in 3.13 and follows CPU affinity; cpu_count() is
	# the portable fallback.
	count = getattr(os, "process_cpu_count", os.cpu_count)
	return count() or 1


def default_max_workers() -> int:
	"""Number of decode lanes when LT_MAX_WORKERS is unset: one per core.

	A lane batches whatever is waiting when it becomes free, thus a lane per
	core keeps every core busy, and speakers beyond the lanes share a batch
	rather than a turn. More lanes than cores defeats that: the queue rarely
	holds more jobs than lanes, so nothing batches and the decodes only take
	turns on the cores. With 16 speakers on a 4-core host, 8 lanes fell 6.7 s
	behind realtime in 20 s, and 4 lanes kept pace at 0.7 s.
	"""
	return available_cores()


# Low-pass FIR that downsamples to ENGINE_RATE for the VAD. The recognizer
# resamples internally, but the VAD does not. It reads every 512-sample window as
# 32 ms at ENGINE_RATE, thus feeding it 48 kHz audio makes each window cover a
# third of the intended span. Measured on an 11 s clip, that finds speech in 5 %
# of windows instead of 68 %, which silences almost everything.
#
# A chunk is therefore downsampled twice, once here for the VAD and again inside
# sherpa-onnx for the recognizer. Feeding this decimated copy to both would avoid
# that, but it is the worse trade:
# measured over 90 s of speech it saves 0.14 s of CPU, which is 0.15 % of
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
	"""Block until the model files exist and have stopped growing.

	supervisord starts this server at the same time as the main app, but the app
	only downloads the model in its `/init` handler, which Nextcloud calls after
	the container reports healthy. Exiting here would burn the whole download
	window in restarts, thus it waits for the files instead.
	"""  # noqa
	announced = False
	sizes: dict[str, int] = {}
	while True:
		paths = model_paths(model_dir)
		if paths:
			# huggingface_hub writes in place, thus require two consecutive
			# identical sizes before treating a file as complete.
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


# How often the wait for the VAD model repeats its log line. The healthcheck only
# checks that this process is alive, thus the log is where a stalled download
# shows.
VAD_WAIT_LOG_INTERVAL = 60.0


async def wait_for_vad(vad_model: str, explicit: bool):
	"""Block until the VAD model exists.

	Only the default path is downloaded by /init, thus only that path is worth
	waiting for. A path set with LT_ASR_VAD_MODEL is the operator's, and a
	missing file there is a typo that no amount of waiting fixes.

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
	# onnxruntime reads encoder.data from next to encoder.onnx by itself.
	paths.pop("encoder_data", None)

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
	"""Build one speaker's VAD. start() has already waited for the model file."""
	if os.environ.get("LT_ASR_DISABLE_VAD", "false").lower() in ("1", "true"):
		LOGGER.warning(
			"VAD disabled: every silent participant will cost nearly as much CPU as a speaking one"
		)
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


class Lanes:
	"""Decode threads that batch the streams waiting for them.

	Each lane is a thread that takes every job waiting in the shared queue and
	runs the streams those jobs fed through one decode_streams() call. A batch
	shares one encoder run across its streams, which is cheaper than decoding
	them one by one. Measured on a 4-core host with 16 speakers sending 400 ms
	chunks in real time for 20 s, a thread pool of 8 used 100.8 CPU seconds and
	fell 7.4 s behind, and 4 lanes used 76.2 and stayed within 0.7 s.

	With no more waiting streams than lanes, every batch holds one stream and
	this is the same as a thread pool. Batching starts only when speakers
	outnumber the lanes, which is also when the plain pool falls behind.

	A batched stream's result is not identical to decoding it alone, because the
	int8 encoder quantises activations with one scale per batch. Scored against
	the 1120 ms model on the same audio, the word error rate moved from 7.69 % to
	8.60 % with 8 speakers and stayed at 10.62 % with 4. That is the price of
	keeping up, and it is paid only past one speaker per core.
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
		"""Run ingest, decode the stream it returns, then return collect(decoded).

		ingest feeds audio and returns the stream to decode, or None to skip the
		decode. Pass None to only run collect on a lane, for state changes that
		must not overlap a decode of the same stream.
		"""
		loop = asyncio.get_running_loop()
		future = loop.create_future()
		self.__jobs.put((ingest, collect, loop, future))
		return await future

	def __work(self):
		while True:
			jobs = [self.__jobs.get()]
			# Take this lane's share of what is waiting, not all of it. One lane
			# holding every stream leaves the others idle: offline, a single batch
			# of 8 took 782 ms at p95 per 400 ms chunk, and 4 batches of 2 316 ms.
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
	# The awaiting connection may have closed, which cancels its future.
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
		# Tokens of the current utterance already sent in an early final, and the
		# seconds of audio decoded since the last final of either kind.
		self.__sent_tokens = 0
		self.__since_final = 0.0
		self.__language = AUTO_LANGUAGE
		self.__sample_rate = 48000

	@property
	def language(self) -> str:
		return self.__language

	async def __run(self, fn, *args):
		return await self.__lanes.submit(None, lambda _decoded: fn(*args))

	def __create_stream(self) -> sherpa_onnx.OnlineStream:
		stream = self.__recognizer.create_stream()
		# The language prompt conditions the model on every chunk. set_language()
		# has already mapped an id without a prompt to AUTO_LANGUAGE.
		stream.set_option("language", LANGUAGE_PROMPTS[self.__language])
		return stream

	def __reset_stream(self):
		self.__stream = self.__create_stream()
		self.__vad_tail = np.empty(0, dtype=np.float32)
		self.__between_utterances = True
		self.__pre_speech.clear()
		self.__sent_tokens = 0
		self.__since_final = 0.0

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

	async def set_language(self, requested: str, sample_rate: float | None) -> None:
		language = supported_language(requested)
		if language != requested:
			LOGGER.info("Unsupported language %r, detecting the spoken language instead",
						requested, extra={"tag": "asr"})

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

	def __ingest(self, pcm: np.ndarray) -> sherpa_onnx.OnlineStream | None:
		"""Feed a chunk, and return the stream if it now needs decoding."""
		if self.__stream is None:
			return None

		# The endpoint timer runs on DECODED frames, because only decode_stream
		# advances `num_trailing_blanks` and `num_processed_frames`. Thus an open
		# utterance must keep decoding through a pause, or it never closes and
		# its final is never sent. It is safe to drop silence only after an
		# endpoint, and then the audio is dropped outright. Feeding audio that is
		# never decoded builds an unbounded backlog, which is decoded in a burst
		# at the next word, so the cost is deferred and not avoided.
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

		# Read the result BEFORE reset(), which discards the segment's text. The
		# tokens are joined rather than read from .text, because the early final
		# cuts by token. Both are identical, as no inverse text normalisation or
		# homophone replacer is configured.
		tokens = self.__recognizer.get_result_all(self.__stream).tokens
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
			await self.__run(self.__reset_stream)

		pcm = np.frombuffer(frame, dtype=np.int16).astype(np.float32) / 32768.0
		pcm = np.ascontiguousarray(pcm)
		text, is_final = await self.__lanes.submit(lambda: self.__ingest(pcm), self.__collect)

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
		tokens = self.__recognizer.get_result_all(self.__stream).tokens
		return "".join(tokens[self.__sent_tokens:])

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
					# An unsupported language falls back to automatic detection, thus a
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

	# The app's /init handler fetches both, and it runs after this process starts.
	# Thus waiting here keeps the VAD from being skipped on a cold deploy.
	await wait_for_model(model_dir)
	if os.environ.get("LT_ASR_DISABLE_VAD", "false").lower() not in ("1", "true"):
		await wait_for_vad(vad_model, explicit=bool(vad_override))
	recognizer = build_recognizer(model_dir)
	LOGGER.info("Loaded the ASR model from %s", model_dir, extra={"tag": "asr"})

	# _env_int already falls back when the variable is unset or empty.
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

	# compression=None: PCM barely compresses, to 0.76 of its size, and the
	# deflate cost lands on the event loop. An external client may still offer
	# permessage-deflate, thus the server refuses it as well.
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
