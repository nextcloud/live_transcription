#
# SPDX-FileCopyrightText: 2025 Nextcloud GmbH and Nextcloud contributors
# SPDX-License-Identifier: AGPL-3.0-or-later
#

import logging
import os
import time
from typing import Literal

import niquests
from atranslator import ATranslator
from constants import CACHE_TRANSLATION_TASK_TYPES_FOR, OCP_TASK_PROC_SCHED_RETRIES
from livetypes import (
	LanguageModel,
	SupportedTranslationLanguages,
	TranslateException,
	TranslateFatalException,
	TranslateLangPairException,
)
from models import VOSK_SUPPORTED_LANGUAGE_MAP
from nc_py_api import AsyncNextcloudApp, NextcloudException
from pydantic import BaseModel, ValidationError

LOGGER = logging.getLogger("lt.ocp_translator")  # todo: ocp_translator/translator
TRANSLATE_TASK_TYPE = "core:text2text:translate"
AUTO_DETECT_ORIGIN_LANG_ID = "detect_language"

class Task(BaseModel):
	id: int
	status: str
	output: dict[str, str] | None = None


class TaskResponse(BaseModel):
	task: Task


InputShapeType = Literal[
	"Number",
	"Text",
	"Audio",
	"Image",
	"Video",
	"File",
	"Enum",
	"ListOfNumbers",
	"ListOfTexts",
	"ListOfImages",
	"ListOfAudios",
	"ListOfVideos",
	"ListOfFiles",
]

class InputShape(BaseModel):
	name: str
	description: str
	type: InputShapeType


class InputShapeEnum(BaseModel):
	name: str
	value: str


class TaskType(BaseModel):
	name: str
	description: str
	inputShape: dict[str, InputShape]
	inputShapeEnumValues: dict[str, list[InputShapeEnum]]
	inputShapeDefaults: dict[str, str | int | float]
	optionalInputShape: dict[str, InputShape]
	optionalInputShapeEnumValues: dict[str, list[InputShapeEnum]]
	optionalInputShapeDefaults: dict[str, str | int | float]
	outputShape: dict[str, InputShape]
	outputShapeEnumValues: dict[str, list[InputShapeEnum]]
	optionalOutputShape: dict[str, InputShape]
	optionalOutputShapeEnumValues: dict[str, list[InputShapeEnum]]


class TaskTypesResponse(BaseModel):
	types: dict[str, TaskType]


