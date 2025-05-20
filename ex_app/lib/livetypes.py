"""Types."""

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
	sessionId: str
	enable: bool
