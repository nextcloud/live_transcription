#!/usr/bin/env python3
#
# SPDX-FileCopyrightText: 2026 Nextcloud GmbH and Nextcloud contributors
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Join a Talk call as a regular user, the way the Talk web client does it.

The participant is a real user session, not an internal client of the signaling
server, because live_transcription ignores internal sessions:

  1. OCS: join the conversation, which yields the Nextcloud session id
  2. HPB: hello v2.0 with the ticket from the signaling settings, then join the room
  3. OCS: join the call

Roles:

  speaker   joins with audio and publishes an audio file in a loop through the SFU,
            until it is stopped with SIGINT/SIGTERM. With --video it also publishes
            a moving test pattern in the same offer, like a browser with a camera.
  listener  joins without media, enables live transcription for itself and counts
            the transcript messages that arrive on its signaling connection. It
            prints TRANSCRIPT for each transcript of the speaker and TRANSCRIBED
            once enough of them arrived, at least one of them final and one
            matching the expected words. It then exits 0. With --stay it keeps
            counting until it is stopped with SIGINT/SIGTERM and then exits 0.
            It exits 1 when the transcripts do not arrive in time, when the
            signaling connection closes, or when it is stopped before that.

Usage:

  talk_participant.py speaker  --url URL --token TOKEN --user U --password P --audio FILE [--video]
  talk_participant.py listener --url URL --token TOKEN --user U --password P --speaker-user S [--stay]
