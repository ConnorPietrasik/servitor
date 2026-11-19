#!/usr/bin/env bash
# Servitor host-side one-way sync.
#
# Fast config/app push for daily edits:
#   1. config  -> versioned dir under /var/lib/servitor/config + atomic
#                 active-pointer swap (the app picks it up at next spawn;
#                 live sessions keep their config until respawn)
#   2. lib/    -> /var/lib/servitor/lib (durable, git-backed, never GC'd)
#   3. app     -> /opt/servitor/app (code only; the venv is preserved,
#                 previous tree retained for the update rollback)
#   4. key slice -> the guest's 0600 env file (OMNISSIAH_API_KEY /
#                 EXA_API_KEY; rotation = edit host .env, re-run)
#   5. extension -> pi-web-access at the .env selection (host npm stage,
#                 rename on the guest, installed version stamped in the
#                 registry)
#
# Restart policy: when the deployed SHAPE changed, the sync writes
# the update-pending flag (restart + doctor) and NEVER blocks on a run:
#   - no run active  -> the app's tick restarts within a few seconds; the
#                       sync waits briefly, runs the doctor, and reports
#   - run active     -> the flag survives, the sync exits 0 immediately
#                       ("update-waiting": the tick applies at the first
#                       idle moment; doctor then runs on the new instance)
# When nothing changed: NO-OP — nothing is touched, no restart, exit 0.
#
# Usage (host, from a checkout with the .env):
#   deploy/sync.sh [--env .env] [--ssh root@<vm-edge-ip>] [--deploy-key ~/.srv/deploy_key]
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="$REPO/.env"
SSH_TARGET=""
# ${HOME:-/root}: panel-do's scrubbed env has no HOME (and set -u would die
# on a bare $HOME here, before --deploy-key is parsed); /root/.srv is the
# canonical root-side home, and the flag overrides anyway.
DEPLOY_KEY="${SERVITOR_DEPLOY_KEY:-${HOME:-/root}/.srv/deploy_key}"
STAGING_WAIT_SECS="${STAGING_WAIT_SECS:-150}"

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
: "${OMNISSIAH_API_KEY:?OMNISSIAH_API_KEY missing in $ENV_FILE}"
: "${SERVITOR_PI_WEB_ACCESS_VERSION:=0.48.0}"
: "${SERVITOR_VM_IP:?SERVITOR_VM_IP missing in $ENV_FILE (the guest edge address)}"
# doctor.sh defaults the same way: the .env may omit settled-default lines
# ("the file may omit any non-REQUIRED line"), and a bare $SERVITOR_PORT
# under set -u would die mid-script (after the pushes, before the flag).
: "${SERVITOR_PORT:=8787}"
SSH_TARGET="${SSH_TARGET:-root@${SERVITOR_VM_IP}}"
[[ -f "$DEPLOY_KEY" ]] || { echo "deploy key not found: $DEPLOY_KEY" >&2; exit 1; }

SSH=(ssh -i "$DEPLOY_KEY" -o BatchMode=yes -o StrictHostKeyChecking=accept-new \
     -o ConnectTimeout=10 "$SSH_TARGET")
