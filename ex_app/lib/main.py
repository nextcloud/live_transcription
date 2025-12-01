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
from livetypes import LanguageSetRequest, SpreedClientException, TranscribeRequest, VoskException

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
from models import LANGUAGE_MAP, LanguageModel
from nc_py_api import AsyncNextcloudApp, NextcloudApp
from nc_py_api.ex_app import AppAPIAuthMiddleware, persistent_storage, run_app, set_handlers, setup_nextcloud_logging
from service import Application

LOGGER_CONFIG_NAME = "../../logger_config.yaml"
LOGGER = logging.getLogger("lt")
SERVICE: Application
ENABLED = Event()
MODELS_TO_FETCH = {
	"Nextcloud-AI/vosk-models": {
		"local_dir": persistent_storage(),
		"revision": "06f2f156dcd79092400891afb6cf8101e54f6ba2",
	}
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
async def set_call_language(req: LanguageSetRequest):
	try:
		if not req.langId or req.langId not in LANGUAGE_MAP:
			return JSONResponse(
				status_code=400,
				content={"error": "Invalid or unsupported language ID provided."}
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


@ROUTER_V1.post("/call/leave")
async def leave_call(roomToken: str = Body(embed=True)):
	await SERVICE.leave_call(roomToken)


@ROUTER_V1.post("/call/transcribe")
async def transcribe_call(req: TranscribeRequest):
	await SERVICE.transcript_req(req)


@ROUTER_V1.get("/languages")
def get_supported_languages() -> dict[str, LanguageModel]:
	return LANGUAGE_MAP


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
				"supported_languages": LANGUAGE_MAP,
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
