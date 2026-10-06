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

# The ASR server, unless an external one is configured
if [ "$(echo "${LT_DISABLE_INTERNAL_ASR:-false}" | tr '[:upper:]' '[:lower:]')" != "true" ] \
   && [ "${LT_DISABLE_INTERNAL_ASR:-0}" != "1" ]; then
  # Only a :<digits> host suffix is a port, anything else means the default.
  ASR_PORT=$(echo "${LT_ASR_SERVER_URL:-ws://localhost:2702}" | sed -nE 's|^[a-z]+://[^/]*:([0-9]+).*|\1|p')
  if ! grep -qi ":$(printf '%04X' "${ASR_PORT:-2702}") " /proc/net/tcp /proc/net/tcp6 2>/dev/null; then
    exit 1
  fi
fi
