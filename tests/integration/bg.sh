#!/usr/bin/env bash
#
# SPDX-FileCopyrightText: 2026 Nextcloud GmbH and Nextcloud contributors
# SPDX-License-Identifier: AGPL-3.0-or-later
#
# Run the call participants in the background across workflow steps.
#
#   bg.sh start NAME TIMEOUT -- CMD...   start CMD, wait until it prints "READY"
#   bg.sh wait NAME TIMEOUT [WATCH...]   wait for NAME to exit and return its exit code,
#                                        failing early when a WATCH process dies first
#   bg.sh stop NAME...                   send SIGTERM and wait for a clean exit
#
# Each process leaves NAME.log, NAME.pid and, once it ended, NAME.exit in $BG_DIR.

set -euo pipefail

BG_DIR=${BG_DIR:-.}
mkdir -p "$BG_DIR"

die() {
	echo "::error::$*"
	exit 1
}

show_log() {
	echo "::group::$1.log"
	cat "$BG_DIR/$1.log" || true
	echo "::endgroup::"
}

running() {
	[ -f "$BG_DIR/$1.pid" ] && [ ! -f "$BG_DIR/$1.exit" ]
}

cmd_start() {
	local name=$1 timeout=$2
	shift 2
	[ "${1:-}" = "--" ] && shift
	rm -f "$BG_DIR/$name".{log,pid,exit}

	# The wrapper records the exit code, so later steps can tell how the process ended.
	bash -c '"${@:2}" & echo $! > "$1.pid"; wait $!; echo $? > "$1.exit"' _ "$BG_DIR/$name" "$@" \
		> "$BG_DIR/$name.log" 2>&1 < /dev/null &

	local waited=0
	while [ "$waited" -lt "$timeout" ]; do
		if grep -qx READY "$BG_DIR/$name.log"; then
			echo "$name is ready after ${waited}s"
			return 0
		fi
		if [ -f "$BG_DIR/$name.exit" ]; then
			show_log "$name"
			die "$name exited with code $(cat "$BG_DIR/$name.exit") before it was ready"
		fi
		sleep 1
		waited=$((waited + 1))
	done
	show_log "$name"
	die "$name was not ready within ${timeout}s"
}

cmd_wait() {
	local name=$1 timeout=$2
	shift 2
	local waited=0 watch
	while [ ! -f "$BG_DIR/$name.exit" ]; do
		for watch in "$@"; do
			if ! running "$watch"; then
				show_log "$watch"
				die "$watch ended while waiting for $name"
			fi
		done
		[ "$waited" -lt "$timeout" ] || die "$name did not finish within ${timeout}s"
		sleep 2
		waited=$((waited + 2))
	done
	show_log "$name"
	local code
	code=$(cat "$BG_DIR/$name.exit")
	[ "$code" -eq 0 ] || die "$name failed with exit code $code"
}

cmd_stop() {
	local name waited
	for name in "$@"; do
		running "$name" || continue
		kill -TERM "$(cat "$BG_DIR/$name.pid")" 2>/dev/null || true
	done
	for name in "$@"; do
		[ -f "$BG_DIR/$name.pid" ] || continue
		waited=0
		while [ ! -f "$BG_DIR/$name.exit" ] && [ "$waited" -lt 30 ]; do
			sleep 1
			waited=$((waited + 1))
		done
		if [ -f "$BG_DIR/$name.exit" ]; then
			echo "$name stopped with exit code $(cat "$BG_DIR/$name.exit")"
		else
			kill -KILL "$(cat "$BG_DIR/$name.pid")" 2>/dev/null || true
			echo "$name did not stop within 30s and was killed"
		fi
	done
}

action=${1:-}
shift || true
case "$action" in
	start) cmd_start "$@" ;;
	wait) cmd_wait "$@" ;;
	stop) cmd_stop "$@" ;;
	*) die "usage: $0 start|wait|stop ..." ;;
esac