run_guest() { "${SSH[@]}" "$@"; }
# Guest state read with transport-failure detection. ssh exits with the
# REMOTE command's rc — except 255, which is ssh's own failure (connection
# dropped / host down). rc 255 means the guest is UNREADABLE, not
# "nothing deployed": the empty readings a dead ssh fakes look exactly like
# a fresh image and manufacture a shape diff — a needless deploy + restart
# of a healthy VM — so 255 aborts BEFORE anything is pushed (exit 1 =
# pre-deploy failure in update.sh's rc contract: nothing to roll back).
# Every other rc is a legitimate observation and is kept as-is:
#   readlink 1 = no active version yet (fresh image)
#   node 127/1 = extension not installed
#   find|…|cut pipelines always end 0 (even a missing tree — the
#     empty-input hash, which correctly forces a push)
#   cd X && find 1 = no such dir (fresh image)
# fail-fast ($4): the venv + app tree ship with the image and Registry
# creates its db when absent, so a non-zero registry read is an anomaly,
# not a fresh-image state — abort, as the old set -e crash did, but with a
# reason on the line.
state_read() { # $1 = label, $2 = variable, $3 = remote command, $4 = fail-fast
  local -n _sr="$2"  # nameref: a quoted "$2"="…" would parse as a COMMAND, not an assignment
  local rc=0
  _sr="$(run_guest "$3")" || rc=$?
  if [[ $rc -eq 255 ]]; then
    echo "sync: FAIL — reading $1: ssh to $SSH_TARGET failed (the guest is unreadable — an empty reading would fake a fresh image and manufacture a shape diff); aborting before any change" >&2
    exit 1
  elif [[ -n "${4:-}" && $rc -ne 0 ]]; then
    echo "sync: FAIL — reading $1: remote command exited $rc (guest venv/registry unreadable — not a fresh-image state); aborting before any change" >&2
    exit 1
  fi
}

mkdir -p /srv/servitor-sync
WORK="$(mktemp -d /srv/servitor-sync/XXXXXX)"
cleanup() { rm -rf "$WORK"; }
trap cleanup EXIT

# ---- test-only hooks (never set in a real deploy) -------------------------
# SERVITOR_TEST_CONFIG_DIR: sync this dir instead of agent-config/ (the
# update-waiting drill points the guest at the host's mock router).
# SERVITOR_TEST_CONFIG_FAULT=bork: corrupt models.json in the artifact
# (fault-injected doctor drill: the deployed shape must fail the doctor,
# and the automatic rollback must restore a healthy state).
CONFIG_SRC="$REPO/agent-config"
[[ -n "${SERVITOR_TEST_CONFIG_DIR:-}" ]] && CONFIG_SRC="$SERVITOR_TEST_CONFIG_DIR"

# ---- what's deployed right now (guest) ----------------------------------
state_read "config active pointer" GUEST_ACTIVE "readlink /var/lib/servitor/config/active 2>/dev/null"
state_read "extension stamp (registry)" GUEST_EXT_STAMP "cd /opt/servitor/app && .venv/bin/python -c \"
from app.registry import Registry
r = Registry('/var/lib/servitor/registry.db')
print(r.get_version('pi_web_access') or '')
r.close()\"" 1
state_read "extension installed" GUEST_EXT_INSTALLED "node -e \"try{console.log(require('/opt/pi-ext/pi-web-access/package.json').version)}catch(e){console.log('')}\""
state_read "app tree hash" GUEST_APP_SHA "cd /opt/servitor/app && find app pyproject.toml -type f -not -path '*/__pycache__/*' 2>/dev/null | LC_ALL=C sort | xargs -r sha256sum 2>/dev/null | sha256sum | cut -d' ' -f1"
state_read "ui dist hash" GUEST_UI_SHA "cd /opt/servitor/app/ui/dist 2>/dev/null && find . -type f | LC_ALL=C sort | xargs -r sha256sum | sha256sum | cut -d' ' -f1"
state_read "env key slice" GUEST_ENV_SHA "grep -E '^(OMNISSIAH_API_KEY|EXA_API_KEY|SERVITOR_PI_VERSION|SERVITOR_PI_WEB_ACCESS_VERSION)=' /etc/servitor/servitor.env | LC_ALL=C sort | sha256sum | cut -d' ' -f1"

# ---- artifacts (host) ----------------------------------------------------
# config version: CONTENT-addressed (the directory's file set + bytes), so a
# second sync of an unchanged tree is a deterministic NO-OP regardless of
# time.
mkdir -p "$WORK/config"
cp -a "$CONFIG_SRC/." "$WORK/config/"
if [[ -n "${SERVITOR_TEST_CONFIG_FAULT:-}" ]]; then
  echo "    TEST HOOK: fault-injecting the config artifact (${SERVITOR_TEST_CONFIG_FAULT})"
  printf 'NOT-JSON {{{{\n' >> "$WORK/config/models.json"