"""

import argparse
import asyncio
import contextlib
import json
import logging
import re
import signal
import sys
import time
from fractions import Fraction

import av
import numpy as np
import requests
import websockets
from aiortc import RTCPeerConnection, RTCSessionDescription
from aiortc.mediastreams import MediaStreamTrack
from aiortc.sdp import candidate_from_sdp

LOGGER = logging.getLogger("talk_participant")

IN_CALL = 1
WITH_AUDIO = 2
WITH_VIDEO = 4

SAMPLE_RATE = 48000
FRAME_SAMPLES = SAMPLE_RATE * 20 // 1000
# silence after every pass over the file, so the recognizer sees an utterance end
LOOP_GAP_SECONDS = 1.5
VIDEO_WIDTH = 640
VIDEO_HEIGHT = 360
VIDEO_FPS = 15
VIDEO_CLOCK = 90000
BAR_WIDTH = 40
BAR_STEP = 8
MSG_TIMEOUT = 15
ANSWER_TIMEOUT = 30


class LoopingFileTrack(MediaStreamTrack):
	"""Loop an audio file as 20 ms, 48 kHz mono frames, paced to real time."""

	kind = "audio"

	def __init__(self, path: str):
		super().__init__()
		self.pcm = self._decode(path)
		self.pos = 0
		self.pts = 0
		self.loops = 0
		self._start: float | None = None

	@staticmethod
	def _decode(path: str) -> np.ndarray:
		resampler = av.audio.resampler.AudioResampler(format="s16", layout="mono", rate=SAMPLE_RATE)
		chunks = []
		with av.open(path) as container:
			stream = next(s for s in container.streams if s.type == "audio")
			for frame in container.decode(stream):
				chunks.extend(f.to_ndarray().reshape(-1) for f in resampler.resample(frame))
			chunks.extend(f.to_ndarray().reshape(-1) for f in resampler.resample(None))
		chunks.append(np.zeros(int(SAMPLE_RATE * LOOP_GAP_SECONDS), dtype=np.int16))
		return np.concatenate(chunks).astype(np.int16)

	async def recv(self):
		if self._start is None:
			self._start = time.perf_counter()
		else:
			# absolute schedule, so a slow frame does not accumulate drift
			delay = self._start + self.pts / SAMPLE_RATE - time.perf_counter()
			if delay > 0:
				await asyncio.sleep(delay)

		end = self.pos + FRAME_SAMPLES
		chunk = self.pcm[self.pos:end]
		if len(chunk) < FRAME_SAMPLES:
			self.loops += 1
			end = FRAME_SAMPLES - len(chunk)
			chunk = np.concatenate([chunk, self.pcm[:end]])
		self.pos = end

		frame = av.AudioFrame.from_ndarray(chunk.reshape(1, -1), format="s16", layout="mono")
		frame.sample_rate = SAMPLE_RATE
		frame.pts = self.pts
		frame.time_base = Fraction(1, SAMPLE_RATE)
		self.pts += FRAME_SAMPLES
		return frame


class TestPatternTrack(MediaStreamTrack):
	"""A vertical bar that moves over a gray background, 640x360 at 15 frames/s, paced to real time.

	The frames are built directly in yuv420p, the format the VP8 encoder uses, so the
	encoder does not convert them and the speaker stays cheap on CPU.
	"""

	kind = "video"

	def __init__(self):
		super().__init__()
		self.frames = 0
		self._start: float | None = None
		# a Y plane followed by the U and V planes at a quarter of its size each
		self._background = np.full((VIDEO_HEIGHT * 3 // 2, VIDEO_WIDTH), 128, dtype=np.uint8)
		self._background[:VIDEO_HEIGHT] = 64

	async def recv(self):
		if self._start is None:
			self._start = time.perf_counter()
		else:
			# absolute schedule, so a slow frame does not accumulate drift
			delay = self._start + self.frames / VIDEO_FPS - time.perf_counter()
			if delay > 0:
				await asyncio.sleep(delay)

		image = self._background.copy()
		x = self.frames * BAR_STEP % (VIDEO_WIDTH - BAR_WIDTH)
		image[:VIDEO_HEIGHT, x:x + BAR_WIDTH] = 235

		frame = av.VideoFrame.from_ndarray(image, format="yuv420p")
		frame.pts = self.frames * VIDEO_CLOCK // VIDEO_FPS
		frame.time_base = Fraction(1, VIDEO_CLOCK)
		self.frames += 1
		return frame


class Participant:
	def __init__(self, url: str, token: str, user: str, password: str):
		self.url = url.rstrip("/")
		self.token = token
		self.user = user
		self.password = password
		self.http = requests.Session()
		self.http.headers.update({"OCS-APIRequest": "true", "Accept": "application/json"})
		self.ws: websockets.ClientConnection | None = None
		self.nc_sid: str | None = None
		self.hpb_sid: str | None = None
		self._msg_id = 0
		self.closed = asyncio.Event()

	async def ocs(self, method: str, path: str, **data) -> dict:
		def call():
			r = self.http.request(method, f"{self.url}/ocs/v2.php/apps/spreed/api/{path}", json=data or None,
				timeout=60)
			if not r.ok:
				raise RuntimeError(f"{self.user}: {method} {path} -> {r.status_code} {r.text[:300]}")
			return r.json()["ocs"]["data"]

		return await asyncio.get_running_loop().run_in_executor(None, call)

	async def login(self):
		"""Authenticate with an app password, like the Talk mobile clients.

		With the login password every request logs the user in again, and Talk ends
		the room session of a user who logs out, so the participant would leave the
		conversation right after joining it. App passwords keep the session.
		"""
		def call():
			r = self.http.get(f"{self.url}/ocs/v2.php/core/getapppassword", auth=(self.user, self.password), timeout=60)
			r.raise_for_status()
			return r.json()["ocs"]["data"]["apppassword"]

		self.http.auth = (self.user, await asyncio.get_running_loop().run_in_executor(None, call))

	async def send(self, message: dict):
		self._msg_id += 1
		message["id"] = str(self._msg_id)
		await self.ws.send(json.dumps(message))

	async def recv(self) -> dict:
		return json.loads(await asyncio.wait_for(self.ws.recv(), MSG_TIMEOUT))

	async def join(self, flags: int):
		await self.login()
		joined = await self.ocs("POST", f"v4/room/{self.token}/participants/active")
		self.nc_sid = joined["sessionId"]
		settings = await self.ocs("GET", f"v3/signaling/settings?token={self.token}")
		if settings.get("signalingMode") != "external":
			raise RuntimeError(f"signaling mode is {settings.get('signalingMode')!r}, the HPB is not configured")

		hpb_url = re.sub(r"^http", "ws", settings["server"].rstrip("/")) + "/spreed"
		self.ws = await websockets.connect(hpb_url, max_size=None)
		await self.send({"type": "hello", "hello": {"version": "2.0", "auth": {
			"url": f"{self.url}/ocs/v2.php/apps/spreed/api/v3/signaling/backend",
			"params": settings["helloAuthParams"]["2.0"],
		}}})
		self.hpb_sid = (await self.expect("hello"))["hello"]["sessionid"]
		await self.send({"type": "room", "room": {"roomid": self.token, "sessionid": self.nc_sid}})
		await self.expect("room")
		LOGGER.info("%s joined the room, HPB session %s", self.user, self.hpb_sid)

		await self.ocs("POST", f"v4/call/{self.token}", flags=flags)
		LOGGER.info("%s joined the call with flags %d", self.user, flags)

	async def expect(self, msg_type: str) -> dict:
		while True:
			message = await self.recv()
			if message.get("type") == msg_type:
				return message
			if message.get("type") == "error":
				raise RuntimeError(f"{self.user}: signaling error while waiting for {msg_type!r}: {message}")
			self.handle_early(message)

	def handle_early(self, message: dict):
		"""Messages that arrive before the read loop runs; subclasses keep what they need."""

	async def read_loop(self):
		try:
			async for raw in self.ws:
				await self.handle(json.loads(raw))
		except websockets.ConnectionClosedOK:
			# a send in handle() on a connection that closed normally
			pass
		except Exception:
			# the caller only sees that the reader ended, so the cause goes to the log here, also
			# for ConnectionClosedError: a protocol error or a network failure
			LOGGER.exception("%s: the signaling reader failed", self.user)
		finally:
			self.closed.set()

	async def handle(self, message: dict):
		if message.get("type") == "error":
			LOGGER.warning("%s: signaling error %s", self.user, message.get("error"))
		elif message.get("type") == "bye":
			LOGGER.warning("%s: the signaling server closed the session", self.user)

	async def leave(self):
		with contextlib.suppress(Exception):
			await self.ocs("DELETE", f"v4/call/{self.token}")
		with contextlib.suppress(Exception):
			await self.ocs("DELETE", f"v4/room/{self.token}/participants/active")
		if self.ws:
			with contextlib.suppress(Exception):
				await self.send({"type": "bye", "bye": {}})
			with contextlib.suppress(Exception):
				await self.ws.close()
		if self.http.auth:
			with contextlib.suppress(Exception):
				await asyncio.get_running_loop().run_in_executor(None, lambda: self.http.delete(
					f"{self.url}/ocs/v2.php/core/apppassword", timeout=60))
		LOGGER.info("%s left", self.user)


class Speaker(Participant):
	def __init__(self, *args, audio: str, video: bool):
		super().__init__(*args)
		self.track = LoopingFileTrack(audio)
		self.video_track = TestPatternTrack() if video else None
		self.pc: RTCPeerConnection | None = None
		self.answered = asyncio.Event()
		self.failed = asyncio.Event()

	async def publish(self):
		"""Send the offer to our own session, which asks the SFU to create a publisher."""
		self.pc = RTCPeerConnection()
		self.pc.addTrack(self.track)
		if self.video_track:
			self.pc.addTrack(self.video_track)

		@self.pc.on("connectionstatechange")
		async def on_state():
			LOGGER.info("%s: peer connection %s", self.user, self.pc.connectionState)
			if self.pc.connectionState == "failed":
				self.failed.set()

		await self.pc.setLocalDescription(await self.pc.createOffer())
		await self.send({"type": "message", "message": {
			"recipient": {"type": "session", "sessionid": self.hpb_sid},
			"data": {"to": self.hpb_sid, "type": "offer", "sid": str(int(time.time() * 1000)), "roomType": "video",
				"payload": {"nick": self.user, "type": "offer", "sdp": self.pc.localDescription.sdp}},
		}})
		await asyncio.wait_for(self.answered.wait(), ANSWER_TIMEOUT)
		LOGGER.info("%s: the SFU accepted the %s stream", self.user, "audio and video" if self.video_track else "audio")

	async def handle(self, message: dict):
		await super().handle(message)
		if message.get("type") != "message" or not self.pc:
			return
		data = message["message"].get("data", {})
		if data.get("type") == "answer" and self.pc.signalingState == "have-local-offer":
			await self.pc.setRemoteDescription(RTCSessionDescription(type="answer", sdp=data["payload"]["sdp"]))
			self.answered.set()
		elif data.get("type") == "candidate":
			candidate = data["payload"]["candidate"]
			if candidate.get("candidate"):
				parsed = candidate_from_sdp(candidate["candidate"].removeprefix("candidate:"))
				parsed.sdpMid = candidate.get("sdpMid")
				parsed.sdpMLineIndex = candidate.get("sdpMLineIndex")
				await self.pc.addIceCandidate(parsed)

	async def leave(self):
		if self.pc:
			with contextlib.suppress(Exception):
				await self.pc.close()
		await super().leave()


class Listener(Participant):
	def __init__(self, *args, speaker_user: str, needed: int, needed_finals: int, expect: re.Pattern):
		super().__init__(*args)
		self.speaker_user = speaker_user
		self.needed = needed
		self.needed_finals = needed_finals
		self.expect_re = expect
		self.speaker_sids: set[str] = set()
		self.transcripts: list[dict] = []
		self.finals = 0
		self.matched = False
		self.done = asyncio.Event()

	def track_sessions(self, message: dict):
		"""Remember the HPB session ids of the speaker, from joins and participant updates."""
		event = message.get("event", {})
		if event.get("target") == "room" and event.get("type") == "join":
			for entry in event.get("join", []):
				if entry.get("userid") == self.speaker_user:
					self.speaker_sids.add(entry["sessionid"])
		elif event.get("target") == "participants" and event.get("type") == "update":
			for entry in event.get("update", {}).get("users", []):
				if entry.get("userId") == self.speaker_user and entry.get("sessionId"):
					self.speaker_sids.add(entry["sessionId"])

	def handle_early(self, message: dict):
		self.track_sessions(message)

	async def handle(self, message: dict):
		await super().handle(message)
		self.track_sessions(message)
		if message.get("type") != "message":
			return
		data = message["message"].get("data", {})
		if data.get("type") != "transcript":
			return

		text = (data.get("message") or "").strip()
		from_speaker = data.get("speakerSessionId") in self.speaker_sids
		LOGGER.info("transcript final=%s from_speaker=%s: %r", data.get("final"), from_speaker, text)
		if not text or not from_speaker:
			return
		self.transcripts.append(data)
		self.finals += bool(data.get("final"))
		self.matched = self.matched or bool(self.expect_re.search(text))
		print("TRANSCRIPT", flush=True)
		if (not self.done.is_set() and len(self.transcripts) >= self.needed and self.finals >= self.needed_finals
				and self.matched):
			print("TRANSCRIBED", flush=True)
			self.done.set()

	async def enable_transcription(self):
		await self.ocs("POST", f"v1/live-transcription/{self.token}")
		LOGGER.info("%s enabled live transcription", self.user)


async def wait_any(timeout: float | None, *events: asyncio.Event):
	"""Wait until one of the events is set or the timeout passed."""
	waits = [asyncio.create_task(e.wait()) for e in events]
	await asyncio.wait(waits, timeout=timeout, return_when=asyncio.FIRST_COMPLETED)
	for task in waits:
		task.cancel()


def stop_event() -> asyncio.Event:
	"""An event that SIGINT and SIGTERM set, so the participant can leave the call cleanly."""
	stop = asyncio.Event()
	for sig in (signal.SIGINT, signal.SIGTERM):
		asyncio.get_running_loop().add_signal_handler(sig, stop.set)
	return stop


async def run_speaker(args) -> int:
	speaker = Speaker(args.url, args.token, args.user, args.password, audio=args.audio, video=args.video)
	stop = stop_event()

	try:
		await speaker.join(IN_CALL | WITH_AUDIO | (WITH_VIDEO if args.video else 0))
		reader = asyncio.create_task(speaker.read_loop())
		await speaker.publish()
		print("READY", flush=True)
		await wait_any(None, stop, speaker.closed, speaker.failed)
		LOGGER.info("%s: played the file %d times", args.user, speaker.track.loops)
		reader.cancel()
		if not stop.is_set():
			LOGGER.error("%s: lost the call before being stopped", args.user)
			return 1
		return 0
	finally:
		await speaker.leave()


async def run_listener(args) -> int:
	listener = Listener(args.url, args.token, args.user, args.password, speaker_user=args.speaker_user,
		needed=args.min_transcripts, needed_finals=args.min_finals, expect=re.compile(args.expect, re.IGNORECASE))
	stop = stop_event()
	try:
		await listener.join(IN_CALL)
		reader = asyncio.create_task(listener.read_loop())
		await listener.enable_transcription()
		print("READY", flush=True)
		await wait_any(args.timeout, listener.done, listener.closed, stop)

		count = len(listener.transcripts)
		if listener.done.is_set():
			LOGGER.info("SUCCESS: %d transcripts (%d final) of %s received, expected words matched",
				count, listener.finals, args.speaker_user)
			if not args.stay:
				reader.cancel()
				return 0
			LOGGER.info("%s stays in the call until it is stopped", args.user)
			await wait_any(None, listener.closed, stop)
			reader.cancel()
			if stop.is_set():
				LOGGER.info("%s stopped after %d transcripts (%d final) of %s", args.user, len(listener.transcripts),
					listener.finals, args.speaker_user)
				return 0
			LOGGER.error("FAILURE: the signaling connection closed after %d transcripts of %s",
				len(listener.transcripts), args.speaker_user)
			return 1

		reader.cancel()
		if listener.closed.is_set():
			reason = "the signaling connection closed"
		elif stop.is_set():
			reason = "the listener was stopped"
		else:
			reason = f"{args.timeout}s passed"
		LOGGER.error(
			"FAILURE: %s with %d/%d transcripts and %d/%d finals of %s (speaker sessions %s), "
			"expected words matched: %s",
			reason, count, args.min_transcripts, listener.finals, args.min_finals, args.speaker_user,
			sorted(listener.speaker_sids), listener.matched)
		return 1
	finally:
		await listener.leave()


def main() -> int:
	parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
	parser.add_argument("role", choices=("speaker", "listener"))
	parser.add_argument("--url", required=True, help="Nextcloud base URL")
	parser.add_argument("--token", required=True, help="conversation token")
	parser.add_argument("--user", required=True)
	parser.add_argument("--password", required=True)
	parser.add_argument("--audio", help="speaker: audio file to loop")
	parser.add_argument("--video", action="store_true",
		help="speaker: also publish a 640x360, 15 frames/s test pattern, like a camera")
	parser.add_argument("--speaker-user", help="listener: user whose speech is transcribed")
	parser.add_argument("--min-transcripts", type=int, default=10, help="listener: transcripts needed to pass")
	parser.add_argument("--min-finals", type=int, default=1, help="listener: final transcripts needed to pass")
	parser.add_argument("--expect", default=r"\b(americans?|country)\b",
		help="listener: regex that at least one transcript must match")
	parser.add_argument("--timeout", type=float, default=300, help="listener: seconds to wait for transcripts")
	parser.add_argument("--stay", action="store_true",
		help="listener: stay in the call after the transcripts arrived, until SIGINT/SIGTERM")
	args = parser.parse_args()
	if args.role == "speaker" and not args.audio:
		parser.error("speaker needs --audio")
	if args.role == "listener" and not args.speaker_user:
		parser.error("listener needs --speaker-user")

	logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s",
		datefmt="%H:%M:%S")
	for name in ("aiortc", "aioice", "websockets"):
		logging.getLogger(name).setLevel(logging.WARNING)

	return asyncio.run(run_speaker(args) if args.role == "speaker" else run_listener(args))


if __name__ == "__main__":
	sys.exit(main())