class OCPTranslator(ATranslator):
	def __init__(self, origin_language: str, target_language: str, room_token: str, room_owner_id: str):
		super().__init__(origin_language, target_language, room_token)
		self.room_owner_id = room_owner_id

		self.__ocp_origin_lang_id = origin_language
		self.__task_types_cache: tuple[float, TaskTypesResponse] | None = None

	async def translate(self, message: str) -> str:  # noqa: C901
		nc = AsyncNextcloudApp()
		await nc.set_user(self.room_owner_id)

		sched_tries = OCP_TASK_PROC_SCHED_RETRIES
		while True:
			try:
				sched_tries -= 1
				if sched_tries <= 0:
					raise TranslateException("Failed to schedule Nextcloud TaskProcessing task, tried 3 times")

				# todo: webhook callback instead of polling
				response = await nc.ocs(
					"POST",
					"/ocs/v2.php/taskprocessing/schedule",
					json={
						"type": TRANSLATE_TASK_TYPE,
						"appId": os.getenv("APP_ID", "live_transcription"),
						"customId": f"lt-{self.room_token}-{self.origin_language}-{self.target_language}",
						"input": {
							"input": message,
							"origin_language": self.__ocp_origin_lang_id,
							"target_language": self.target_language,
						},
					},
				)
				break
			except NextcloudException as e:
				if e.status_code == niquests.codes.precondition_failed:  # type: ignore[attr-defined]
					raise TranslateFatalException(
						"Failed to schedule Nextcloud TaskProcessing task: "
						"This app is setup to use a translation provider in Nextcloud. "
						"No such provider is installed on Nextcloud instance. "
						"Please install integration_openai, translate2 or any other text2text translate provider.",
					) from e

				if e.status_code == niquests.codes.too_many_requests:  # type: ignore[attr-defined]
					LOGGER.warning("Rate limited during task scheduling, waiting 30s before retrying", extra={
						"origin_language": self.origin_language,
						"target_language": self.target_language,
						"tries_left": sched_tries,
						"tag": "translate",
					})
					time.sleep(30)
					continue

				LOGGER.error("NextcloudException during task scheduling", exc_info=e, extra={
					"origin_language": self.origin_language,
					"target_language": self.target_language,
					"tries_left": sched_tries,
					"nc_exc_reason": str(e.reason),
					"nc_exc_info": str(e.info),
					"nc_exc_status_code": str(e.status_code),
					"tag": "translate",
				})
				raise TranslateException("Failed to schedule Nextcloud TaskProcessing task") from e

		try:
			task = TaskResponse.model_validate(response).task
			LOGGER.debug("Initial task schedule response", extra={
				"origin_language": self.origin_language,
				"target_language": self.target_language,
				"task": task,
				"tag": "translate",
			})

			i = 0
			# wait for 30 minutes
			while task.status != "STATUS_SUCCESSFUL" and task.status != "STATUS_FAILED" and i < 60 * 6:
				if i < 60 * 3:
					time.sleep(5)
					i += 1
				else:
					# pool every 10 secs in the second half
					time.sleep(10)
					i += 2

				try:
					response = await nc.ocs("GET", f"/ocs/v1.php/taskprocessing/task/{task.id}")
				except NextcloudException as e:
					if e.status_code == niquests.codes.too_many_requests:  # type: ignore[attr-defined]
						LOGGER.warning("Rate limited during task polling, waiting 10s before retrying", extra={
							"origin_language": self.origin_language,
							"target_language": self.target_language,
							"tries_so_far": i,
							"tag": "translate",
						})
						time.sleep(10)
						i += 2
						continue
					raise TranslateException("Failed to poll Nextcloud TaskProcessing task") from e
				except niquests.RequestException as e:
					LOGGER.warning("Ignored error during task polling", exc_info=e, extra={
						"origin_language": self.origin_language,
						"target_language": self.target_language,
						"tries_so_far": i,
						"tag": "translate",
					})
					time.sleep(5)
					i += 1
					continue

				task = TaskResponse.model_validate(response).task
				LOGGER.debug(f"Task poll ({i * 5}s) response", extra={  # noqa: G004
					"origin_language": self.origin_language,
					"target_language": self.target_language,
					"tries_so_far": i,
					"task": task,
					"tag": "translate",
				})
		except ValidationError as e:
			raise TranslateException("Failed to parse Nextcloud TaskProcessing task result") from e

		if task.status != "STATUS_SUCCESSFUL":
			raise TranslateException("Nextcloud TaskProcessing Task failed")

		if not isinstance(task.output, dict) or "output" not in task.output:
			raise TranslateException('"output" key not found in Nextcloud TaskProcessing task result')

		return task.output["output"]

	async def __get_task_types(self) -> TaskTypesResponse:
		nc = AsyncNextcloudApp()
		await nc.set_user(self.room_owner_id)

		if self.__task_types_cache:
			cached_time, cached_ttypes = self.__task_types_cache
			if (time.time() - cached_time) < CACHE_TRANSLATION_TASK_TYPES_FOR:
				return cached_ttypes

		try:
			response = await nc.ocs(
				"GET",
				"/ocs/v2.php/taskprocessing/tasktypes",
			)
		except NextcloudException as e:
			raise TranslateFatalException("Failed to fetch Nextcloud TaskProcessing types") from e

		try:
			task_types = TaskTypesResponse.model_validate(response)
			LOGGER.debug("Fetched task types", extra={
				"task_types": task_types,
				"tag": "translate",
			})
		except (KeyError, TypeError, ValidationError) as e:
			raise TranslateException("Failed to parse Nextcloud TaskProcessing types") from e

		if TRANSLATE_TASK_TYPE not in task_types.types:
			raise TranslateFatalException(
				"Nextcloud TaskProcessing does not have text2text translate task type, "
				"this app is setup to use a translation provider in Nextcloud. "
				"No such provider is installed on Nextcloud instance. "
				"Please install integration_openai, translate2 or any other text2text translate provider.",
			)

		if (
			task_types.types[TRANSLATE_TASK_TYPE].inputShape.get("origin_language") is None
			or task_types.types[TRANSLATE_TASK_TYPE].inputShape.get("target_language") is None
		):
			raise TranslateFatalException(
				'Nextcloud TaskProcessing text2text translate task type does not have both "origin_language" and'
				' "target_language" input shapes, which is unexpected and cannot be worked with,'
				' shutting down translator.',
			)

		self.__task_types_cache = (time.time(), task_types)
		return task_types

	async def is_language_pair_supported(self):
		"""Also sets self.__ocp_origin_lang_id to AUTO_DETECT_ORIGIN_LANG_ID if the origin language is not supported but auto-detect is."""  # noqa: E501

		task_types = await self.__get_task_types()

		if not any(
			olang.value == self.origin_language
			for olang in task_types.types[TRANSLATE_TASK_TYPE].inputShapeEnumValues["origin_language"]
		):
			if not any(
				olang.value == AUTO_DETECT_ORIGIN_LANG_ID
				for olang in task_types.types[TRANSLATE_TASK_TYPE].inputShapeEnumValues["origin_language"]
			):
				raise TranslateLangPairException(
					f'Nextcloud TaskProcessing text2text translate task type does not support origin language'
					f' "{self.origin_language}", nor any auto-detection of the origin language.'
				)
			self.__ocp_origin_lang_id = AUTO_DETECT_ORIGIN_LANG_ID

		if not any(
			tlang.value == self.target_language
			for tlang in task_types.types[TRANSLATE_TASK_TYPE].inputShapeEnumValues["target_language"]
		):
			raise TranslateLangPairException(
				"Nextcloud TaskProcessing text2text translate task type does not support translation to target language"
				f' "{self.target_language}".',
			)

	async def get_translation_languages(self) -> SupportedTranslationLanguages:
		"""
		Raises
		------
			TranslateFatalException
			TranslateException
		"""  # noqa
		task_types = await self.__get_task_types()
		olangs = {
			olang.value: VOSK_SUPPORTED_LANGUAGE_MAP.get(olang.value, LanguageModel(name=olang.value))
			for olang in task_types.types[TRANSLATE_TASK_TYPE].inputShapeEnumValues["origin_language"]
		}
		tlangs = {
			tlang.value: VOSK_SUPPORTED_LANGUAGE_MAP.get(tlang.value, LanguageModel(name=tlang.value))
			for tlang in task_types.types[TRANSLATE_TASK_TYPE].inputShapeEnumValues["target_language"]
		}
		return SupportedTranslationLanguages(
			origin_languages=olangs,
			target_languages=tlangs,
		)
