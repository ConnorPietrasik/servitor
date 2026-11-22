#!/usr/bin/env bash
# Servitor doctor — host wrapper.
#
# Runs the guest's read-only certification (python -m app.doctor, the
# synced tree's own venv — the doctor is part of the deployed app), then
# the ONE live probe only the host can make:
#   open — the host (inside SERVITOR_EDGE_SUBNET) reaches SERVITOR_PORT
#          (live TCP connect — the only check that proves edge ingress
#          reachability: the app's tick path runs the doctor in-guest and
#          cannot probe its own ingress from the edge).
# The ruleset CONTENT (default-deny, port admitted from the edge subnet
# only) is certified guest-side by app.doctor's firewall.ruleset /
# firewall.port checks — asserting the same nft state again over the same
# ssh would add nothing (and the guest's own IP IS in the edge subnet by
# design, so a live negative probe from it is impossible).
# Doctor must be green before the service starts serving and after every
# sync; while the update-waiting flag is set, the guest doctor certifies
# the running state and reports the staged target as a warning.
#
# Usage: deploy/doctor.sh [--env .env] [--ssh root@<vm-edge-ip>] [--deploy-key K]
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="$REPO/.env"
SSH_TARGET=""
# ${HOME:-/root}: panel-do's scrubbed env has no HOME (set -u would die on a
# bare $HOME); /root/.srv is the canonical root-side home.
DEPLOY_KEY="${SERVITOR_DEPLOY_KEY:-${HOME:-/root}/.srv/deploy_key}"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --env) ENV_FILE="$2"; shift 2 ;;
    --ssh) SSH_TARGET="$2"; shift 2 ;;
    --deploy-key) DEPLOY_KEY="$2"; shift 2 ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
done

[[ -f "$ENV_FILE" ]] || { echo "env file not found: $ENV_FILE" >&2; exit 1; }
set -a; source <(grep -E '^[A-Za-z_][A-Za-z0-9_]*=' "$ENV_FILE"); set +a
: "${SERVITOR_VM_IP:?SERVITOR_VM_IP missing in $ENV_FILE (the guest edge address)}"
: "${SERVITOR_PORT:=8787}"
# the edge credential (invariant 3): the key-gate probes below compare the
# app's verdict against this .env's value
: "${SERVITOR_EDGE_KEY:?SERVITOR_EDGE_KEY missing in $ENV_FILE (the edge credential, invariant 3)}"
# The keyed probe carries it on a quoted curl-config line via stdin (curl
# -K -), never argv: /proc/<pid>/cmdline is world-readable (unlike environ),
# so a secret on argv would leak to every local uid for the life of the
# probe — the same contract as portcullis's panel-do healthz probe. The
# contract value is hex (openssl rand -hex 32); a quote, backslash or
# newline is a mis-set secret the config line can't carry — fail fast:
# this script is a gate, not a viewer.
case "$SERVITOR_EDGE_KEY" in
  *\"*|*\\*|*$'\n'*) echo "SERVITOR_EDGE_KEY is not a plain value (quote/backslash/newline) — the curl config line can't carry it" >&2; exit 1 ;;
esac
SSH_TARGET="${SSH_TARGET:-root@${SERVITOR_VM_IP}}"
[[ -f "$DEPLOY_KEY" ]] || { echo "deploy key not found: $DEPLOY_KEY" >&2; exit 1; }
SSH=(ssh -i "$DEPLOY_KEY" -o BatchMode=yes -o StrictHostKeyChecking=accept-new -o ConnectTimeout=10 "$SSH_TARGET")

echo "=== doctor: guest side (read-only certification) ==="
set +e
"${SSH[@]}" "
  set -e
  set -a; . /etc/servitor/servitor.env; set +a
  cd /opt/servitor/app
  exec .venv/bin/python -m app.doctor \
    --state /var/lib/servitor \
    --pi-node \"\${SERVITOR_PI_NODE_BIN:-/usr/sbin/node}\" \
    --pi-cli \"\${SERVITOR_PI_CLI_JS}\" \
    --pi-ext \"\${SERVITOR_PI_EXT_DIR}\" \
    --toolkit \"\${SERVITOR_TOOLKIT_DIR:-/opt/toolkit}\" \
    --update-pending-path /var/lib/servitor/update-pending.json
"
GUEST_RC=$?
set -e

echo "=== doctor: firewall probe (host-side) ==="
PROBE_RC=0
# open: from the host (edge subnet) -> the port is admitted end-to-end
# (routing + nft input chain + app listening). The ruleset CONTENT is the
# guest doctor's job (asserted seconds earlier in the same run).
if timeout 5 bash -c "exec 3<>/dev/tcp/${SERVITOR_VM_IP}/${SERVITOR_PORT}" 2>/dev/null; then
  echo "PASS  firewall.probe_open   — port ${SERVITOR_PORT} open from ${SERVITOR_VM_IP} (edge subnet)"
else
  echo "FAIL  firewall.probe_open   — port ${SERVITOR_PORT} NOT reachable from the host"
  PROBE_RC=1
fi

# key gate (invariant 3): the app must REJECT the keyless request and ADMIT
# the keyed one — proves the credential is enforced, the guest env key
# matches THIS .env, and the edge contract holds end-to-end (the firewall
# admits the whole edge subnet, so without the gate any neighbor container
# would drive the agent unauthed). 403-everything (keyed also 403) is the
# signature of a drifted/missing guest key — a sync fixes it. The keyed
# request carries the credential via stdin (curl -K -), not argv (guard
# at the top).
code_nokey="$(curl -s -o /dev/null -w '%{http_code}' -m 5 "http://${SERVITOR_VM_IP}:${SERVITOR_PORT}/healthz" 2>/dev/null || true)"
if [[ "$code_nokey" == "403" ]]; then
  echo "PASS  firewall.probe_key_gate — keyless /healthz rejected (403 — the edge credential is enforced)"
else
  echo "FAIL  firewall.probe_key_gate — keyless /healthz got ${code_nokey:-000} (want 403: the app is serving unkeyed or is down)"
  PROBE_RC=1
fi
code_key="$(printf '%s\n' "header = \"X-Servitor-Edge-Key: ${SERVITOR_EDGE_KEY}\"" \
  | curl -s -o /dev/null -w '%{http_code}' -m 5 -K - "http://${SERVITOR_VM_IP}:${SERVITOR_PORT}/healthz" 2>/dev/null || true)"
if [[ "$code_key" == "200" ]]; then
  echo "PASS  firewall.probe_keyed   — keyed /healthz answered 200 (guest env key == this .env's key)"
else
  echo "FAIL  firewall.probe_keyed   — keyed /healthz got ${code_key:-000} (want 200: guest env key drifted from $ENV_FILE, or the app is down — run deploy/sync.sh)"
  PROBE_RC=1
fi

RC=0
[[ "$GUEST_RC" -eq 0 ]] || RC=1
[[ "$PROBE_RC" -eq 0 ]] || RC=1
if [[ "$RC" -eq 0 ]]; then
  echo "RESULT: GREEN"
else
  echo "RESULT: FAIL (guest doctor rc=$GUEST_RC; probes rc=$PROBE_RC)"
fi
exit "$RC"
