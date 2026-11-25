#!/usr/bin/env bash
# Servitor host-side pull: the kept shelf, mirrored to the host.
#
# The shelf (`/var/lib/servitor/keep`) outlives every session and is deleted
# only by an explicit delete in the UI — the one part of the guest with no
# other copy. This is that copy: host-side, one-way (guest -> host), over the
# same deploy key `sync` uses. The guest holds no host credentials and the
# agent has no route here — reconciliation stays host-side, as with git.
#
# Data budget (this may run over a metered link): the shelf is LISTED first
# through the API and the run reports the byte count before moving any of
# them; rsync moves changed bytes only (size+mtime), so a re-run is
# near-free; --new-only reports what the host lacks and then pulls ONLY
# that; --dry-run costs nothing. The host copy never deletes by surprise:
# files the shelf no longer has stay on the host unless --mirror says
# otherwise.
#
# Usage (host, from a checkout with the .env):
#   deploy/pull.sh [--env .env] [--ssh root@<vm-edge-ip>] [--deploy-key ~/.srv/deploy_key]
#                  [--dest DIR] [--new-only] [--mirror] [--dry-run]
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="$REPO/.env"
SSH_TARGET=""
DEPLOY_KEY="${SERVITOR_DEPLOY_KEY:-${HOME:-/root}/.srv/deploy_key}"
DEST=""
NEW_ONLY=0
MIRROR=0
DRY_RUN=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --env) ENV_FILE="$2"; shift 2 ;;
    --ssh) SSH_TARGET="$2"; shift 2 ;;
    --deploy-key) DEPLOY_KEY="$2"; shift 2 ;;
    --dest) DEST="$2"; shift 2 ;;
    --new-only) NEW_ONLY=1; shift ;;
    --mirror) MIRROR=1; shift ;;
    --dry-run) DRY_RUN=1; shift ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
done

[[ -f "$ENV_FILE" ]] || { echo "env file not found: $ENV_FILE" >&2; exit 1; }
set -a; source <(grep -E '^[A-Za-z_][A-Za-z0-9_]*=' "$ENV_FILE"); set +a
: "${SERVITOR_VM_IP:?SERVITOR_VM_IP missing in $ENV_FILE (the guest edge address)}"
# doctor.sh/sync.sh default the same way: .env may omit a settled-default line
# and a bare $SERVITOR_PORT under set -u would die mid-script
: "${SERVITOR_PORT:=8787}"
SSH_TARGET="${SSH_TARGET:-root@${SERVITOR_VM_IP}}"
DEST="${DEST:-${SERVITOR_PULL_DIR:-${HOME:-/root}/servitor-kept}}"
SHELF="/var/lib/servitor/keep"
[[ -f "$DEPLOY_KEY" ]] || { echo "deploy key not found: $DEPLOY_KEY" >&2; exit 1; }

# an array, not a string: an unquoted $STRING expansion never re-interprets
# quotes, so the @Q idiom in a plain variable would hand ssh an identity path
# wrapped in literal quote characters. rsync's -e re-parses through a shell,
# so it gets the same command re-quoted with printf %q.
SSH=(ssh -i "$DEPLOY_KEY" -o BatchMode=yes -o StrictHostKeyChecking=accept-new -o ConnectTimeout=10)
RSH="$(printf '%q ' "${SSH[@]}")"

# ---- what is on the shelf (API, over the edge; the edge credential rides) --
# The listing is the honest size estimate: paths, sizes, mtimes. It is read
# through the API rather than a remote `find` so this script pulls exactly the
# set the human sees in the UI — not whatever happens to be under the mount.
LIST_URL="http://${SERVITOR_VM_IP}:${SERVITOR_PORT}/api/v1/files"
CURL=(curl -sf -m 20)
# the key rides on a quoted curl-config line via stdin, never argv: the
# sibling scripts (sync.sh/doctor.sh) make the same point — /proc/<pid>/
# cmdline is world-readable (unlike environ), so a secret on argv would leak
# to every local uid for the life of the request. The config line can't carry
# a quote in the value, and the contract value is hex anyway — say so rather
# than die inside curl with a malformed config.
if [[ -n "${SERVITOR_EDGE_KEY:-}" ]]; then
  case "$SERVITOR_EDGE_KEY" in
    *\"*|*\\*|*$'\n'*) echo "SERVITOR_EDGE_KEY is not a plain value (quote/backslash/newline) — the config line can't carry it" >&2; exit 1 ;;
  esac
  if ! LIST="$(printf 'header = "X-Servitor-Edge-Key: %s"\n' "$SERVITOR_EDGE_KEY" | "${CURL[@]}" -K - "$LIST_URL" 2>/dev/null)"; then
    # The API is the gate's business; an unreachable edge must not silently
    # fall back to a raw remote walk (which would pull files the UI would not
    # show). Say so and stop, before any transfer.
    echo "pull: FAIL — cannot read the shelf listing from $LIST_URL (is the service up, and is SERVITOR_EDGE_KEY current?)" >&2
    exit 1
  fi
