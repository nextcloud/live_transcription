#!/usr/bin/env bash
#
# SPDX-FileCopyrightText: 2026 Nextcloud GmbH and Nextcloud contributors
# SPDX-License-Identifier: AGPL-3.0-or-later
#
# The network of the stress test: one network namespace for each participant, some of
# them behind a NAT router or a firewall, and tc for impaired links and outages.
#
#   stress_netns.sh up N [PLAN]   create the bridge, the routers and the namespaces lts-0 .. lts-(N-1)
#   stress_netns.sh down          remove all of it and the firewall rules
#   stress_netns.sh shape K SPEC  give the link of lts-K the netem/tbf settings SPEC, see below
#   stress_netns.sh cut K         drop every packet of lts-K, without a FIN or a RST to either side
#   stress_netns.sh heal K [SPEC] let the packets of lts-K through again, with the shaping SPEC if given
#
# Without NAT a namespace lts-k has the address 10.201.(1 + k / 250).(1 + k % 250)/16 on a veth
# into the bridge ltsbr0 (host side ltsv<k>, eth0 inside), and the bridge has the host address
# 10.201.0.1/16, at which the participants reach Nextcloud, the HPB, Janus and TURN.
#
# PLAN is a file with one line "k kind" for each namespace that is not plain, kind being:
#   nat       behind the NAT router ltsr-nat: a private address 192.168.<1 + k / 250>.<1 + k % 250>/16
#             in a LAN bridge of the router, which masquerades to its address 10.201.0.2 on ltsbr0.
#             All NATed participants share that one public address, like a home or office network.
#   turnonly  behind the NAT router ltsr-fw, whose firewall lets out only TCP to Nextcloud and
#             the HPB (the ports of STRESS_TCP_PORTS, default 8080 and 8081) and UDP and TCP to
#             the TURN server (10.201.0.1:3478), like a corporate network: ICE must use the TURN
#             relay. Public address 10.201.0.3.
#
# shape SPEC is "delay=MS jitter=MS loss=PERCENT rate=KBIT", any subset, applied in both
# directions: on eth0 inside the namespace (upload) and on the host side of its veth (download).
#
# Needs root. up and down are idempotent, and down also cleans up after a partial up.

set -euo pipefail

BRIDGE=ltsbr0
HOST_ADDR=10.201.0.1/16
GATEWAY=10.201.0.1
TURN_PORT=3478
# the web server and the signaling server, the only TCP ports that the TURN-only firewall lets through
TCP_PORTS=${STRESS_TCP_PORTS:-8080,8081}
# The host firewall may drop what it does not know, as Docker and ufw do. These rules
# accept everything that comes from the bridge, to the host and between the namespaces.
FIREWALL_RULES=("INPUT -i $BRIDGE -j ACCEPT" "FORWARD -i $BRIDGE -j ACCEPT")
# router namespace, its public address on the bridge, its LAN prefix
ROUTERS=("ltsr-nat 10.201.0.2 192.168" "ltsr-fw 10.201.0.3 172.20")

die() {
	echo "::error::$*" >&2
	exit 1
}

ns_addr() {
	echo "10.201.$((1 + $1 / 250)).$((1 + $1 % 250))"
}

lan_addr() {
	echo "$1.$((1 + $2 / 250)).$((1 + $2 % 250))"
}

router_of() {
	case "$1" in
		nat) echo "${ROUTERS[0]}" ;;
		turnonly) echo "${ROUTERS[1]}" ;;
		*) die "unknown kind $1" ;;
	esac
}

