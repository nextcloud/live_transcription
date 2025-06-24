"""Service module."""

import asyncio
import concurrent
import hashlib
import hmac
import json
import os
import pprint
import threading
from collections.abc import Callable, Coroutine
from enum import IntEnum
from functools import partial
from secrets import token_urlsafe
from threading import RLock
from traceback import print_exc
from typing import Any
from urllib.parse import urlparse

from aiortc import AudioStreamTrack, RTCPeerConnection, RTCSessionDescription
from aiortc.mediastreams import MediaStreamError
from aiortc.rtcconfiguration import RTCConfiguration, RTCIceServer
from aiortc.sdp import candidate_from_sdp
from av.audio.frame import AudioFrame
from av.audio.resampler import AudioResampler
from av.frame import Frame
from dotenv import load_dotenv
from livetypes import HPBSettings, StreamEndedException, Target, TranscribeRequest
from nc_py_api import NextcloudApp
from nc_py_api.ex_app import persistent_storage
from print_color import print
from vosk import KaldiRecognizer, Model
from websockets import ClientConnection, connect
from websockets.exceptions import ConnectionClosedOK

load_dotenv()
# os.environ['VOSK_MODEL_PATH'] = persistent_storage()

MSG_RECEIVE_TIMEOUT = 10  # seconds
MAX_CONNECT_TRIES = 5  # maximum number of connection attempts

class CALL_FLAG(IntEnum):
	DISCONNECTED = 0
	IN_CALL      = 1
	WITH_AUDIO   = 2
	WITH_VIDEO   = 4
	WITH_PHONE   = 8


class ConnectResult(IntEnum):
	SUCCESS = 0
	FAILURE = 1  # do not retry
	RETRY   = 2


def hmac_sha256(key, message):
	return hmac.new(
		key.encode("utf-8"),
		message.encode("utf-8"),
		hashlib.sha256
	).hexdigest()


