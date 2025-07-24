"""Service module."""

import asyncio
import dataclasses
import hashlib
import hmac
import json
import os
import pprint
import ssl
import threading
from contextlib import suppress
from enum import IntEnum
from functools import partial
from secrets import token_urlsafe
from traceback import print_exc
from urllib.parse import urlparse

from aiortc import AudioStreamTrack, RTCPeerConnection, RTCSessionDescription
from aiortc.mediastreams import MediaStreamError
from aiortc.rtcconfiguration import RTCConfiguration, RTCIceServer
from aiortc.sdp import candidate_from_sdp
from av.audio.frame import AudioFrame
from av.audio.resampler import AudioResampler
from dotenv import load_dotenv
from livetypes import (
	HPBSettings,
	LanguageSetRequest,
	SpreedClientException,
	StreamEndedException,
	Target,
	TranscribeRequest,
)
from nc_py_api import NextcloudApp
from print_color import print
from websockets import ClientConnection, connect
from websockets.exceptions import ConnectionClosed, WebSocketException

load_dotenv()

MSG_RECEIVE_TIMEOUT = 10  # seconds
MAX_CONNECT_TRIES = 5  # maximum number of connection attempts
MAX_AUDIO_FRAMES = 20  # maximum number of audio frames to collect before sending to Vosk


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


def get_ssl_context(server_addr: str) -> ssl.SSLContext | None:
	nc = NextcloudApp()

	if server_addr.startswith("ws://"):
		print("Using default SSL context for insecure WebSocket connection (ws://)", tag="connection", color="blue")
		return None

	if os.environ.get("SKIP_CERT_VERIFY", "false").lower() == "true":
		print("Skipping certificate verification for WebSocket connection", tag="connection", color="blue")
		ssl_ctx = ssl.SSLContext()
		ssl_ctx.check_hostname = False
		ssl_ctx.verify_mode = ssl.CERT_NONE
		return ssl_ctx

	if nc.app_cfg.options.nc_cert and isinstance(nc.app_cfg.options.nc_cert, ssl.SSLContext):
		# Use the SSL context provided by nc_py_api
		print("Using SSL context provided by nc_py_api", tag="connection", color="blue")
		return nc.app_cfg.options.nc_cert

	# verify certificate normally and don't use SSLContext from nc_py_api
	print("Using default SSL context for WebSocket connection", tag="connection", color="blue")
	return None


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
			print("Track ended", tag="track", color="blue")
			self._ended = True

	async def receive(self) -> AudioFrame:
		"""Receive the next audio frame."""
		if self._ended:
			raise StreamEndedException("Track has ended")
		return await self.track.recv()  # type: ignore[return-value]

	def stop(self):
		"""Stop the audio stream."""
		self.track.stop()


# data carrier in the transcript_queue
@dataclasses.dataclass
class Transcript:
	message: str
	speaker_session_id: str


