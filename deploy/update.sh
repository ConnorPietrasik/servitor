#!/usr/bin/env bash
# Servitor update — the single versioned path (PLAN §9).
#
# One command takes the guest from its current state to "matches the
# repo + the .env version selections", verified:
#
#   1. RESOLVE   .env specs -> exact versions (host npm view)
#   2. GATE      the conformance suite runs HOST-SIDE against the
#                resolved versions (pi installed into a scratch prefix,
#                extension staged at the resolved version). Red => stop;
#                nothing is deployed. The guest is untouched here.
#   3. PROVISION only what changed:
#                pi        => image rebuild (build.sh; the previous
#                             image is retained for rollback)
#                extension => reuse the gate's staging
#                config/lib/app => the sync's artifacts
#   4. DEPLOY    the sync's swap-based path (flag + tick; D8)
#   5. VERIFY    doctor. Doctor fails => AUTOMATIC ROLLBACK: swap back
#                the retained previous artifacts, restart, re-doctor,
#                report. The host is never left half-updated.
#
# No-op run: when the resolved selections match what's deployed, NOTHING
# is touched (no restart) and the command exits 0.
#
# A run active at the restart step => the D8 update-waiting path (not a
# failure): steps 1-3 proceed, the flag is set, the command exits 0.
# An IMAGE swap is explicit operator downtime: the flag carries
# image_swap, which the app's tick never applies.
#
# Usage: deploy/update.sh [--env .env] [--ssh root@<vm-edge-ip>] [--deploy-key K]
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="$REPO/.env"
SSH_TARGET=""
DEPLOY_KEY="${SERVITOR_DEPLOY_KEY:-${HOME}/.srv/deploy_key}"

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
: "${SERVITOR_PI_VERSION:?SERVITOR_PI_VERSION missing}"
: "${SERVITOR_PI_WEB_ACCESS_VERSION:=0.47.0}"
: "${SERVITOR_VM_IP:?SERVITOR_VM_IP missing in $ENV_FILE (the guest edge address)}"
SSH_TARGET="${SSH_TARGET:-root@${SERVITOR_VM_IP}}"
[[ -f "$DEPLOY_KEY" ]] || { echo "deploy key not found: $DEPLOY_KEY" >&2; exit 1; }
SSH=(ssh -i "$DEPLOY_KEY" -o BatchMode=yes -o StrictHostKeyChecking=accept-new -o ConnectTimeout=10 "$SSH_TARGET")
run_guest() { "${SSH[@]}" "$@"; }

echo "==> [1/5] resolve: .env specs -> exact versions (host npm)"
PI_RESOLVED="$(npm view "@earendil-works/pi-coding-agent@${SERVITOR_PI_VERSION}" version 2>/dev/null | tail -1)"
[[ -n "$PI_RESOLVED" ]] || { echo "could not resolve pi spec: $SERVITOR_PI_VERSION" >&2; exit 1; }
EXT_RESOLVED=""
EXT_STAGING=""
if [[ -n "$SERVITOR_PI_WEB_ACCESS_VERSION" ]]; then
  EXT_RESOLVED="$(npm view "pi-web-access@${SERVITOR_PI_WEB_ACCESS_VERSION}" version 2>/dev/null | tail -1)"
  [[ -n "$EXT_RESOLVED" ]] || { echo "could not resolve extension spec: $SERVITOR_PI_WEB_ACCESS_VERSION" >&2; exit 1; }
fi
echo "    pi:        $SERVITOR_PI_VERSION -> $PI_RESOLVED"
[[ -n "$EXT_RESOLVED" ]] && echo "    extension: $SERVITOR_PI_WEB_ACCESS_VERSION -> $EXT_RESOLVED"

echo "==> [2/5] what's deployed (guest)"
GUEST_PI="$(run_guest "node -e \"try{console.log(require('/usr/lib/node_modules/@earendil-works/pi-coding-agent/package.json').version)}catch(e){console.log('')}\"" || true)"
GUEST_EXT="$(run_guest "node -e \"try{console.log(require('/opt/pi-ext/pi-web-access/package.json').version)}catch(e){console.log('')}\"" || true)"
echo "    deployed:  pi=$GUEST_PI ext=${GUEST_EXT:-none}"

# ---- no-op check (the config/app/key shape is decided by the sync) -----
PI_BUMP=""
EXT_BUMP=""
[[ "$PI_RESOLVED" != "$GUEST_PI" ]] && PI_BUMP=1
[[ -n "$EXT_RESOLVED" && "$EXT_RESOLVED" != "$GUEST_EXT" ]] && EXT_BUMP=1

echo "==> [3/5] host-side gate: conformance against the RESOLVED versions"
# the gate installs the resolved versions into a scratch prefix and runs
# the full suite — the guest is untouched until it passes
export SERVITOR_PI_VERSION="$PI_RESOLVED"
export SERVITOR_PI_WEB_ACCESS_VERSION="${EXT_RESOLVED:-}"
GATE_LOG="$(mktemp /tmp/servitor-gate.XXXXXX.log)"
set +e
if [[ -n "$EXT_BUMP" || -n "$PI_BUMP" ]]; then
  # versions moved: the FULL conformance gate (root)
  sudo -n "$REPO/.venv/bin/python" -m pytest test/conformance -q 2>&1 | tail -15 | tee "$GATE_LOG" \
    || sudo "$REPO/.venv/bin/python" -m pytest test/conformance -q 2>&1 | tail -15 | tee "$GATE_LOG"
  GATE_RC=${PIPESTATUS[0]}
