#!/usr/bin/env python3
#
# SPDX-FileCopyrightText: 2026 Nextcloud GmbH and Nextcloud contributors
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Run a Talk call with many speakers and listeners, each in its own process and network namespace.

The orchestrator runs as root. It forks one child per participant. Each child enters the
network namespace lts-<k> (see stress_netns.sh), drops to --uid/--gid and joins the call as
its user, so every participant reaches Nextcloud, the HPB and Janus from its own address.
Speakers use the namespaces 0 .. S-1, listeners the namespaces S .. S+L-1.

  speaker   publishes the audio file through the SFU. It sends speech in its windows of
            the speaking schedule and silence outside them, like an open microphone.
  listener  enables live transcription and records every transcript it receives.

The speaking schedule starts once all participants joined. Turn k belongs to speaker
k % S and lasts --turn seconds. The next turn starts --overlap seconds before the current
one ends, and after every second turn nobody speaks for --pause seconds.

With --drop-probability each participant drops once at a random time: half of the drops
leave the call cleanly, the other half close the connections without leaving. The
participant rejoins 2 to 8 seconds later. The random choices derive from --seed.

Output in --out: <user>.jsonl with one JSON object per event of each participant,
schedule.json with the speaking windows, summary.json with the exit code and the memory
of each participant. stress_check.py evaluates them.

