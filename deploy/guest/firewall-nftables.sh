#!/usr/bin/env bash
# Guest nftables: default-deny input; admit SERVITOR_PORT from
# the edge subnet ONLY (plus loopback, established/related, ops SSH from
# the host's edge-bridge IP /32 + the ops LAN subnet). All values from
# the 0600 env file — nothing hardcoded here.
# IPv4-only in v1 (the edge subnet is the host's docker network).
set -euo pipefail

: "${SERVITOR_PORT:?SERVITOR_PORT required (env file)}"
: "${SERVITOR_EDGE_SUBNET:?SERVITOR_EDGE_SUBNET required (env file)}"
: "${SERVITOR_HOST_BRIDGE_IP:?SERVITOR_HOST_BRIDGE_IP required (env file)}"
: "${SERVITOR_OPS_SUBNET:?SERVITOR_OPS_SUBNET required (env file)}"

# fail closed on malformed values before touching the live ruleset
[[ "$SERVITOR_PORT" =~ ^[0-9]+$ ]] && (( SERVITOR_PORT >= 1 && SERVITOR_PORT <= 65535 )) \
  || { echo "bad SERVITOR_PORT: $SERVITOR_PORT" >&2; exit 1; }
[[ "$SERVITOR_EDGE_SUBNET" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+/[0-9]+$ ]] \
  || { echo "bad SERVITOR_EDGE_SUBNET: $SERVITOR_EDGE_SUBNET" >&2; exit 1; }
[[ "$SERVITOR_HOST_BRIDGE_IP" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]] \
  || { echo "bad SERVITOR_HOST_BRIDGE_IP: $SERVITOR_HOST_BRIDGE_IP" >&2; exit 1; }
[[ "$SERVITOR_OPS_SUBNET" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+/[0-9]+$ ]] \
  || { echo "bad SERVITOR_OPS_SUBNET: $SERVITOR_OPS_SUBNET" >&2; exit 1; }

cat <<EOF | nft -f -
flush ruleset
table inet servitor {
	chain input {
		type filter hook input priority 0; policy drop;
		iif "lo" accept
		ct state established,related accept
		ct state invalid drop
		ip saddr { $SERVITOR_EDGE_SUBNET } tcp dport $SERVITOR_PORT accept comment "servitor api — edge subnet only"
		# ops ssh from the host bridge IP AND the ops subnet: the host's
		# own ssh to the VM sources the host's EDGE BRIDGE IP (the kernel
		# picks the output-interface address for the connected route) —
		# admit exactly that /32 (the full edge segment would put every
		# edge container on sshd's doorstep; LAN-only would lock the host
		# out of its own VM). A LAN workstation (host forward rule, no
		# SNAT) keeps its LAN source address — hence the ops subnet too.
		ip saddr { $SERVITOR_HOST_BRIDGE_IP/32, $SERVITOR_OPS_SUBNET } tcp dport 22 accept comment "ops ssh — host bridge IP + LAN"
	}
	chain forward {
		type filter hook forward priority 0; policy drop;
	}
	chain output {
		type filter hook output priority 0; policy accept;
	}
}
EOF

echo "nftables applied: port $SERVITOR_PORT from $SERVITOR_EDGE_SUBNET; ssh from $SERVITOR_HOST_BRIDGE_IP/32 + $SERVITOR_OPS_SUBNET; default-deny"
