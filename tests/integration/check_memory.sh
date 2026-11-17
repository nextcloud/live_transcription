#!/usr/bin/env bash
#
# SPDX-FileCopyrightText: 2026 Nextcloud GmbH and Nextcloud contributors
# SPDX-License-Identifier: AGPL-3.0-or-later
#
# Check that a process in a container does not grow in memory while a call runs.
#
#   check_memory.sh CONTAINER SECONDS MAX_GROWTH_MIB [PATTERN]
#
# The app must not keep media that nothing reads. aiortc decodes every track it
# receives into a queue, and a track that nothing reads grows that queue without
# end. This showed as RSS growth of 16 MiB per second for each camera when the app
# received video, and 1.15 MiB per second for each speaker when the app kept a
# speaker's audio after its connection to the ASR server closed.
#
# Every 5 seconds for SECONDS seconds, reads the RSS of the process whose command
# line contains PATTERN (default /ex_app/lib/main.py) with "docker top", which runs
# the host ps and needs no tool in the image. Fails as soon as the RSS grew by more
# than MAX_GROWTH_MIB since the first sample, when no process matches, or when the
# pid changes because the process restarted.

set -euo pipefail

container=$1
seconds=$2
max_growth=$3
pattern=${4:-/ex_app/lib/main.py}
interval=5

die() {
	echo "::error::$*"
	exit 1
}

# prints "PID RSS_KIB" of the first process whose command line contains the pattern
sample() {
	docker top "$container" -eo pid,rss,args | awk -v pattern="$pattern" \
		'NR > 1 && index(substr($0, index($0, $3)), pattern) { print $1, $2; exit }'
}

first_pid=
first_rss=
elapsed=0
while :; do
	read -r pid rss <<< "$(sample)" || true
	[ -n "${pid:-}" ] || die "no process in $container matches '$pattern' after ${elapsed}s"
	if [ -z "$first_pid" ]; then
		first_pid=$pid
		first_rss=$rss
	fi
	[ "$pid" = "$first_pid" ] || die "the process '$pattern' in $container restarted after ${elapsed}s (pid $first_pid, now $pid)"

	read -r mib growth over <<< "$(awk -v rss="$rss" -v first="$first_rss" -v max="$max_growth" \
		'BEGIN { g = (rss - first) / 1024; printf "%.1f %.1f %d\n", rss / 1024, g, (g > max) }')"
	echo "${elapsed}s pid $pid RSS ${mib} MiB, growth ${growth} MiB"
	# the first sample is the baseline, so growth counts from the second sample on
	[ "$elapsed" -eq 0 ] || [ "$over" -eq 0 ] ||
		die "RSS of '$pattern' in $container grew by ${growth} MiB in ${elapsed}s, more than ${max_growth} MiB"

	[ "$elapsed" -lt "$seconds" ] || break
	sleep "$interval"
	elapsed=$((elapsed + interval))
done

echo "RSS of '$pattern' in $container grew by ${growth} MiB in ${seconds}s (limit ${max_growth} MiB), from $(awk -v r="$first_rss" 'BEGIN { printf "%.1f", r / 1024 }') to ${mib} MiB"
