#!/usr/bin/env python3
#
# SPDX-FileCopyrightText: 2026 Nextcloud GmbH and Nextcloud contributors
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Check the records of a stress_call.py run: delivery, fairness, timeliness, attribution and reconnects.

Reads <user>.jsonl, schedule.json and summary.json from DIR, prints a report and one
::error:: line per failed check, writes DIR/report.json and exits 1 if a check failed.

  participants  every participant joined and exited with code 0, nobody lost its
                signaling connection without a planned drop
  delivery      every listener got a final transcript and at least --min-per-speaker
                transcripts of each speaker
  fairness      among the listeners that never dropped, (max - min) / max of the
                transcript count is below --max-gap
  timeliness    in each speaking window, every listener connected at its start gets the
                first transcript of that speaker within --first-latency seconds, and no
                transcript of a speaker arrives later than --tail-latency seconds after
                the end of that speaker's window
  attribution   while only speaker A speaks, no transcript is attributed to speaker B,
                and every transcript names a speaker
  reconnects    every participant that dropped rejoined, a listener gets transcripts
                again in the first window after its rejoin, and a speaker is transcribed
                again in its first window after its rejoin
  content       in each speaking window that was heard, the transcripts of its speaker
                contain at least one of the expected words of the recording it played
                (audio/clips.json), whatever language the window before had

Usage: stress_check.py DIR [--min-per-speaker 5] [--max-gap 0.10] [--first-latency 8] [--tail-latency 8]
                           [--clips audio/clips.json]
