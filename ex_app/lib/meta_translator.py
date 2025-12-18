#
# SPDX-FileCopyrightText: 2025 Nextcloud GmbH and Nextcloud contributors
# SPDX-License-Identifier: AGPL-3.0-or-later
#

import asyncio
import logging
import threading
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
		self.translators_lock = asyncio.Lock()
		self.sid_translation_lang_map: dict[str, str] = {}  # {"nc_session_id": "target_lang_id"}
		self.sid_translation_lang_map_lock = asyncio.Lock()
		self.task_lock = asyncio.Lock()
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

		async with self.sid_translation_lang_map_lock:
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
				await self.__remove_translator_int(self.sid_translation_lang_map[nc_session_id], nc_session_id)
			self.sid_translation_lang_map[nc_session_id] = target_lang_id

		async with self.translators_lock:
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

		await self.start()

	async def is_translation_target(self, nc_session_id: str) -> bool:
		async with self.sid_translation_lang_map_lock:
			return nc_session_id in self.sid_translation_lang_map

	async def __remove_translator_int(self, target_lang_id: str, nc_session_id: str):
		async with self.translators_lock:
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
		async with self.sid_translation_lang_map_lock:
			if nc_session_id not in self.sid_translation_lang_map:
				LOGGER.info("NC session id not found in the translation map when trying to remove it", extra={
					"nc_session_id": nc_session_id,
					"room_lang_id": self.room_lang_id,
					"tag": "translate",
				})
				return
			target_lang_id = self.sid_translation_lang_map[nc_session_id]
			await self.__remove_translator_int(target_lang_id, nc_session_id)
			del self.sid_translation_lang_map[nc_session_id]
			LOGGER.debug("Removed NC session id from the translation map", extra={
				"nc_session_id": nc_session_id,
				"room_lang_id": self.room_lang_id,
				"tag": "translate",
			})

			if bool(self.sid_translation_lang_map):
				self.should_translate.set()
			else:
				self.should_translate.clear()
				await self.shutdown()

	async def start(self):
		async with self.task_lock:
			if self.task and not self.task.done():
				LOGGER.debug("Translation task already running", extra={
					"room_lang_id": self.room_lang_id,
					"tag": "translate",
				})
				return
			self.task = asyncio.create_task(self.__run_translation_task(), name=f"MetaTranslator-{self.room_lang_id}")
			self.task.add_done_callback(self.__done_cb)
		self.should_translate.set()

	async def shutdown(self):
		async with self.task_lock:
			if self.task and not self.task.done():
				LOGGER.debug("Cancelling translation task", extra={
					"room_lang_id": self.room_lang_id,
					"tag": "translate",
				})
				self.task.cancel()

	def __done_cb(self, future: asyncio.Future):
		if not self.task:
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
			# todo: perf optimization: get multiple items if the queue is long,
			# todo: join them with "separator" and translate in one go
			segment: TranslateInputOutput = await self.translate_queue_input.get()  # type: ignore[annotation-unchecked]
			# todo
			LOGGER.debug("Translate input queue item:", extra={
				"segment": segment,
				"tag": "translate",
			})

			try:
				async with self.translators_lock:
					_tasks = []
					for translator in self.translators.values():
						segment.target_language = translator.target_language
						segment.target_nc_session_ids = translator.nc_session_ids.copy()
						_tasks.append(asyncio.wait_for(
							self.__handle_translation(translator, segment),
							timeout=OCP_TASK_TIMEOUT,
						))
					# await all translation tasks, they have their own callbacks to put results into the output queue
					# the translators lock is held during this time to avoid translators being removed while translating
					#   and for translated segments to be in order
					await asyncio.gather(*_tasks)
			except asyncio.CancelledError:
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

	async def __shutdown_translators(self, nc_session_ids: list[str]):
		LOGGER.info("Shutting down translators for NC session ids: %s", nc_session_ids, extra={
			"room_lang_id": self.room_lang_id,
			"tag": "translate",
		})

		for nc_session_id in nc_session_ids:
			try:
				await self.remove_translator(nc_session_id)
			except asyncio.CancelledError:
				raise
			except Exception as e:
				LOGGER.error("Exception during removing translator in shutdown translators", exc_info=e, extra={
					"room_lang_id": self.room_lang_id,
					"nc_session_id": nc_session_id,
					"tag": "translate",
				})

	async def __shutdown_all_translators(self):
		LOGGER.info("Shutting down all translators", extra={
			"room_lang_id": self.room_lang_id,
			"tag": "translate",
		})
		async with self.sid_translation_lang_map_lock:
			nc_session_ids = list(self.sid_translation_lang_map.copy().keys())

		await self.__shutdown_translators(nc_session_ids)

	async def __shutdown_translators_for_lang(self, target_lang_id: str):
		LOGGER.info("Shutting down translators for target language: %s", target_lang_id, extra={
			"room_lang_id": self.room_lang_id,
			"tag": "translate",
		})
		async with self.sid_translation_lang_map_lock:
			nc_session_ids = [
				sid for sid, lang in self.sid_translation_lang_map.items()
				if lang == target_lang_id
			]

		await self.__shutdown_translators(nc_session_ids)

	async def __handle_translation(
		self,
		translator: ATranslator,
		segment: TranslateInputOutput,
	):
		try:
			segment.message = await translator.translate(segment.message)
			LOGGER.debug("Translation task completely successfully", extra={
				"origin_language": segment.origin_language,
				"target_language": segment.target_language,
				"translated_text": segment.message,
				"tag": "translate",
			})
			self.translate_queue_output.put_nowait(segment)
		except asyncio.CancelledError:
			raise
		except TranslateLangPairException as e:
			LOGGER.error(
				"Translation service task raised a fatal language pair exception,"
				" removing all translation clients for this language pair",
				exc_info=e,
				extra={
					"origin_language": segment.origin_language,
					"target_language": segment.target_language,
					"tag": "translate",
				},
			)
			# remove all translators for this language pair
			_t = asyncio.create_task(self.__shutdown_translators_for_lang(segment.target_language))
			self.__asyncio_tasks_bin.add(_t)
			_t.add_done_callback(self.__asyncio_tasks_bin.discard)
		except TranslateFatalException as e:
			LOGGER.error(
				"Translation service task raised a fatal exception,"
				" removing all translation clients",
				exc_info=e,
				extra={
					"origin_language": segment.origin_language,
					"target_language": segment.target_language,
					"tag": "translate",
				},
			)
			# remove all translators
			_t = asyncio.create_task(self.__shutdown_all_translators())
			self.__asyncio_tasks_bin.add(_t)
			_t.add_done_callback(self.__asyncio_tasks_bin.discard)
		except TranslateException as e:
			LOGGER.error("Translation task raised a translation exception", exc_info=e, extra={
				"origin_language": segment.origin_language,
				"target_language": segment.target_language,
				"tag": "translate",
			})
		except Exception as e:
			LOGGER.error("Translation task raised an exception", exc_info=e, extra={
				"origin_language": segment.origin_language,
				"target_language": segment.target_language,
				"tag": "translate",
			})

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
