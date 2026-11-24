#!/usr/bin/env bash
#
# SPDX-FileCopyrightText: 2026 Nextcloud GmbH and Nextcloud contributors
# SPDX-License-Identifier: AGPL-3.0-or-later
#
# Create a network namespace for each participant of the stress test, so every
# participant reaches Nextcloud, the HPB and Janus from its own IP address.
#
#   stress_netns.sh up N     create the bridge and the namespaces lts-0 .. lts-(N-1)
#   stress_netns.sh down     remove the namespaces, the veths, the bridge and the firewall rules
#
# The bridge ltsbr0 has the host address 10.201.0.1/16. The namespace lts-i has the
# address 10.201.(1 + i / 250).(1 + i % 250) on a veth into the bridge (host side ltsv<i>,
# eth0 in the namespace), lo up, and a default route via 10.201.0.1. The services that the
# participants use must listen on 0.0.0.0 or on 10.201.0.1.
#
# Needs root. Both commands are idempotent, and "down" also cleans up after a partial "up".

set -euo pipefail

BRIDGE=ltsbr0
HOST_ADDR=10.201.0.1/16
GATEWAY=10.201.0.1
# The host firewall may drop what it does not know, as Docker and ufw do. These rules
# accept everything that comes from the bridge, to the host and between the namespaces.
FIREWALL_RULES=("INPUT -i $BRIDGE -j ACCEPT" "FORWARD -i $BRIDGE -j ACCEPT")

die() {
	echo "::error::$*" >&2
	exit 1
}

ns_addr() {
	echo "10.201.$((1 + $1 / 250)).$((1 + $1 % 250))"
}

cmd_up() {
	local count=${1:-}
	[[ "$count" =~ ^[0-9]+$ ]] && [ "$count" -ge 1 ] || die "usage: $0 up N"
	[ "$count" -le 1000 ] || die "at most 1000 namespaces fit into 10.201.1.1 .. 10.201.4.250"

	ip link show "$BRIDGE" > /dev/null 2>&1 || ip link add "$BRIDGE" type bridge
	ip addr replace "$HOST_ADDR" dev "$BRIDGE"
	ip link set "$BRIDGE" up

	local i ns veth
	for ((i = 0; i < count; i++)); do
		ns=lts-$i
		veth=ltsv$i
		[ -e "/run/netns/$ns" ] || ip netns add "$ns"
		if ! ip link show "$veth" > /dev/null 2>&1; then
			# an eth0 left in the namespace lost its peer and went with it
			ip link add "$veth" type veth peer name eth0 netns "$ns"
		fi
		ip link set "$veth" master "$BRIDGE" up
		ip -n "$ns" addr replace "$(ns_addr "$i")/16" dev eth0
		ip -n "$ns" link set lo up
		ip -n "$ns" link set eth0 up
		ip -n "$ns" route replace default via "$GATEWAY"
	done

	local rule
	for rule in "${FIREWALL_RULES[@]}"; do
		# shellcheck disable=SC2086 # the rule is a list of words
		iptables -C $rule 2> /dev/null || iptables -I $rule
	done
	echo "$count namespaces lts-0 .. lts-$((count - 1)) on $BRIDGE ($HOST_ADDR)"
}

cmd_down() {
	local ns link rule removed=0
	for ns in $(ip netns list | awk '$1 ~ /^lts-[0-9]+$/ { print $1 }'); do
		ip netns delete "$ns"
		removed=$((removed + 1))
	done
	# deleting a namespace deletes its veth end and the host end with it, but not always at once
	for link in $(ip -o link show | awk -F': ' '{ sub(/@.*/, "", $2); print $2 }' | grep -E '^ltsv[0-9]+$' || true); do
		ip link delete "$link" 2> /dev/null || true
	done
	if ip link show "$BRIDGE" > /dev/null 2>&1; then
		ip link delete "$BRIDGE"
	fi
	for rule in "${FIREWALL_RULES[@]}"; do
		# shellcheck disable=SC2086 # the rule is a list of words
		while iptables -C $rule 2> /dev/null; do
			iptables -D $rule
		done
	done
	echo "removed $removed namespaces, $BRIDGE and its firewall rules"
}

[ "$(id -u)" -eq 0 ] || die "$0 needs root"
action=${1:-}
shift || true
case "$action" in
	up) cmd_up "$@" ;;
	down) cmd_down ;;
	*) die "usage: $0 up N | down" ;;
esac
