import os
from asyncio import Queue
from unittest import mock

import pytest
from websockets import ClientConnection

from ex_app.lib.transcriber import VoskTranscriber

LT_VOSK_SERVER_URL = "ws://127.0.0.1:8989"

@pytest.fixture(scope="module")
def vosk_transcriber_init_data():
	os.environ["LT_VOSK_SERVER_URL"] = LT_VOSK_SERVER_URL
	return {
		"session_id": "dumm10",
		"language": "dumm11",
		"transcript_queue": Queue(),
	}


@pytest.mark.asyncio
async def test_vosk_connection(vosk_transcriber_init_data, wss_env_var):
	transcriber = VoskTranscriber(**vosk_transcriber_init_data)

	with (
		mock.patch("websockets.ClientConnection") as mock_client_conn,
		mock.patch("websockets.connect", return_value=mock_client_conn) as mock_connect,
	):
		# todo
		# mock_connect.return_value = mock_client_conn
		await transcriber.connect()
		mock_connect.assert_called_with(LT_VOSK_SERVER_URL)
