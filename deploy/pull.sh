#!/usr/bin/env bash
# Servitor host-side pull: the durable bytes, mirrored to the host.
#
# Two areas outlive every session and are deleted only by an explicit delete
# in the UI — the part of the guest with no other copy:
#
#   the kept shelf     /var/lib/servitor/keep   -> <dest>/
#   the library's UI saves                       -> <dest>/lib/
#
# The guest's library (`lib/`) ALSO holds the repo-pushed files, but those
# already have a host copy: the checkout's `lib/` is the source of truth and
# the sync's additive push keeps it current. So the pull takes only what the
# last push's manifest (`/var/lib/servitor/.lib-manifest`) does not list —
# the saves a human made in the UI. A guest with no manifest (no push landed
# yet) has no way to tell, and the whole library is pulled rather than
# guessed.
#
# One-way (guest -> host), over the same deploy key `sync` uses. The guest
# holds no host credentials and the agent has no route here — reconciliation
# stays host-side, as with git.
#
# Data budget (this may run over a metered link): both areas are LISTED first
# through the API and the run reports the byte count before moving any of
# them; rsync moves changed bytes only (size+mtime), so a re-run is
# near-free; --new-only reports what the host lacks and then pulls ONLY
# that; --dry-run costs nothing. The host copy never deletes by surprise:
# files an area no longer has stay on the host unless --mirror says
# otherwise. --mirror prunes each mirror to EXACTLY its pull set: the shelf
# through rsync --delete (the library mirror lives UNDER the shelf's mirror
# root and is shielded from that delete with an anchored /lib exclude), the
# library host-side (rsync's --delete is a no-op under --files-from —
# RsyncProject/rsync#533 — a list of individual paths never defines a
# parent's contents, so it prunes nothing).
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
LIB_AREA="/var/lib/servitor/lib"
LIB_MANIFEST="/var/lib/servitor/.lib-manifest"
[[ -f "$DEPLOY_KEY" ]] || { echo "deploy key not found: $DEPLOY_KEY" >&2; exit 1; }

# an array, not a string: an unquoted $STRING expansion never re-interprets
# quotes, so the @Q idiom in a plain variable would hand ssh an identity path
# wrapped in literal quote characters. rsync's -e re-parses through a shell,
# so it gets the same command re-quoted with printf %q.
SSH=(ssh -i "$DEPLOY_KEY" -o BatchMode=yes -o StrictHostKeyChecking=accept-new -o ConnectTimeout=10)
RSH="$(printf '%q ' "${SSH[@]}")"

# ---- what is on the durable areas (API, over the edge; the key rides) ----
# The listings are the honest size estimate: paths, sizes, mtimes. They are
# read through the API rather than a remote `find` so this script pulls
# exactly the set the human sees in the UI — not whatever happens to be under
# the mount.
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
fi
fetch_listing() { # $1 = label, $2 = variable, $3 = url
  local out="" rc=0
  if [[ -n "${SERVITOR_EDGE_KEY:-}" ]]; then
    out="$(printf 'header = "X-Servitor-Edge-Key: %s"\n' "$SERVITOR_EDGE_KEY" | "${CURL[@]}" -K - "$3" 2>/dev/null)" || rc=$?
  else
    out="$("${CURL[@]}" "$3" 2>/dev/null)" || rc=$?
  fi
  if [[ $rc -ne 0 ]]; then
    # The API is the gate's business; an unreachable edge must not silently
    # fall back to a raw remote walk (which would pull files the UI would not
    # show). Say so and stop, before any transfer.
    echo "pull: FAIL — cannot read $1 from $3 (is the service up, and is SERVITOR_EDGE_KEY current?)" >&2
    exit 1
  fi
  printf -v "$2" '%s' "$out"
}
fetch_listing "the shelf listing" LIST "http://${SERVITOR_VM_IP}:${SERVITOR_PORT}/api/v1/files"
fetch_listing "the library listing" LIB_LIST "http://${SERVITOR_VM_IP}:${SERVITOR_PORT}/api/v1/files/lib"