fi
# the identity is computed from the STAGED artifact (after the test fault
# hook): the version-dir name is the identity of what actually gets deployed.
# A faulted artifact must have its OWN name — hashed before the fault it
# would be indistinguishable from the clean baseline, and the shape check
# would NO-OP the faulted deploy (the fault drill could never land).
CONFIG_CONTENT_SHA="$(cd "$WORK/config" && find . -type f | LC_ALL=C sort | xargs sha256sum | sha256sum | cut -d' ' -f1)"
# purely content-addressed: the identity is the content alone. A commit
# prefix in the name would make EVERY commit (even one that never touches
# agent-config) "change" the config — re-stage + a service restart on every
# sync for nothing.
CONFIG_VERSION="c${CONFIG_CONTENT_SHA:0:12}"
# the test-only router URL must never ride to the guest: the live router
# comes from models.json (baseUrl) + the env key
if [[ -f "$WORK/config/models.json" ]] && grep -oE '"baseUrl": *"[^"]*"' "$WORK/config/models.json" | grep -vq '"baseUrl": *"https://' ; then
  echo "WARN: models.json has a non-https baseUrl (a local router?); check before deploying" >&2
fi

# content-addressed like the config (find|sort|sha256): independent of tar
# metadata (owner name vs numeric uid, mtime, traversal order), so a second
# sync of an unchanged tree is a deterministic NO-OP
APP_SHA="$(cd "$REPO" && find app pyproject.toml -type f -not -path '*/__pycache__/*' 2>/dev/null | LC_ALL=C sort | xargs -r sha256sum 2>/dev/null | sha256sum | cut -d' ' -f1)"

# UI: build host-side when the ui/ source moved since the last build
# (stamp: ui/.ui-src-sha), then hash the dist — the same content
# addressing as config/app, so a second sync of an unchanged tree is a
# deterministic NO-OP. NO --ignore-scripts (vite/esbuild need install
# scripts — unlike the pinned pi stage). The app serves the dist on the
# same port; a UI-only change deploys here without an image rebuild.
# (The stamp is excluded from the source hash — it is the hash's output,
# and hashing it in would re-trigger a build on every run.)
UI_SRC_SHA="$(cd "$REPO/ui" && find . -type f -not -path './node_modules/*' -not -path './dist/*' -not -path './.ui-src-sha' | LC_ALL=C sort | xargs -r sha256sum | sha256sum | cut -d' ' -f1)"
if [[ ! -f "$REPO/ui/dist/index.html" || "$(cat "$REPO/ui/.ui-src-sha" 2>/dev/null || true)" != "$UI_SRC_SHA" ]]; then
  echo "sync: building ui (vite)…"
  ( cd "$REPO/ui" && npm ci --no-audit --no-fund >/dev/null && npm run build >/dev/null ) \
    || { echo "ui build failed (npm ci + npm run build in $REPO/ui)" >&2; exit 1; }
  echo "$UI_SRC_SHA" > "$REPO/ui/.ui-src-sha"
fi
UI_DIST_SHA="$(cd "$REPO/ui/dist" && find . -type f | LC_ALL=C sort | xargs -r sha256sum | sha256sum | cut -d' ' -f1)"
KEYS_SHA="$( { printf 'OMNISSIAH_API_KEY=%s\nEXA_API_KEY=%s\nSERVITOR_PI_VERSION=%s\nSERVITOR_PI_WEB_ACCESS_VERSION=%s\n' "$OMNISSIAH_API_KEY" "${EXA_API_KEY:-}" "${SERVITOR_PI_VERSION:-}" "${SERVITOR_PI_WEB_ACCESS_VERSION:-}"; } | LC_ALL=C sort | sha256sum | cut -d' ' -f1)"