The orchestrator prints READY once all participants joined, ends the call after --minutes,
and exits 0 when every participant joined and exited cleanly, 1 otherwise.
"""

import argparse
import contextlib
import functools
import hashlib
import json
import multiprocessing
import os
import random
import re
import signal
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path

import requests
from websockets.exceptions import ConnectionClosed
from websockets.sync.client import connect

# the call flags of Talk, as in talk_participant.py, which a listener must not import (it loads aiortc)
IN_CALL = 1
WITH_AUDIO = 2

MAX_PARALLEL_JOINS = 8
HTTP_TIMEOUT = 60
MSG_TIMEOUT = 15
ANSWER_TIMEOUT = 30
JOIN_ATTEMPTS = 2
JOIN_RETRY_DELAY = 3
# time for all participants to join before the speaking schedule starts without the missing ones
JOIN_DEADLINE = 600
# between READY and the first speaking window, so the app has subscribed to the speakers: in a
# local run with 2 speakers its peer connections were up 15 s after the speakers joined
START_DELAY = 20
# a drop happens this long before the end at the latest, so the rejoin is inside the run
DROP_MARGIN = 30
REJOIN_DELAY = (2, 8)
STOP_TIMEOUT = 60
MEMORY_SAMPLE_INTERVAL = 15


def speaker_user(i: int) -> str:
	return f"stress-speaker-{i + 1}"


def listener_user(i: int) -> str:
	return f"stress-listener-{i + 1:03d}"


def namespace_address(k: int) -> str:
	"""The address of namespace lts-k, as stress_netns.sh assigns it."""
	return f"10.201.{1 + k // 250}.{1 + k % 250}"


def schedule(t0: float, duration: float, speakers: int, turn: float, overlap: float, pause: float) -> list[dict]:
	"""The speaking windows of the call, in epoch seconds."""
	period = 2 * turn - overlap + pause
	windows = []
	k = 0
	while (start := k // 2 * period + k % 2 * (turn - overlap)) < duration:
		windows.append({"speaker": speaker_user(k % speakers), "start": t0 + start,
			"end": t0 + min(start + turn, duration)})
		k += 1
	return windows


@dataclass
class Config:
	url: str
	token: str
	password_prefix: str
	audio: str
	speakers: list[str]
	duration: float
	turn: float
	overlap: float
	pause: float
	drop_probability: float
	seed: str
	out: Path


@dataclass
class Shared:
	"""What the orchestrator and the children share across the fork."""

	joined: multiprocessing.Queue
	join_gate: multiprocessing.Semaphore
	start: multiprocessing.Value
	go: multiprocessing.Event


class Record:
	"""Append the events of one participant to <user>.jsonl, one JSON object per line."""

	def __init__(self, path: Path):
		self.file = path.open("a", buffering=1, encoding="utf-8")
		self.lock = threading.Lock()

	def __call__(self, event: str, **fields):
		line = json.dumps({"t": round(time.time(), 3), "event": event, **fields})
		with self.lock:
			self.file.write(line + "\n")


class Participant:
	"""A user session in the call, synchronous: requests for OCS, a websocket and a reader thread for the HPB."""

	flags = IN_CALL

	def __init__(self, cfg: Config, user: str, record: Record):
		self.url = cfg.url.rstrip("/")
		self.token = cfg.token
		self.user = user
		self.password = cfg.password_prefix + user
		self.record = record
		self.http = requests.Session()
		# no proxy and no netrc of the environment, the services are on the bridge
		self.http.trust_env = False
		self.http.headers.update({"OCS-APIRequest": "true", "Accept": "application/json"})
		self.ws = None
		self.nc_sid: str | None = None
		self.hpb_sid: str | None = None
		self._msg_id = 0
		# set by the reader thread when the HPB connection closes while the participant did not close it
		self.lost = threading.Event()
		self.closing = False

	def ocs(self, method: str, path: str, **data) -> dict:
		r = self.http.request(method, f"{self.url}/ocs/v2.php/apps/spreed/api/{path}", json=data or None,
			timeout=HTTP_TIMEOUT)
		if not r.ok:
			raise RuntimeError(f"{method} {path} -> {r.status_code} {r.text[:300]}")
		return r.json()["ocs"]["data"]

	def login(self):
		"""Use an app password, because Talk ends the room session when a login password logs in again."""
		r = self.http.get(f"{self.url}/ocs/v2.php/core/getapppassword", auth=(self.user, self.password),
			timeout=HTTP_TIMEOUT)
		r.raise_for_status()
		self.http.auth = (self.user, r.json()["ocs"]["data"]["apppassword"])

	def send(self, message: dict):
		self._msg_id += 1
		message["id"] = str(self._msg_id)
		self.ws.send(json.dumps(message))

	def expect(self, msg_type: str) -> dict:
		while True:
			message = json.loads(self.ws.recv(MSG_TIMEOUT))
			if message.get("type") == msg_type:
				return message
			if message.get("type") == "error":
				raise RuntimeError(f"signaling error while waiting for {msg_type!r}: {message}")
			self.handle(message)

	def join(self) -> dict:
		"""Join the room, the HPB session and the call, then start what the role does in the call."""
		self.cut()
		if not self.http.auth:
			self.login()
		self.nc_sid = self.ocs("POST", f"v4/room/{self.token}/participants/active")["sessionId"]
		settings = self.ocs("GET", f"v3/signaling/settings?token={self.token}")
		if settings.get("signalingMode") != "external":
			raise RuntimeError(f"signaling mode is {settings.get('signalingMode')!r}, the HPB is not configured")

		hpb_url = re.sub(r"^http", "ws", settings["server"].rstrip("/")) + "/spreed"
		self.closing = False
		self.lost.clear()
		# legacy=True: the connection outlives this method, thus no context manager
		self.ws = connect(hpb_url, max_size=None, proxy=None, open_timeout=MSG_TIMEOUT, legacy=True)
		self.send({"type": "hello", "hello": {"version": "2.0", "auth": {
			"url": f"{self.url}/ocs/v2.php/apps/spreed/api/v3/signaling/backend",
			"params": settings["helloAuthParams"]["2.0"],
		}}})
		self.hpb_sid = self.expect("hello")["hello"]["sessionid"]
		self.send({"type": "room", "room": {"roomid": self.token, "sessionid": self.nc_sid}})
		self.expect("room")
		self.ocs("POST", f"v4/call/{self.token}", flags=self.flags)
		threading.Thread(target=self.read_loop, args=(self.ws,), daemon=True).start()
		self.start()
		return {"nc_session": self.nc_sid, "hpb_session": self.hpb_sid, "address": self.ws.local_address[0]}

	def start(self):
		"""What the role does once it is in the call."""

	def read_loop(self, ws):
		try:
			for raw in ws:
				try:
					self.handle(json.loads(raw))
				except Exception as e:
					self.record("error", message=f"handling a signaling message: {e!r}")
		except (ConnectionClosed, OSError):
			pass
		if ws is self.ws and not self.closing:
			self.lost.set()

	def handle(self, message: dict):
		if message.get("type") == "error":
			self.record("error", message=f"signaling error {message.get('error')}")

	def leave(self):
		"""Leave the call and the room, then end the HPB session."""
		self.closing = True
		with contextlib.suppress(Exception):
			self.ocs("DELETE", f"v4/call/{self.token}")
		with contextlib.suppress(Exception):
			self.ocs("DELETE", f"v4/room/{self.token}/participants/active")
		if self.ws:
			with contextlib.suppress(Exception):
				self.send({"type": "bye", "bye": {}})
			with contextlib.suppress(Exception):
				self.ws.close()

	def cut(self):
		"""Close the connections without telling anybody, like a client that loses its network."""
		self.closing = True
		if self.ws:
			# shuts the TCP connection down without a websocket close frame
			self.ws.close_socket()
		# the kept-alive OCS connections go too, the app password and the session cookie stay
		self.http.close()

	def logout(self):
		with contextlib.suppress(Exception):
			self.http.delete(f"{self.url}/ocs/v2.php/core/apppassword", timeout=HTTP_TIMEOUT)


class Listener(Participant):
	def __init__(self, cfg: Config, user: str, record: Record):
		super().__init__(cfg, user, record)
		self.speakers = set(cfg.speakers)
		# HPB session id -> speaker user, from the join events and the participant updates
		self.speaker_of: dict[str, str] = {}

	def track_sessions(self, event: dict):
		if event.get("target") == "room" and event.get("type") == "join":
			for entry in event.get("join", []):
				if entry.get("userid") in self.speakers:
					self.speaker_of[entry["sessionid"]] = entry["userid"]
		elif event.get("target") == "participants" and event.get("type") == "update":
			for entry in event.get("update", {}).get("users", []):
				if entry.get("userId") in self.speakers and entry.get("sessionId"):
					self.speaker_of[entry["sessionId"]] = entry["userId"]

	def handle(self, message: dict):
		super().handle(message)
		if message.get("type") == "event":
			self.track_sessions(message.get("event", {}))
			return
		if message.get("type") != "message":
			return
		data = message["message"].get("data", {})
		if data.get("type") != "transcript":
			return
		sid = data.get("speakerSessionId")
		self.record("transcript", speaker=self.speaker_of.get(sid), session=sid, final=bool(data.get("final")),
			text=(data.get("message") or "").strip())

	def start(self):
		self.ocs("POST", f"v1/live-transcription/{self.token}")


@functools.cache
def scheduled_track_class():
	"""The audio track of a speaker. Only speakers import aiortc and av, so a listener stays small."""
	from aiortc.mediastreams import MediaStreamTrack
	from talk_participant import LoopingFileTrack

	class ScheduledTrack(LoopingFileTrack):
		"""Loop the decoded file and send silence outside the speaker's windows."""

		def __init__(self, pcm, windows: list[tuple[float, float]]):
			MediaStreamTrack.__init__(self)
			self.pcm = pcm
			self.pos = 0
			self.pts = 0
			self.loops = 0
			self._start = None
			self.windows = windows

		async def recv(self):
			frame = await super().recv()
			now = time.time()
			if not any(start <= now < end for start, end in self.windows):
				plane = frame.planes[0]
				plane.update(bytes(plane.buffer_size))
			return frame

	return ScheduledTrack


