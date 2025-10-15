#
# SPDX-FileCopyrightText: 2025 Nextcloud GmbH and Nextcloud contributors
# SPDX-License-Identifier: AGPL-3.0-or-later
#

import threading
from abc import ABC, abstractmethod


class ATranslator(ABC):
	origin_language: str
	target_language: str
	nc_session_ids: set[str]
	nc_session_ids_lock: threading.Lock

	def __init__(self, origin_language: str, target_language: str):
		self.origin_language = origin_language
		self.target_language = target_language
		self.nc_session_ids = set()
		self.nc_session_ids_lock = threading.Lock()

	def add_session_id(self, session_id: str):
		with self.nc_session_ids_lock:
			self.nc_session_ids.add(session_id)

	def remove_session_id(self, session_id: str):
		with self.nc_session_ids_lock:
			self.nc_session_ids.discard(session_id)

	@abstractmethod
	async def translate(self, message: str) -> str:
		...
