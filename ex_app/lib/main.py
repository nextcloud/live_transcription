"""Live transcription app."""

import os
from contextlib import asynccontextmanager
from pathlib import Path
from threading import Event

# isort: off
from livetypes import LanguageSetRequest, SpreedClientException, TranscribeRequest, VoskException
# isort: on

from dotenv import load_dotenv
from fastapi import BackgroundTasks, Body, FastAPI
from fastapi.responses import JSONResponse
from models import LANGUAGE_MAP, LanguageModel
from nc_py_api import NextcloudApp
from nc_py_api.ex_app import AppAPIAuthMiddleware, LogLvl, persistent_storage, run_app, set_handlers
from service import Application

load_dotenv()
SERVICE: Application
ENABLED = Event()
MODELS_TO_FETCH = {
    "Nextcloud-AI/vosk-models": {
		"local_dir": persistent_storage(),
		"revision": "22028d0a4474d7fc210cf7867da9eac6e3eb22e0",
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
    yield


APP = FastAPI(lifespan=lifespan)
# APP.add_middleware(AppAPIAuthMiddleware)  # set global AppAPI authentication middleware


@APP.exception_handler(SpreedClientException)
async def spreed_client_exception_handler(request, exc: SpreedClientException):
    return JSONResponse(
        status_code=500,
        content={"error": str(exc)},
    )


@APP.get("/enabled")
async def get_enabled():
    return {"enabled": ENABLED.is_set()}


@APP.post("/setCallLanguage")
async def set_call_language(req: LanguageSetRequest):
    try:
        await SERVICE.set_call_language(req)
        return JSONResponse(status_code=200, content={"message": "Language set successfully for the call"})
    except VoskException as e:
        print(f"VoskException during set_call_language: {e}", flush=True)
        return JSONResponse(status_code=e.retcode, content={"error": str(e)})
    except SpreedClientException:
        raise
    except Exception as e:
        print(f"Exception during set_call_language: {e}", flush=True)
        return JSONResponse(status_code=500, content={"error": "Failed to set language for the call"})


@APP.post("/leaveCall")
async def leave_call(roomToken: str = Body(embed=True)):
    SERVICE.leave_call(roomToken)


@APP.post("/transcribeCall")
async def transcribe_call(req: TranscribeRequest, bg: BackgroundTasks):
    bg.add_task(SERVICE.transcript_req, req)


@APP.get("/getSupportedLanguages")
async def get_supported_languages() -> dict[str, LanguageModel]:
    return LANGUAGE_MAP


def enabled_handler(enabled: bool, nc: NextcloudApp) -> str:
    # This will be called each time application is `enabled` or `disabled`
    # NOTE: `user` is unavailable on this step, so all NC API calls that require it will fail as unauthorized.
    print(f"enabled={enabled}", flush=True)
    if enabled:
        ENABLED.set()
        nc.log(LogLvl.WARNING, f"Hello from {nc.app_cfg.app_name} :)")
    else:
        ENABLED.clear()
        nc.log(LogLvl.WARNING, f"Bye bye from {nc.app_cfg.app_name} :(")
    # In case of an error, a non-empty short string should be returned, which will be shown to the NC administrator.
    return ""


if __name__ == "__main__":
    # Wrapper around `uvicorn.run`.
    # You are free to call it directly, with just using the `APP_HOST` and `APP_PORT` variables from the environment.
    os.chdir(Path(__file__).parent)
    run_app("main:APP", log_level="trace")