class Speaker(Participant):
	"""Publishes audio through the SFU.

	aiortc is asyncio only, so the peer connection runs in an asyncio event loop on a thread of
	its own. Everything else in the speaker is synchronous and hands work to that loop.
	"""

	flags = IN_CALL | WITH_AUDIO

	def __init__(self, cfg: Config, user: str, record: Record):
		import asyncio

		from talk_participant import LoopingFileTrack

		super().__init__(cfg, user, record)
		self.asyncio = asyncio
		self.pcm = LoopingFileTrack._decode(cfg.audio)
		self.windows: list[tuple[float, float]] = []
		self.pc = None
		self.answered = threading.Event()
		self.loop = asyncio.new_event_loop()
		threading.Thread(target=self.loop.run_forever, daemon=True).start()

	def run(self, coro, timeout: float = ANSWER_TIMEOUT):
		return self.asyncio.run_coroutine_threadsafe(coro, self.loop).result(timeout)

	async def _offer(self) -> str:
		from aiortc import RTCPeerConnection

		pc = RTCPeerConnection()
		pc.addTrack(scheduled_track_class()(self.pcm, self.windows))

		@pc.on("connectionstatechange")
		async def on_state():
			if pc.connectionState == "failed" and pc is self.pc:
				self.record("error", message="the peer connection to the SFU failed")

		self.pc = pc
		await pc.setLocalDescription(await pc.createOffer())
		return pc.localDescription.sdp

	def start(self):
		"""Send the offer to our own session, which asks the SFU to create a publisher."""
		self.answered.clear()
		sdp = self.run(self._offer())
		self.send({"type": "message", "message": {
			"recipient": {"type": "session", "sessionid": self.hpb_sid},
			"data": {"to": self.hpb_sid, "type": "offer", "sid": str(int(time.time() * 1000)), "roomType": "video",
				"payload": {"nick": self.user, "type": "offer", "sdp": sdp}},
		}})
		if not self.answered.wait(ANSWER_TIMEOUT):
			raise RuntimeError(f"no answer of the SFU within {ANSWER_TIMEOUT}s")

	def handle(self, message: dict):
		from aiortc import RTCSessionDescription
		from aiortc.sdp import candidate_from_sdp

		super().handle(message)
		if message.get("type") != "message" or not self.pc:
			return
		data = message["message"].get("data", {})
		if data.get("type") == "answer" and self.pc.signalingState == "have-local-offer":
			self.run(self.pc.setRemoteDescription(RTCSessionDescription(type="answer", sdp=data["payload"]["sdp"])))
			self.answered.set()
		elif data.get("type") == "candidate":
			candidate = data["payload"]["candidate"]
			if candidate.get("candidate"):
				parsed = candidate_from_sdp(candidate["candidate"].removeprefix("candidate:"))
				parsed.sdpMid = candidate.get("sdpMid")
				parsed.sdpMLineIndex = candidate.get("sdpMLineIndex")
				self.run(self.pc.addIceCandidate(parsed))

	async def _cut_pc(self, pc):
		# Closing the ICE transports first closes the UDP sockets, so the close of the peer
		# connection sends neither an RTCP BYE nor a DTLS close to the SFU.
		for transceiver in pc.getTransceivers():
			with contextlib.suppress(Exception):
				await transceiver.sender.transport.transport.stop()
		await pc.close()

	def leave(self):
		pc, self.pc = self.pc, None
		if pc:
			with contextlib.suppress(Exception):
				self.run(pc.close())
		super().leave()

	def cut(self):
		pc, self.pc = self.pc, None
		if pc:
			with contextlib.suppress(Exception):
				self.run(self._cut_pc(pc))
		super().cut()


