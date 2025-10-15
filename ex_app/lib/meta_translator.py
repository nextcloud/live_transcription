#
# SPDX-FileCopyrightText: 2025 Nextcloud GmbH and Nextcloud contributors
# SPDX-License-Identifier: AGPL-3.0-or-later
#

import asyncio
import logging
import queue
import threading

from atranslator import ATranslator
from livetypes import TranslateInputOutput
from ocp_translator import OCPTranslator

LOGGER = logging.getLogger("lt.meta_translator")

class MetaTranslator:
	def __init__(
		self,
		room_lang_id: str,
		translate_queue_input: queue.Queue,
		translate_queue_output: queue.Queue,
	):
		self.translators: dict[str, ATranslator] = {} # key: target language
		self.translators_lock = threading.Lock()
		self.sid_translation_lang_map: dict[str, str] = {}  # {"nc_session_id": "target_lang_id"}
		self.sid_translation_lang_map_lock = threading.Lock()
		self.task_lock = threading.Lock()
		self.task: asyncio.Task | None = None

		self.room_lang_id = room_lang_id
		self.translate_queue_input = translate_queue_input
		self.translate_queue_output = translate_queue_output

	def add_translator(self, target_lang_id: str, nc_session_id: str):
		with self.sid_translation_lang_map_lock:
			if nc_session_id in self.sid_translation_lang_map:
				if self.sid_translation_lang_map[nc_session_id] == target_lang_id:
					LOGGER.info("Session id already registered for this language pair, not adding again", extra={
						"origin_language": self.room_lang_id,
						"target_language": target_lang_id,
						"nc_session_id": nc_session_id,
						"tag": "translate",
					})
					return
				LOGGER.info(
					"Session id already registered for a different target language, removing the old translator"
					" before adding the new one.",
					extra={
						"origin_language": self.room_lang_id,
						"old_target_language": self.sid_translation_lang_map[nc_session_id],
						"new_target_language": target_lang_id,
						"nc_session_id": nc_session_id,
						"tag": "translate",
					},
				)
				self.__remove_translator_int(self.sid_translation_lang_map[nc_session_id], nc_session_id)
			self.sid_translation_lang_map[nc_session_id] = target_lang_id

		with self.translators_lock:
			if target_lang_id not in self.translators:
				LOGGER.debug("No existing translator found for the language pair, creating one", extra={
					"origin_language": self.room_lang_id,
					"target_language": target_lang_id,
					"nc_session_id": nc_session_id,
					"tag": "translate",
				})
				# todo: room owner id from talk api
				self.translators[target_lang_id] = OCPTranslator(self.room_lang_id, target_lang_id, "admin")
			self.translators[target_lang_id].add_session_id(nc_session_id)
			LOGGER.debug("Added NC session id to the translator", extra={
				"origin_language": self.room_lang_id,
				"target_language": target_lang_id,
				"nc_session_id": nc_session_id,
				"tag": "translate",
			})

	def __remove_translator_int(self, target_lang_id: str, nc_session_id: str):
		with self.translators_lock:
			if target_lang_id not in self.translators:
				LOGGER.info("No existing translator found for the language pair when a nc_session_id was requested"
					" to be removed from it.",
					extra={
						"origin_language": self.room_lang_id,
						"target_language": target_lang_id,
						"nc_session_id": nc_session_id,
						"tag": "translate",
					},
				)
				return
			self.translators[target_lang_id].remove_session_id(nc_session_id)
			if not self.translators[target_lang_id].nc_session_ids:
				LOGGER.debug("No more NC session ids left for the translator, removing it", extra={
					"origin_language": self.room_lang_id,
					"target_language": target_lang_id,
					"nc_session_id": nc_session_id,
					"tag": "translate",
				})
				del self.translators[target_lang_id]

	def remove_translator(self, nc_session_id: str):
		with self.sid_translation_lang_map_lock:
			if nc_session_id not in self.sid_translation_lang_map:
				LOGGER.info("NC session id not found in the translation map when trying to remove it", extra={
					"nc_session_id": nc_session_id,
					"room_lang_id": self.room_lang_id,
					"tag": "translate",
				})
				return
			target_lang_id = self.sid_translation_lang_map[nc_session_id]
			self.__remove_translator_int(target_lang_id, nc_session_id)
			del self.sid_translation_lang_map[nc_session_id]
			LOGGER.debug("Removed NC session id from the translation map", extra={
				"nc_session_id": nc_session_id,
				"room_lang_id": self.room_lang_id,
				"tag": "translate",
			})

	@property
	def is_any_translation_enabled(self) -> bool:
		with self.sid_translation_lang_map_lock:
			return bool(self.sid_translation_lang_map)

	def start(self):
		with self.task_lock:
			if self.task and not self.task.done():
				LOGGER.debug("Translation task already running", extra={
					"room_lang_id": self.room_lang_id,
					"tag": "translate",
				})
				return
			self.task = asyncio.create_task(self.__run_translation_task(), name=f"MetaTranslator-{self.room_lang_id}")
			self.task.add_done_callback(self.__done_cb)

	def shutdown(self):
		with self.task_lock:
			if self.task and not self.task.done():
				LOGGER.debug("Cancelling translation task", extra={
					"room_lang_id": self.room_lang_id,
					"tag": "translate",
				})
				self.task.cancel()

	def __done_cb(self, future: asyncio.Future):
		with self.task_lock:
			if not self.task or self.task.done():
				return
			LOGGER.debug("Stopping translation task in callback", extra={
					"room_lang_id": self.room_lang_id,
					"tag": "translate",
				})
			future.cancel("Cancelling translation task in meta translator in the callback")
			self.task = None

	async def __run_translation_task(self):
		LOGGER.debug("Starting meta translation task", extra={
			"room_lang_id": self.room_lang_id,
			"tag": "translate",
		})

		# todo: debug
		while True:
			LOGGER.debug("Translate input queue size: %d", self.translate_queue_input.qsize(), extra={
				"room_lang_id": self.room_lang_id,
				"tag": "translate",
			})
			LOGGER.debug("Translate output queue size: %d", self.translate_queue_output.qsize(), extra={
				"room_lang_id": self.room_lang_id,
				"tag": "translate",
			})
			await asyncio.sleep(5)  # debug

		while True:
			segment: TranslateInputOutput = self.translate_queue_input.get()  # type: ignore[annotation-unchecked]

			# todo
			LOGGER.debug("Got transcript segment from the input queue", extra={
				"origin_language": segment.origin_language,
				"target_language": segment.target_language,
				"speaker_session_id": segment.speaker_session_id,
				"target_nc_session_ids": segment.target_nc_session_ids,
				"segment_message": segment.message,
				"room_token": self.room_token,
				"tag": "translate",
			})

			try:
				with self.translators_lock:
					for translator in self.translators.values():
						segment.target_language = translator.target_lang_id
						segment.target_nc_session_ids = translator.nc_session_ids.copy()
						task = asyncio.create_task(
							asyncio.wait_for(translator.translate(segment.message), timeout=10),
							name=f"Translator-{segment.speaker_session_id}-{translator.target_lang_id}",
						)
						task.add_done_callback(self.__translation_task_cb, segment, self.translate_queue_output)
			except asyncio.CancelledError:
				LOGGER.debug("Translation task cancelled", extra={
					"origin_language": segment.origin_language,
					"target_language": segment.target_language,
					"nc_session_id": segment.speaker_session_id,
					"tag": "translate",
				})
				raise
			except Exception as e:
				LOGGER.error("Exception during translation task: %s", e, exc_info=e, extra={
					"origin_language": segment.origin_language,
					"target_language": segment.target_language,
					"nc_session_id": segment.speaker_session_id,
					"tag": "translate",
				})

	async def __translation_task_cb(
		self,
		segment: TranslateInputOutput,
		output_queue: queue.Queue,
		future: asyncio.Future,
	):
		if future.cancelled():
			LOGGER.debug("Translation task cancelled in callback", extra={
				"origin_language": segment.origin_language,
				"target_language": segment.target_language,
				"tag": "translate",
			})
			return

		exc = future.exception()
		if exc:
			LOGGER.error("Translation task raised an exception: %s", exc, exc_info=exc, extra={
				"origin_language": segment.origin_language,
				"target_language": segment.target_language,
				"tag": "translate",
			})
			return

		translated_text = future.result()  # string
		segment.message = translated_text
		LOGGER.debug("Translation task completely successfully", extra={
			"origin_language": segment.origin_language,
			"target_language": segment.target_language,
			"translated_text": segment.message,
			"tag": "translate",
		})
		output_queue.put_nowait(segment)