up_router() {
	local name=$1 public=$2 prefix=$3 kind=$4
	[ -e "/run/netns/$name" ] || ip netns add "$name"
	ip link show "${name}-w" > /dev/null 2>&1 || ip link add "${name}-w" type veth peer name wan netns "$name"
	ip link set "${name}-w" master "$BRIDGE" up
	ip -n "$name" addr replace "$public/16" dev wan
	ip -n "$name" link set wan up
	ip -n "$name" link set lo up
	ip -n "$name" route replace default via "$GATEWAY"
	ip -n "$name" link show lan > /dev/null 2>&1 || ip -n "$name" link add lan type bridge
	ip -n "$name" addr replace "$prefix.0.1/16" dev lan
	ip -n "$name" link set lan up
	ip netns exec "$name" sysctl -qw net.ipv4.ip_forward=1
	# a fresh ruleset each time: NAT of the LAN to the public address, as a home router does
	ip netns exec "$name" iptables -t nat -F
	ip netns exec "$name" iptables -F
	ip netns exec "$name" iptables -t nat -A POSTROUTING -o wan -j MASQUERADE
	if [ "$kind" = turnonly ]; then
		# only signaling, the web server and TURN; no direct UDP to Janus, thus ICE needs the relay
		ip netns exec "$name" iptables -P FORWARD DROP
		ip netns exec "$name" iptables -A FORWARD -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT
		ip netns exec "$name" iptables -A FORWARD -i lan -p tcp -d "$GATEWAY" -m multiport --dports "$TCP_PORTS" -j ACCEPT
		ip netns exec "$name" iptables -A FORWARD -i lan -p udp -d "$GATEWAY" --dport "$TURN_PORT" -j ACCEPT
		ip netns exec "$name" iptables -A FORWARD -i lan -p tcp -d "$GATEWAY" --dport "$TURN_PORT" -j ACCEPT
	fi
}

cmd_up() {
	local count=${1:-} plan=${2:-}
	[[ "$count" =~ ^[0-9]+$ ]] && [ "$count" -ge 1 ] || die "usage: $0 up N [PLAN]"
	[ "$count" -le 1000 ] || die "at most 1000 namespaces fit into 10.201.1.1 .. 10.201.4.250"
	declare -A kind_of=()
	if [ -n "$plan" ]; then
		local k kind
		while read -r k kind; do
			[ -n "$k" ] && kind_of[$k]=$kind
		done < "$plan"
	fi

	ip link show "$BRIDGE" > /dev/null 2>&1 || ip link add "$BRIDGE" type bridge
	ip addr replace "$HOST_ADDR" dev "$BRIDGE"
	ip link set "$BRIDGE" up
	local router name public prefix
	for router in "${ROUTERS[@]}"; do
		read -r name public prefix <<< "$router"
		up_router "$name" "$public" "$prefix" "$([ "$name" = ltsr-fw ] && echo turnonly || echo nat)"
	done

	local i ns veth kind lan addr
	for ((i = 0; i < count; i++)); do
		ns=lts-$i
		veth=ltsv$i
		kind=${kind_of[$i]:-direct}
		[ -e "/run/netns/$ns" ] || ip netns add "$ns"
		if ! ip link show "$veth" > /dev/null 2>&1; then
			# an eth0 left in the namespace lost its peer and went with it
			ip link add "$veth" type veth peer name eth0 netns "$ns"
		fi
		ip -n "$ns" link set lo up
		if [ "$kind" = direct ]; then
			ip link set "$veth" master "$BRIDGE" up
			addr=$(ns_addr "$i")
			ip -n "$ns" addr replace "$addr/16" dev eth0
			ip -n "$ns" link set eth0 up
			ip -n "$ns" route replace default via "$GATEWAY"
		else
			read -r name public prefix <<< "$(router_of "$kind")"
			# the host side of the veth goes into the router's LAN bridge
			ip link set "$veth" netns "$name"
			ip -n "$name" link set "$veth" master lan up
			addr=$(lan_addr "$prefix" "$i")
			ip -n "$ns" addr replace "$addr/16" dev eth0
			ip -n "$ns" link set eth0 up
			ip -n "$ns" route replace default via "$prefix.0.1"
		fi
	done

	local rule
	for rule in "${FIREWALL_RULES[@]}"; do
		# shellcheck disable=SC2086 # the rule is a list of words
		iptables -C $rule 2> /dev/null || iptables -I $rule
	done
	echo "$count namespaces lts-0 .. lts-$((count - 1)) on $BRIDGE ($HOST_ADDR)," \
		"$(printf '%s\n' "${kind_of[@]}" | grep -c '^nat$' || true) behind NAT," \
		"$(printf '%s\n' "${kind_of[@]}" | grep -c '^turnonly$' || true) behind a TURN-only firewall"
}

