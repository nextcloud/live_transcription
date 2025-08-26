#
# SPDX-FileCopyrightText: 2025 Nextcloud GmbH and Nextcloud contributors
# SPDX-License-Identifier: AGPL-3.0-or-later
#

import hashlib
import hmac
import logging
import os
import ssl
from urllib.parse import urlparse

from livetypes import HPBSettings
from nc_py_api import NextcloudApp

LOGGER = logging.getLogger("lt.utils")


def hmac_sha256(key, message):
	return hmac.new(
		key.encode("utf-8"),
		message.encode("utf-8"),
		hashlib.sha256
	).hexdigest()


def get_ssl_context(server_addr: str) -> ssl.SSLContext | None:
	nc = NextcloudApp()

	if server_addr.startswith(("ws://", "http://")):
		LOGGER.info("Using default SSL context for insecure WebSocket connection (ws://)", extra={
			"server_addr": server_addr,
			"tag": "connection",
		})
		return None

	cert_verify = os.environ.get("SKIP_CERT_VERIFY", "false").lower()
	if cert_verify in ("true", "1"):
		LOGGER.info("Skipping certificate verification for WebSocket connection", extra={
			"server_addr": server_addr,
			"SKIP_CERT_VERIFY": cert_verify,
			"tag": "connection",
		})
		ssl_ctx = ssl.SSLContext()
		ssl_ctx.check_hostname = False
		ssl_ctx.verify_mode = ssl.CERT_NONE
		return ssl_ctx

	if nc.app_cfg.options.nc_cert and isinstance(nc.app_cfg.options.nc_cert, ssl.SSLContext):
		# Use the SSL context provided by nc_py_api
		LOGGER.info("Using SSL context provided by nc_py_api", extra={
			"server_addr": server_addr,
			"tag": "connection",
		})
		return nc.app_cfg.options.nc_cert

	# verify certificate normally and don't use SSLContext from nc_py_api
	LOGGER.info("Using default SSL context for WebSocket connection", extra={
		"server_addr": server_addr,
		"tag": "connection",
	})
	return None


def check_hpb_env_vars():
	# Check if the required environment variables are set
	required_vars = ("LT_HPB_URL", "LT_INTERNAL_SECRET")
	missing_vars = [var for var in required_vars if not os.getenv(var)]
	if missing_vars:
		raise ValueError(f"Missing environment variables: {', '.join(missing_vars)}")

	hpb_url = os.environ["LT_HPB_URL"]
	hpb_url_host = urlparse(hpb_url).hostname
	if not hpb_url_host:
		raise ValueError(
			f"Could not detect hostname in LT_HPB_URL env var: {hpb_url}. "
			"Verify that it is a valid URL with a protocol and hostname."
		)

	vosk_url = os.environ.get("LT_VOSK_SERVER_URL")
	if vosk_url:
		vosk_url_parsed = urlparse(vosk_url)
		vosk_host = vosk_url_parsed.hostname
		if not vosk_host:
			raise ValueError(
				f"Could not detect hostname in LT_VOSK_SERVER_URL: {vosk_url}. "
				"Verify that it is a valid URL with a protocol and hostname."
			)
		try:
			_ = vosk_url_parsed.port
		except ValueError as e:
			raise ValueError(f"Invalid VOSK server URL: {vosk_url}") from e


def get_hpb_settings() -> HPBSettings:
	check_hpb_env_vars()
	try:
		nc = NextcloudApp(npa_nc_cert=get_ssl_context(f"${os.environ['NEXTCLOUD_URL']}"))
		settings = nc.ocs("GET", "/ocs/v2.php/apps/spreed/api/v3/signaling/settings")
		hpb_settings = HPBSettings(**settings)
		LOGGER.debug("HPB settings retrieved successfully", extra={
			"stun_servers": [s.urls for s in hpb_settings.stunservers],
			"turn_servers": [t.urls for t in hpb_settings.turnservers],
			"server": hpb_settings.server,
			"tag": "hpb_settings",
		})
		return hpb_settings
	except Exception as e:
		raise Exception("Error getting HPB settings") from e
