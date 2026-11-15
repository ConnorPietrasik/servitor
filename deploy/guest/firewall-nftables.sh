#!/usr/bin/env bash
# Guest nftables (PLAN §2/§6): default-deny input; admit SERVITOR_PORT from
# the edge subnet ONLY (plus loopback, established/related, ops SSH from the
# LAN subnet). All values from the 0600 env file — nothing hardcoded here.
# IPv4-only in v1 (the edge subnet is the host's docker network).
set -euo pipefail

: "${SERVITOR_PORT:?SERVITOR_PORT required (env file)}"
: "${SERVITOR_EDGE_SUBNET:?SERVITOR_EDGE_SUBNET required (env file)}"
: "${SERVITOR_OPS_SUBNET:?SERVITOR_OPS_SUBNET required (env file)}"

# fail closed on malformed values before touching the live ruleset
[[ "$SERVITOR_PORT" =~ ^[0-9]+$ ]] && (( SERVITOR_PORT >= 1 && SERVITOR_PORT <= 65535 )) \
  || { echo "bad SERVITOR_PORT: $SERVITOR_PORT" >&2; exit 1; }
[[ "$SERVITOR_EDGE_SUBNET" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+/[0-9]+$ ]] \
  || { echo "bad SERVITOR_EDGE_SUBNET: $SERVITOR_EDGE_SUBNET" >&2; exit 1; }
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
		ip saddr { $SERVITOR_EDGE_SUBNET } tcp dport $SERVITOR_PORT accept comment "servitor api — edge subnet only (invariant 3)"
		ip saddr { $SERVITOR_OPS_SUBNET } tcp dport 22 accept comment "ops ssh — LAN only"
	}
	chain forward {
		type filter hook forward priority 0; policy drop;
	}
	chain output {
		type filter hook output priority 0; policy accept;
	}
}
EOF

echo "nftables applied: port $SERVITOR_PORT from $SERVITOR_EDGE_SUBNET; ssh from $SERVITOR_OPS_SUBNET; default-deny"
