import asyncio
import base64
import concurrent.futures
import hashlib
import hmac
import json
import os
import pprint
import random
from enum import Enum, IntEnum
from threading import Event

import httpx
from aiortc import RTCPeerConnection, RTCSessionDescription
# from aiortc.contrib.media import MediaRecorder
from aiortc.rtcconfiguration import RTCConfiguration, RTCIceServer
from aiortc.sdp import candidate_from_sdp
from av.audio.resampler import AudioResampler
from dotenv import load_dotenv
from nc_py_api.ex_app import persistent_storage
from print_color import print
from pydantic import BaseModel
from vosk import KaldiRecognizer, Model
from websockets import connect
from websockets.exceptions import ConnectionClosedOK

load_dotenv()

class CALL_FLAG(IntEnum):
	DISCONNECTED = 0
	IN_CALL      = 1
	WITH_AUDIO   = 2
	WITH_VIDEO   = 4
	WITH_PHONE   = 8


class WaitFor(Enum):
	HELLO        = None
	PARTICIPANTS = None
	OFFER        = None
	CANDIDATE    = None
	MUTEUNMUTE   = None


class Target(BaseModel):
	session_id: str
	raw_message: dict
	in_call: bool = False
	muted: bool = True


targets: dict[str, Target] = {}
app_started = Event()


def hmac_sha256(key, message):
	return hmac.new(
		key.encode("utf-8"),
		message.encode("utf-8"),
		hashlib.sha256
	).hexdigest()


class WebSocketSignaling:
	def __init__(self, websocketURL, backendURL, secret):
		self._websopcketURL = websocketURL
		self._backendURL = backendURL
		self.id = 0
		self.secret = secret
		self._server = None
		self.resumeid = None
		self.sessionid = None

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
		await self.send_incall()

	async def send_message(self, message: dict):
		self.id += 1
		message['id'] = str(self.id)
		print('Message sent:', tag='sent_message', color='green')
		pprint.pprint(message)
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

	async def send_join(self, roomToken):
		await self.send_message({"type":"room","room":{"roomid":roomToken,"sessionid":self.sessionid}})

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
		await self._server.close()
		self._server = None

	async def receive(self):
		if not self._server:
			return None
		message = json.loads(await self._server.recv())
		print('Message received:', tag='received_message', color='purple')
		pprint.pprint(message)
		return message


vosk_interface = os.environ.get('VOSK_SERVER_INTERFACE', '0.0.0.0')
vosk_port = int(os.environ.get('VOSK_SERVER_PORT', 2700))
vosk_model_path = os.environ.get('VOSK_MODEL_PATH', persistent_storage())
vosk_dump_file = os.environ.get('VOSK_DUMP_FILE', None)

model = Model(vosk_model_path)
pool = concurrent.futures.ThreadPoolExecutor((os.cpu_count() or 1))
dump_fd = None if vosk_dump_file is None else open(vosk_dump_file, "wb")


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


class VoskTranscriber:
	def __init__(self, user_connection):
		self.__resampler = AudioResampler(format='s16', layout='mono', rate=48000)
		self.__pc = user_connection
		self.__audio_task = None
		self.__track = None
		self.__channel = None
		self.__recognizer = KaldiRecognizer(model, 48000)


	async def set_audio_track(self, track):
		self.__track = track

	async def set_text_channel(self, channel):
		self.__channel = channel

	async def start(self):
		self.__audio_task = asyncio.create_task(self.__run_audio_xfer())

	async def stop(self):
		if self.__audio_task is not None:
			self.__audio_task.cancel()
			self.__audio_task = None

	async def __run_audio_xfer(self):
		loop = asyncio.get_running_loop()

		max_frames = 20
		frames = []
		while True:
			fr = await self.__track.recv()
			frames.append(fr)

			# We need to collect frames so we don't send partial results too often
			if len(frames) < max_frames:
				continue

			dataframes = bytearray(b'')
			for fr in frames:
				for rfr in self.__resampler.resample(fr):
					dataframes += bytes(rfr.planes[0])[:rfr.samples * 2]
			frames.clear()

			if dump_fd is not None:
				dump_fd.write(bytes(dataframes))

			result = await loop.run_in_executor(pool, process_chunk, self.__recognizer, bytes(dataframes))
			print(result)
			# self.__channel.send(result)


