import os
import ssl

import pytest

from ex_app.lib.utils import get_ssl_context


@pytest.fixture
def set_skip_cert_verify_env():
	os.environ["SKIP_CERT_VERIFY"] = "1"
	yield
	os.unsetenv("SKIP_CERT_VERIFY")


def test_wss_get_ssl_context(wss_env_var):
	r = get_ssl_context(wss_env_var)
	assert isinstance(r, ssl.SSLContext)
	assert r.verify_mode == ssl.VerifyMode.CERT_REQUIRED


def test_ws_get_ssl_context(ws_env_var):
	r = get_ssl_context(ws_env_var)
	assert r is None


def test_wss_no_verify_get_ssl_context(set_skip_cert_verify_env, wss_env_var):
	r = get_ssl_context(wss_env_var)
	assert isinstance(r, ssl.SSLContext)
	assert r.verify_mode == ssl.VerifyMode.CERT_NONE


def test_ws_no_verify_get_ssl_context(set_skip_cert_verify_env, ws_env_var):
	r = get_ssl_context(ws_env_var)
	assert r is None