if [[ "$NEW_ONLY" == "1" && "$MIRROR" == "1" ]]; then
  echo "pull: --new-only and --mirror are opposites (pull only what is missing vs delete what is gone)" >&2
  exit 2
fi

WORK="$(mktemp -d)"
cleanup() { rm -rf "$WORK"; }
trap cleanup EXIT
# the listings ride as FILES: the summary program below comes from a heredoc
# (which owns stdin), and a payload in argv would sit in the process table for
# the life of the summary
printf '%s' "$LIST" > "$WORK/keep.json"
printf '%s' "$LIB_LIST" > "$WORK/lib.json"
# the last lib push's manifest (what the repo owns on the guest): absent or
# empty = no new-shape push landed yet = nothing is accounted for by the repo,
# so the whole library is the pull set (the conservative direction)
"${SSH[@]}" "$SSH_TARGET" "cat $LIB_MANIFEST 2>/dev/null" > "$WORK/lib-manifest" || true

# both areas empty is nothing to do — say so BEFORE the summary, so an empty
# run never reads as "0 files to pull" followed by "there is nothing here"
if ! python3 -c 'import json,sys; k=json.load(open(sys.argv[1])).get("keep"); l=json.load(open(sys.argv[2])).get("lib"); sys.exit(0 if (k or l) else 1)' "$WORK/keep.json" "$WORK/lib.json"; then
  echo "pull: nothing to do — the shelf is empty and the library holds no files (keep a file, or press → lib, first)"
  exit 0
fi

python3 - "$WORK/keep.json" "$WORK/lib.json" "$WORK/lib-manifest" "$DEST" "$NEW_ONLY" "$WORK/keep-files.txt" "$WORK/lib-files.txt" <<'PY'
import json, os, sys

keep_path, lib_path, man_path, dest = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4]
new_only = sys.argv[5] == "1"
keep_list, lib_list = sys.argv[6], sys.argv[7]
keep_rows = json.load(open(keep_path)).get("keep", [])
lib_rows = json.load(open(lib_path)).get("lib", [])
# the library's pull set: what the UI saved — everything the last push's
# manifest does not list. The manifest'd half already has a host copy: the
# checkout's lib/ is the source of truth for it, and pulling it again would
# pay metered bytes for bytes git already has.
manifest = set()
if os.path.isfile(man_path) and os.path.getsize(man_path) > 0:
    manifest = {line.strip() for line in open(man_path) if line.strip()}
lib_rows = [r for r in lib_rows if r["path"] not in manifest]

def host_have(root):
    have = set()
    if os.path.isdir(root):
        for dirpath, _d, names in os.walk(root):
            for n in names:
                have.add(os.path.relpath(os.path.join(dirpath, n), root))
    return have

todo_keep, todo_lib = keep_rows, lib_rows
if new_only:
    have_keep, have_lib = host_have(dest), host_have(os.path.join(dest, "lib"))
    todo_keep = [r for r in keep_rows if r["path"] not in have_keep]
    todo_lib = [r for r in lib_rows if r["path"] not in have_lib]
# the pull restriction, as rsync --files-from relpaths: the transfer is
# exactly these lists, nothing else moves (the library is ALWAYS restricted —
# its repo-pushed half belongs to the checkout, not to this mirror)
for rows_out, list_path in ((todo_keep, keep_list), (todo_lib, lib_list)):
    with open(list_path, "w") as f:
        for r in rows_out:
            f.write(r["path"] + "\n")

for label, rows, todo in (("shelf", keep_rows, todo_keep), ("library (UI saves)", lib_rows, todo_lib)):
    total = sum(r["size"] for r in todo)
    print(
        f"{label}: {len(rows)} file(s); to pull {len(todo)} ({total / (1024 * 1024):.1f} MB)"
        + (f", already on the host {len(rows) - len(todo)}" if new_only else "")
    )
    for r in todo[:20]:
        print(f"  {r['path']}  {r['size'] / (1024 * 1024):.1f} MB")
    if len(todo) > 20:
        print(f"  … and {len(todo) - 20} more")
