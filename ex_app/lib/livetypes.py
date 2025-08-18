#
# SPDX-FileCopyrightText: 2025 Nextcloud GmbH and Nextcloud contributors
# SPDX-License-Identifier: AGPL-3.0-or-later
#

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


class LanguageSetRequest(BaseModel):
	roomToken: str
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