# the host-side end of the veth of lts-k: in the root namespace, or in a router namespace
host_end() {
	local k=$1 router name
	if ip link show "ltsv$k" > /dev/null 2>&1; then
		echo ""
		return
	fi
	for router in "${ROUTERS[@]}"; do
		read -r name _ <<< "$router"
		if ip -n "$name" link show "ltsv$k" > /dev/null 2>&1; then
			echo "$name"
			return
		fi
	done
	die "no veth ltsv$k"
}

# run a command in the namespace of the host-side end of the veth of lts-k
at_host_end() {
	local where=$1
	shift
	if [ -z "$where" ]; then "$@"; else ip netns exec "$where" "$@"; fi
}

netem_args() {
	local delay=0 jitter=0 loss=0 rate="" kv
	for kv in "$@"; do
		case "$kv" in
			delay=*) delay=${kv#delay=} ;;
			jitter=*) jitter=${kv#jitter=} ;;
			loss=*) loss=${kv#loss=} ;;
			rate=*) rate=${kv#rate=} ;;
			*) die "unknown shape setting $kv" ;;
		esac
	done
	# jitter as a normal distribution around the delay; netem reorders packets when the jitter
	# exceeds the gap between them, as a real network with several paths does
	local args="delay ${delay}ms"
	[ "$jitter" != 0 ] && args+=" ${jitter}ms distribution normal"
	[ "$loss" != 0 ] && args+=" loss ${loss}%"
	[ -n "$rate" ] && args+=" rate ${rate}kbit"
	echo "$args"
}

cmd_shape() {
	local k=${1:?usage: $0 shape K SPEC}
	shift
	local args where
	args=$(netem_args "$@")
	where=$(host_end "$k")
	# shellcheck disable=SC2086 # args is a list of words
	ip netns exec "lts-$k" tc qdisc replace dev eth0 root netem $args
	# shellcheck disable=SC2086
	at_host_end "$where" tc qdisc replace dev "ltsv$k" root netem $args
	echo "lts-$k: $args"
}

# A silent outage: every packet of the namespace is lost in both directions, as when a laptop
# leaves the WLAN. Nothing tells either side; the connections time out by themselves.
cmd_cut() {
	local k=${1:?usage: $0 cut K}
	local where
	where=$(host_end "$k")
	ip netns exec "lts-$k" tc qdisc replace dev eth0 root netem loss 100%
	at_host_end "$where" tc qdisc replace dev "ltsv$k" root netem loss 100%
}

cmd_heal() {
	local k=${1:?usage: $0 heal K [SPEC]}
	shift
	if [ $# -gt 0 ]; then
		cmd_shape "$k" "$@"
		return
	fi
	local where
	where=$(host_end "$k")
	ip netns exec "lts-$k" tc qdisc del dev eth0 root 2> /dev/null || true
	at_host_end "$where" tc qdisc del dev "ltsv$k" root 2> /dev/null || true
}

cmd_down() {
	local ns link rule removed=0
	for ns in $(ip netns list | awk '$1 ~ /^lts-[0-9]+$/ || $1 ~ /^ltsr-/ { print $1 }'); do
		ip netns delete "$ns"
		removed=$((removed + 1))
	done
	# deleting a namespace deletes its veth end and the host end with it, but not always at once
	for link in $(ip -o link show | awk -F': ' '{ sub(/@.*/, "", $2); print $2 }' |
		grep -E '^(ltsv[0-9]+|ltsr-.*-w)$' || true); do
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
	shape) cmd_shape "$@" ;;
	cut) cmd_cut "$@" ;;
	heal) cmd_heal "$@" ;;
	*) die "usage: $0 up N [PLAN] | down | shape K SPEC | cut K | heal K [SPEC]" ;;
esac
