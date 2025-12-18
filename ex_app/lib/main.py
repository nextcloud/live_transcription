#
# SPDX-FileCopyrightText: 2025 Nextcloud GmbH and Nextcloud contributors
# SPDX-License-Identifier: AGPL-3.0-or-later
#

import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path
from threading import Event

# isort: off
from livetypes import (
	LanguageModel,
	RoomLanguageSetRequest,
	SpreedClientException,
	SupportedTranslationLanguages,
	TargetLanguageSetRequest,
	TranscribeRequest,
	TranslateFatalException,
	TranslateLangPairException,
	TranscriptTargetNotFoundException,
	VoskException,
)
from dotenv import load_dotenv
load_dotenv()

# skip certificate verification for all nc_py_api connections if env var is set
__skip_cert_verify = os.environ.get("SKIP_CERT_VERIFY", "false").lower()
if __skip_cert_verify in ("true", "1"):
	os.environ["NPA_NC_CERT"] = "false"
# isort: on

import uvicorn
from fastapi import Body, FastAPI
from fastapi.responses import JSONResponse
from fastapi.routing import APIRouter
from logger import get_logging_config, setup_logging
from models import VOSK_SUPPORTED_LANGUAGE_MAP
from nc_py_api import AsyncNextcloudApp, NextcloudApp
from nc_py_api.ex_app import AppAPIAuthMiddleware, persistent_storage, run_app, set_handlers, setup_nextcloud_logging
from service import Application

LOGGER_CONFIG_NAME = "../../logger_config.yaml"
LOGGER = logging.getLogger("lt")
SERVICE: Application
ENABLED = Event()
MODELS_TO_FETCH = {
	# todo: some huggingface_hub/tqdm error
	# "Nextcloud-AI/vosk-models": {
	# 	"local_dir": persistent_storage(),
	# 	"revision": "06f2f156dcd79092400891afb6cf8101e54f6ba2",
	# }
}
# todo: declarative settings for language override and model download


@asynccontextmanager
async def lifespan(app: FastAPI):
	global SERVICE
	set_handlers(app, enabled_handler, models_to_fetch=MODELS_TO_FETCH)
	SERVICE = Application()
	nc = NextcloudApp()
	if nc.enabled_state:
		ENABLED.set()
	LOGGER.info("App is %s on startup", "enabled" if ENABLED.is_set() else "disabled")
	yield


APP = FastAPI(lifespan=lifespan)
APP.add_middleware(AppAPIAuthMiddleware)  # set global AppAPI authentication middleware
ROUTER_V1 = APIRouter(prefix="/api/v1", tags=["v1"])

@APP.exception_handler(SpreedClientException)
async def spreed_client_exception_handler(request, exc: SpreedClientException):
	return JSONResponse(
		status_code=500,
		content={"error": str(exc)},
	)


@APP.get("/enabled")
async def get_enabled():
	return {"enabled": ENABLED.is_set()}


@ROUTER_V1.post("/call/set-language",
	responses={
		200: {"description": "Language set successfully for the call"},
		400: {"description": "Invalid or unsupported language ID provided."},
		404: {"description": "Spreed client not found for the provided room token."},
		500: {"description": "Failed to set language for the call."}
	})
async def set_call_language(req: RoomLanguageSetRequest):
	try:
		if not req.langId or req.langId not in VOSK_SUPPORTED_LANGUAGE_MAP:
			return JSONResponse(
				status_code=400,
				content={"error": "Invalid or unsupported language ID provided."},
			)
		await SERVICE.set_call_language(req)
		return JSONResponse(status_code=200, content={"message": "Language set successfully for the call"})
	except VoskException as e:
		LOGGER.exception("VoskException during set_call_language", exc_info=e)
		return JSONResponse(status_code=e.retcode, content={"error": str(e)})
	except SpreedClientException as e:
		return JSONResponse(status_code=404, content={"error": str(e)})
	except Exception as e:
		LOGGER.exception("Exception during set_call_language", exc_info=e)
		return JSONResponse(status_code=500, content={"error": "Failed to set language for the call"})


