import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ex_app.lib.audio_stream import AudioStream
from ex_app.lib.constants import VOSK_CONNECT_TIMEOUT
from ex_app.lib.livetypes import StreamEndedException, Transcript, VoskException
from ex_app.lib.transcriber import VoskTranscriber


@pytest.fixture
def mock_transcript_queue():
	return asyncio.Queue()


@pytest.fixture
def transcriber(mock_transcript_queue):
	return VoskTranscriber(session_id="test_session", language="en", transcript_queue=mock_transcript_queue)


@pytest.mark.asyncio
async def test_transcriber_initialization(transcriber, mock_transcript_queue):
	assert transcriber.__session_id == "test_session"
	assert transcriber.__language == "en"
	assert transcriber.__transcript_queue == mock_transcript_queue
	assert transcriber.__voskcon is None
	assert transcriber.__audio_task is None


# @pytest.mark.asyncio
# async def test_connect(transcriber):
# 	mock_websocket = AsyncMock()
# 	mock_websocket.send = AsyncMock()

# 	with patch("websockets.connect", return_value=mock_websocket) as mock_connect:
# 		await transcriber.connect()

# 		mock_connect.assert_called_once_with(
# 			transcriber._VoskTranscriber__server_url,
# 			open_timeout=VOSK_CONNECT_TIMEOUT,
# 		)
# 		mock_websocket.send.assert_called_once()
# 		sent_data = json.loads(mock_websocket.send.call_args[0][0])
# 		assert sent_data["config"]["sample_rate"] == 48000
# 		assert sent_data["config"]["language"] == "en"
# 		assert transcriber._VoskTranscriber__voskcon == mock_websocket


# @pytest.mark.asyncio
# async def test_start(transcriber):
# 	mock_stream = MagicMock(spec=AudioStream)
# 	mock_task = MagicMock(spec=asyncio.Task)

# 	with patch("asyncio.create_task", return_value=mock_task) as mock_create_task:
# 		transcriber.start(mock_stream)

# 		mock_create_task.assert_called_once()
# 		assert transcriber.audio_task == mock_task
# 		mock_task.add_done_callback.assert_called_once()


# @pytest.mark.asyncio
# async def test_shutdown(transcriber):
# 	mock_task = MagicMock(spec=asyncio.Task)
# 	mock_task.done.return_value = False
# 	transcriber.audio_task = mock_task

# 	transcriber.shutdown()

# 	mock_task.cancel.assert_called_once()
# 	assert transcriber.audio_task is None


# @pytest.mark.asyncio
# async def test_set_language(transcriber):
# 	# First set up a mock websocket connection
# 	mock_websocket = AsyncMock()
# 	mock_websocket.recv.side_effect = ['{"success": true}']
# 	transcriber._VoskTranscriber__voskcon = mock_websocket

# 	# Test successful language change
# 	await transcriber.set_language("es")

# 	# Verify the language was changed
# 	assert transcriber._VoskTranscriber__language == "es"

# 	# Verify the correct messages were sent
# 	assert mock_websocket.send.call_count == 1
# 	sent_data = json.loads(mock_websocket.send.call_args[0][0])
# 	assert sent_data["config"]["language"] == "es"

# 	# Verify recv was called
# 	mock_websocket.recv.assert_called_once()


# @pytest.mark.asyncio
# async def test_set_language_failure(transcriber):
# 	# First set up a mock websocket connection
# 	mock_websocket = AsyncMock()
# 	mock_websocket.recv.side_effect = ['{"success": false}']
# 	transcriber._VoskTranscriber__voskcon = mock_websocket

# 	# Test failed language change
# 	with pytest.raises(VoskException):
# 		await transcriber.set_language("es")


# @pytest.mark.asyncio
# async def test_set_language_no_connection(transcriber):
# 	# Test with no websocket connection
# 	with pytest.raises(VoskException):
# 		await transcriber.set_language("es")


# @pytest.mark.asyncio
# async def test_run_audio_xfer(transcriber):
# 	# Set up mock websocket connection
# 	mock_websocket = AsyncMock()
# 	mock_websocket.recv.return_value = '{"text": "Hello world"}'
# 	transcriber._VoskTranscriber__voskcon = mock_websocket

# 	# Create a mock audio stream
# 	mock_stream = MagicMock(spec=AudioStream)
# 	mock_frame = MagicMock()
# 	mock_frame.samples = 480
# 	mock_frame.planes = [bytearray(b"\x00\x01" * 240)]
# 	mock_stream.receive.return_value = mock_frame

# 	# Mock the resampler
# 	with patch.object(transcriber._VoskTranscriber__resampler, "resample", return_value=[mock_frame]):
# 		# Run the audio transfer task
# 		task = asyncio.create_task(transcriber._VoskTranscriber__run_audio_xfer(mock_stream))

# 		# Wait for the task to complete
# 		await asyncio.sleep(0.1)

# 		# Verify that the transcript was added to the queue
# 		transcript = await transcriber._VoskTranscriber__transcript_queue.get()
# 		assert isinstance(transcript, Transcript)
# 		assert transcript.final is True
# 		assert transcript.lang_id == "en"
# 		assert transcript.message == "Hello world"
# 		assert transcript.speaker_session_id == "test_session"

# 		# Cancel the task
# 		task.cancel()


# @pytest.mark.asyncio
# async def test_run_audio_xfer_partial(transcriber):
# 	# Set up mock websocket connection
# 	mock_websocket = AsyncMock()
# 	mock_websocket.recv.return_value = '{"partial": "Hello"}'
# 	transcriber._VoskTranscriber__voskcon = mock_websocket

# 	# Create a mock audio stream
# 	mock_stream = MagicMock(spec=AudioStream)
# 	mock_frame = MagicMock()
# 	mock_frame.samples = 480
# 	mock_frame.planes = [bytearray(b"\x00\x01" * 240)]
# 	mock_stream.receive.return_value = mock_frame

# 	# Mock the resampler
# 	with patch.object(transcriber._VoskTranscriber__resampler, "resample", return_value=[mock_frame]):
# 		# Run the audio transfer task
# 		task = asyncio.create_task(transcriber._VoskTranscriber__run_audio_xfer(mock_stream))

# 		# Wait for the task to complete
# 		await asyncio.sleep(0.1)

# 		# Verify that the transcript was added to the queue
# 		transcript = await transcriber._VoskTranscriber__transcript_queue.get()
# 		assert isinstance(transcript, Transcript)
# 		assert transcript.final is False
# 		assert transcript.lang_id == "en"
# 		assert transcript.message == "Hello"
# 		assert transcript.speaker_session_id == "test_session"

# 		# Cancel the task
# 		task.cancel()


# @pytest.mark.asyncio
# async def test_run_audio_xfer_stream_ended(transcriber):
# 	# Set up mock websocket connection
# 	mock_websocket = AsyncMock()
# 	transcriber._VoskTranscriber__voskcon = mock_websocket

# 	# Create a mock audio stream that raises StreamEndedException
# 	mock_stream = MagicMock(spec=AudioStream)
# 	mock_stream.receive.side_effect = StreamEndedException()

# 	# Run the audio transfer task
# 	task = asyncio.create_task(transcriber._VoskTranscriber__run_audio_xfer(mock_stream))

# 	# Wait for the task to complete
# 	await asyncio.sleep(0.1)

# 	# Cancel the task
# 	task.cancel()

# 	# Verify the connection was closed
# 	mock_websocket.close.assert_called_once()