class SpreedClient:
	def __init__(
		self,
		room_token: str,
		# transcript_queue: asyncio.Queue,
		hpb_settings: HPBSettings,
		stream_listener: Callable[[str, Any, str], None], # session_id, stream, room_token
	) -> None:
		self.id = 0
		self._server: ClientConnection | None = None
		self._monitor: asyncio.Task | None = None
		self.peer_connections: dict[str, PeerConnection] = {}
		self.targets: dict[str, Target] = {}
		self.target_lock = RLock()
		# self.transcript_queue = transcript_queue

		self.resumeid = None
		self.sessionid = None

		nc = NextcloudApp()
		self._websopcket_url = os.environ["LT_HPB_URL"]
		self._backendURL = nc.app_cfg.endpoint + "/ocs/v2.php/apps/spreed/api/v3/signaling/backend"
		self.secret = os.environ["LT_INTERNAL_SECRET"]

		self.room_token = room_token
		self.hpb_settings = hpb_settings
		self.stream_listener = stream_listener


	async def connect(self) -> ConnectResult:
		nc = NextcloudApp()
		websopcket_host = urlparse(self._websopcket_url).hostname
		if nc.app_cfg.options.nc_cert is False:
			self._server = await connect(self._websopcket_url)
		else:
			self._server = await connect(
				self._websopcket_url,
				server_hostname=websopcket_host,
				ssl=nc.app_cfg.options.nc_cert,
			)

		await self.send_hello()

		msg_counter = 0
		while True:
			message = await self.receive(MSG_RECEIVE_TIMEOUT)
			if message is None:
				print(f"No message received for {MSG_RECEIVE_TIMEOUT} secs, aborting...", tag="connection", color="red")
				return ConnectResult.FAILURE

			if message.get("type") == "error":
				print(
					"Error message received:", message.get("error", {}).get("message"),
					"\nError details:", message.get("error", {}).get("details"),
					tag="connection",
					color="red",
				)
				return ConnectResult.FAILURE

			if message.get("type") == "bye":
				print("Received bye message, closing connection", tag="connection", color="blue")
				return ConnectResult.FAILURE

			if message.get("type") == "welcome":
				continue

			if message.get("type") == "hello":
				self.sessionid = message["hello"]["sessionid"]
				self.resumeid = message["hello"]["resumeid"]
				break

			if msg_counter > 10:
				print("Too many messages received without 'welcome', reconnecting...", tag="connection", color="red")
				return ConnectResult.RETRY

		self._monitor = asyncio.create_task(self.signalling_monitor())
		# self._transcript_sender = asyncio.create_task(self.transcript_sender())
		await self.send_incall()
		await self.send_join()
		print("Connected to signaling server")
		return ConnectResult.SUCCESS

	async def send_message(self, message: dict):
		if not self._server:
			print("No server connection, cannot send message", tag="connection", color="red")
			return

		self.id += 1
		message["id"] = str(self.id)
		# print('Message sent:', tag='sent_message', color='green', flush=True)
		# pprint.pprint(message)
		await self._server.send(json.dumps(message))

	async def send_hello(self):
		nonce = token_urlsafe(64)
		await self.send_message({
			"type": "hello",
			"hello": {
				"version": "2.0",
				"auth": {
					"type": "internal",
					"params": {
						"random": nonce,
						"token": hmac_sha256(self.secret, nonce),
						"backend": self._backendURL,
					}
				},
			},
		})

	async def send_incall(self):
		await self.send_message({
			"type": "internal",
			"internal": {
				"type": "incall",
				"incall": {
					"incall": CALL_FLAG.IN_CALL,
				},
			},
		})

	async def send_join(self):
		await self.send_message({
			"type": "room",
			"room": {
				"roomid": self.room_token,
				"sessionid": self.sessionid
			}
		})

	async def send_offer_request(self, publisher_session_id):
		await self.send_message({
			"type": "message",
			"message": {
				"recipient": {
					"type": "session",
					"sessionid": publisher_session_id
				},
				"data": {
					"type": "requestoffer",
					"roomType": "video"
				}
			}
		})

	async def send_offer_answer(self, publisher_session_id, offer_sid, sdp):
		await self.send_message({
			"type": "message",
			"message": {
				"recipient": {
					"type": "session",
					"sessionid": publisher_session_id
				},
				"data": {
					"to": publisher_session_id,
					"type": "answer",
					"roomType": "video",
					"sid": offer_sid,
					"payload": {
						"nick": "I am the big transcriber",
						"type": "answer",
						"sdp": sdp
					}
				}
			}
		})

	async def send_candidate(self, sender, offer_sid, candidate_str):
		await self.send_message({
			"type": "message",
			"message": {
				"recipient": {
					"type": "session",
					"sessionid": sender,
				},
				"data": {
					"to": sender,
					"type": "candidate",
					"sid": offer_sid,
					"roomType": "video",
					"payload": {
						"candidate": {
							"candidate": candidate_str,
							"sdpMLineIndex": 0,
							"sdpMid": "0",
						}
					}
				}
			}
		})

	async def send_bye(self):
		await self.send_message({
			"type": "bye",
			"bye": {}
		})

	async def send_transcript(self, message: str, spk_session_id: str):
		# todo
		print("speaker session id:", spk_session_id, tag="vosk", color="green")
		with self.target_lock:
			sids = list(self.targets.keys())
		for sid in sids:
			await self.send_message({
				"type": "message",
				"message": {
					"recipient": {
						"type": "session",
						"sessionid": sid,
					},
					"data": {
						"message": message,
						"type": "transcript",
						"speakerSessionId": spk_session_id,
					}
				}
			})

	async def close(self, using_resume: bool = False):
		await self.send_bye()

		if not using_resume:
			for pc in self.peer_connections.values():
				if pc.pc.connectionState != "closed" and pc.pc.connectionState != "failed":
					print(f"Closing peer connection for session {pc.session_id}", tag="connection", color="blue")
					await pc.pc.close()
			self.peer_connections.clear()
			self.resumeid = None
			self.sessionid = None

		if self._monitor and not self._monitor.done():
			print("Cancelling monitor task", tag="monitor", color="red")
			# Cancel the monitor task if it's still running
			self._monitor.cancel()
			self._monitor = None

		if self._server:
			print("Closing server connection", tag="connection", color="red")
			# Close the WebSocket connection if it's still open
			await self._server.close()
			self._server = None

	async def receive(self, timeout: int = 0) -> dict | None:
		if not self._server:
			return None

		# todo: handle exceptions
		if timeout > 0:
			try:
				received_msg = await asyncio.wait_for(self._server.recv(), timeout)
			except TimeoutError:
				print("Timeout while waiting for message", tag="timeout", color="red")
				return None
		else:
			received_msg = await self._server.recv()

		message = json.loads(received_msg)
		# todo: debug message
		# todo: add logger_config.yaml
		print("Message received:", tag="received_message", color="purple")
		pprint.pprint(message)
		return message

	def add_target(self, session_id: str):
		with self.target_lock:
			if session_id not in self.targets:
				self.targets[session_id] = Target()
			else:
				print(f"Target '{session_id}' already exists", tag="target", color="yellow")

	def remove_target(self, session_id: str):
		# todo: start timeout for leaving the call here
		with self.target_lock:
			if session_id in self.targets:
				del self.targets[session_id]
			else:
				print(f"Target '{session_id}' does not exist", tag="target", color="yellow")

	def is_target(self, session_id: str) -> bool:
		with self.target_lock:
			return session_id in self.targets

	async def signalling_monitor(self):
		"""Monitor the signaling server for incoming messages."""
		while True:
			# if not app_started.is_set():
			# 	break

			try:
				message = await self.receive()
			except ConnectionClosedOK:
				print("Connection closed", tag="connection", color="blue", flush=True)
				# break

			if message.get("type") == "error":
				print(
					"Error message received:", message.get("error", {}).get("message"),
					"\nError details:", message.get("error", {}).get("details"),
					tag="connection",
					color="red",
				)
				asyncio.get_event_loop().call_soon_threadsafe(self.close)
				return

			if (
				message["type"] == "event"
				and message["event"]["target"] == "participants"
				and message["event"]["type"] == "update"
			):
				print("Participants update!", tag="participants", color="blue")
				if message["event"]["update"].get("all") and message["event"]["update"].get("incall") == 0:
					print("Call ended for everyone, closing connection", tag="participants", color="red")
					asyncio.get_event_loop().call_soon_threadsafe(self.close)
					return

				users_update = message["event"]["update"].get("users", [])
				if not users_update:
					continue

				for user_desc in users_update:
					if (user_desc["inCall"] & CALL_FLAG.IN_CALL and user_desc["inCall"] & CALL_FLAG.WITH_AUDIO):
						print("User join with audio", user_desc, tag="participants", color="blue")
						await self.send_offer_request(user_desc["sessionId"])
						continue

				# the last user just left the call, live_transcription is the only one left
				if (len(users_update) == 2):
					if (
						users_update[0].get("sessionId") != self.sessionid
						and users_update[1].get("sessionId") != self.sessionid
					):
						# false alarm, we are not the only one left
						continue

					transcriber_index = 0 if users_update[0].get("sessionId") == self.sessionid else 1
					if (
						users_update[transcriber_index].get("inCall") & CALL_FLAG.IN_CALL
						and users_update[transcriber_index^1].get("inCall") == CALL_FLAG.DISCONNECTED
					):
						asyncio.get_event_loop().call_soon_threadsafe(self.close)
						return

			if message["type"] == "message" and message["message"]["data"]["type"] == "offer":
				print("Got offer from", message["message"]["sender"]["sessionid"], tag="offer", color="blue")
				await self.handle_offer(message)
				continue

			if message["type"] == "message" and message["message"]["data"]["type"] == "candidate":
				print("Got candidate", tag="candidate", color="blue")
				candidate = candidate_from_sdp(message["message"]["data"]["payload"]["candidate"]["candidate"])
				candidate.sdpMid = message["message"]["data"]["payload"]["candidate"]["sdpMid"]
				candidate.sdpMLineIndex = message["message"]["data"]["payload"]["candidate"]["sdpMLineIndex"]
				await self.peer_connections[message["message"]["sender"]["sessionid"]].pc.addIceCandidate(candidate)
				continue

			if message["type"] == "bye":
				print("Received bye message, closing connection", tag="bye", color="blue")
				asyncio.get_event_loop().call_soon_threadsafe(self.close)

	async def handle_offer(self, message):
		"""Handle incoming offer messages."""
		print("Got offer from", message["message"]["sender"]["sessionid"], tag="offer", color="blue")
		# targets[message['message']['sender']['sessionid']].in_call = True

		ice_servers = RTCConfiguration(
			iceServers=[
				RTCIceServer(urls=self.hpb_settings.stunservers[0].urls),
				RTCIceServer(
					urls=self.hpb_settings.turnservers[0].urls,
					username=self.hpb_settings.turnservers[0].username,
					credential=self.hpb_settings.turnservers[0].credential,
				),
			],
		)
		pc = RTCPeerConnection(configuration=ice_servers)
		pc.addTransceiver("audio", direction="recvonly")
		@pc.on("track")
		async def on_track(track):
			if track.kind == "audio":
				print(f"Receiving {track.kind}", tag="track", color="magenta")
				stream = AudioStream(track)
				self.stream_listener(message["message"]["sender"]["sessionid"], stream, self.room_token)
		self.peer_connections[message["message"]["sender"]["sessionid"]] = PeerConnection(
			session_id=message["message"]["sender"]["sessionid"],
			pc=pc,
		)

		await pc.setRemoteDescription(
			RTCSessionDescription(type="offer", sdp=message["message"]["data"]["payload"]["sdp"])
		)

		answer = await pc.createAnswer()
		await pc.setLocalDescription(answer)
		await self.send_offer_answer(message["message"]["data"]["from"], message["message"]["data"]["sid"], answer.sdp)

		local_sdp = pc.localDescription.sdp
		# todo
		# print('local sdp:', local_sdp, tag='sdp', color='blue')

		for line in local_sdp.splitlines():
			if line.startswith("a=candidate:"):
				await self.send_candidate(
					message["message"]["sender"]["sessionid"],
					message["message"]["data"]["sid"],
					line[2:],
				)


