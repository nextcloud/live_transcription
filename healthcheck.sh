#!/bin/bash
#
# SPDX-FileCopyrightText: 2025 Nextcloud GmbH and Nextcloud contributors
# SPDX-License-Identifier: AGPL-3.0-or-later
#

set -e

# FRP
if [ -f /frpc.toml ] && [ -n "$HP_SHARED_KEY" ]; then
  if pgrep -x "frpc" > /dev/null; then
    exit 0
  else
    exit 1
  fi
fi

# main app
curl -sSf "http://$APP_HOST:$APP_PORT/heartbeat" > /dev/null

# Check that the internal ASR server process runs. Do not check its port: the
# server opens it only after /init downloads the model, and AppAPI calls /init
# only after this check passes. LT_DISABLE_INTERNAL_VOSK is the deprecated
# pre-3.0 name.
DISABLE_ASR="$(echo "${LT_DISABLE_INTERNAL_ASR:-${LT_DISABLE_INTERNAL_VOSK:-false}}" | tr '[:upper:]' '[:lower:]')"
if [ "$DISABLE_ASR" != "true" ] && [ "$DISABLE_ASR" != "1" ]; then
  if ! pgrep -f "/ex_app/lib/asr_server.py" > /dev/null; then
    exit 1
  fi
fi
