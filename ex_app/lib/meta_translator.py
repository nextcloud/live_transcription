#
# SPDX-FileCopyrightText: 2025 Nextcloud GmbH and Nextcloud contributors
# SPDX-License-Identifier: AGPL-3.0-or-later
#

import asyncio
import logging
import threading
from functools import partial
from time import time

from atranslator import ATranslator
from constants import CACHE_TRANSLATION_LANGS_FOR, OCP_TASK_TIMEOUT
from livetypes import (
	SupportedTranslationLanguages,
	TranslateException,
	TranslateFatalException,
	TranslateInputOutput,
	TranslateLangPairException,
)
from ocp_translator import OCPTranslator

LOGGER = logging.getLogger("lt.meta_translator")

class MetaTranslator:
	def __init__(
		self,
		room_token: str,
		room_lang_id: str,
		translate_queue_input: asyncio.Queue,
		translate_queue_output: asyncio.Queue,
		should_translate: threading.Event,
	):
		self.translators: dict[str, ATranslator] = {} # key: target language
		self.translators_lock = threading.RLock()
		self.sid_translation_lang_map: dict[str, str] = {}  # {"nc_session_id": "target_lang_id"}
		self.sid_translation_lang_map_lock = threading.Lock()
		self.task_lock = threading.Lock()
		self.task: asyncio.Task | None = None
		self.__asyncio_tasks_bin: set[asyncio.Task] = set()
		self.__translation_languages_cache: tuple[float, SupportedTranslationLanguages] | None = None

		self.room_token = room_token
		self.room_lang_id = room_lang_id
		self.translate_queue_input = translate_queue_input
		self.translate_queue_output = translate_queue_output
		self.should_translate = should_translate

	async def add_translator(self, target_lang_id: str, nc_session_id: str):
		"""
		Raises
		------
			TranslateFatalException: If a fatal error occurs and all translators should be removed
			TranslateLangPairException: If the language pair is not supported
			TranslateException: If any other translation error occurs
		"""  # noqa

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
				self.translators[target_lang_id] = OCPTranslator(
					self.room_lang_id, target_lang_id, self.room_token, "admin"
				)
				try:
					await self.translators[target_lang_id].is_language_pair_supported()
				except TranslateException:
					del self.translators[target_lang_id]
					del self.sid_translation_lang_map[nc_session_id]
					raise

			self.translators[target_lang_id].add_session_id(nc_session_id)
			LOGGER.debug("Added NC session id to the translator", extra={
				"origin_language": self.room_lang_id,
				"target_language": target_lang_id,
				"nc_session_id": nc_session_id,
				"tag": "translate",
			})

		# todo: maybe make this and shutdown internal?
		self.start()

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

	async def remove_translator(self, nc_session_id: str):
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
		if self.__is_any_translation_enabled():
			self.should_translate.set()
		else:
			self.should_translate.clear()
			self.shutdown()

	def __is_any_translation_enabled(self) -> bool:
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
		self.should_translate.set()

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

		# # todo: debug
		# while True:
		# 	try:
		# 		segment: TranslateInputOutput = self.translate_queue_input.get_nowait()
		# 		LOGGER.debug("Translate input queue item:", extra={
		# 			"segment": segment,
		# 			"tag": "translate",
		# 		})
		# 	except Exception as e:
		# 		LOGGER.debug("Translate input queue error", exc_info=e, extra={
		# 			"room_lang_id": self.room_lang_id,
		# 			"tag": "translate",
		# 		})
		# 		await asyncio.sleep(1)
		# 		continue

		# 	LOGGER.debug("Translate input queue size: %d", self.translate_queue_input.qsize(), extra={
		# 		"room_lang_id": self.room_lang_id,
		# 		"tag": "translate",
		# 	})
		# 	LOGGER.debug("Translate output queue size: %d", self.translate_queue_output.qsize(), extra={
		# 		"room_lang_id": self.room_lang_id,
		# 		"tag": "translate",
		# 	})
		# 	await asyncio.sleep(5)  # debug

		while True:
			segment: TranslateInputOutput = await self.translate_queue_input.get()  # type: ignore[annotation-unchecked]
			# todo
			LOGGER.debug("Translate input queue item:", extra={
				"segment": segment,
				"tag": "translate",
			})

			try:
				with self.translators_lock:
					for translator in self.translators.values():
						segment.target_language = translator.target_language
						segment.target_nc_session_ids = translator.nc_session_ids.copy()
						task = asyncio.create_task(
							asyncio.wait_for(translator.translate(segment.message), timeout=OCP_TASK_TIMEOUT),
							name=f"Translator-{segment.speaker_session_id}-{translator.target_language}",
						)
						task.add_done_callback(
							partial(self.__translation_task_cb, segment, self.translate_queue_output)
						)
			except asyncio.CancelledError:
				LOGGER.debug("Translation task cancelled", extra={
					"origin_language": segment.origin_language,
					"target_language": segment.target_language,
					"nc_session_id": segment.speaker_session_id,
					"tag": "translate",
				})
				raise
			except TimeoutError as e:
				LOGGER.error("Translation task timed out: %s", e, exc_info=e, extra={
					"origin_language": segment.origin_language,
					"target_language": segment.target_language,
					"nc_session_id": segment.speaker_session_id,
					"tag": "translate",
				})
			except Exception as e:
				LOGGER.error("Exception during translation task: %s", e, exc_info=e, extra={
					"origin_language": segment.origin_language,
					"target_language": segment.target_language,
					"nc_session_id": segment.speaker_session_id,
					"tag": "translate",
				})

	def __translation_task_cb(
		self,
		segment: TranslateInputOutput,
		output_queue: asyncio.Queue,
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
		if isinstance(exc, TranslateFatalException):
			LOGGER.error(
				"Translation service task raised a fatal exception,"
				" removing all translation clients",
				exc_info=exc,
				extra={
					"origin_language": segment.origin_language,
					"target_language": segment.target_language,
					"tag": "translate",
				},
			)
			# remove all translators
			with self.sid_translation_lang_map_lock:
				nc_session_ids = list(self.sid_translation_lang_map.keys())
			with self.translators_lock:
				for nc_session_id in nc_session_ids:
					_t = asyncio.create_task(self.remove_translator(nc_session_id))
					self.__asyncio_tasks_bin.add(_t)
					_t.add_done_callback(self.__asyncio_tasks_bin.discard)
			return

		if isinstance(exc, TranslateLangPairException):
			LOGGER.error(
				"Translation service task raised a fatal language pair exception,"
				" removing all translation clients for this language pair",
				exc_info=exc,
				extra={
					"origin_language": segment.origin_language,
					"target_language": segment.target_language,
					"tag": "translate",
				},
			)
			# remove all translators for this language pair
			with self.sid_translation_lang_map_lock:
				__nc_session_ids = [
					sid for sid, lang in self.sid_translation_lang_map.items()
					if lang == segment.target_language
				]
			with self.translators_lock:
				for nc_session_id in __nc_session_ids:
					_t = asyncio.create_task(self.remove_translator(nc_session_id))
					self.__asyncio_tasks_bin.add(_t)
					_t.add_done_callback(self.__asyncio_tasks_bin.discard)
			return

		if exc:
			LOGGER.error("Translation task raised an exception", exc_info=exc, extra={
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

	async def is_target_lang_supported(self, target_lang_id: str) -> bool:
		# todo: room owner id from talk api
		tmp_translator = OCPTranslator(self.room_lang_id, target_lang_id, self.room_token, "admin")
		return await tmp_translator.is_language_pair_supported()

	async def get_translation_languages(self) -> SupportedTranslationLanguages:
		if self.__translation_languages_cache:
			cached_time, cached_langs = self.__translation_languages_cache
			if (time() - cached_time) < CACHE_TRANSLATION_LANGS_FOR:
				return cached_langs

		# todo: room owner id from talk api
		# use any target lang id, e.g. english
		tmp_translator = OCPTranslator(self.room_lang_id, "en", self.room_token, "admin")
		langs = await tmp_translator.get_translation_languages()
		self.__translation_languages_cache = (time(), langs)
		return langs
