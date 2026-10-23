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

# The ASR server process, unless an external one is configured. The port is not
# probed: the server opens it only after the model is on disk, and the model is
# downloaded by /init, which AppAPI calls only once this check passes.
# LT_DISABLE_INTERNAL_VOSK is the deprecated pre-3.0 name, read when the new one
# is unset.
DISABLE_ASR="$(echo "${LT_DISABLE_INTERNAL_ASR:-${LT_DISABLE_INTERNAL_VOSK:-false}}" | tr '[:upper:]' '[:lower:]')"
if [ "$DISABLE_ASR" != "true" ] && [ "$DISABLE_ASR" != "1" ]; then
  if ! pgrep -f "/ex_app/lib/asr_server.py" > /dev/null; then
    exit 1
  fi
fi
