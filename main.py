import asyncio
import datetime
import hashlib
import hmac
import json
import math
import pprint
import random
from contextlib import suppress
from enum import Enum, IntEnum
from threading import Event

import cv2
import numpy
from aiortc import RTCPeerConnection, RTCSessionDescription, VideoStreamTrack
from aiortc.contrib.media import MediaBlackhole, MediaPlayer, MediaRecorder
from aiortc.contrib.signaling import BYE, TcpSocketSignaling, add_signaling_arguments, create_signaling
from aiortc.sdp import candidate_from_sdp
from av import VideoFrame
from print_color import print
from pydantic import BaseModel
from websockets import connect


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
					"roomType": "video" # todo: check audio
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

	async def send_candidate(self, offer_sid, candiate_str):
		await self.send_message({
			"type": "message",
			"message": {
				"recipient": {
					"type": "session",
					"sessionid": self.sessionid,
				},
				"data": {
					"to": self.sessionid,
					"type": "candidate",
					"sid": offer_sid,
					"roomType": "video-or-screen",
					"payload": {
						"candidate": {
							"candiate": candiate_str,
							"sdpMLineIndex": 0,
							"sdpMid": "0"
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

	async def receive(self, timeout):
		if not self._server:
			return None

		try:
			json_msg = asyncio.wait_for(self._server.recv(), timeout)
		except asyncio.TimeoutError:
			print('Timeout waiting for message', tag='timeout', color='red')
			return None

		message = json.loads(json_msg)
		print('Message received:', tag='received_message', color='purple')
		pprint.pprint(message)
		return message

	async def wait_for(self, event, timeout=30):
		match event:
			case WaitFor.HELLO:
				message = await self.receive(timeout)
				if not message:
					print('Error receiving response for hello', tag='timeout', color='red')
					return None
				if message['type'] == 'hello':
					self.sessionid = message['hello']['sessionid']
					self.resumeid = message['hello']['resumeid']
					return self.sessionid

			case WaitFor.PARTICIPANTS:
				message = await self.receive(timeout)
				if not message:
					print('Error receiving response for participants in room', tag='timeout', color='red')
					return None
				if message['type'] == 'event' and message['event']['target'] == 'participants' and message['event']['type'] == 'update':
					return message

			case WaitFor.OFFER:
				message = await self.receive(timeout)
				if not message:
					print('Error receiving response for offer', tag='timeout', color='red')
					return None
				if message['type'] == 'message' and message['message']['data']['type'] == 'offer':
					return message

			case WaitFor.CANDIDATE:
				message = await self.receive(timeout)
				if not message:
					print('Error receiving response for candidate', tag='timeout', color='red')
					return None
				if message['type'] == 'message' and message['message']['data']['type'] == 'candidate':
					return message

			case WaitFor.MUTEUNMUTE:
				message = await self.receive(timeout)
				if not message:
					print('Error receiving response for mute/unmute', tag='timeout', color='red')
					return None
				if message['type'] == 'message' and (message['message']['data']['type'] == 'mute' or message['message']['data']['type'] == 'unmute'):
					return message

			case _:
				print('Unknown event type', tag='error', color='yellow')
				return None


class FlagVideoStreamTrack(VideoStreamTrack):
	"""
	A video track that returns an animated flag.
	"""

	def __init__(self):
		super().__init__()  # don't forget this!
		self.counter = 0
		height, width = 480, 640

		# generate flag
		data_bgr = numpy.hstack(
			[
				self._create_rectangle(
					width=213, height=480, color=(255, 0, 0)
				),  # blue
				self._create_rectangle(
					width=214, height=480, color=(255, 255, 255)
				),  # white
				self._create_rectangle(width=213, height=480, color=(0, 0, 255)),  # red
			]
		)

		# shrink and center it
		M = numpy.float32([[0.5, 0, width / 4], [0, 0.5, height / 4]])
		data_bgr = cv2.warpAffine(data_bgr, M, (width, height))

		# compute animation
		omega = 2 * math.pi / height
		id_x = numpy.tile(numpy.array(range(width), dtype=numpy.float32), (height, 1))
		id_y = numpy.tile(
			numpy.array(range(height), dtype=numpy.float32), (width, 1)
		).transpose()

		self.frames = []
		for k in range(30):
			phase = 2 * k * math.pi / 30
			map_x = id_x + 10 * numpy.cos(omega * id_x + phase)
			map_y = id_y + 10 * numpy.sin(omega * id_x + phase)
			self.frames.append(
				VideoFrame.from_ndarray(
					cv2.remap(data_bgr, map_x, map_y, cv2.INTER_LINEAR), format="bgr24"
				)
			)

	async def recv(self):
		pts, time_base = await self.next_timestamp()

		frame = self.frames[self.counter % 30]
		frame.pts = pts
		frame.time_base = time_base
		self.counter += 1
		return frame

	def _create_rectangle(self, width, height, color):
		data_bgr = numpy.zeros((height, width, 3), numpy.uint8)
		data_bgr[:, :] = color
		return data_bgr


class VideoReceiver:
	def __init__(self):
		self.track = None

	async def handle_track(self, track):
		print("Inside handle track")
		self.track = track
		frame_count = 0
		while True:
			try:
				print("Waiting for frame...")
				frame = await asyncio.wait_for(track.recv(), timeout=5.0)
				frame_count += 1
				print(f"Received frame {frame_count}")
				
				if isinstance(frame, VideoFrame):
					print(f"Frame type: VideoFrame, pts: {frame.pts}, time_base: {frame.time_base}")
					frame = frame.to_ndarray(format="bgr24")
				elif isinstance(frame, numpy.ndarray):
					print("Frame type: numpy array")
				else:
					print(f"Unexpected frame type: {type(frame)}")
					continue

				# Add timestamp to the frame
				current_time = datetime.now()
				new_time = current_time - datetime.timedelta( seconds=55)
				timestamp = new_time.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
				cv2.putText(frame, timestamp, (10, frame.shape[0] - 30), cv2.FONT_HERSHEY_SIMPLEX, 1, (0, 255, 0), 2, cv2.LINE_AA)
				cv2.imwrite(f"imgs/received_frame_{frame_count}.jpg", frame)
				print(f"Saved frame {frame_count} to file")
				cv2.imshow("Frame", frame)

				# Exit on 'q' key press
				if cv2.waitKey(1) & 0xFF == ord('q'):
						break
			except asyncio.TimeoutError:
					print("Timeout waiting for frame, continuing...")
			except Exception as e:
					print(f"Error in handle_track: {str(e)}")
					if "Connection" in str(e):
							break
	print("Exiting handle_track")


async def run(pc, signaling, recorder):
	ice_candidates = []
	offer_sid = ''
	answer_sent = Event()

	@pc.on("track")
	async def on_track(track):
		print("Receiving %s" % track.kind, tag='track', color='magenta')
		if track.kind == "video" or track.kind == "audio":
			asyncio.ensure_future(recorder.handle_track(track))
		# if track.kind == "audio":
		# 	recorder.addTrack(track)
		else:
			print("Unknown track kind: %s" % track.kind, tag='track', color='red')

	# @pc.on('iceconnectionstatechange')
	# async def on_iceconnectionstatechange():
	# 	print("ICE connection state is", pc.iceConnectionState, tag='ice', color='blue')

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
			# HAPPENS before we receive candidates from the signalling server

			# send answer after local candidate gathering
			answer = await pc.createAnswer()
			await pc.setLocalDescription(answer)
			await signaling.send_offer_answer(message['message']['data']['from'], message['message']['data']['sid'], answer.sdp)
			answer_sent.set()

			# for message in ice_candidates:
			# 	# print('Gathered candidate:', candidate, tag='ice', color='blue')
			# 	candidate = candidate_from_sdp(message['message']['data']['payload']['candidate']['candidate'])
			# 	candidate.sdpMid = message['message']['data']['payload']['candidate']['sdpMid']
			# 	candidate.sdpMLineIndex = message['message']['data']['payload']['candidate']['sdpMLineIndex']
			# 	await pc.addIceCandidate(candidate)
			# 	await signaling.send_candidate(offer_sid, message['message']['data']['payload']['candidate']['candidate'])

	#if role == "offer":
	#	# send offer
	#	add_tracks()
	#pc.createDataChannel('transcript')
	#await pc.setLocalDescription(await pc.createOffer())
	#await signaling.send(pc.localDescription)

	# consume signaling
	# while True:

	# while True:
		# try:
		# 	message = await signaling.receive()
		# 	if not message:
		# 		raise Exception("No message received")
		# except Exception:
		# 	print('Connection closed', tag='connection', color='red')
		# 	break

	# connect signaling
	await signaling.connect()
	await signaling.send_join('i6h8x8q9')
	await signaling.send_hello()
	hello = await signaling.wait_for(WaitFor.HELLO, timeout=10)
	if not hello:
		print('Error receiving hello', tag='timeout', color='red')
		return
	await signaling.send_incall()

	print('Connected to signaling server')

	message = await signaling.wait_for(WaitFor.PARTICIPANTS)
	print('New participants update!', tag='participants', color='blue')
	for user_description in message['event']['update']['users']:
		if user_description['inCall'] & CALL_FLAG.IN_CALL and user_description['inCall'] & CALL_FLAG.WITH_AUDIO:
			print('User join with audio', user_description, tag='participants', color='blue')
			# pc.addTransceiver('audio', direction='recvonly')
			# pc.addTransceiver('video', direction='recvonly')
			targets[user_description['sessionId']] = Target(
				session_id=user_description['sessionId'],
				raw_message=user_description,
				in_call=True,
				muted=True
			)
			await signaling.send_offer_request(user_description['sessionId'])

	if message['type'] == 'message' and message['message']['data']['type'] == 'offer':
		offer_sid = message['message']['data']['sid']
		await pc.setRemoteDescription(RTCSessionDescription(type='offer', sdp=message['message']['data']['payload']['sdp']))

		# send answer after local candidate gathering

		# answer = await pc.createAnswer()
		# await pc.setLocalDescription(answer)
		# await signaling.send_offer_answer(message['message']['data']['from'], message['message']['data']['sid'], answer.sdp)
		# await recorder.start()

	if message['type'] == 'message' and message['message']['data']['type'] == 'candidate':
		print('got candidate')
		candidate = candidate_from_sdp(message['message']['data']['payload']['candidate']['candidate'])
		candidate.sdpMid = message['message']['data']['payload']['candidate']['sdpMid']
		candidate.sdpMLineIndex = message['message']['data']['payload']['candidate']['sdpMLineIndex']
		await pc.addIceCandidate(candidate)
		ice_candidates.append(message)
	elif answer_sent.is_set():
		# send all the local candidates to the signaling server
		cns = pc.localDescription
		print('local description:', cns, tag='local_description', color='blue')

	if (
		message['type'] == 'message'
		and (message['message']['data']['type'] == 'mute' or message['message']['data']['type'] == 'unmute')
		and message['message']['data']['payload']['name'] == 'audio'
	):
		# got a audio toggle message
		if message['message']['data']['type'] == 'mute':
			print('muting audio for session id', message['message']['sender']['sessionid'])
		else:
			print('unmuting audio for session id', message['message']['sender']['sessionid'])


if __name__ == "__main__":
	#logging.basicConfig(level=logging.DEBUG)

	# create signaling and peer connection
	app_started.set()
	signaling = WebSocketSignaling(
		'wss://hpb.pluton.minifox.fr/standalone-signaling/spreed',
		'https://nuage.minifox.fr',
		"jemangedesinternalsecret"
	)
	pc = RTCPeerConnection()
	# recorder = MediaRecorder("/home/tyrell/nextcloud-docker-dev/workspace/live-transcription-tests/output.mp3")
	recorder = VideoReceiver()

	# run event loop
	loop = asyncio.get_event_loop()
	try:
		loop.run_until_complete(
			run(
				pc=pc,
				signaling=signaling,
				recorder=recorder,
			)
		)
	except KeyboardInterrupt:
		pass
	finally:
		# cleanup
		app_started.clear()
		# loop.run_until_complete(recorder.stop())
		loop.run_until_complete(signaling.close())
		loop.run_until_complete(pc.close())
