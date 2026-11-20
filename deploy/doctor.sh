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

RC=0
[[ "$GUEST_RC" -eq 0 ]] || RC=1
[[ "$PROBE_RC" -eq 0 ]] || RC=1
if [[ "$RC" -eq 0 ]]; then
  echo "RESULT: GREEN"
else
  echo "RESULT: FAIL (guest doctor rc=$GUEST_RC; probes rc=$PROBE_RC)"
fi
exit "$RC"