def join_with_retry(p: Participant, shared: Shared, event: str, stop: threading.Event) -> bool:
	"""Join, at most MAX_PARALLEL_JOINS participants at the same time, and try once more on a failure."""
	for attempt in range(1, JOIN_ATTEMPTS + 1):
		try:
			with shared.join_gate:
				info = p.join()
			p.record(event, attempt=attempt, **info)
			p.record("ready")
			return True
		except Exception as e:
			p.record("error", message=f"{event} attempt {attempt}: {e!r}", phase=event, attempt=attempt)
			with contextlib.suppress(Exception):
				p.cut()
			if stop.wait(JOIN_RETRY_DELAY):
				return False
	return False


def plan_drop(cfg: Config, user: str, t0: float) -> tuple[float, str, float] | None:
	rng = random.Random(f"{cfg.seed}/{user}")  # noqa: S311 - a reproducible test schedule, not a secret
	if rng.random() >= cfg.drop_probability:
		return None
	at = t0 + rng.uniform(0, max(0, cfg.duration - DROP_MARGIN))
	return at, rng.choice(("clean", "abrupt")), rng.uniform(*REJOIN_DELAY)


def drop(p: Participant, kind: str, pause: float):
	p.record("drop", kind=kind, rejoin_after=round(pause, 1))
	if kind == "clean":
		p.leave()
	else:
		p.cut()


