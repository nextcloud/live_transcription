"""Service module."""

import asyncio
import concurrent
import hashlib
import hmac
import json
import os
import pprint
import random
from collections.abc import Callable
from enum import IntEnum
from functools import partial
from traceback import print_exc
from typing import Any, Generator
from urllib.parse import urlparse

from aiortc import AudioStreamTrack, RTCPeerConnection, RTCSessionDescription
from aiortc.rtcconfiguration import RTCConfiguration, RTCIceServer
from aiortc.sdp import candidate_from_sdp
from av.audio.resampler import AudioResampler
from av.frame import Frame
from dotenv import load_dotenv
from livetypes import HPBSettings, StreamEndedException
from nc_py_api import AsyncNextcloudApp, NextcloudApp
from nc_py_api.ex_app import persistent_storage
from print_color import print
from vosk import KaldiRecognizer, Model
from websockets import connect
from websockets.exceptions import ConnectionClosedOK

load_dotenv()
# os.environ['VOSK_MODEL_PATH'] = persistent_storage()

class CALL_FLAG(IntEnum):
	DISCONNECTED = 0
	IN_CALL      = 1
	WITH_AUDIO   = 2
	WITH_VIDEO   = 4
	WITH_PHONE   = 8


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
		auth_token: str,
		hpb_location: str,
		hpb_settings: HPBSettings,
		stream_listener: Callable[[str, Any], None], # user_id, stream
	) -> None:
		self.id = 0
		self._server = None
		self._monitor = None
		self.peer_connections: dict[str, PeerConnection] = {}

		self.resumeid = None
		self.sessionid = None

		nc = NextcloudApp()
		self._websopcketURL = os.environ["LT_HPB_URL"]
		self._backendURL = nc.app_cfg.endpoint + '/ocs/v2.php/apps/spreed/api/v3/signaling/backend'
		self.secret = auth_token

		self.room_token = room_token
		self.hpb_location = hpb_location
		self.hpb_settings = hpb_settings
		self.stream_listener = stream_listener

	async def connect(self):
		self._server = await connect(self._websopcketURL)
		await self.send_hello()
		while True:
			message = await self.receive()
			if message['type'] == 'welcome':
				continue
			if message['type'] == 'hello':
				self.sessionid = message['hello']['sessionid']
				self.resumeid = message['hello']['resumeid']
				break
		self._monitor = asyncio.create_task(self.signalling_monitor())
		await self.send_incall()
		await self.send_join()
		print('Connected to signaling server')

	async def send_message(self, message: dict):
		self.id += 1
		message['id'] = str(self.id)
		# print('Message sent:', tag='sent_message', color='green', flush=True)
		# pprint.pprint(message)
		await self._server.send(json.dumps(message))

	async def send_hello(self):
		nonce = '' + str(random.random()) + '' + str(random.random())+ '' + str(random.random())
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
			'type': 'internal',
			'internal': {
				'type': 'incall',
				'incall': {
					'incall': 1, # PARTICIPANT.CALL_FLAG.IN_CALL
				},
			},
		})

	async def send_join(self):
		await self.send_message({"type":"room","room":{"roomid": self.room_token,"sessionid": self.sessionid}})

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
					# "sessionid": self.sessionid,
					"sessionid": sender,
				},
				"data": {
					# "to": self.sessionid,
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

	async def close(self):
		await self.send_message({
			"type": "bye",
			"bye": {},
		})
		if self._monitor:
			self._monitor.cancel()
			self._monitor = None
		await self._server.close()
		self._server = None

	async def receive(self):
		if not self._server:
			return None
		message = json.loads(await self._server.recv())
		# print('Message received:', tag='received_message', color='purple')
		# pprint.pprint(message)
		return message

	async def signalling_monitor(self):
		"""Monitor the signaling server for incoming messages."""
		while True:
			# if not app_started.is_set():
			# 	break

			try:
				message = await self.receive()
			except ConnectionClosedOK:
				print('Connection closed', tag='connection', color='blue', flush=True)
				# break

			if message['type'] == 'event' and message['event']['target'] == 'participants' and message['event']['type'] == 'update':
				print('New participants update!', tag='participants', color='blue')
				for user_description in message['event']['update']['users']:
					if user_description['inCall'] & CALL_FLAG.IN_CALL and user_description['inCall'] & CALL_FLAG.WITH_AUDIO:
						print('User join with audio', user_description, tag='participants', color='blue')
						# targets[user_description['sessionId']] = Target(
						# 	session_id=user_description['sessionId'],
						# 	raw_message=user_description,
						# 	in_call=True,
						# 	muted=True
						# )
						await self.send_offer_request(user_description['sessionId'])

			if message['type'] == 'message' and message['message']['data']['type'] == 'offer':
				print('Got offer from', message['message']['sender']['sessionid'], tag='offer', color='blue')
				await self.handle_offer(message)

			if message['type'] == 'message' and message['message']['data']['type'] == 'candidate':
				print('Got candidate', tag='candidate', color='blue')
				candidate = candidate_from_sdp(message['message']['data']['payload']['candidate']['candidate'])
				candidate.sdpMid = message['message']['data']['payload']['candidate']['sdpMid']
				candidate.sdpMLineIndex = message['message']['data']['payload']['candidate']['sdpMLineIndex']
				await self.peer_connections[message['message']['sender']['sessionid']].pc.addIceCandidate(candidate)

			# todo: handle bye message

	async def handle_offer(self, message):
		"""Handle incoming offer messages."""
		print('Got offer from', message['message']['sender']['sessionid'], tag='offer', color='blue')
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
		pc.addTransceiver('audio', direction='recvonly')
		@pc.on("track")
		async def on_track(track):
			if track.kind == "audio":
				print("Receiving %s" % track.kind, tag='track', color='magenta')
				stream = AudioStream(track)
				self.stream_listener(message['message']['sender']['sessionid'], 'dummy', stream, self.room_token)
		self.peer_connections[message['message']['sender']['sessionid']] = PeerConnection(
			# todo
			user_id='dummy',
			session_id=message['message']['sender']['sessionid'],
			pc=pc,
		)

		await pc.setRemoteDescription(RTCSessionDescription(type='offer', sdp=message['message']['data']['payload']['sdp']))

		answer = await pc.createAnswer()
		await pc.setLocalDescription(answer)
		await self.send_offer_answer(message['message']['data']['from'], message['message']['data']['sid'], answer.sdp)

		local_sdp = pc.localDescription.sdp
		# print('local sdp:', local_sdp, tag='sdp', color='blue')

		for line in local_sdp.splitlines():
			if line.startswith("a=candidate:"):
				await self.send_candidate(
					message['message']['sender']['sessionid'],
					message['message']['data']['sid'],
					line[2:],
				)


class PeerConnection:
	def __init__(self, user_id: str, session_id: str, pc: RTCPeerConnection):
		self.user_id = user_id
		self.session_id = session_id
		self.pc = pc


class AudioStream:
	def __init__(self, track: AudioStreamTrack):
		self.track = track
		self._ended = False

		@track.on("ended")
		async def on_ended():
			print("Track ended", tag='track', color='magenta')
			self._ended = True

	# async def receive(self) -> Generator[Frame]:
	async def receive(self) -> Frame:
		"""Receive the next audio frame."""
		if self._ended:
			raise StreamEndedException("Track has ended")
		# yield await self.track.recv()
		return await self.track.recv()

	def stop(self):
		"""Stop the audio stream."""
		self.track.stop()


def process_chunk(rec, message):
	try:
		res = rec.AcceptWaveform(message)
	except Exception:
		result = None
	else:
		if res > 0:
			result = rec.Result()
		else:
			result = rec.PartialResult()
	return result


MODEL_MAP = {
	"en": "vosk-model-en-us-0.22",
	"de": "vosk-model-de-0.21",
	"hi": "vosk-model-hi-0.22",
}
vosk_pool = concurrent.futures.ThreadPoolExecutor((os.cpu_count() or 1))

class VoskTranscriber:
	def __init__(self, language: str):
		self.__resampler = AudioResampler(format='s16', layout='mono', rate=48000)
		self.__audio_task: dict[str, asyncio.Task] = None
		# todo
		# model = Model(model_name=MODEL_MAP[language])
		model = Model('../../persistent_storage/vosk-model-en-us-0.22')
		self.__recognizer = KaldiRecognizer(model, 48000)

	async def start(self, session_id: str, stream: AudioStream, text_listener: Callable[[str], None]):
		self.__audio_task[session_id] = asyncio.create_task(self.__run_audio_xfer(stream, text_listener))
		self.__audio_task[session_id].add_done_callback(partial(self.stop, session_id))

	async def stop(self, session_id: str):
		print('Stopping audio task', tag='vosk', color='red', flush=True)
		if self.__audio_task[session_id] is not None:
			self.__audio_task[session_id].cancel()
			self.__audio_task[session_id] = None

	async def __run_audio_xfer(self, stream: AudioStream, text_listener: Callable[[str], None]):
		loop = asyncio.get_running_loop()

		max_frames = 20
		frames = []
		try:
			# async for fr in stream.receive():
			while True:
				fr = await stream.receive()
				frames.append(fr)

				# We need to collect frames so we don't send partial results too often
				if len(frames) < max_frames:
					continue

				dataframes = bytearray(b'')
				for fr in frames:
					for rfr in self.__resampler.resample(fr):
						dataframes += bytes(rfr.planes[0])[:rfr.samples * 2]
				frames.clear()

				result = await loop.run_in_executor(vosk_pool, process_chunk, self.__recognizer, bytes(dataframes))
				print(result, tag='vosk', color='green')
				# todo
				asyncio.create_task(text_listener(result))
		except StreamEndedException:
			print("Stream ended", tag='stream', color='red')
		except Exception as e:
			print("Error in transcriber", e, tag='transcriber', color='red')
			print_exc()


class Application:
	spreedClients: dict[str, SpreedClient] = {}
	transcribers: dict[str, VoskTranscriber] = {}

	def __init__(self, hpb_settings: HPBSettings):
		self.hpb_settings = hpb_settings

	async def join_call(self, room_token: str, auth_token: str, hpb_location: str) -> None:
		"""Join a call."""
		print(f"Joining call with room token: {room_token}, auth token: {auth_token}, hpb location: {hpb_location}")
		# todo
		self.spreedClients[room_token] = SpreedClient(room_token, auth_token, hpb_location, self.hpb_settings, self.stream_listener)
		await self.spreedClients[room_token].connect()

	async def send_chat_msg(self, room_token: str, message: str) -> None:
		# todo: doesn't work
		nc = AsyncNextcloudApp()
		nc.set_user("admin")
		await nc.talk.send_message(message, room_token, actor_display_name="Transcriber")

		# import base64

		# params = {
		# 	"message": message,
		# 	"actorDisplayName": "transcriber",
		# 	"referenceId": hashlib.sha256("7a5de6443be1b885dbad887fad026aa2dbb3df299928ac2bad66bf9bacb62920".encode("UTF-8")).hexdigest(),
		# 	"format": "json",
		# }
		# r = await self.client.post(f"{os.environ["NEXTCLOUD_URL2"]}/ocs/v2.php/apps/spreed/api/v1/chat/{room_token}", json=params, headers={
		# 	"OCS-APIRequest": "true",
		# 	# "Accept": "application/json",
		# 	"content-type": "application/json",
		# 	'Authorization': 'Basic ' + base64.b64encode(f'{os.environ['NC_USER']}:{os.environ['NC_PASS']}'.encode()).decode(),
		# })
		# if r.status_code // 100 != 2:
		# 	print(f"Error sending chat message: {r.text}", tag='chat', color='red')
		# else:
		# 	print(f"Chat message sent: {message}", tag='chat', color='green')

	def stream_listener(self, session_id: str, user_id: str, stream: AudioStream, room_token: str) -> None:
		"""Handle incoming audio stream."""
		print(f"Received audio stream from {user_id}")
		language = "en"  # TODO: Get language from stream/ocs
		if language not in self.transcribers:
			self.transcribers[language] = VoskTranscriber(language)
		asyncio.run_coroutine_threadsafe(
			self.transcribers[language].start(session_id, stream, partial(self.send_chat_msg, room_token)),
			asyncio.get_event_loop(),
		)
		print(f"Started transcriber for {user_id} in {language}")

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