# extension: host npm stage at the .env selection (the same command the
# conformance gate uses — --ignore-scripts, absolute prefix)
EXT_VERSION=""
if [[ -n "$SERVITOR_PI_WEB_ACCESS_VERSION" ]]; then
  EXT_STAGING="$WORK/ext-prefix"
  mkdir -p "$EXT_STAGING"
  if [[ ! -f "$EXT_STAGING/node_modules/pi-web-access/package.json" ]]; then
    npm install --ignore-scripts --no-audit --no-fund \
      "--prefix=$EXT_STAGING" "pi-web-access@$SERVITOR_PI_WEB_ACCESS_VERSION" >/dev/null \
      || { echo "extension stage failed (pi-web-access@$SERVITOR_PI_WEB_ACCESS_VERSION)" >&2; exit 1; }
  fi
  # self-contain the extension: npm hoists its deps to SIBLINGS under
  # node_modules/, but only pi-web-access/ ships to the guest (single-dir
  # tar). Move every dep into pi-web-access/node_modules/ so Node's module
  # resolution finds them and the tar below is complete (fixes the runtime
  # "Cannot find module 'p-limit'" extension-load failure).
  NM="$EXT_STAGING/node_modules"
  mkdir -p "$NM/pi-web-access/node_modules"
  shopt -s dotglob nullglob
  for entry in "$NM"/*; do
    [ "$(basename "$entry")" = "pi-web-access" ] && continue
    mv "$entry" "$NM/pi-web-access/node_modules/"
  done
  shopt -u dotglob nullglob
  EXT_VERSION="$(node -e "console.log(require('$EXT_STAGING/node_modules/pi-web-access/package.json').version)")"
fi

# ---- shape change? --------------------------------------------------------
CHANGED=()
[[ "$CONFIG_VERSION" != "$(basename "${GUEST_ACTIVE:-}")" ]] && CHANGED+=("config")
[[ "$EXT_VERSION" != "${GUEST_EXT_INSTALLED:-}" ]] && CHANGED+=("extension")
[[ "$APP_SHA" != "$GUEST_APP_SHA" ]] && CHANGED+=("app")
[[ "$UI_DIST_SHA" != "$GUEST_UI_SHA" ]] && CHANGED+=("ui")
[[ "$KEYS_SHA" != "$GUEST_ENV_SHA" ]] && CHANGED+=("env")

if [[ ${#CHANGED[@]} -eq 0 ]]; then
  echo "sync: NO-OP (config=$CONFIG_VERSION ext=${EXT_VERSION:-none} app+env unchanged) — nothing touched, no restart"
  exit 0
fi
echo "sync: shape changed: ${CHANGED[*]}"

# ---- push artifacts --------------------------------------------------------
echo "sync: pushing artifacts to $SSH_TARGET"
SCp() { scp -i "$DEPLOY_KEY" -o BatchMode=yes -o StrictHostKeyChecking=accept-new -q "$@"; }

# app first (previous tree retained for rollback). Refresh it BEFORE every
# push, flat: rm-then-copy, because `cp -a` into an EXISTING dir nests the
# package one level too deep (a rolled-back tree then lands at
# /opt/servitor/app/app/app — the service keeps running the stale flat
# tree and the rollback's doctor runs stale code). The image may ship
# stale app.previous* from its own build-time sync — the refresh replaces
# them before any rollback can see them.
run_guest '
  rm -rf /opt/servitor/app.previous.app /opt/servitor/app.previous.pyproject /opt/servitor/app.previous
  cp -a /opt/servitor/app/app /opt/servitor/app.previous.app
  cp /opt/servitor/app/pyproject.toml /opt/servitor/app.previous.pyproject 2>/dev/null || true
  touch /opt/servitor/app.previous
' || true
# replace the package atomically-enough: rm first, then extract — a bare
# tar -x over an existing tree keeps stale files (including nested
# app/app/app from old bugs) and GUEST_APP_SHA (which hashes the whole
# tree) would never match the repo, forcing an app re-push on every sync
run_guest "rm -rf /opt/servitor/app/app"
tar -C "$REPO" --exclude='app/__pycache__' --exclude='app/*/__pycache__' -cf "$WORK/app.tar" app pyproject.toml
SCp "$WORK/app.tar" "$SSH_TARGET:/tmp/servitor-app.tar"
run_guest "tar -xf /tmp/servitor-app.tar -C /opt/servitor/app && rm -f /tmp/servitor-app.tar"