def run_participant(p: Participant, cfg: Config, shared: Shared, stop: threading.Event) -> int:
	if not join_with_retry(p, shared, "joined", stop):
		return 0 if stop.is_set() else 2
	shared.joined.put(p.user)
	while not shared.go.wait(0.5):
		if stop.is_set():
			p.leave()
			p.record("left")
			return 0
	t0 = shared.start.value
	if isinstance(p, Speaker):
		p.windows.extend((w["start"], w["end"]) for w in schedule(
			t0, cfg.duration, len(cfg.speakers), cfg.turn, cfg.overlap, cfg.pause) if w["speaker"] == p.user)
	planned = plan_drop(cfg, p.user, t0)

	while not stop.is_set():
		if planned and time.time() >= planned[0]:
			_, kind, pause = planned
			planned = None
			drop(p, kind, pause)
			if stop.wait(pause):
				break
			if not join_with_retry(p, shared, "rejoined", stop):
				return 0 if stop.is_set() else 2
		elif p.lost.is_set():
			p.record("error", message="the signaling connection closed", disconnected=True)
			if not join_with_retry(p, shared, "rejoined", stop):
				return 0 if stop.is_set() else 2
		stop.wait(0.2)

	# every participant leaves at the end, three requests each, thus through the gate as well
	with shared.join_gate:
		p.leave()
		p.logout()
	p.record("left")
	return 0


def child(cfg: Config, shared: Shared, role: str, k: int, user: str, uid: int, gid: int):
	"""Enter namespace lts-k, drop root and run the participant. Runs in the forked child."""
	stop = threading.Event()
	for sig in (signal.SIGTERM, signal.SIGINT):
		signal.signal(sig, lambda *_: stop.set())
	with open(f"/run/netns/lts-{k}") as ns:
		os.setns(ns.fileno(), os.CLONE_NEWNET)
	os.setgroups([])
	os.setgid(gid)
	os.setuid(uid)

	record = Record(cfg.out / f"{user}.jsonl")
	code = 1
	try:
		p = Speaker(cfg, user, record) if role == "speaker" else Listener(cfg, user, record)
		code = run_participant(p, cfg, shared, stop)
	except Exception as e:
		record("error", message=f"crashed: {e!r}", fatal=True)
	sys.exit(code)


def memory_kib(pid: int) -> dict[str, int]:
	"""Rss and Pss of a process. Pss splits the pages that the fork shares among the processes."""
	values = {}
	with contextlib.suppress(OSError), open(f"/proc/{pid}/smaps_rollup") as f:
		for line in f:
			name, _, rest = line.partition(":")
			if name in ("Rss", "Pss"):
				values[name.lower()] = int(rest.split()[0])
	return values


