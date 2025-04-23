import random

from websockets import connect
import json
import asyncio
from hashlib import sha256
import hmac
import hashlib

def hmac_sha256(key, message):
	return hmac.new(
		key.encode("utf-8"),
		message.encode("utf-8"),
		hashlib.sha256
	).hexdigest()

id = 1
NEXTCLOUD_URL = 'https://nuage.minifox.fr'

async def run():
	async with connect('wss://hpb.pluton.minifox.fr/standalone-signaling/spreed') as websocket:
		message = await websocket.recv()
		print(message)
		nonce = '' + str(random.random()) + '' + str(random.random())+ '' + str(random.random())
		print(nonce)
		sentMessage = json.dumps({
			"type": "hello",
			"id": str(id),
			"hello": {
				"version": "2.0",
				"auth": {
					"type": "internal",
					"params": {
						"random": nonce,
						"token": hmac_sha256("jemangedesinternalsecret", nonce),
						"backend": NEXTCLOUD_URL,
					}
				},
			},
		})
		print(sentMessage)
		await websocket.send(sentMessage)
		while True:
			message = await websocket.recv()
			print(message)
		hello = json.loads(message)


asyncio.run(run())