# ui (the static dist; the app serves it on the same port — rm+extract
# like the app: a tar -x over the old tree keeps deleted asset files).
# The app resolves <app root>/ui/dist (app/api/app.py) — the tar holds the
# dist's CONTENTS, so the target is the dist dir itself, one level below ui/.
tar -C "$REPO/ui/dist" -cf "$WORK/ui.tar" .
SCp "$WORK/ui.tar" "$SSH_TARGET:/tmp/servitor-ui.tar"
run_guest "
  set -e
  rm -rf /opt/servitor/app/ui
  mkdir -p /opt/servitor/app/ui/dist
  tar -xf /tmp/servitor-ui.tar -C /opt/servitor/app/ui/dist
  rm -f /tmp/servitor-ui.tar
"

# config: versioned stage + atomic active-pointer swap. The version dir is
# the CONTENT-ADDRESSed name (== CONFIG_VERSION): the directory IS the
# identity, which is what makes the NO-OP check above stable. (The app's
# boot-time _stage_config uses its own vN naming only when NO version
# exists yet: first boot stages v1; the first sync replaces it with the
# content-addressed name.) The previous active version is recorded in
# .previous-active; the update's automatic rollback flips back to it.
PREV_ACTIVE="$(run_guest "readlink /var/lib/servitor/config/active 2>/dev/null" || true)"
tar -C "$WORK/config" -cf "$WORK/config.tar" .
SCp "$WORK/config.tar" "$SSH_TARGET:/tmp/servitor-config.tar"
run_guest "
  set -e
  ROOT=/var/lib/servitor/config
  if [[ -n '$PREV_ACTIVE' ]]; then echo '$PREV_ACTIVE' > \$ROOT/.previous-active; fi
  rm -rf \$ROOT/.incoming && mkdir -p \$ROOT/.incoming
  tar -xf /tmp/servitor-config.tar -C \$ROOT/.incoming
  rm -f /tmp/servitor-config.tar
  rm -rf \$ROOT/$CONFIG_VERSION   # idempotent: replace any same-named version
  mv \$ROOT/.incoming \$ROOT/$CONFIG_VERSION   # atomic: same filesystem
  ln -s $CONFIG_VERSION \$ROOT/.active-tmp-\$\$ && mv -T \$ROOT/.active-tmp-\$\$ \$ROOT/active
"
NEW_VERSION="$CONFIG_VERSION"
echo "    config active -> $NEW_VERSION"

# lib/ (durable, human-curated; pushed as-is)
if [[ -d "$REPO/lib" ]]; then
  tar -C "$REPO" -cf "$WORK/lib.tar" lib
  SCp "$WORK/lib.tar" "$SSH_TARGET:/tmp/servitor-lib.tar"
  run_guest "rm -rf /var/lib/servitor/lib && tar -xf /tmp/servitor-lib.tar -C /var/lib/servitor && rm -f /tmp/servitor-lib.tar"
fi

