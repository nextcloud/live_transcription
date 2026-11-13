#!/usr/bin/env bash
#
# SPDX-FileCopyrightText: 2026 Nextcloud GmbH and Nextcloud contributors
# SPDX-License-Identifier: AGPL-3.0-or-later
#
# Check with occ that a call runs in a conversation with at least N participants.
#
#   check_call.sh TOKEN N [TIMEOUT]
#
# Waits up to TIMEOUT seconds (default 60) for "talk:monitor:room" to count N sessions
# in the call, then requires the conversation in "talk:monitor:calls" and an active call
# in "talk:active-calls". Set OCC to the occ command (default ./occ).

set -euo pipefail

token=$1
expected=$2
timeout=${3:-60}
read -r -a occ <<< "${OCC:-./occ}"

die() {
	echo "::error::$*"
	exit 1
}

in_call=0
waited=0
while :; do
	room=$("${occ[@]}" talk:monitor:room "$token" --output=json)
	in_call=$(jq -r '.call' <<< "$room")
	if [ "$in_call" -ge "$expected" ] || [ "$waited" -ge "$timeout" ]; then
		break
	fi
	sleep 2
	waited=$((waited + 2))
done
echo "talk:monitor:room $token: $room"
[ "$in_call" -ge "$expected" ] || die "$in_call sessions in the call after ${timeout}s, expected at least $expected"

calls=$("${occ[@]}" talk:monitor:calls --output=json)
echo "talk:monitor:calls: $calls"
jq -e --arg token "$token" 'has($token)' <<< "$calls" > /dev/null || die "talk:monitor:calls does not list $token"

# talk:active-calls exits with 1 when calls are in progress
code=0
active=$("${occ[@]}" talk:active-calls --output=json) || code=$?
echo "talk:active-calls: $active"
[ "$code" -eq 1 ] || die "talk:active-calls reports no call in progress"

echo "The call in $token runs with $in_call participants"