else
  # no version move: the versioned gate is the deployed-state check;
  # the fast shape changes (config/app/keys) still run the sync + doctor
  echo "    (no version bump — skipping the full conformance re-gate; shape changes gate via the doctor)"
  GATE_RC=0
fi
set -e
[[ "$GATE_RC" -eq 0 ]] || { echo "GATE RED — nothing deployed (log: $GATE_LOG)" >&2; exit 1; }
echo "    gate GREEN"

# the gate's scratch staging is reusable for the extension push:
# (the conformance installer pins the scratch prefix under test/.scratch;
#  the sync re-stages independently when the extension bumps)

echo "==> [4/5] provision + deploy"
if [[ -n "$PI_BUMP" ]]; then
  echo "    pi bumped: rebuilding the image (previous retained for rollback)…"
  sudo -n unshare -m bash "$REPO/deploy/guest/build.sh" --env "$ENV_FILE" --deploy-key "${DEPLOY_KEY}.pub" \
    || sudo unshare -m bash "$REPO/deploy/guest/build.sh" --env "$ENV_FILE" --deploy-key "${DEPLOY_KEY}.pub"
  # the image swap is EXPLICIT operator downtime: flag it, never auto-apply
  run_guest "cd /opt/servitor/app && .venv/bin/python -c \"
from app.update_pending import UpdatePending, save
save('/var/lib/servitor/update-pending.json', UpdatePending(
    reason='update: image swap pending (explicit operator downtime)',
    restart=False, doctor=False, image_swap=True))
print('image-swap flag set (explicit apply required)')
\""
  echo "    IMAGE SWAP PENDING: apply with explicit downtime (harness --apply-image), then re-run update."
  echo "update: OK (versioned gate green; image swap pending explicit apply)"
  exit 0
fi

# config/app/lib/keys/extension: the sync's swap-based path (its own
# shape detection decides no-op vs flag). Sync rc contract:
#   0  = NO-OP, or deployed + doctor GREEN, or update-WAITING (run active)
#   3  = deploy LANDED but doctor RED -> the automatic rollback below
#   1+ = pre-deploy failure (nothing to roll back)
set +e
bash "$REPO/deploy/sync.sh" --env "$ENV_FILE" --ssh "$SSH_TARGET" --deploy-key "$DEPLOY_KEY"
SYNC_RC=$?
set -e
if [[ $SYNC_RC -eq 1 ]]; then
  echo "sync failed before deploy (rc=$SYNC_RC) — nothing to roll back" >&2
  exit 1
fi
[[ $SYNC_RC -eq 0 || $SYNC_RC -eq 3 ]] || { echo "sync failed (rc=$SYNC_RC) — aborting" >&2; exit 1; }

echo "==> [5/5] verify (doctor is inside the sync's post-restart path; re-check)"
if bash "$REPO/deploy/doctor.sh" --env "$ENV_FILE" --ssh "$SSH_TARGET" --deploy-key "$DEPLOY_KEY"; then
  echo "update: OK — matches repo + .env selections, doctor GREEN"
  exit 0
fi

# ---- automatic rollback --------------------------------------------------
echo "update: doctor RED after deploy — AUTOMATIC ROLLBACK" >&2
# single-quoted: every expansion happens on the GUEST. The old double-quoted
# form expanded $PREV_CFG host-side, where it is unbound — under set -u the
# host aborted the rollback before the guest ever saw it (and $(cat ...)
# ran on the host, where the path does not exist).
run_guest '
  set -e
  # previous extension (retained at /opt/pi-ext/.previous)
  if test -d /opt/pi-ext/.previous; then
    rm -rf /opt/pi-ext/pi-web-access
    mv /opt/pi-ext/.previous /opt/pi-ext/pi-web-access
  fi
  # previous config version: the sync records the previous active name in
  # .previous-active at swap time (content-addressed names cannot be
  # recovered by globbing v*)
  PREV_CFG="$(cat /var/lib/servitor/config/.previous-active 2>/dev/null || true)"
  if [ -n "$PREV_CFG" ] && [ -d "/var/lib/servitor/config/$PREV_CFG" ]; then
    ln -s "$PREV_CFG" /var/lib/servitor/config/.active-tmp-rollback
    mv -T /var/lib/servitor/config/.active-tmp-rollback /var/lib/servitor/config/active
    echo "config rolled back to $PREV_CFG"
  else
    echo "WARN: no .previous-active marker — config pointer left as-is" >&2
  fi
  # previous app tree (retained by the sync, refreshed flat before every
  # push). Guard on the DIRECTORY, not the marker file: a marker without
  # the tree would make the mv below fail under set -e and abort the
  # whole rollback
  if test -d /opt/servitor/app.previous.app; then
    rm -rf /opt/servitor/app/app
    mv /opt/servitor/app.previous.app /opt/servitor/app/app
    if test -f /opt/servitor/app.previous.pyproject; then
      mv /opt/servitor/app.previous.pyproject /opt/servitor/app/pyproject.toml
    fi
    rm -f /opt/servitor/app.previous
  fi
  systemctl restart servitor || true
'
sleep 5
if bash "$REPO/deploy/doctor.sh" --env "$ENV_FILE" --ssh "$SSH_TARGET" --deploy-key "$DEPLOY_KEY"; then
  echo "update: ROLLED BACK — previous versions restored, service healthy, doctor GREEN"
  exit 0
fi
echo "update: ROLLBACK FAILED — manual intervention required (previous artifacts retained on the guest)" >&2
exit 1