class SpreedClient:
	def __init__(
		self,
		room_token: str,
		hpb_settings: HPBSettings,
		lang_id: str,
	) -> None:
		self.id = 0
		self._server: ClientConnection | None = None
		self._monitor: asyncio.Task | None = None
		self.peer_connections: dict[str, PeerConnection] = {}
		self.targets: dict[str, Target] = {}
		self.target_lock = threading.RLock()
		self.transcript_queue = asyncio.Queue()
		self.transcriber: VoskTranscriber | None = None

		self.resumeid = None
		self.sessionid = None

		nc = NextcloudApp()
		self._websopcket_url = os.environ["LT_HPB_URL"]
		self._backendURL = nc.app_cfg.endpoint + "/ocs/v2.php/apps/spreed/api/v3/signaling/backend"
		self.secret = os.environ["LT_INTERNAL_SECRET"]

		self.room_token = room_token
		self.hpb_settings = hpb_settings
		self.lang_id = lang_id


	async def connect(self) -> ConnectResult:
		websopcket_host = urlparse(self._websopcket_url).hostname
		ssl_ctx = get_ssl_context(self._websopcket_url)
		self._server = await connect(
			self._websopcket_url,
			**({
				"server_hostname": websopcket_host,
				"ssl": ssl_ctx,  # type: ignore[arg-type]
			} if ssl_ctx else {}),
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

		# connect to the Vosk server
		try:
			self.transcriber = VoskTranscriber(self.lang_id, self.transcript_queue)
			await self.transcriber.connect()
		except Exception as e:
			print(
				f'Error connecting to the Vosk server at "{os.getenv("LT_VOSK_SERVER_URL", "ws://localhost:2702")}". '
				'Cannot continue further.', e, tag="vosk", color="red"
			)
			raise SpreedClientException(
				'Error connecting to the Vosk transcription server. '
				'Please check for the logs of that service. '
				f'It was expected to be available at "{os.getenv("LT_VOSK_SERVER_URL", "ws://localhost:2702")}"'
			) from e

		self._monitor = asyncio.create_task(self.signalling_monitor())
		self._transcript_sender = asyncio.create_task(self.transcipt_queue_consumer())

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
		await self._server.send(json.dumps(message))
		# print("Message sent:", tag="sent_message", color="green", flush=True)
		# pprint.pprint(message)

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

	# todo: add function to reconnect to hpb, full SpreedClient lifecycle
	async def close(self, using_resume: bool = False):
		with suppress(Exception):
			await self.send_bye()

		if not using_resume:
			for pc in self.peer_connections.values():
				if pc.pc.connectionState != "closed" and pc.pc.connectionState != "failed":
					print(f"Closing peer connection for session {pc.session_id}", tag="connection", color="blue")
					with suppress(Exception):
						await pc.pc.close()
			self.peer_connections.clear()
			self.resumeid = None
			self.sessionid = None

		with suppress(Exception):
			if self._monitor and not self._monitor.done():
				print("Cancelling monitor task", tag="monitor", color="blue")
				# Cancel the monitor task if it's still running
				self._monitor.cancel()
				self._monitor = None

			if self._transcript_sender and not self._transcript_sender.done():
				print("Cancelling transcript consumer task", tag="consumer", color="blue")
				self._transcript_sender.cancel()
				self._transcript_sender = None

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
		# print("Message received:", tag="received_message", color="purple")
		# pprint.pprint(message)
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
			except ConnectionClosed as e:
				print("HPB websocket connection closed:", e, tag="connection", color="blue", flush=True)
				# todo: retry connection?
				# break
			except WebSocketException as e:
				print("HPB websocket error:", e, tag="connection", color="red", flush=True)
				await self.close(using_resume=True)
				break

			if message.get("type") == "error":
				print(
					"Error message received:", message.get("error", {}).get("message"),
					"\nError details:", message.get("error", {}).get("details"),
					tag="connection",
					color="red",
				)
				# todo: only close if the error is not recoverable
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

		ice_servers = []
		for stunserver in self.hpb_settings.stunservers:
			ice_servers.append(
				RTCIceServer(urls=stunserver.urls)
			)
		for turnserver in self.hpb_settings.turnservers:
			ice_servers.append(
				RTCIceServer(
					urls=turnserver.urls,
					username=turnserver.username,
					credential=turnserver.credential,
				)
			)
		if len(ice_servers) == 0:
			ice_servers = None
		rtc_config = RTCConfiguration(iceServers=ice_servers)
		pc = RTCPeerConnection(configuration=rtc_config)

		pc.addTransceiver("audio", direction="recvonly")
		@pc.on("track")
		async def on_track(track):
			if track.kind == "audio":
				print(
					f"Receiving {track.kind} from {message["message"]["sender"]["sessionid"]}",
					tag="track",
					color="blue",
				)
				stream = AudioStream(track)
				if self.transcriber:
					await self.transcriber.start(self.sessionid, stream)
					print(f'Started transcriber for "{self.sessionid}" in "{self.lang_id}"', tag="offer", color="blue")
				else:
					print("Transcriber is not initialized, cannot start transcribing", tag="offer", color="red")
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

	def set_language(self, lang_id: str):
		self.lang_id = lang_id

	async def transcipt_queue_consumer(self):
		"""Consume transcripts from the queue and send them to the server."""
		print("Starting the transcript queue consumer", tag="transcript", color="blue")
		while True:
			transcript: Transcript = await self.transcript_queue.get()
			if transcript is None:
				print("Received None in transcript queue, stopping consumer", tag="transcript", color="blue")
				break

			# todo
			print(
				"Speaker session id:", transcript.speaker_session_id,
				tag="speaker_session_id",
				color="green",
			)
			try:
				await asyncio.wait_for(
					self.send_transcript(
						message=transcript.message,
						spk_session_id=transcript.speaker_session_id,
					),
					timeout=10,
				)
			except TimeoutError:
				print("Timeout while sending transcript", tag="transcript", color="red")
				continue
			except Exception as e:
				print(f"Error while sending transcript in room {self.room_token}: {e}", tag="transcript", color="red")
				continue


class VoskTranscriber:
	def __init__(self, language: str, transcript_queue: asyncio.Queue):
		self.__resampler = AudioResampler(format="s16", layout="mono", rate=48000)
		self.__audio_task: dict[str, asyncio.Task] = {}
		self.__language = language
		self.__server_url = os.environ.get("LT_VOSK_SERVER_URL", "ws://localhost:2702")
		self.__voskcon: ClientConnection | None = None
		self.__voskcon_lock = asyncio.Lock()
		self.__transcript_queue = transcript_queue

	async def connect(self):
		ssl_ctx = get_ssl_context(self.__server_url)
		async with self.__voskcon_lock:
			self.__voskcon: ClientConnection | None = await connect(
				self.__server_url,
				*({
					"server_hostname": urlparse(self.__server_url).hostname,
					"ssl": ssl_ctx,
				} if ssl_ctx else {})
			)
			await self.__voskcon.send(
				json.dumps({
					"config": {
						"sample_rate": 48000,
						"language": self.__language,
						# "show_words": True,
					}
				})
			)

	async def start(
		self,
		session_id: str,
		stream: AudioStream,
	):
		if session_id in self.__audio_task:
			self.stop(stream, session_id, self.__audio_task[session_id])
		self.__audio_task[session_id] = asyncio.create_task(self.__run_audio_xfer(stream, session_id))
		self.__audio_task[session_id].add_done_callback(partial(self.stop, stream, session_id))

	def stop(self, stream: AudioStream, session_id: str, future: asyncio.Future):
		if session_id in self.__audio_task:
			print("Stopping audio task for session_id: " + session_id, tag="vosk", color="blue", flush=True)
			stream.stop()
			future.cancel("Cancelling audio task in VoskTranscriber for session_id: " + session_id)
			self.__audio_task[session_id].cancel()
			del self.__audio_task[session_id]

	async def set_language(self, language: str) -> bool:
		if not self.__voskcon:
			print("Vosk connection is not established, cannot switch language", tag="vosk", color="red")
			return False
		if self.__language == language:
			print(f"Language is already set to {language}, no need to switch", tag="vosk", color="blue")
			return True

		print(f"Switching Vosk language from {self.__language} to {language}", tag="vosk", color="blue")
		self.__language = language
		async with self.__voskcon_lock:
			await self.__voskcon.send(
				json.dumps({
					"config": {
						"language": self.__language,
					}
				})
			)
			response = None
			max_received_msgs = MAX_CONNECT_TRIES
			while (not response or "success" not in response) and max_received_msgs > 0:
				response = await self.__voskcon.recv()
				max_received_msgs -= 1
		if not response or "success" not in response:
			print(
				"Expected 'success' in response from Vosk server after switching language, but got:",
				response, tag="vosk", color="red",
			)
			if max_received_msgs <= 0:
				print(
					"Max received messages limit reached while waiting for Vosk server response",
					tag="vosk",
					color="red",
				)
			return False
		print("Response from Vosk server after switching language:", response, tag="vosk", color="blue")
		try:
			json_res = json.loads(response)
		except json.JSONDecodeError:
			print("Error decoding JSON response from Vosk server after switching language", tag="vosk", color="red")
			return False
		print("Language switched successfully", tag="vosk", color="green")
		return json_res.get("success", False)

	async def __run_audio_xfer(self, stream: AudioStream, speaker_session_id: str):
		frames = []
		try:
			# todo
			from time import perf_counter
			while True:
				fr = await stream.receive()
				# todo
				start = perf_counter()
				frames.append(fr)

				# We need to collect frames so we don't send partial results too often
				if len(frames) < MAX_AUDIO_FRAMES:
					continue

				dataframes = bytearray(b"")
				for fr in frames:
					for rfr in self.__resampler.resample(fr):
						dataframes += bytes(rfr.planes[0])[:rfr.samples * 2]
				frames.clear()

				async with self.__voskcon_lock:
					await self.__voskcon.send(bytes(dataframes))
					result = await self.__voskcon.recv()
				end = perf_counter()

				if not result:
					continue

				# todo
				print(result, tag=f"vosk {end - start:.2f}", color="green")

				try:
					json_msg = json.loads(result)
				except json.JSONDecodeError:
					print("Error decoding JSON message:", result, tag="vosk", color="red")
					continue

				message = json_msg.get("text", "")
				if message == "":
					continue

				self.__transcript_queue.put_nowait(Transcript(
					message=message,
					speaker_session_id=speaker_session_id,
				))
		except StreamEndedException:
			print("Stream ended", tag="stream", color="red")
		except MediaStreamError:
			print("Audio stream is not live", tag="stream", color="red")
		except Exception as e:
			print("Error in transcriber", e, tag="transcriber", color="red")
			print_exc()
		finally:
			print("Closing Vosk connection", tag="vosk", color="blue")
			await self.__voskcon.send('{"eof" : 1}')
			await self.__voskcon.close()
			self.__voskcon = None
			print("VoskTranscriber stopped", tag="vosk", color="green")


class Application:
	def __init__(self) -> None:
		self.hpb_settings = get_hpb_settings()
		self.spreed_clients: dict[str, SpreedClient] = {}
		self.spreed_clients_lock = threading.Lock()

	async def transcript_req(self, req: TranscribeRequest) -> None:
		with self.spreed_clients_lock:
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

		print(f"Joining call with room token: {req.roomToken}, req.sessionId: {req.sessionId}")
		with self.spreed_clients_lock:
			self.spreed_clients[req.roomToken] = SpreedClient(
				req.roomToken,
				self.hpb_settings,
				req.langId,
			)
		self.spreed_clients[req.roomToken].add_target(req.sessionId)

		tries = MAX_CONNECT_TRIES
		last_exc = None
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
				last_exc = e
				await asyncio.sleep(2)

		print(f"Failed to connect after {MAX_CONNECT_TRIES} attempts, giving up", tag="connection", color="red")
		raise SpreedClientException(
			f"Failed to connect to signaling server for room token {req.roomToken} after {MAX_CONNECT_TRIES} attempts"
		) from last_exc

	async def set_call_language(self, req: LanguageSetRequest) -> bool:
		if req.roomToken not in self.spreed_clients:
			print(f"No SpreedClient for room token {req.roomToken}, cannot set language", tag="vosk", color="red")
			return False

		self.spreed_clients_lock.acquire()
		try:
			spreed_client = self.spreed_clients[req.roomToken]
			spreed_client.set_language(req.langId)
			return await spreed_client.transcriber.set_language(req.langId)
		except Exception as e:
			print(f"Error setting language for room token {req.roomToken}: {e}", tag="vosk", color="red")
			return False
		finally:
			self.spreed_clients_lock.release()

	async def leave_room(self, room_token: str):
		# todo
		...


def check_hpb_env_vars():
	# Check if the required environment variables are set
	required_vars = ("LT_HPB_URL", "LT_INTERNAL_SECRET")
	missing_vars = [var for var in required_vars if not os.getenv(var)]
	if missing_vars:
		raise ValueError(f"Missing environment variables: {', '.join(missing_vars)}")

	hpb_url = os.environ["LT_HPB_URL"]
	hpb_url_host = urlparse(hpb_url).hostname
	if not hpb_url_host:
		raise ValueError(
			f"Could not detect hostname in LT_HPB_URL env var: {hpb_url}. "
			"Verify that it is a valid URL with a protocol and hostname."
		)

	vosk_url = os.environ.get("LT_VOSK_SERVER_URL")
	if vosk_url:
		vosk_url_parsed = urlparse(vosk_url)
		vosk_host = vosk_url_parsed.hostname
		if not vosk_host:
			raise ValueError(
				f"Could not detect hostname in LT_VOSK_SERVER_URL: {vosk_url}. "
				"Verify that it is a valid URL with a protocol and hostname."
			)
		try:
			_ = vosk_url_parsed.port
		except ValueError as e:
			raise ValueError(f"Invalid VOSK server URL: {vosk_url}") from e


def get_hpb_settings() -> HPBSettings:
	check_hpb_env_vars()
	try:
		nc = NextcloudApp()
		settings = nc.ocs("GET", "/ocs/v2.php/apps/spreed/api/v3/signaling/settings")
		return HPBSettings(**settings)
	except Exception as e:
		raise Exception("Error getting HPB settings") from e