# for translation
@ROUTER_V1.get("/translation/languages", responses={
		200: {"description": "Supported origin and target translation languages fetched successfully."},
		404: {"description": "Spreed client not found for the provided room token."},
		500: {"description": "An error occurred while fetching supported origin and target translation languages."},
		550: {"description": (
			"A fatal error occurred while fetching supported origin and target translation languages."
			" Do not retry any further attempts until the underlying issue is resolved."
			" The translation provider may be not installed or misconfigured. See the logs for details."
		)},
	},
	response_model=None,
	description=(
		"Fetch supported origin and target translation languages."
		' The origin language list can contain "detect_language" as a special value indicating auto-detection support.'
	),
)
async def get_translation_languages(roomToken: str) -> SupportedTranslationLanguages | JSONResponse:
	try:
		return await SERVICE.get_translation_languages(roomToken)
	except SpreedClientException as e:
		return JSONResponse(status_code=404, content={"error": str(e)})
	except TranslateFatalException as e:
		LOGGER.warning("TranslateFatalException during get_translation_languages", exc_info=e)
		return JSONResponse(
			status_code=550,
			content={"error": "A fatal error occurred while fetching translation languages."},
		)
	except Exception as e:
		LOGGER.exception("Exception during get_translation_languages", exc_info=e)
		return JSONResponse(
			status_code=500,
			content={"error": "An error occurred while fetching translation languages."},
		)


# for translation
@ROUTER_V1.post("/translation/set-target-language",
	description=(
		"Set the target translation language for a participant in a call."
		" Set langId to null to disable translation for the participant."
	),
	responses={
		200: {"description": "Target translation language set successfully for the participant."},
		400: {"description": "Invalid, unsupported or same language ID provided as the origin language."},
		404: {"description": "Spreed client not found for the provided room token."},
		412: {"description": (
			"The participant has not yet enabled transcription in the call,"
			" which is required to receive translated text."
		)},
		500: {"description": "Failed to set the target translation language for the participant."},
		550: {"description": (
			"A fatal error occurred while setting the target translation language for the participant."
			" Do not retry any further translation attempts until the underlying issue is resolved."
			" The translation provider may be not installed or misconfigured. See the logs for details."
		)},
	})
async def set_target_language(req: TargetLanguageSetRequest):
	try:
		await SERVICE.set_target_language(req)
		return JSONResponse(
			status_code=200,
			content={"message": "Target translation language set successfully for the participant."},
		)
	except TranslateLangPairException as e:
		return JSONResponse(status_code=400, content={"error": str(e)})
	except SpreedClientException as e:
		return JSONResponse(status_code=404, content={"error": str(e)})
	except TranscriptTargetNotFoundException as e:
		return JSONResponse(status_code=412, content={"error": str(e)})
	except TranslateFatalException as e:
		LOGGER.warning("TranslateFatalException during set_target_language", exc_info=e)
		return JSONResponse(status_code=550, content={"error": str(e)})
	except Exception as e:
		LOGGER.exception("Exception during set_target_language", exc_info=e)
		return JSONResponse(
			status_code=500,
			content={"error": "Failed to set the target translation language for the participant."},
		)


@ROUTER_V1.post("/call/leave")
async def leave_call(roomToken: str = Body(embed=True)):
	await SERVICE.leave_call(roomToken)


@ROUTER_V1.post("/call/transcribe")
async def transcribe_call(req: TranscribeRequest):
	await SERVICE.transcript_req(req)


@ROUTER_V1.get("/languages")
def get_supported_languages() -> dict[str, LanguageModel]:
	return VOSK_SUPPORTED_LANGUAGE_MAP


APP.include_router(ROUTER_V1)


# until capabilities is supported in nc_py_api
@APP.get("/capabilities")
async def get_capabilities():
	return {
		f"{os.environ['APP_ID']}": {
			"version": f"{os.environ['APP_VERSION']}",
			"features": [
				"live_transcription",
			],
			"live_transcription": {
				"supported_languages": VOSK_SUPPORTED_LANGUAGE_MAP,
			},
		}
	}


def enabled_handler(enabled: bool, nc: NextcloudApp | AsyncNextcloudApp) -> str:
	print(f"enabled={enabled}", flush=True)
	if enabled:
		ENABLED.set()
	else:
		ENABLED.clear()
	return ""


if __name__ == "__main__":
	os.chdir(Path(__file__).parent)

	logging_config = get_logging_config(LOGGER_CONFIG_NAME)
	setup_logging(logging_config)
	setup_nextcloud_logging("lt", logging.WARNING)

	uv_log_config = uvicorn.config.LOGGING_CONFIG  # pyright: ignore[reportAttributeAccessIssue]
	uv_log_config["formatters"]["json"] = logging_config["formatters"]["json"]
	uv_log_config["handlers"]["file_json"] = logging_config["handlers"]["file_json"]

	uv_log_config["loggers"]["uvicorn"]["handlers"].append("file_json")
	uv_log_config["loggers"]["uvicorn.access"]["handlers"].append("file_json")

	run_app("main:APP", log_level="info")
