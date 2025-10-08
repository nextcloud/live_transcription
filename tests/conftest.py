import os

import pytest

WSS_NC_URL = "wss://cloud.hostname.tld/standalone-signaling/spreed"
WS_NC_URL = "ws://cloud.hostname.tld/standalone-signaling/spreed"

@pytest.fixture(scope="session")
def common_env_vars():
	os.environ["APP_ID"] = "myapp"
	os.environ["APP_VERSION"] = "1.0.0"
	os.environ["APP_SECRET"] = "dumm9"  # noqa: S105


@pytest.fixture(scope="function")
def wss_env_var(common_env_vars):
	os.environ["NEXTCLOUD_URL"] = WSS_NC_URL
	yield WSS_NC_URL
	os.unsetenv("NEXTCLOUD_URL")


@pytest.fixture(scope="function")
def ws_env_var(common_env_vars):
	os.environ["NEXTCLOUD_URL"] = WS_NC_URL
	yield WS_NC_URL
	os.unsetenv("NEXTCLOUD_URL")
