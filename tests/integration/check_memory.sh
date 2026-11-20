#!/usr/bin/env bash
#
# SPDX-FileCopyrightText: 2026 Nextcloud GmbH and Nextcloud contributors
# SPDX-License-Identifier: AGPL-3.0-or-later
#
# Check that a container does not grow in memory while a call runs.
#
#   check_memory.sh CONTAINER SECONDS MAX_GROWTH_MIB
#
# The app must not keep media that nothing reads. aiortc decodes every track it
# receives into a queue, and a track that nothing reads grows that queue without
# end. This showed as growth of 16 MiB per second for each camera when the app
# received video, and 1.15 MiB per second for each speaker when the app kept a
# speaker's audio after its connection to the ASR server closed.
#
# Every 5 seconds for SECONDS seconds, reads the anonymous memory of the whole
# container, all of its processes together, from the "anon" line of memory.stat in
# its cgroup on the host. That needs cgroup v2 and no tool in the image. Anonymous
# memory leaves out the page cache, which grows with every log line and every file
# read and is given back under pressure. Fails as soon as the anonymous memory grew
# by more than MAX_GROWTH_MIB since the first sample, when the kernel killed a
# process of the container for lack of memory, or when the container restarted.

set -euo pipefail

container=$1
seconds=$2
max_growth=$3
interval=5

die() {
	echo "::error::$*"
	exit 1
}

started=$(docker inspect -f '{{.State.StartedAt}}' "$container") || die "no container $container"
pid=$(docker inspect -f '{{.State.Pid}}' "$container")
[ "$pid" -gt 0 ] || die "$container does not run"
cgroup=/sys/fs/cgroup$(sed -n 's/^0:://p' "/proc/$pid/cgroup")
[ -r "$cgroup/memory.stat" ] || die "no cgroup v2 memory.stat for $container at $cgroup"

# prints the anonymous memory of the cgroup in bytes
anon() {
	awk '$1 == "anon" { print $2; exit }' "$cgroup/memory.stat" 2> /dev/null
}

# prints how many processes of the cgroup the kernel killed for lack of memory
oom_kills() {
	awk '$1 == "oom_kill" { print $2; exit }' "$cgroup/memory.events" 2> /dev/null
}

first_anon=$(anon)
first_kills=$(oom_kills)
[ -n "$first_anon" ] || die "cannot read the memory of $container"
elapsed=0
while :; do
	[ "$(docker inspect -f '{{.State.StartedAt}}' "$container" 2> /dev/null)" = "$started" ] ||
		die "$container restarted or stopped after ${elapsed}s"
	bytes=$(anon)
	kills=$(oom_kills)
	[ -n "$bytes" ] || die "cannot read the memory of $container after ${elapsed}s"
	[ "$kills" = "$first_kills" ] ||
		die "the kernel killed $((kills - first_kills)) process(es) of $container for lack of memory after ${elapsed}s"

	read -r mib growth over <<< "$(awk -v b="$bytes" -v first="$first_anon" -v max="$max_growth" \
		'BEGIN { g = (b - first) / 1048576; printf "%.1f %.1f %d\n", b / 1048576, g, (g > max) }')"
	echo "${elapsed}s anonymous memory ${mib} MiB, growth ${growth} MiB"
	[ "$over" -eq 0 ] ||
		die "the anonymous memory of $container grew by ${growth} MiB in ${elapsed}s, more than ${max_growth} MiB"

	[ "$elapsed" -lt "$seconds" ] || break
	sleep "$interval"
	elapsed=$((elapsed + interval))
done

echo "the anonymous memory of $container grew by ${growth} MiB in ${seconds}s (limit ${max_growth} MiB)," \
	"from $(awk -v b="$first_anon" 'BEGIN { printf "%.1f", b / 1048576 }') to ${mib} MiB"
