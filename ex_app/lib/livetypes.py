#
# SPDX-FileCopyrightText: 2025 Nextcloud GmbH and Nextcloud contributors
# SPDX-License-Identifier: AGPL-3.0-or-later
#

import dataclasses
from enum import IntEnum

from pydantic import BaseModel


class StunServer(BaseModel):
	urls: list[str]

class TurnServer(BaseModel):
	urls: list[str]
	username: str
	credential: str

class HPBSettings(BaseModel):
	server: str
	stunservers: list[StunServer]
	turnservers: list[TurnServer]


class StreamEndedException(Exception):
	...


class TranscribeRequest(BaseModel):
	roomToken: str
	ncSessionId: str # Nextcloud session ID, not the HPB session ID
	enable: bool = True
	langId: str = "en"
	# the user's language, when set, translate the transcript to this language
	translationTargetLangId: str | None = None


class RoomLanguageSetRequest(BaseModel):
	roomToken: str
	langId: str


class TargetLanguageSetRequest(BaseModel):
	roomToken: str
	ncSessionId: str # Nextcloud session ID, not the HPB session ID
	langId: str


class Target(BaseModel):
	...


class SpreedClientException(Exception):
	"""Base exception for SpreedClient errors."""


class VoskException(Exception):
	retcode: int

	def __init__(self, message: str, retcode: int = 500):
		super().__init__(message)
		self.retcode = retcode


# data carrier in the transcript_queue
@dataclasses.dataclass
class Transcript:
	final: bool
	lang_id: str
	message: str
	speaker_session_id: str


# data carrier in the translate_queue_input & output
@dataclasses.dataclass
class TranslateInputOutput:
	origin_language: str
	target_language: str
	message: str  # can be either input or output
	speaker_session_id: str
	target_nc_session_ids: set[str]


class SigConnectResult(IntEnum):
	SUCCESS = 0
	FAILURE = 1  # do not retry
	RETRY   = 2


class CallFlag(IntEnum):
	DISCONNECTED = 0
	IN_CALL      = 1
	WITH_AUDIO   = 2
	WITH_VIDEO   = 4
	WITH_PHONE   = 8


class TranslateException(Exception):
	...
