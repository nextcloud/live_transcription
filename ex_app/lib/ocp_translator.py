#
# SPDX-FileCopyrightText: 2025 Nextcloud GmbH and Nextcloud contributors
# SPDX-License-Identifier: AGPL-3.0-or-later
#

import asyncio
import json
import logging
import os
from typing import Literal

import niquests
from atranslator import ATranslator
from constants import OCP_TASK_PROC_SCHED_RETRIES
from livetypes import LanguageModel, SupportedTranslationLanguages, TranslateException, TranslateFatalException
from models import LANGUAGE_MAP
from nc_py_api import AsyncNextcloudApp, NextcloudException
from pydantic import BaseModel, ValidationError
from utils import timed_cache_async

LOGGER = logging.getLogger("lt.ocp_translator")
TRANSLATE_TASK_TYPE = "core:text2text:translate"
AUTO_DETECT_ORIGIN_LANG_ID = "detect_language"
OCP_ORIGIN_LANG_ID: str | None = None


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
	def __init__(self, origin_language: str, target_language: str, room_token: str):
		super().__init__(origin_language, target_language, room_token)

	def __try_parse_ocs_response(self, response: niquests.Response | None) -> dict | str:
		if response is None or response.text is None:
			return "No response"
		try:
			ocs_response = json.loads(response.text)
			if not (ocs_data := ocs_response.get("ocs", {}).get("data")):
				return response.text
			return ocs_data
		except json.JSONDecodeError:
			return response.text

	async def translate(self, message: str) -> str:  # noqa: C901
		nc = AsyncNextcloudApp()

		sched_tries = OCP_TASK_PROC_SCHED_RETRIES
		while True:
			try:
				sched_tries -= 1
				if sched_tries <= 0:
					raise TranslateException("Failed to schedule Nextcloud TaskProcessing task, tried 3 times")

				# todo: webhook callback instead of polling, maybe just tighten polling intervals
				response = await nc.ocs(
					"POST",
					"/ocs/v2.php/taskprocessing/tasks_consumer/schedule",
					json={
						"type": TRANSLATE_TASK_TYPE,
						"appId": os.getenv("APP_ID", "live_transcription"),
						"customId": f"lt-{self.room_token}-{self.origin_language}-{self.target_language}",
						"input": {
							"input": message,
							"origin_language": OCP_ORIGIN_LANG_ID or self.origin_language,
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
					await asyncio.sleep(30)
					continue

				if e.status_code // 100 == 4:
					# todo: use this after NextcloudException.response is added in nc_py_api
					ocs_response = self.__try_parse_ocs_response(e.response)
					raise TranslateFatalException(
						f"Failed to schedule Nextcloud TaskProcessing task due to client error: {ocs_response}",
					) from e

				LOGGER.error("NextcloudException during task scheduling", exc_info=e, extra={
					"origin_language": self.origin_language,
					"target_language": self.target_language,
					"tries_left": sched_tries,
					"nc_exc_reason": str(e.reason),
					"nc_exc_info": str(e.info),
					"nc_exc_status_code": str(e.status_code),
					"tag": "translate",
				})
				# todo: use this after NextcloudException.response is added in nc_py_api
				ocs_response = self.__try_parse_ocs_response(e.response)
				raise TranslateException(f"Failed to schedule Nextcloud TaskProcessing task: {ocs_response}") from e
				# raise TranslateException("Failed to schedule Nextcloud TaskProcessing task") from e

		try:
			task = TaskResponse.model_validate(response).task
			LOGGER.debug("Initial task schedule response", extra={
				"origin_language": self.origin_language,
				"target_language": self.target_language,
				"task": task,
				"tag": "translate",
			})

			i = 0
			wait_time = 2
			# wait for 30 minutes
			while task.status != "STATUS_SUCCESSFUL" and task.status != "STATUS_FAILED" and i < 60 * 6:
				if i < 60 * 3:
					await asyncio.sleep(min(wait_time**i, 5)) # 1,2,4,5,5,5,5,5,...
					i += 1
				else:
					# poll every 10 secs in the second half
					await asyncio.sleep(10)
					i += 2

				try:
					response = await nc.ocs("GET", f"/ocs/v1.php/taskprocessing/tasks_consumer/task/{task.id}")
				except NextcloudException as e:
					if e.status_code == niquests.codes.too_many_requests:  # type: ignore[attr-defined]
						LOGGER.warning("Rate limited during task polling, waiting 10s before retrying", extra={
							"origin_language": self.origin_language,
							"target_language": self.target_language,
							"tries_so_far": i,
							"tag": "translate",
						})
						await asyncio.sleep(10)
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
					await asyncio.sleep(5)
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

	@staticmethod
	@timed_cache_async()
	async def __get_task_types() -> TaskTypesResponse:
		"""
		Raises
		------
			TranslateFatalException
			TranslateException
		"""  # noqa
		nc = AsyncNextcloudApp()

		try:
			response = await nc.ocs(
				"GET",
				"/ocs/v2.php/taskprocessing/tasks_consumer/tasktypes",
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
			"origin_language" not in task_types.types[TRANSLATE_TASK_TYPE].inputShape
			or "target_language" not in task_types.types[TRANSLATE_TASK_TYPE].inputShape
		):
			raise TranslateFatalException(
				'Nextcloud TaskProcessing text2text translate task type does not have both "origin_language" and'
				' "target_language" input shapes, which is unexpected and cannot be worked with,'
				' shutting down translator.',
			)

		return task_types

	@staticmethod
	@timed_cache_async()
	async def is_language_pair_supported(origin_language: str, target_language: str) -> bool:
		"""Also sets self.__ocp_origin_lang_id to AUTO_DETECT_ORIGIN_LANG_ID if the origin language is not supported but auto-detect is."""  # noqa: E501

		global OCP_ORIGIN_LANG_ID

		task_types = await OCPTranslator.__get_task_types()

		if not any(
			olang.value == origin_language
			for olang in task_types.types[TRANSLATE_TASK_TYPE].inputShapeEnumValues["origin_language"]
		):
			if not any(
				olang.value == AUTO_DETECT_ORIGIN_LANG_ID
				for olang in task_types.types[TRANSLATE_TASK_TYPE].inputShapeEnumValues["origin_language"]
			):
				LOGGER.warning(
					'Nextcloud TaskProcessing text2text translate task type does not support origin language'
					' "%s", nor any auto-detection of the origin language.',
					origin_language,
				)
				return False
			OCP_ORIGIN_LANG_ID = AUTO_DETECT_ORIGIN_LANG_ID

		if not any(
			tlang.value == target_language
			for tlang in task_types.types[TRANSLATE_TASK_TYPE].inputShapeEnumValues["target_language"]
		):
			LOGGER.warning(
				"Nextcloud TaskProcessing text2text translate task type does not support translation"
				' to target language "%s".',
				target_language,
			)
			return False
		return True

	@staticmethod
	@timed_cache_async()
	async def get_translation_languages() -> SupportedTranslationLanguages:
		"""
		Raises
		------
			TranslateFatalException
			TranslateException
		"""  # noqa
		task_types = await OCPTranslator.__get_task_types()
		olangs = {
			olang.value: LANGUAGE_MAP.get(olang.value, LanguageModel(name=olang.name or olang.value))
			for olang in task_types.types[TRANSLATE_TASK_TYPE].inputShapeEnumValues["origin_language"]
			if olang.value
		}
		tlangs = {
			tlang.value: LANGUAGE_MAP.get(tlang.value, LanguageModel(name=tlang.name or tlang.value))
			for tlang in task_types.types[TRANSLATE_TASK_TYPE].inputShapeEnumValues["target_language"]
			if tlang.value
		}
		return SupportedTranslationLanguages(
			origin_languages=olangs,
			target_languages=tlangs,
		)
