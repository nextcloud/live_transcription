#
# SPDX-FileCopyrightText: 2025 Nextcloud GmbH and Nextcloud contributors
# SPDX-License-Identifier: AGPL-3.0-or-later
#

import dataclasses
from enum import IntEnum

from pydantic import BaseModel, Field


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
	langId: str | None = None  # when None, disable translation for this target


class Target(BaseModel):
	...


class LanguageMetadata(BaseModel):
	separator: str = Field(default=" ", description="Separator used in the language")
	rtl: bool = Field(default=False, description="Indicates if the language is right-to-left (RTL)")


class LanguageModel(BaseModel):
	name: str = Field(..., description="Name of the language")
	metadata: LanguageMetadata = Field(default_factory=LanguageMetadata, description="Metadata for the language")


class SupportedTranslationLanguages(BaseModel):
	origin_languages: dict[str, LanguageModel] = Field(
		..., description="Mapping of origin language IDs to their models"
	)
	target_languages: dict[str, LanguageModel] = Field(
		..., description="Mapping of target language IDs to their models"
	)


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


class ReconnectMethod(IntEnum):
	NO_RECONNECT = 0
	SHORT_RESUME = 1
	FULL_RECONNECT = 2


class CallFlag(IntEnum):
	DISCONNECTED = 0
	IN_CALL      = 1
	WITH_AUDIO   = 2
	WITH_VIDEO   = 4
	WITH_PHONE   = 8


class SpreedClientException(Exception):
	"""Base exception for SpreedClient errors."""


class SpreedRateLimitedException(SpreedClientException):
	"""Exception raised when the Spreed Client is rate limited by the HPB server."""


class VoskException(Exception):
	retcode: int

	def __init__(self, message: str, retcode: int = 500):
		super().__init__(message)
		self.retcode = retcode


class StreamEndedException(Exception):
	...


class TranslateException(Exception):
	"""Base exception for translation errors."""


class TranslateFatalException(TranslateException):
	"""Fatal exception for translation errors.

	Indicates a fatal error that should stop further translation attempts of any more transcript
	chunks and the translator should be removed from the MetaTranslator.
	"""


class TranslateLangPairException(TranslateFatalException):
	"""Exception for unsupported language pairs.

	Indicates that the language pair is not supported by the translation service.
	"""


class TranscriptTargetNotFoundException(Exception):
	"""Exception for missing transcript target.

	Indicates that the transcript target participant has not enabled transcription yet
	for the call and thus cannot receive translated transcripts.
	"""