class Orchestrator:
	def __init__(self, args, cfg: Config):
		self.args = args
		self.cfg = cfg
		self.ctx = multiprocessing.get_context("fork")
		self.shared = Shared(joined=self.ctx.Queue(), join_gate=self.ctx.Semaphore(MAX_PARALLEL_JOINS),
			start=self.ctx.Value("d", 0.0), go=self.ctx.Event())
		self.participants = [("speaker", speaker_user(i)) for i in range(args.speakers)]
		self.participants += [("listener", listener_user(i)) for i in range(args.listeners)]
		self.procs: dict[str, multiprocessing.Process] = {}
		self.memory: dict[str, dict[str, int]] = {}
		self.joined: set[str] = set()
		self.stopped = threading.Event()

	def chown(self, path: Path):
		os.chown(path, self.args.uid, self.args.gid)

	def write_json(self, name: str, data):
		path = self.cfg.out / name
		path.write_text(json.dumps(data, indent=1) + "\n")
		self.chown(path)

	def start_children(self):
		for k, (role, user) in enumerate(self.participants):
			(self.cfg.out / f"{user}.jsonl").unlink(missing_ok=True)
			proc = self.ctx.Process(target=child, name=user,
				args=(self.cfg, self.shared, role, k, user, self.args.uid, self.args.gid))
			proc.start()
			self.procs[user] = proc

	def drain_joined(self, timeout: float):
		with contextlib.suppress(Exception):
			self.joined.add(self.shared.joined.get(timeout=timeout))
			while True:
				self.joined.add(self.shared.joined.get_nowait())

	def wait_for_joins(self):
		deadline = time.time() + JOIN_DEADLINE
		reported = 0
		while len(self.joined) < len(self.participants) and time.time() < deadline and not self.stopped.is_set():
			self.drain_joined(1)
			alive = {u for u, p in self.procs.items() if p.is_alive()}
			if not alive - self.joined:
				break
			if len(self.joined) >= reported + 10:
				reported = len(self.joined)
				print(f"{reported}/{len(self.participants)} participants joined", flush=True)

	def sample_memory(self):
		for user, proc in self.procs.items():
			if proc.pid and proc.is_alive():
				peak = self.memory.setdefault(user, {})
				for name, value in memory_kib(proc.pid).items():
					peak[name] = max(peak.get(name, 0), value)

	def stop_children(self):
		for proc in self.procs.values():
			if proc.is_alive():
				os.kill(proc.pid, signal.SIGTERM)
		deadline = time.time() + STOP_TIMEOUT
		for proc in self.procs.values():
			proc.join(max(0.1, deadline - time.time()))
		for user, proc in self.procs.items():
			if proc.is_alive():
				print(f"{user} did not stop within {STOP_TIMEOUT}s and is killed", flush=True)
				proc.kill()
				proc.join()
		self.drain_joined(0)

	def memory_by_role(self) -> dict[str, dict[str, int]]:
		result = {}
		for role, user in self.participants:
			for name, value in self.memory.get(user, {}).items():
				entry = result.setdefault(role, {})
				entry[f"max_{name}_kib"] = max(entry.get(f"max_{name}_kib", 0), value)
				entry[f"sum_{name}_kib"] = entry.get(f"sum_{name}_kib", 0) + value
		return result

	def run(self) -> int:
		for sig in (signal.SIGTERM, signal.SIGINT):
			signal.signal(sig, lambda *_: self.stopped.set())
		self.start_children()
		self.wait_for_joins()
		all_joined = len(self.joined) == len(self.participants)
		if not all_joined:
			missing = sorted(u for _, u in self.participants if u not in self.joined)
			print(f"::error::{len(missing)} participants did not join: {' '.join(missing)}", flush=True)

		t0 = time.time() + self.args.start_delay
		end = t0 + self.cfg.duration
		self.write_json("schedule.json", schedule(t0, self.cfg.duration, self.args.speakers, self.cfg.turn,
			self.cfg.overlap, self.cfg.pause))
		self.shared.start.value = t0
		self.shared.go.set()
		if all_joined:
			print("READY", flush=True)
		print(f"{len(self.joined)} participants in the call, the schedule ends in {end - time.time():.0f}s",
			flush=True)

		while not self.stopped.is_set() and (left := end - time.time()) > 0:
			self.sample_memory()
			self.stopped.wait(min(MEMORY_SAMPLE_INTERVAL, left))
		self.stop_children()

		participants = {user: {
			"role": role, "namespace": f"lts-{k}", "address": namespace_address(k),
			"joined": user in self.joined, "exitcode": self.procs[user].exitcode, **self.memory.get(user, {}),
		} for k, (role, user) in enumerate(self.participants)}
		failed = sorted(u for u, p in participants.items() if p["exitcode"] != 0)
		self.write_json("summary.json", {
			"token": self.cfg.token, "seed": self.cfg.seed, "start": t0, "end": end,
			"stopped_early": self.stopped.is_set(),
			"speakers": [u for r, u in self.participants if r == "speaker"],
			"listeners": [u for r, u in self.participants if r == "listener"],
			"turn": self.cfg.turn, "overlap": self.cfg.overlap, "pause": self.cfg.pause,
			"drop_probability": self.cfg.drop_probability, "all_joined": all_joined,
			"memory": self.memory_by_role(), "participants": participants,
		})
		for user in failed:
			print(f"::error::{user} exited with code {participants[user]['exitcode']}", flush=True)
		return 0 if all_joined and not failed and not self.stopped.is_set() else 1