# key slice + version selections: merge into the 0600 env file (atomic).
# The selections ride here too (not just the keys): the guest-side doctor
# compares the installed extension against the .env selection, and the
# guest env is the deployment's source of truth — so an extension bump
# must land there or the post-deploy doctor goes red.
# write the key slice to a real temp file first: modern OpenSSH scp (SFTP
# protocol) rejects a process substitution ("/dev/fd/N is not a regular").
# The file lives in $WORK (the 0700 mktemp dir, removed by the trap) and the
# secrets already exist on the host in .env, so this is not a new exposure.
KEYS_TMP="$WORK/servitor-keys.$$"
{ printf 'OMNISSIAH_API_KEY=%s\nEXA_API_KEY=%s\nSERVITOR_PI_VERSION=%s\nSERVITOR_PI_WEB_ACCESS_VERSION=%s\n' "$OMNISSIAH_API_KEY" "${EXA_API_KEY:-}" "${SERVITOR_PI_VERSION:-}" "${SERVITOR_PI_WEB_ACCESS_VERSION:-}"; } > "$KEYS_TMP"
chmod 600 "$KEYS_TMP"
SCp "$KEYS_TMP" "$SSH_TARGET:/tmp/servitor-keys"
run_guest "
  cd /opt/servitor/app && .venv/bin/python - <<'PYEOF'
import os, tempfile
lines = open('/tmp/servitor-keys').read().splitlines()
new = {l.split('=', 1)[0]: l for l in lines if '=' in l}
path = '/etc/servitor/servitor.env'
out = []
seen = set()
for raw in open(path):
    line = raw.rstrip('\n')  # normalize: the write adds exactly one '\n' per line
    if not line or line.lstrip().startswith('#'):
        continue  # drop blank/comment lines (systemd ignores them; keeping
                  # them once was the bloat bug: preserved lines got a second
                  # '\n' every sync and blank lines DOUBLED each pass, blowing
                  # the file past execve's env limit after ~28 syncs)
    k = line.split('=', 1)[0]
    if k in new:
        out.append(new[k]); seen.add(k)
    else:
        out.append(line)
for k, l in new.items():
    if k not in seen:
        out.append(l)
fd, tmp = tempfile.mkstemp(dir='/etc/servitor')
open(tmp, 'w').write(''.join(l + '\n' for l in out))
os.chmod(tmp, 0o600)
os.replace(tmp, path)
PYEOF
  rm -f /tmp/servitor-keys"

# host name pinning (hairpin NAT): the model router's hostname is pinned to a
# LAN IP in the laptop's /etc/hosts (panel-hosts block). The guest has no such
# entry, so its DNS returns a public IP and traffic from the guest — which
# exits through the host's NAT — hairpins and dies; the real-router prompt
# then comes back with empty assistant turns. Mirror the host's pinned lines
# into the guest's /etc/hosts so name resolution matches the laptop. Managed
# between markers, re-synced idempotently; if the laptop has no pinned lines
# (public-only setup) nothing is written.
HOSTS_LINES="$(awk '/# BEGIN panel-hosts/,/# END panel-hosts/' /etc/hosts | grep -E '^[0-9]+(\.[0-9]+)+[[:space:]]')"
if [[ -n "$HOSTS_LINES" ]]; then
  printf '%s\n' "$HOSTS_LINES" > "$WORK/servitor-hosts"
  SCp "$WORK/servitor-hosts" "$SSH_TARGET:/tmp/servitor-hosts"
  run_guest "
    awk '/# BEGIN servitor-hosts/,/# END servitor-hosts/{next} {print}' /etc/hosts > /tmp/hosts.clean
    {
      cat /tmp/hosts.clean
      echo '# BEGIN servitor-hosts (mirrored from the laptop /etc/hosts)'
      cat /tmp/servitor-hosts
      echo '# END servitor-hosts'
    } > /tmp/hosts.new
    mv /tmp/hosts.new /etc/hosts && rm -f /tmp/hosts.clean /tmp/servitor-hosts
  "
fi