"""

import argparse
import json
import math
import sys
import unicodedata
from collections import Counter
from pathlib import Path

# the profiles of stress_call.py that cut the network for a while
NETWORK_OUTAGE = {"blip": True, "outage": True}


def words(text: str) -> set[str]:
	"""The words of a text, compared without case, punctuation and the spelling variants that
	the model and the reference texts use in Devanagari: nukta, and candrabindu for anusvara.
	"""
	text = unicodedata.normalize("NFD", text.lower()).replace("\u093c", "").replace("\u0901", "\u0902")
	text = unicodedata.normalize("NFC", text)
	return set("".join(c if c.isalnum() or unicodedata.category(c).startswith("M") else " " for c in text).split())


def load(directory: Path) -> tuple[dict, list[dict], dict[str, list[dict]]]:
	summary = json.loads((directory / "summary.json").read_text())
	windows = json.loads((directory / "schedule.json").read_text())
	records = {}
	for user in summary["speakers"] + summary["listeners"]:
		path = directory / f"{user}.jsonl"
		lines = path.read_text().splitlines() if path.exists() else []
		records[user] = [json.loads(line) for line in lines if line.strip()]
	return summary, windows, records


def connected_intervals(events: list[dict], end: float) -> list[tuple[float, float]]:
	"""The times in which the participant was in the call and reachable.

	From "ready" to a drop, a lost signaling connection, the start of a network outage, a failed
	media connection of a speaker, or "left". A speaker's media failure or an outage ends the
	interval even though the session may stay, because the participant hears or sends nothing then.
	"""
	breaks = ("drop", "left", "outage_start", "signaling_lost", "media_failed")
	intervals = []
	since = None
	for e in events:
		if e["event"] == "ready" and since is None:
			since = e["t"]
		elif since is not None and (e["event"] in breaks or e.get("disconnected")):
			intervals.append((since, e["t"]))
			since = None
	if since is not None:
		intervals.append((since, end))
	return intervals


def connected(intervals: list[tuple[float, float]], start: float, end: float) -> bool:
	return any(a <= start and end <= b for a, b in intervals)


def percentile(values: list[float], q: float) -> float | None:
	if not values:
		return None
	ordered = sorted(values)
	return ordered[min(len(ordered) - 1, math.ceil(q * len(ordered)) - 1)]


class Checker:
	def __init__(self, args, summary: dict, windows: list[dict], records: dict[str, list[dict]],
			clips: dict[str, dict] | None = None):
		self.args = args
		self.summary = summary
		self.windows = windows
		self.records = records
		self.clips = clips or {}
		self.start = summary["start"]
		self.end = summary["end"]
		self.speakers = summary["speakers"]
		self.listeners = summary["listeners"]
		self.intervals = {u: connected_intervals(records[u], self.end) for u in records}
		self.transcripts = {u: [e for e in records[u] if e["event"] == "transcript"
			and self.start <= e["t"] <= self.end + args.tail_latency] for u in self.listeners}
		self.failures: list[str] = []
		self.report: dict = {}

	def fail(self, check: str, message: str):
		self.failures.append(f"{check}: {message}")

	def windows_of(self, speaker: str) -> list[dict]:
		return [w for w in self.windows if w["speaker"] == speaker]

	def heard(self, window: dict) -> bool:
		"""The speaker was in the call from the start of the window until its first transcript was due."""
		return connected(self.intervals[window["speaker"]], window["start"],
			window["start"] + self.args.first_latency) and window["start"] + self.args.first_latency <= self.end

	def in_window(self, speaker: str, t: float, tail: float) -> bool:
		return any(w["start"] <= t <= w["end"] + tail for w in self.windows_of(speaker))

	def first_transcript(self, listener: str, speaker: str, window: dict) -> float | None:
		times = [e["t"] for e in self.transcripts[listener]
			if e["speaker"] == speaker and window["start"] <= e["t"] <= window["end"] + self.args.tail_latency]
		return min(times) - window["start"] if times else None

	def check_participants(self):
		participants = self.summary["participants"]
		missing = sorted(u for u, p in participants.items() if not p["joined"])
		failed = sorted(f"{u} ({p['exitcode']})" for u, p in participants.items() if p["exitcode"] != 0)
		# a lost signaling connection is expected only during or right after an outage of its own network
		lost = sorted(u for u, events in self.records.items()
			if any(e["event"] == "signaling_lost" and not self.after_outage(u, e["t"]) for e in events))
		errors = Counter(u for u, events in self.records.items() for e in events if e["event"] == "error")
		self.report["participants"] = {"count": len(participants), "not_joined": missing, "failed": failed,
			"lost_connection": lost, "errors": dict(errors)}
		if missing:
			self.fail("participants", f"{len(missing)} did not join: {' '.join(missing)}")
		if failed:
			self.fail("participants", f"exit codes other than 0: {' '.join(failed)}")
		if lost:
			self.fail("participants", f"lost the signaling connection without an outage of its network: "
				f"{' '.join(lost)}")
		if self.summary.get("stopped_early"):
			self.fail("participants", "stress_call.py was stopped before the end of the schedule")

	def outages(self, user: str) -> list[tuple[float, float]]:
		"""(start, end) of every network outage of a participant."""
		result = []
		start = None
		for e in self.records.get(user, []):
			if e["event"] == "outage_start":
				start = e["t"]
			elif e["event"] == "outage_end" and start is not None:
				result.append((start, e["t"]))
				start = None
		return result

	def after_outage(self, user: str, t: float) -> bool:
		"""Whether t is in an outage of the user, or within the time a dead connection takes to be noticed."""
		return any(start <= t <= end + self.args.detect_seconds for start, end in self.outages(user))

	def check_delivery(self):
		counts = {}
		for listener in self.listeners:
			per_speaker = Counter(e["speaker"] for e in self.transcripts[listener])
			finals = sum(e["final"] for e in self.transcripts[listener])
			counts[listener] = {"total": len(self.transcripts[listener]), "finals": finals,
				**{s: per_speaker[s] for s in self.speakers}}
			short = [f"{s} {per_speaker[s]}" for s in self.speakers if per_speaker[s] < self.args.min_per_speaker]
			if not finals:
				self.fail("delivery", f"{listener} got no final transcript")
			if short:
				self.fail("delivery", f"{listener} got fewer than {self.args.min_per_speaker} transcripts of "
					f"{', '.join(short)}")
		self.report["delivery"] = counts

	def check_fairness(self):
		# listeners on a plain network that never dropped: they must get the same transcripts
		steady = [u for u in self.listeners if self.summary["participants"][u]["joined"]
			and self.summary["participants"][u].get("profile", "plain") == "plain"
			and not any(e["event"] in ("drop", "rejoined", "resumed", "signaling_lost") for e in self.records[u])]
		counts = {u: len(self.transcripts[u]) for u in steady}
		gap = None
		if counts and max(counts.values()):
			gap = (max(counts.values()) - min(counts.values())) / max(counts.values())
		self.report["fairness"] = {"listeners": len(steady), "min": min(counts.values(), default=None),
			"max": max(counts.values(), default=None), "gap": gap}
		if not counts:
			self.report["fairness"]["skipped"] = "every listener dropped once"
		elif gap is None:
			self.fail("fairness", "no listener without a drop received transcripts")
		elif gap >= self.args.max_gap:
			low = min(counts, key=counts.get)
			high = max(counts, key=counts.get)
			self.fail("fairness", f"gap {gap:.1%} between {low} ({counts[low]}) and {high} ({counts[high]})")

	def check_timeliness(self):
		latencies = []
		late_first = []
		for window in self.windows:
			# a speaker that dropped is not speaking, and the participants leave at the end, so a
			# window counts when its speaker was in the call until its first transcript was due
			if not self.heard(window):
				continue
			for listener in self.listeners:
				if not connected(self.intervals[listener], window["start"], window["start"] + self.args.first_latency):
					continue
				latency = self.first_transcript(listener, window["speaker"], window)
				if latency is not None:
					latencies.append(latency)
				if latency is None or latency > self.args.first_latency:
					late_first.append((listener, window, latency))
		backlog = [(u, e) for u in self.listeners for e in self.transcripts[u]
			if e["speaker"] in self.speakers and not self.in_window(e["speaker"], e["t"], self.args.tail_latency)]
		self.report["timeliness"] = {"first_latency_p50": percentile(latencies, 0.5),
			"first_latency_p95": percentile(latencies, 0.95), "first_latency_max": max(latencies, default=None),
			"samples": len(latencies), "late_first": len(late_first), "late_tail": len(backlog)}
		for listener, window, latency in late_first[:20]:
			got = "none" if latency is None else f"after {latency:.1f}s"
			self.fail("timeliness", f"{listener}: first transcript of {window['speaker']} in the window at "
				f"+{window['start'] - self.start:.0f}s {got}, limit {self.args.first_latency:g}s")
		for listener, e in backlog[:20]:
			self.fail("timeliness", f"{listener}: transcript of {e['speaker']} at +{e['t'] - self.start:.1f}s, more "
				f"than {self.args.tail_latency:g}s after its window: {e['text']!r}")
		if len(late_first) > 20 or len(backlog) > 20:
			self.fail("timeliness", f"{len(late_first)} late first transcripts and {len(backlog)} late transcripts "
				"in total, the first 20 of each are listed")

	def check_attribution(self):
		wrong = []
		unknown = []
		for listener in self.listeners:
			for e in self.transcripts[listener]:
				if e["speaker"] not in self.speakers:
					unknown.append((listener, e))
					continue
				for window in self.windows:
					other = window["speaker"]
					if other == e["speaker"] or not window["start"] <= e["t"] <= window["end"]:
						continue
					# a window of A alone, after the tail of B's last window
					if not self.in_window(e["speaker"], e["t"], self.args.tail_latency):
						wrong.append((listener, e, other))
						break
		self.report["attribution"] = {"wrong": len(wrong), "unknown_speaker": len(unknown)}
		for listener, e, other in wrong[:20]:
			self.fail("attribution", f"{listener}: transcript attributed to {e['speaker']} at "
				f"+{e['t'] - self.start:.1f}s while only {other} spoke: {e['text']!r}")
		for listener, e in unknown[:20]:
			self.fail("attribution", f"{listener}: transcript of the unknown session {e.get('session')} at "
				f"+{e['t'] - self.start:.1f}s: {e['text']!r}")

	def check_reconnects(self):
		drops = {}
		for user, events in self.records.items():
			drop = next((e for e in events if e["event"] == "drop"), None)
			if not drop:
				continue
			rejoin = next((e for e in events if e["event"] == "rejoined" and e["t"] > drop["t"]), None)
			entry = drops[user] = {"kind": drop["kind"], "at": round(drop["t"] - self.start, 1),
				"rejoined_after": rejoin and round(rejoin["t"] - drop["t"], 1)}
			if not rejoin:
				self.fail("reconnects", f"{user} dropped ({drop['kind']}) and did not rejoin")
				continue
			if user in self.speakers:
				window = next((w for w in self.windows_of(user) if w["start"] >= rejoin["t"] and self.heard(w)), None)
				if window is None:
					continue
				heard = [u for u in self.listeners if self.first_transcript(u, user, window) is not None]
				entry["next_window_listeners"] = len(heard)
				if not heard:
					self.fail("reconnects", f"{user} rejoined after a {drop['kind']} drop and was not transcribed "
						f"in its next window at +{window['start'] - self.start:.0f}s")
			else:
				window = next((w for w in self.windows if w["start"] >= rejoin["t"] and self.heard(w)), None)
				if window is None:
					continue
				latency = self.first_transcript(user, window["speaker"], window)
				entry["next_window_latency"] = latency
				if latency is None or latency > self.args.first_latency:
					got = "none" if latency is None else f"after {latency:.1f}s"
					self.fail("reconnects", f"{user} rejoined after a {drop['kind']} drop and got the first "
						f"transcript of {window['speaker']} in the window at +{window['start'] - self.start:.0f}s "
						f"{got}")
		self.report["reconnects"] = drops

	def check_content(self):
		"""Each heard window was transcribed as the recording it played, in that recording's language."""
		per_language: dict[str, dict] = {}
		missing = []
		for window in self.windows:
			clip = self.clips.get(window.get("clip", ""))
			if clip is None or not self.heard(window):
				continue
			heard = set()
			until = window["end"] + self.args.tail_latency
			for listener in self.listeners:
				for e in self.transcripts[listener]:
					if e["speaker"] == window["speaker"] and window["start"] <= e["t"] <= until:
						heard |= words(e["text"])
			found = [w for w in clip["expect"] if words(w) <= heard]
			stats = per_language.setdefault(clip["language"], {"windows": 0, "matched": 0})
			stats["windows"] += 1
			stats["matched"] += bool(found)
			if not found:
				missing.append((window, clip))
		self.report["content"] = per_language
		for window, clip in missing:
			self.fail("content", f"{window['speaker']} played {clip['file']} ({clip['language']}) in the window at "
				f"+{window['start'] - self.start:.0f}s, and no transcript of it contains any of {clip['expect']}")

	def first_window_after(self, user: str, t: float) -> dict | None:
		"""The first heard window that starts after t: of the user if a speaker, else of any speaker."""
		candidates = self.windows_of(user) if user in self.speakers else self.windows
		return next((w for w in candidates if w["start"] >= t and self.heard(w)), None)

	def recovered(self, user: str, after: float) -> tuple[bool | None, str]:
		"""Whether the user hears (listener) or is heard (speaker) again in the first window after a time.

		None when no window is left to tell.
		"""
		window = self.first_window_after(user, after)
		if window is None:
			return None, "no window left"
		at = f"+{window['start'] - self.start:.0f}s"
		if user in self.speakers:
			heard = sum(1 for u in self.listeners if self.first_transcript(u, user, window) is not None)
			return heard > 0, f"{heard} listeners got its window at {at}"
		latency = self.first_transcript(user, window["speaker"], window)
		if latency is None:
			return False, f"nothing of {window['speaker']} in the window at {at}"
		return latency <= self.args.first_latency, f"first transcript of the window at {at} after {latency:.1f}s"

	def check_network(self):  # noqa: C901 - one branch for each symptom
		"""Each participant with an impaired network showed its symptom and behaved as expected.

		nat and turnonly: a speaker's media reached the SFU over the expected path, srflx or host
		behind NAT, a TURN relay behind the TURN-only firewall. latency, jitter, lossy and slow: the
		participant stayed connected, and a listener got the transcripts in time, under the same
		limits as everybody. blip and outage: the network was cut; the participant noticed it,
		reconnected (resumed its session or joined with a new one) after the network came back,
		and heard or was heard in the first window after that.
		"""
		per_user = {}
		for user, info in self.summary["participants"].items():
			profile = info.get("profile", "plain")
			if profile == "plain":
				continue
			events = self.records.get(user, [])
			entry = per_user[user] = {"profile": profile, "role": info["role"]}
			paths = [e for e in events if e["event"] == "media_connected"]
			if paths:
				entry["ice"] = sorted({f"{e.get('local')}->{e.get('remote')}" for e in paths})
			if info["role"] == "speaker" and paths:
				local = {e.get("local") for e in paths}
				if profile == "turnonly" and local != {"relay"}:
					self.fail("network", f"{user} is behind a TURN-only firewall, but its media used {entry['ice']}")
				if info.get("kind") == "nat" and "relay" in local:
					self.fail("network", f"{user} is behind a plain NAT, but its media needed TURN: {entry['ice']}")
			elif info["role"] == "speaker" and info["joined"]:
				self.fail("network", f"{user} ({profile}) never got its media connected to the SFU")

			outages = self.outages(user)
			if NETWORK_OUTAGE.get(profile):
				if not outages:
					entry["outage"] = "none, the schedule had no room for it"
					continue
				start, end = outages[0]
				entry["outage"] = f"+{start - self.start:.0f}s for {end - start:.0f}s"
				noticed = next((e for e in events if e["event"] in ("signaling_lost", "media_failed")
					and start <= e["t"] <= end + self.args.detect_seconds), None)
				back = next((e for e in events if e["event"] in ("resumed", "rejoined", "republished")
					and e["t"] >= start), None)
				entry["noticed_after"] = noticed and round(noticed["t"] - start, 1)
				entry["recovery"] = back and ("resumed" if back["event"] == "resumed" else "new session")
				entry["back_after_heal"] = back and round(back["t"] - end, 1)
				if noticed is None and end - start > self.args.detect_seconds:
					self.fail("network", f"{user}: the network was cut for {end - start:.0f}s at "
						f"+{start - self.start:.0f}s and the participant did not notice")
				if noticed is not None and back is None:
					self.fail("network", f"{user}: lost its connection in the outage at +{start - self.start:.0f}s "
						"and never got back")
					continue
				if back is not None and back["t"] - end > self.args.recovery_seconds:
					self.fail("network", f"{user}: back {back['t'] - end:.0f}s after the network came back, "
						f"limit {self.args.recovery_seconds:g}s")
				ok, how = self.recovered(user, max(end, back["t"] if back else end))
				entry["next_window"] = how
				if ok is False:
					self.fail("network", f"{user}: after the outage at +{start - self.start:.0f}s: {how}")
			elif any(e["event"] == "signaling_lost" for e in events):
				entry["signaling_lost"] = sum(1 for e in events if e["event"] == "signaling_lost")
				self.fail("network", f"{user} ({profile}) lost its signaling connection without an outage")
		self.report["network"] = per_user

	def run(self) -> int:
		for check in (self.check_participants, self.check_delivery, self.check_fairness, self.check_timeliness,
				self.check_attribution, self.check_reconnects, self.check_content, self.check_network):
			check()
		self.report["memory"] = self.summary.get("memory")
		self.report["failures"] = self.failures
		self.print_report()
		return 1 if self.failures else 0

	def print_report(self):
		r = self.report
		print(f"{len(self.speakers)} speakers, {len(self.listeners)} listeners, "
			f"{len(self.windows)} speaking windows in {self.end - self.start:.0f}s")
		p = r["participants"]
		print(f"participants: {p['count'] - len(p['not_joined'])}/{p['count']} joined, {len(p['failed'])} failed, "
			f"{sum(p['errors'].values())} error events")
		totals = [c["total"] for c in r["delivery"].values()]
		if totals:
			print(f"delivery: {min(totals)} to {max(totals)} transcripts per listener, "
				f"{sum(totals)} in total")
		for speaker in self.speakers:
			counts = [c[speaker] for c in r["delivery"].values()]
			if counts:
				print(f"  {speaker}: {min(counts)} to {max(counts)} transcripts per listener")
		f = r["fairness"]
		if f.get("skipped"):
			print(f"fairness: skipped, {f['skipped']}")
		else:
			gap = "n/a" if f["gap"] is None else f"{f['gap']:.1%}"
			print(f"fairness: {f['listeners']} listeners without a drop, {f['min']} to {f['max']} transcripts, "
				f"gap {gap}")
		t = r["timeliness"]

		def seconds(value):
			return "n/a" if value is None else f"{value:.2f}s"

		print(f"timeliness: first transcript p50 {seconds(t['first_latency_p50'])}, p95 "
			f"{seconds(t['first_latency_p95'])}, max {seconds(t['first_latency_max'])} over {t['samples']} "
			f"windows and listeners, {t['late_first']} late, {t['late_tail']} after the tail")
		a = r["attribution"]
		print(f"attribution: {a['wrong']} attributed to the wrong speaker, {a['unknown_speaker']} to an unknown one")
		print(f"reconnects: {len(r['reconnects'])} drops")
		for user, d in sorted(r["reconnects"].items()):
			print(f"  {user}: {d['kind']} at +{d['at']}s, rejoined after {d['rejoined_after']}s, "
				+ ", ".join(f"{k} {v}" for k, v in d.items() if k.startswith("next_window")))
		if r.get("content"):
			print("content: " + ", ".join(f"{lang} {c['matched']}/{c['windows']}"
				for lang, c in sorted(r["content"].items())) + " windows with the expected words")
		if r.get("network"):
			print(f"network: {len(r['network'])} participants with an impaired network")
			for user, n in sorted(r["network"].items(), key=lambda x: (x[1]["profile"], x[0])):
				print(f"  {user} ({n['role']}, {n['profile']}): " + ", ".join(f"{k} {v}" for k, v in n.items()
					if k not in ("profile", "role")))
		for role, m in (r["memory"] or {}).items():
			print(f"memory of the {role}s: max RSS {m.get('max_rss_kib', 0) / 1024:.1f} MiB, "
				f"max PSS {m.get('max_pss_kib', 0) / 1024:.1f} MiB, "
				f"PSS of all {m.get('sum_pss_kib', 0) / 1024:.0f} MiB")
		for failure in self.failures:
			print(f"::error::{failure}")
		print("FAILED" if self.failures else "PASSED")