def main() -> int:
	parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
	parser.add_argument("--url", required=True, help="Nextcloud base URL, reachable from the namespaces")
	parser.add_argument("--token", required=True, help="conversation token")
	parser.add_argument("--speakers", type=int, default=2)
	parser.add_argument("--listeners", type=int, default=100)
	parser.add_argument("--password-prefix", default="", help="the password of each user is this prefix + user name")
	parser.add_argument("--audio", required=True, help="audio file that the speakers loop")
	parser.add_argument("--minutes", type=float, default=4, help="length of the speaking schedule")
	parser.add_argument("--turn", type=float, default=20, help="seconds of one speaking turn")
	parser.add_argument("--overlap", type=float, default=5, help="seconds in which two turns overlap")
	parser.add_argument("--pause", type=float, default=10, help="seconds of silence after every second turn")
	parser.add_argument("--start-delay", type=float, default=START_DELAY,
		help="seconds between READY and the first speaking window")
	parser.add_argument("--drop-probability", type=float, default=0.15)
	parser.add_argument("--seed", help="seed of the drops, default derived from the token")
	parser.add_argument("--out", required=True, type=Path, help="directory for the records")
	parser.add_argument("--uid", type=int, required=True, help="user id that the participants run as")
	parser.add_argument("--gid", type=int, required=True, help="group id that the participants run as")
	args = parser.parse_args()
	if os.geteuid() != 0:
		parser.error("needs root to enter the network namespaces")
	if not 0 <= args.overlap < args.turn:
		parser.error("--overlap must be shorter than --turn")
	if args.speakers < 1 or args.listeners < 0:
		parser.error("needs at least one speaker")

	args.out.mkdir(parents=True, exist_ok=True)
	os.chown(args.out, args.uid, args.gid)
	cfg = Config(url=args.url, token=args.token, password_prefix=args.password_prefix, audio=args.audio,
		speakers=[speaker_user(i) for i in range(args.speakers)], duration=args.minutes * 60, turn=args.turn,
		overlap=args.overlap, pause=args.pause, drop_probability=args.drop_probability,
		seed=args.seed or hashlib.sha256(args.token.encode()).hexdigest()[:16], out=args.out)
	return Orchestrator(args, cfg).run()


if __name__ == "__main__":
	sys.exit(main())