class PeerConnection:
	def __init__(self, session_id: str, pc: RTCPeerConnection):
		self.session_id = session_id
		self.pc = pc


class AudioStream:
	def __init__(self, track: AudioStreamTrack):
		self.track = track
		self._ended = False

		@track.on("ended")
		async def on_ended():
			print("Track ended", tag="track", color="magenta")
			self._ended = True

	async def receive(self) -> AudioFrame:
		"""Receive the next audio frame."""
		if self._ended:
			raise StreamEndedException("Track has ended")
		return await self.track.recv()  # type: ignore[return-value]

	def stop(self):
		"""Stop the audio stream."""
		self.track.stop()


MODEL_MAP = {
	"en": "vosk-model-en-us-0.22",
	"de": "vosk-model-de-0.21",
	"hi": "vosk-model-hi-0.22",
}
vosk_pool = concurrent.futures.ThreadPoolExecutor(os.cpu_count() or 1)
vosk_lock = threading.Lock()

def process_chunk(recognizer: KaldiRecognizer, chunk: bytes) -> str | None:
	try:
		with vosk_lock:
			res = recognizer.AcceptWaveform(chunk)
			if res > 0:
				# todo
				# return recognizer.FinalResult()
				return recognizer.Result()
			return recognizer.PartialResult()
			# return None
	except Exception as e:
		print("Error processing chunk", e, tag="vosk", color="red")
		return None


