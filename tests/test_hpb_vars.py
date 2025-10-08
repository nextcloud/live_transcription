import os
from unittest.mock import patch

import pytest
from nc_py_api import NextcloudApp

from ex_app.lib.utils import check_hpb_env_vars, get_hpb_settings

HPB_SETTINGS_RESPONSE = {
	"signalingMode": "external",
	"userId": None,
	"hideWarning": True,
	"server": "wss://cloud.hostname.tld/standalone-signaling",
	"ticket": "dumm1:1759228623::dumm2",
	"helloAuthParams": {
		"1.0": {
			"userid": None,
			"ticket": "dumm1:1759228623::dumm2",
		},
		"2.0": {
			"token": "dumm3.dumm4.dumm5",
		},
	},
	"federation": None,
	"stunservers": [{"urls": ["stun:stun.nextcloud.com:443"]}],
	"turnservers": [{
		"urls": ["turn:localhost:3478?transport=udp", "turn:localhost:3478?transport=tcp"],
		"username": "dumm6:dumm7",
		"credential": "dumm8",
	}],
	"sipDialinInfo": "",
}

@pytest.mark.parametrize(("hpb_url", "internal_secret", "vosk_url", "raises"), (
	("", "", "", True),
	("invalid", "", "", True),
	("wss://cloud.hostname.tld/standalone-signaling/spreed", "", "", True),
	("wss://cloud.hostname.tld/standalone-signaling/spreed", "1234", "invalid", True),
	("wss://cloud.hostname.tld/standalone-signaling/spreed", "1234", "ws://localhost:808080", True),
	("wss://cloud.hostname.tld/standalone-signaling/spreed", "1234", "", False),
	("ws://cloud.hostname.tld/standalone-signaling/spreed", "1234", "ws://localhost:8080", False),
	("wss://cloud.hostname.tld/standalone-signaling/spreed", "1234", "ws://localhost:8080", False),
))
def test_unset_env_vars(hpb_url, internal_secret, vosk_url, raises):
	os.environ["LT_HPB_URL"] = hpb_url
	os.environ["LT_INTERNAL_SECRET"] = internal_secret
	os.environ["LT_VOSK_SERVER_URL"] = vosk_url

	if not raises:
		check_hpb_env_vars()
		return

	with pytest.raises(ValueError):
		check_hpb_env_vars()


def test_get_hpb_settings(wss_env_var):
	with patch.object(NextcloudApp, "ocs", return_value=HPB_SETTINGS_RESPONSE):
		hpb_settings = get_hpb_settings()
		assert hpb_settings.server == HPB_SETTINGS_RESPONSE["server"]
		assert hpb_settings.stunservers[0].urls == HPB_SETTINGS_RESPONSE["stunservers"][0]["urls"]
		assert hpb_settings.turnservers[0].urls == HPB_SETTINGS_RESPONSE["turnservers"][0]["urls"]
		assert hpb_settings.turnservers[0].username == HPB_SETTINGS_RESPONSE["turnservers"][0]["username"]
		assert hpb_settings.turnservers[0].credential == HPB_SETTINGS_RESPONSE["turnservers"][0]["credential"]
