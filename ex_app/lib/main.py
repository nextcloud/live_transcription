"""Simplest example."""

import os
from contextlib import asynccontextmanager
from pathlib import Path

# isort: off
from livetypes import TranscribeRequest
# isort: on

from dotenv import load_dotenv
from fastapi import BackgroundTasks, FastAPI
from nc_py_api import NextcloudApp
from nc_py_api.ex_app import AppAPIAuthMiddleware, LogLvl, run_app, set_handlers
from service import Application, check_hpb_env_vars, get_hpb_settings

load_dotenv()
application: Application

@asynccontextmanager
async def lifespan(app: FastAPI):
    global application
    set_handlers(app, enabled_handler, default_init=False)
    check_hpb_env_vars()
    hpb_settings = get_hpb_settings()
    application = Application(hpb_settings)
    yield


APP = FastAPI(lifespan=lifespan)
# APP.add_middleware(AppAPIAuthMiddleware)  # set global AppAPI authentication middleware


@APP.post("/transcribeCall")
async def transcribe_call(req: TranscribeRequest, bg: BackgroundTasks):
    bg.add_task(application.transcript_req, req)


def report_100():
    nc = NextcloudApp()
    nc.set_init_status(100)

@APP.post("/init")
async def init_fn(bg: BackgroundTasks):
    # todo: download vosk en model
    print("init_fn called", flush=True)
    bg.add_task(report_100)
    ...


def enabled_handler(enabled: bool, nc: NextcloudApp) -> str:
    # This will be called each time application is `enabled` or `disabled`
    # NOTE: `user` is unavailable on this step, so all NC API calls that require it will fail as unauthorized.
    print(f"enabled={enabled}", flush=True)
    if enabled:
        nc.log(LogLvl.WARNING, f"Hello from {nc.app_cfg.app_name} :)")
    else:
        nc.log(LogLvl.WARNING, f"Bye bye from {nc.app_cfg.app_name} :(")
    # In case of an error, a non-empty short string should be returned, which will be shown to the NC administrator.
    return ""


if __name__ == "__main__":
    # Wrapper around `uvicorn.run`.
    # You are free to call it directly, with just using the `APP_HOST` and `APP_PORT` variables from the environment.
    os.chdir(Path(__file__).parent)
    run_app("main:APP", log_level="trace")