class VoskTranscriber:
	def __init__(self, language: str):
		self.__text_listener_tasks: set[asyncio.Task] = set()
		self.__resampler = AudioResampler(format="s16", layout="mono", rate=48000)
		self.__audio_task: dict[str, asyncio.Task] = {}
		# todo
		# model = Model(model_name=MODEL_MAP[language])
		model = Model("../../persistent_storage/vosk-model-en-us-0.22-lgraph")
		self.__recognizer = KaldiRecognizer(model, 48000)

	async def start(
		self,
		session_id: str,
		stream: AudioStream,
		text_listener: Callable[[str], Coroutine[Any, Any, None]]
	):
		self.__audio_task[session_id] = asyncio.create_task(self.__run_audio_xfer(stream, text_listener))
		self.__audio_task[session_id].add_done_callback(partial(self.stop, stream, session_id))

	def stop(self, stream: AudioStream, session_id: str, future: asyncio.Future):
		print("Stopping audio task for session_id: " + session_id, tag="vosk", color="red", flush=True)
		if self.__audio_task[session_id] is not None:
			stream.stop()
			future.cancel("Cancelling audio task in VoskTranscriber for session_id: " + session_id)
			self.__audio_task[session_id].cancel()
			del self.__audio_task[session_id]

	async def __run_audio_xfer(self, stream: AudioStream, text_listener: Callable[[str], Coroutine[Any, Any, None]]):
		loop = asyncio.get_running_loop()

		max_frames = 20
		frames = []
		try:
			while True:
				fr = await stream.receive()
				frames.append(fr)

				# We need to collect frames so we don't send partial results too often
				if len(frames) < max_frames:
					continue

				dataframes = bytearray(b"")
				for fr in frames:
					for rfr in self.__resampler.resample(fr):
						dataframes += bytes(rfr.planes[0])[:rfr.samples * 2]
				frames.clear()

				result = await loop.run_in_executor(vosk_pool, process_chunk, self.__recognizer, bytes(dataframes))

				if not result:
					continue

				# todo
				print(result, tag="vosk", color="green")

				try:
					json_msg = json.loads(result)
				except json.JSONDecodeError:
					print("Error decoding JSON message:", result, tag="vosk", color="red")
					continue

				message = json_msg.get("text", "")
				if message == "":
					continue

				task: asyncio.Task = asyncio.create_task(text_listener(message))
				self.__text_listener_tasks.add(task)
				task.add_done_callback(self.__text_listener_tasks.discard)
		except StreamEndedException:
			print("Stream ended", tag="stream", color="red")
		except MediaStreamError:
			print("Audio stream is not live", tag="stream", color="red")
		except Exception as e:
			print("Error in transcriber", e, tag="transcriber", color="red")
			print_exc()


