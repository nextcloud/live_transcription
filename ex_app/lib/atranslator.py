#
# SPDX-FileCopyrightText: 2025 Nextcloud GmbH and Nextcloud contributors
# SPDX-License-Identifier: AGPL-3.0-or-later
#

from abc import ABC, abstractmethod

from livetypes import SupportedTranslationLanguages


class ATranslator(ABC):
	origin_language: str
	target_language: str
	room_token: str
	nc_session_ids: set[str]

	def __init__(self, origin_language: str, target_language: str, room_token: str):
		self.nc_session_ids = set()

		self.origin_language = origin_language
		self.target_language = target_language
		self.room_token = room_token

	def add_session_id(self, session_id: str):
		self.nc_session_ids.add(session_id)

	def remove_session_id(self, session_id: str):
		self.nc_session_ids.discard(session_id)

	@abstractmethod
	async def translate(self, message: str) -> str:
		...

	@abstractmethod
	async def is_language_pair_supported(self) -> bool:
		...

	@abstractmethod
	async def get_translation_languages(self) -> SupportedTranslationLanguages:
		...