else
  if ! LIST="$("${CURL[@]}" "$LIST_URL" 2>/dev/null)"; then
    echo "pull: FAIL — cannot read the shelf listing from $LIST_URL (is the service up?)" >&2
    exit 1
  fi
fi

if [[ "$NEW_ONLY" == "1" && "$MIRROR" == "1" ]]; then
  echo "pull: --new-only and --mirror are opposites (pull only what is missing vs delete what is gone)" >&2
  exit 2
fi

WORK="$(mktemp -d)"
cleanup() { rm -rf "$WORK"; }
trap cleanup EXIT
# the listing rides as a FILE: the summary program below comes from a heredoc
# (which owns stdin), and a payload in argv would sit in the process table for
# the life of the summary
printf '%s' "$LIST" > "$WORK/keep.json"

# an empty shelf is nothing to do — say so BEFORE the summary, so an empty
# run never reads as "0 files to pull" followed by "there is nothing here"
if ! python3 -c 'import json,sys; sys.exit(0 if json.load(open(sys.argv[1])).get("keep") else 1)' "$WORK/keep.json"; then
  echo "pull: the shelf is empty — nothing to do (keep a file in the UI first)"
  exit 0
fi

python3 - "$WORK/keep.json" "$DEST" "$NEW_ONLY" "$WORK/new-only.txt" <<'PY'
import json, os, sys

path, dest, new_only, list_path = sys.argv[1], sys.argv[2], sys.argv[3] == "1", sys.argv[4]
rows = json.load(open(path)).get("keep", [])
have = set()
todo = rows
if new_only:
    if os.path.isdir(dest):
        for dirpath, _d, names in os.walk(dest):
            for n in names:
                have.add(os.path.relpath(os.path.join(dirpath, n), dest))
    todo = [r for r in rows if r["path"] not in have]
    # the pull restriction, as rsync --files-from relpaths: the transfer is
    # exactly this list, nothing else moves
    with open(list_path, "w") as f:
        for r in todo:
            f.write(r["path"] + "\n")
total = sum(r["size"] for r in todo)
print(
    f"shelf: {len(rows)} file(s); to pull {len(todo)} ({total / (1024 * 1024):.1f} MB)"
    + (f", already on the host {len(rows) - len(todo)}" if new_only else "")
)
for r in todo[:20]:
    print(f"  {r['path']}  {r['size'] / (1024 * 1024):.1f} MB")
if len(todo) > 20:
    print(f"  … and {len(todo) - 20} more")
PY

if [[ "$NEW_ONLY" == "1" ]] && [[ ! -s "$WORK/new-only.txt" ]]; then
  echo "pull: the host already has everything on the shelf — nothing to pull"
  exit 0
fi

if [[ "$DRY_RUN" == "1" ]]; then
  echo "pull: --dry-run (no bytes moved)"
  exit 0
fi

# the guest-side half of this transfer must exist: an image built before the
# pins gained rsync will not have it, and rsync's own failure for a missing
# remote binary is a cryptic "connection unexpectedly closed" — say so
# plainly, before any byte moves
if ! "${SSH[@]}" "$SSH_TARGET" "command -v rsync" >/dev/null 2>&1; then
  echo "pull: FAIL — the guest has no rsync (its image predates the pins gaining it): rebuild the image (make update) and retry" >&2
  exit 1
fi

mkdir -p "$DEST"
RSYNC=(rsync -a --partial --human-readable --itemize-changes -e "$RSH")
# --delete only on an explicit --mirror: a backup that silently drops the
# files the shelf no longer has is a way to lose a kept file twice
[[ "$MIRROR" == "1" ]] && RSYNC+=(--delete)
# --new-only: move exactly what the host lacks, and nothing else (rsync
# creates the parent directories of a listed file on its own)
[[ "$NEW_ONLY" == "1" ]] && RSYNC+=(--files-from="$WORK/new-only.txt")
echo "pull: $SSH_TARGET:$SHELF/ -> $DEST/"
"${RSYNC[@]}" "$SSH_TARGET:$SHELF/" "$DEST/"
echo "pull: OK — the kept shelf is on the host at $DEST"