class Application:
	def __init__(self, hpb_settings: HPBSettings):
		self.hpb_settings = hpb_settings
		self.spreed_clients: dict[str, SpreedClient] = {}
		self.transcribers: dict[str, VoskTranscriber] = {}
		self.transcript_queue: asyncio.Queue = asyncio.Queue()
		self.queue_consumers: set[asyncio.Task] = set()

	async def transcript_req(self, req: TranscribeRequest) -> None:
		if req.roomToken in self.spreed_clients:
			print(f"Already in call with room token: {req.roomToken}, adding target {req.sessionId}")
			if req.enable:
				self.spreed_clients[req.roomToken].add_target(req.sessionId)
			else:
				self.spreed_clients[req.roomToken].remove_target(req.sessionId)
			return

		if not req.enable:
			print(
				f"Received request to turn off transcription for room token: {req.roomToken}, "
				"session id: {req.sessionId}\nbut no call is active. Ignoring request.",
			)
			return

		# todo: join if not already in call
		print(f"Joining call with room token: {req.roomToken}, req.sessionId: {req.sessionId}")
		# todo
		# self.transcript_queues[req.roomToken] = asyncio.Queue()
		self.spreed_clients[req.roomToken] = SpreedClient(
			req.roomToken,
			# self.transcript_queues[req.roomToken],
			self.hpb_settings,
			self.stream_listener,
			# todo: verify if this works
			# partial(self.exit_room, req.roomToken),  # type: ignore[arg-type]
		)
		self.spreed_clients[req.roomToken].add_target(req.sessionId)

		task: asyncio.Task = asyncio.create_task(self.queue_consumer())
		self.queue_consumers.add(task)
		task.add_done_callback(self.queue_consumers.discard)

		tries = MAX_CONNECT_TRIES
		while tries > 0:
			try:
				conn_result = await self.spreed_clients[req.roomToken].connect()
				match conn_result:
					case ConnectResult.SUCCESS:
						print(
							f"Connected to signaling server for room token: {req.roomToken}",
							tag="connection",
							color="green",
						)
						return
					case ConnectResult.FAILURE:
						# do not retry
						print(
							f"(try: {MAX_CONNECT_TRIES + 1 - tries}) "
							f"Failed to connect to signaling server for room token: {req.roomToken}",
							tag="connection",
							color="red",
						)
						await self.spreed_clients[req.roomToken].close()
						del self.spreed_clients[req.roomToken]
						return
					case ConnectResult.RETRY:
						print(
							f"(try: {MAX_CONNECT_TRIES + 1 - tries}) "
							f"Retrying connection to signaling server for room token: {req.roomToken}",
							tag="connection",
							color="yellow",
						)
						await self.spreed_clients[req.roomToken].close(using_resume=True)
				tries -= 1
				await asyncio.sleep(2)
			except Exception as e:
				print(
					f"(try: {MAX_CONNECT_TRIES + 1 - tries}) "
					f"Error connecting to signaling server for room token {req.roomToken}: {e}",
					tag="connection",
					color="red",
				)
				tries -= 1
				await asyncio.sleep(2)

		print(f"Failed to connect after {MAX_CONNECT_TRIES} attempts, giving up", tag="connection", color="red")

	async def on_transcript(self, room_token: str, spk_session_id: str, message: str):
		sclient = self.spreed_clients.get(room_token)
		if sclient is None:
			print(f"Error: no SpreedClient for room token {room_token}", tag="vosk", color="red")
			return
		await self.transcript_queue.put(sclient.send_transcript(message, spk_session_id))

	async def queue_consumer(self):
		while True:
			# todo: make this scale
			coroutine = await self.transcript_queue.get()
			if coroutine is None:
				# our task is done
				break

			try:
				await asyncio.wait_for(coroutine, timeout=10)
			except TimeoutError:
				print("Timeout waiting for transcript sender", tag="vosk", color="red")
				continue
			except Exception as e:
				print("Error sending transcript in room ", self.room_token, e, tag="vosk", color="red")
				continue

	def stream_listener(self, session_id: str, stream: AudioStream, room_token: str) -> None:
		"""Handle incoming audio stream."""
		print(f"Received audio stream from {session_id}")
		language = "en"  # TODO: Get language from stream/ocs
		if language not in self.transcribers:
			self.transcribers[language] = VoskTranscriber(language)
		asyncio.run_coroutine_threadsafe(
			self.transcribers[language].start(session_id, stream, partial(self.on_transcript, room_token, session_id)),
			asyncio.get_event_loop(),
		)
		print(f"Started transcriber for {session_id} in {language}")

def check_hpb_env_vars():
	# Check if the required environment variables are set
	required_vars = ("LT_HPB_URL", "LT_INTERNAL_SECRET")
	missing_vars = [var for var in required_vars if not os.getenv(var)]
	if missing_vars:
		raise ValueError(f"Missing environment variables: {', '.join(missing_vars)}")

	hpb_url = os.environ["LT_HPB_URL"]
	hpb_url_host = urlparse(hpb_url).hostname
	if not hpb_url_host:
		raise ValueError(f"Invalid HPB URL: {hpb_url}")


def get_hpb_settings() -> HPBSettings:
	try:
		nc = NextcloudApp()
		settings = nc.ocs("GET", "/ocs/v2.php/apps/spreed/api/v3/signaling/settings")
		return HPBSettings(**settings)
	except Exception as e:
		print_exc()
		raise Exception("Error getting HPB settings") from e