async def run(pc, signaling, transcriber):
	@pc.on("track")
	async def on_track(track):
		if track.kind == "audio":
			print("Receiving %s" % track.kind, tag='track', color='magenta')
			await transcriber.set_audio_track(track)

		@track.on("ended")
		async def on_ended():
			print("Track ended", tag='track', color='magenta')
			await transcriber.stop()

	@pc.on('connectionstatechange')
	async def on_connectionstatechange():
		print('Connection state change:', pc.connectionState, tag='connection', color='blue')
		if pc.connectionState == 'connected':
			print('Peers successfully connected', tag='connection', color='blue')

	@pc.on('icegatheringstatechange')
	async def on_icegatheringstatechange():
		print('ICE gathering state changed to', pc.iceGatheringState, tag='ice', color='blue')
		if pc.iceGatheringState == 'complete':
			print('All ICE candidates have been gathered.', tag='ice', color='blue')

	#if role == "offer":
	#	# send offer
	#	add_tracks()
	#pc.createDataChannel('transcript')
	#await pc.setLocalDescription(await pc.createOffer())
	#await signaling.send(pc.localDescription)

	await signaling.connect()
	await signaling.send_join('i6h8x8q9')
	pc.addTransceiver('audio', direction='recvonly')

	print('Connected to signaling server')

	while True:
		if not app_started.is_set():
			break

		try:
			message = await signaling.receive()
		except ConnectionClosedOK:
			print('Connection closed', tag='connection', color='blue')
			break

		if message['type'] == 'event' and message['event']['target'] == 'participants' and message['event']['type'] == 'update':
			print('New participants update!', tag='participants', color='blue')
			for user_description in message['event']['update']['users']:
				if user_description['inCall'] & CALL_FLAG.IN_CALL and user_description['inCall'] & CALL_FLAG.WITH_AUDIO:
					print('User join with audio', user_description, tag='participants', color='blue')
					targets[user_description['sessionId']] = Target(
						session_id=user_description['sessionId'],
						raw_message=user_description,
						in_call=True,
						muted=True
					)
					await signaling.send_offer_request(user_description['sessionId'])

		if message['type'] == 'message' and message['message']['data']['type'] == 'offer':
			# offer_sid = message['message']['data']['sid']
			print('Got offer from', message['message']['sender']['sessionid'], tag='offer', color='blue')
			targets[message['message']['sender']['sessionid']].in_call = True
			await pc.setRemoteDescription(RTCSessionDescription(type='offer', sdp=message['message']['data']['payload']['sdp']))

			answer = await pc.createAnswer()
			await pc.setLocalDescription(answer)
			await signaling.send_offer_answer(message['message']['data']['from'], message['message']['data']['sid'], answer.sdp)

			local_sdp = pc.localDescription.sdp
			# print('local sdp:', local_sdp, tag='sdp', color='blue')

			for line in local_sdp.splitlines():
				if line.startswith("a=candidate:"):
					await signaling.send_candidate(
						message['message']['sender']['sessionid'],
						message['message']['data']['sid'],
						line[2:],
					)

			await transcriber.start()


		if message['type'] == 'message' and message['message']['data']['type'] == 'candidate':
			print('Got candidate', tag='candidate', color='blue')
			candidate = candidate_from_sdp(message['message']['data']['payload']['candidate']['candidate'])
			candidate.sdpMid = message['message']['data']['payload']['candidate']['sdpMid']
			candidate.sdpMLineIndex = message['message']['data']['payload']['candidate']['sdpMLineIndex']
			await pc.addIceCandidate(candidate)


if __name__ == "__main__":
	# https://nuage.minifox.fr/call/i6h8x8q9

	res = httpx.get('https://nuage.minifox.fr/ocs/v2.php/apps/spreed/api/v3/signaling/settings',
		headers={
			'OCS-APIRequest': 'true',
			'Authorization': 'Bearer ' + base64.b64encode(f'{os.environ['NC_USER']}:{os.environ['NC_PASS']}'.encode()).decode(),
		},
		params={
			'token': 'i6h8x8q9',
			'format': 'json',
		},
	)
	res.raise_for_status()

	stun_servers = res.json()['ocs']['data']['stunservers'][0]
	turn_servers = res.json()['ocs']['data']['turnservers'][0]
	print('STUN servers:', stun_servers, tag='servers', color='magenta')
	print('TURN servers:', turn_servers, tag='servers', color='magenta')

	# create signaling and peer connection
	app_started.set()
	signaling = WebSocketSignaling(
		'wss://hpb.pluton.minifox.fr/standalone-signaling/spreed',
		'https://nuage.minifox.fr/ocs/v2.php/apps/spreed/api/v3/signaling/backend',
		os.environ["SIGNALLING_SECRET"],
	)

	ice_servers = RTCConfiguration(
		iceServers=[
			RTCIceServer(urls=stun_servers['urls']),
			RTCIceServer(
				urls=turn_servers['urls'],
				username=turn_servers['username'],
				credential=turn_servers['credential'],
			),
		],
	)
	pc = RTCPeerConnection(configuration=ice_servers)
	# recorder = MediaRecorder("/home/tyrell/nextcloud-docker-dev/workspace/live-transcription-tests/output.ogg")
	transcriber = VoskTranscriber(pc)

	# run event loop
	loop = asyncio.get_event_loop()
	try:
		loop.run_until_complete(
			run(
				pc=pc,
				signaling=signaling,
				transcriber=transcriber,
			)
		)
	except KeyboardInterrupt:
		pass
	finally:
		# cleanup
		app_started.clear()
		loop.run_until_complete(transcriber.stop())
		loop.run_until_complete(signaling.close())
		loop.run_until_complete(pc.close())
