#
# SPDX-FileCopyrightText: 2025 Nextcloud GmbH and Nextcloud contributors
# SPDX-License-Identifier: AGPL-3.0-or-later
#

import logging
import os
import time

import niquests
from atranslator import ATranslator
from constants import OCP_TASK_PROC_SCHED_RETRIES
from livetypes import TranslateException
from nc_py_api import AsyncNextcloudApp, NextcloudException
from pydantic import BaseModel, ValidationError

LOGGER = logging.getLogger("lt.ocp_translator")  # todo: ocp_translator/translator

class Task(BaseModel):
	id: int
	status: str
	output: dict[str, str] | None = None


class Response(BaseModel):
	task: Task


class OCPTranslator(ATranslator):
	def __init__(self, origin_language: str, target_language: str, room_owner_id: str):
		# first user who requests this pair of translation
		self.room_owner_id = room_owner_id
		super().__init__(origin_language, target_language)

	async def translate(self, message: str) -> str:  # noqa: C901
		nc = AsyncNextcloudApp()
		await nc.set_user(self.room_owner_id)

		sched_tries = OCP_TASK_PROC_SCHED_RETRIES
		while True:
			try:
				sched_tries -= 1
				if sched_tries <= 0:
					raise TranslateException("Failed to schedule Nextcloud TaskProcessing task, tried 3 times")

				response = await nc.ocs(
					"POST",
					"/ocs/v1.php/taskprocessing/schedule",
					json={
						"type": "core:text2text:translate",
						"appId": os.getenv("APP_ID", "live_transcriber"),
						"input": { "input": message },
					},
				)
				break
			except NextcloudException as e:
				if e.status_code == niquests.codes.precondition_failed:  # type: ignore[attr-defined]
					raise TranslateException(
						"Failed to schedule Nextcloud TaskProcessing task: "
						"This app is setup to use a translation provider in Nextcloud. "
						"No such provider is installed on Nextcloud instance. "
						"Please install integration_openai, translate2 or any other text2text translate provider."
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

				raise TranslateException("Failed to schedule Nextcloud TaskProcessing task") from e

		try:
			task = Response.model_validate(response).task
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

				task = Response.model_validate(response).task
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
