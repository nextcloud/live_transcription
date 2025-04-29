"""Simplest example."""

import asyncio
import datetime
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated

# isort: off
from livetypes import HPBSettings
# isort: on

from dotenv import load_dotenv
from fastapi import BackgroundTasks, Depends, FastAPI, Request, WebSocket
from fastapi.responses import JSONResponse
from nc_py_api import NextcloudApp, NextcloudException
from nc_py_api.ex_app import AppAPIAuthMiddleware, LogLvl, nc_app, run_app, set_handlers
from pydantic import BaseModel
from service import Application

load_dotenv()

@asynccontextmanager
async def lifespan(app: FastAPI):
    set_handlers(app, enabled_handler, default_init=False)
    yield


APP = FastAPI(lifespan=lifespan)
# APP.add_middleware(AppAPIAuthMiddleware)  # set global AppAPI authentication middleware

application = Application()

class TranscribeRequest(BaseModel):
    roomToken: str
    authToken: str
    hpbLocation: str


@APP.post("/transcribeCall")
async def transcribe_call(req: TranscribeRequest, bg: BackgroundTasks):
    try:
        import base64

        import httpx

        # todo
        # nc = NextcloudApp()
        # settings = nc.ocs("/ocs/v2.php/apps/spreed/api/v3/signaling/settings", params={ 'token': req.roomToken })
        settings = httpx.get(f'{os.environ["NEXTCLOUD_URL2"]}/ocs/v2.php/apps/spreed/api/v3/signaling/settings',
            headers={
                'OCS-APIRequest': 'true',
                'Authorization': 'Bearer ' + base64.b64encode(f'{os.environ['NC_USER']}:{os.environ['NC_PASS']}'.encode()).decode(),
            },
            params={
                'token': req.roomToken,
                'format': 'json',
            },
        )
        if settings.status_code != 200:
            raise NextcloudException(f"Error getting settings: {settings.text}")
        print(f"Settings: {settings.json()}", flush=True)
        settings = settings.json()['ocs']['data']

    except NextcloudException as e:
        print(f"Error getting settings: {e}")
        return JSONResponse(status_code=500, content={"error": "Error getting HPB settings for " + req.hpbLocation})

    # await application.join_call(req.roomToken, req.authToken, req.hpbLocation, HPBSettings(**settings))
    bg.add_task(application.join_call, req.roomToken, req.authToken, req.hpbLocation, HPBSettings(**settings))


def report_100():
    nc = NextcloudApp()
    nc.set_init_status(100)

@APP.post("/init")
async def init_fn(bg: BackgroundTasks):
    # download vosk en model
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