# self-heal the edge gateway on images built before the build.sh fix:
# they bake in a 5-octet invalid gateway (the subnet's zero-base + ".1")
# which systemd-networkd silently drops — the guest boots with no default
# route and the live-router gates die. Rewrite + restart networkd only when
# the line is wrong (a networkd restart briefly flaps the ssh link; the
# `|| true` swallows that, the next sync sees the fixed line)
if [[ -n "${SERVITOR_EDGE_SUBNET:-}" ]]; then
  GOOD_GW="$(cut -d. -f1-3 <<<"${SERVITOR_EDGE_SUBNET%%/*}").1"
  run_guest "grep -qx 'Gateway=${GOOD_GW}' /etc/systemd/network/10-servitor.network || { sed -i 's/^Gateway=.*/Gateway=${GOOD_GW}/' /etc/systemd/network/10-servitor.network; systemctl restart systemd-networkd; sleep 1; }" || true
fi

# extension: stage + rename (the previous install retained at .previous)
if [[ -n "$EXT_VERSION" ]]; then
  tar -C "$EXT_STAGING/node_modules" -cf "$WORK/ext.tar" pi-web-access
  SCp "$WORK/ext.tar" "$SSH_TARGET:/tmp/servitor-ext.tar"
  run_guest "
    set -e
    cd /opt/pi-ext
    if test -d pi-web-access; then rm -rf .previous; mv pi-web-access .previous; fi
    rm -rf .staging && mkdir .staging
    tar -xf /tmp/servitor-ext.tar -C .staging
    mv .staging/pi-web-access pi-web-access && rmdir .staging
    rm -f /tmp/servitor-ext.tar
    cd /opt/servitor/app && .venv/bin/python -c \"
from app.registry import Registry
r = Registry('/var/lib/servitor/registry.db')
r.set_version('pi_web_access', '$EXT_VERSION')
r.close()
print('stamp: pi-web-access $EXT_VERSION')
\""
fi

# ---- the restart (flag, never a block) ------------------------------------
run_guest "cd /opt/servitor/app && .venv/bin/python -c \"
from app.update_pending import UpdatePending, save
save('/var/lib/servitor/update-pending.json', UpdatePending(
    reason='sync $(date -u +%Y-%m-%dT%H:%M:%SZ) config=$NEW_VERSION ext=$EXT_VERSION',
    restart=True, doctor=True))
print('update-pending flag written')
\""

# If a run is ACTIVE right now, exit 0 IMMEDIATELY (never block on a
# run). The flag survives; the app's tick applies restart+doctor at the
# first idle moment. Query the app over the edge (the port is open from
# the edge subnet by design).
RUN_ACTIVE=0
SESS_JSON="$(curl -sf -m 5 "http://${SERVITOR_VM_IP}:${SERVITOR_PORT}/api/v1/sessions" 2>/dev/null || true)"
if [[ -n "$SESS_JSON" ]]; then
  while IFS= read -r sid; do
    [[ -n "$sid" ]] || continue
    if curl -sf -m 5 "http://${SERVITOR_VM_IP}:${SERVITOR_PORT}/api/v1/sessions/$sid/probe" \
        2>/dev/null | grep -q '"busy": *true'; then
      RUN_ACTIVE=1; break
    fi
  done < <(printf '%s' "$SESS_JSON" | python3 -c 'import json,sys; [print(s["id"]) for s in json.load(sys.stdin).get("sessions", [])]' 2>/dev/null || true)
fi
if [[ "$RUN_ACTIVE" == "1" ]]; then
  echo "sync: OK — update-WAITING (a run is active; the flag survives and the tick applies restart+doctor at the first idle moment). Nothing was interrupted."
  exit 0
fi

echo "sync: flag written; waiting up to ${STAGING_WAIT_SECS}s for the app's tick to apply it (never blocks a run)…"
APPLIED=0
for ((i = 0; i < STAGING_WAIT_SECS; i += 5)); do
  rc=0
  run_guest "test -f /var/lib/servitor/update-pending.json" 2>/dev/null || rc=$?
  # rc is the REMOTE test's exit status only when the ssh itself succeeded:
  # 0 = the flag is still staged, 1 = it has been cleared. Any other rc
  # (255 = an ssh hiccup) is NOT "applied" — a transient ssh failure once
  # flipped APPLIED while the tick was mid-apply, and the edge probe raced
  # a healthy restart into a false exit 3 (a rollback of a sound deploy).
  if [[ $rc -eq 1 ]]; then
    APPLIED=1; break
  fi
  sleep 5
done

if [[ "$APPLIED" == "1" ]]; then
  echo "sync: flag cleared by the new instance; confirming the edge port before the doctor…"
  # APPLIED means the flag FILE is gone — and the file outlives the restart:
  # the tick's consume_restart() only drops the restart bit (crash-safe,
  # pre-restart; the doctor-only remainder stays) and the NEW instance's
  # tick unlinks it only AFTER its post-restart doctor runs. So here the
  # service has already been up long enough to survive a full in-guest
  # doctor: the drain (≤ 90 s grace, < the unit's TimeoutStopSec) + boot
  # happen while the file still EXISTS (that is the update-WAITING path
  # below, where the tick certifies in-guest). The one way the port can be
  # down here is a post-doctor crash that systemd is cycling
  # (Restart=on-failure, RestartSec=2) — the probe budget is therefore a
  # BOOT budget (≤ TimeoutStartSec + imports), not drain+boot. Probe the
  # real bind address over the edge (the app binds SERVITOR_BIND — a
  # localhost probe can never see it), and never run the doctor against a
  # dead port: a refused-port doctor yields a mixed report (spawn checks
  # pass, everything HTTP fails) that misreads "still starting" as a broken
  # deploy.
  READY=0
  for ((i = 0; i < 90; i += 2)); do
    if curl -sf -m 3 "http://${SERVITOR_VM_IP}:${SERVITOR_PORT}/healthz" >/dev/null 2>&1; then
      READY=1
      break
    fi
    sleep 2
  done
  if [[ "$READY" != "1" ]]; then
    echo "sync: FAIL — service not answering on ${SERVITOR_VM_IP}:${SERVITOR_PORT} after the edge-probe window (~90 s when refused fast; ≤ ~225 s when each probe holds for its 3 s timeout) — exit 3: deploy landed; the update path must roll back" >&2
    exit 3
  fi
  if bash "$(dirname "${BASH_SOURCE[0]}")/doctor.sh" --env "$ENV_FILE" --ssh "$SSH_TARGET" --deploy-key "$DEPLOY_KEY"; then
    echo "sync: OK — shape deployed, doctor GREEN"
    exit 0
  else
    # exit 3 = deploy LANDED but the doctor is red: deploy/update.sh owns
    # the automatic rollback for this outcome. exit 1 is reserved for
    # pre-deploy failures (nothing to roll back).
    echo "sync: FAIL — doctor RED after deploy (exit 3: deploy landed; the update path must roll back)" >&2
    exit 3
  fi
else
  # Not applied within the wait. (A run being active can't be the reason:
  # that exits earlier, before the staging wait begins.) So the tick is
  # still applying the flag: pre-restart doctor + restart + post-restart
  # doctor, and EACH doctor carries a 300 s live-run cap — a full apply can
  # take ~4–12 min, well past the 150 s staging wait. Fine: the flag
  # survives, the tick finishes the job, and the post-restart doctor
  # certifies in-guest. Never blocks, always exit 0.
  echo "sync: OK — update-WAITING (no run active; the tick is still applying the staged restart + doctor — that can take up to ~10 min: two doctor runs, each with a 300 s live-run cap). The post-restart doctor certifies in-guest; verify with deploy/doctor.sh once it settles."
  exit 0
fi