def main() -> int:
	parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
	parser.add_argument("dir", type=Path, help="the --out directory of stress_call.py")
	parser.add_argument("--min-per-speaker", type=int, default=5, help="transcripts per speaker for each listener")
	parser.add_argument("--max-gap", type=float, default=0.10, help="largest fairness gap among steady listeners")
	parser.add_argument("--first-latency", type=float, default=8,
		help="seconds from the start of a window to its first transcript")
	parser.add_argument("--tail-latency", type=float, default=8,
		help="seconds after the end of a window in which its transcripts may still arrive")
	parser.add_argument("--clips", type=Path, default=Path(__file__).parent / "audio" / "clips.json",
		help="manifest of the recordings, with the expected words of each")
	parser.add_argument("--detect-seconds", type=float, default=45,
		help="seconds after an outage in which a participant may still notice its dead connection")
	parser.add_argument("--recovery-seconds", type=float, default=40,
		help="seconds after the network came back in which a participant must be back in the call")
	args = parser.parse_args()

	clips = {c["file"]: c for c in json.loads(args.clips.read_text(encoding="utf-8"))["clips"]}
	checker = Checker(args, *load(args.dir), clips=clips)
	code = checker.run()
	(args.dir / "report.json").write_text(json.dumps(checker.report, indent=1) + "\n")
	return code


if __name__ == "__main__":
	sys.exit(main())