PY

if [[ "$NEW_ONLY" == "1" ]] && [[ ! -s "$WORK/keep-files.txt" && ! -s "$WORK/lib-files.txt" ]]; then
  echo "pull: the host already has everything the guest holds that the repo does not — nothing to pull"
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
pull_rsync() { # $1 = guest src/ (no host: prefix), $2 = host dest/, $3 = files-from ("" = whole area), $4+ = extra flags
  local src=$1 dest=$2 ff=$3
  shift 3
  local RSYNC=(rsync -a --partial --human-readable --itemize-changes -e "$RSH")
  # --delete only on an explicit --mirror, and only for a WHOLE-AREA
  # transfer: under --files-from, rsync's --delete deletes NOTHING — it
  # prunes only inside directories whose complete contents are being
  # synced, and a list of individual paths never defines a parent's
  # contents (RsyncProject/rsync#533). A mirror of a file list is pruned
  # host-side instead (the library's: below); a backup that silently drops
  # the files an area no longer has is a way to lose them twice.
  [[ "$MIRROR" == "1" && -z "$ff" ]] && RSYNC+=(--delete)
  # files-from: move exactly this list, and nothing else (rsync creates the
  # parent directories of a listed file on its own)
  [[ -n "$ff" ]] && RSYNC+=(--files-from="$ff")
  RSYNC+=("$@")
  echo "pull: $SSH_TARGET:$src -> $dest/"
  "${RSYNC[@]}" "$SSH_TARGET:$src" "$dest/"
}
KEEP_FF=""
[[ "$NEW_ONLY" == "1" ]] && KEEP_FF="$WORK/keep-files.txt"
# the shelf's --delete prunes the WHOLE $DEST — including the library
# mirror, which lives under it. Shield that tree with an ANCHORED top-level
# pattern (a /lib elsewhere — e.g. a session bucket's own lib/ folder — is
# not the mirror and must still move); the shelf's own top-level entries
# are UUID session buckets, so /lib at the root is the mirror and nothing
# else. Excluded files are not deleted by --delete without
# --delete-excluded.
if [[ "$MIRROR" == "1" ]]; then
  [[ -s "$WORK/keep-files.txt" ]] && pull_rsync "$SHELF/" "$DEST" "$KEEP_FF" --exclude /lib
else
  [[ -s "$WORK/keep-files.txt" ]] && pull_rsync "$SHELF/" "$DEST" "$KEEP_FF"
fi
# the library is always files-from (its pull set is the manifest complement)
[[ -s "$WORK/lib-files.txt" ]] && pull_rsync "$LIB_AREA/" "$DEST/lib" "$WORK/lib-files.txt"
# the library's mirror, pruned host-side (rsync cannot do it under
# files-from): a file under $DEST/lib that is not in the pull set is gone
# from the mirror — a UI delete, or a file the first no-manifest pull took
# in whole-library form that the manifest now accounts for to the repo.
# --mirror only, as the shelf's prune: without it a stale file stays.
if [[ "$MIRROR" == "1" ]] && [[ -d "$DEST/lib" ]]; then
  python3 - "$DEST/lib" "$WORK/lib-files.txt" <<'PY'
import os, sys
root, list_path = sys.argv[1], sys.argv[2]
keep = {line.strip() for line in open(list_path) if line.strip()}
pruned = 0
for dirpath, _dirs, names in os.walk(root, topdown=False):
    for n in names:
        p = os.path.join(dirpath, n)
        if not os.path.isfile(p):
            continue  # a dir is the rmdir's job; anything odd is left alone
        if os.path.relpath(p, root) not in keep:
            os.unlink(p)
            pruned += 1
    if dirpath != root:
        try:
            os.rmdir(dirpath)  # empties only: a prune, never a nuke
        except OSError:
            pass
if pruned:
    print(f"pull: mirrored {root}/ — removed {pruned} file(s) the guest no longer holds")
PY
fi
echo "pull: OK — the host copy is at $DEST (the shelf at its root, the library's UI saves under $DEST/lib/)"
