#
# SPDX-FileCopyrightText: 2025 Nextcloud GmbH and Nextcloud contributors
# SPDX-License-Identifier: AGPL-3.0-or-later
#

import logging

from aiortc import AudioStreamTrack
from av.audio.frame import AudioFrame
from livetypes import StreamEndedException

LOGGER = logging.getLogger("lt.audio_stream")


class AudioStream:
	def __init__(self, track: AudioStreamTrack):
		self.track = track
		self._ended = False

		@track.on("ended")
		async def on_ended():
			LOGGER.debug("Track ended", extra={"tag": "track"})
			self._ended = True

	async def receive(self) -> AudioFrame:
		"""Receive the next audio frame."""
		if self._ended:
			raise StreamEndedException("Track has ended")
		return await self.track.recv()  # type: ignore[return-value]

	def stop(self):
		"""Stop the audio stream."""
		self.track.stop()
