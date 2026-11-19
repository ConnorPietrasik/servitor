#!/usr/bin/env bash
# Servitor update — the single day-2 command (portcullis parity).
#
# One command takes the guest to "current within the operator's slots",
# verified. The update FLOATS — it does not ask, and a current world is a
# result of the update, not a state the operator opts into:
#
#   1. RESOLVE    pi + extension: the .env line is an EXACT version (X.Y.Z)
#                 and a FLOOR — anything else fails the preflight (a range
#                 could never round-trip a deploy: installed == env is
#                 doctor-asserted). The target is the newest npm release in
#                 the same MAJOR (the 0.x extension floats across minors
#                 too — stable across them in practice; a real 1.0.0 is a
#                 major and gets reported, not applied). A newer
#                 out-of-major release is REPORTED (MAJOR AVAILABLE) and
#                 never applied — accepting one is editing the .env line
#                 to that major's release.
#   2. PRECHECK   the deployed doctor must be green before anything moves
#                 (a pre-existing failure would auto-revert the whole
#                 update). Skipped only on a world-current run — an
#                 age-out-only rebuild still pays it (and the gate below):
#                 both fail fast BEFORE the ~40-min build, and the gate
#                 re-certifies the deployed versions before the rebuild
#                 reinstalls them into the new image.
#   3. GATE       the conformance suite runs HOST-SIDE against the
#                 resolved versions (the guest is untouched until it
#                 passes), then the .env lines are floated to the targets
#                 — bump before apply, portcullis order: build.sh reads
#                 the .env for the pi install, and the sync's key slice
#                 carries the values into the guest's 0600 env file (the
#                 guest doctor asserts installed == env).
#   4. PROVISION  only what moved:
#                 pi or stale userland => image rebuild (the rebuild
#                 re-resolves the Arch packages — kernel/node/python float
#                 with it, recorded in the manifest) + image swap (clean
#                 ACPI shutdown, domain retarget, start: explicit
#                 downtime, loudly reported). Retention keeps exactly the
#                 new image + the previous one (the rollback target) — the
#                 build prunes everything older (files AND manifest
#                 entries; an image triple is multi-GB and the freshness
#                 floor makes rebuilds routine)
#                 extension => the sync's host stage
#   5. DEPLOY     the sync's swap-based path (never blocks a run — an
#                 active run gets the update-waiting flag and the tick
#                 applies at the first idle moment).
#   6. VERIFY     doctor. Doctor red => AUTOMATIC ROLLBACK (image, the
#                 manifest's active and the .env back to what is actually
#                 running — always; the shape back to the retained
#                 previous TOO, but only when THIS run's sync deployed it:
#                 a sync that failed before deploying left the shape
#                 untouched, and reverting it would roll an EARLIER
#                 deploy's change back a generation), re-doctor, report.
#
# No failure path leaves the world ahead of what is running: a BUILD
# failure restores the .env to the pre-run selections (it was floated
# before the build — a floated .env over an unchanged guest would leak
# into the guest's env file on the next servitor-sync: installed !=
# env, doctor red), and a SWAP failure additionally restores the
# manifest's active to the still-running image (the freshness floor
# measures the running image's age). Re-run make update either way: a
# built-but-not-swapped image is REUSED (the swap is retried — a rebuild
# would just re-convert the same deterministic name over the finished
# image, re-paying the ~40-min build), never rebuilt in place.
#
# A world-current run (no in-major release newer than the deployed one, the
# image within the freshness floor, the shape unchanged) touches nothing,
# restarts nothing, and reports NO-OP: the update ran and had nothing to
# change.
#
# Runs as root only (the gate's venv, the image build, the swap). Knobs —
# precedence: a non-empty .env line (a settled choice) > the caller's
# environment (the portcullis update tail inherits UPDATE_DOCTOR) > the
# default below (an empty .env line is "unset"):
#   UPDATE_DOCTOR=1         precheck + verify doctor (0 = skip both)
#   IMAGE_MAX_AGE_DAYS=7    rebuild the image when its manifest built_at is
#                           older than this — the guest userland (kernel
#                           included) is no older than the floor after an
#                           update. 0 = rebuild as soon as the image is
#                           older than TODAY (a same-day image is age 0 —
#                           within the floor; and a rebuild always carries
#                           a fresh date-stamped name, so it can never
#                           overwrite the running image's file).
#   SHUTDOWN_TIMEOUT=240    ACPI shutdown budget for the image swap (the
#                           app's abort-then-drain is up to 120 s)
#
# Usage: sudo deploy/update.sh [--env .env] [--ssh root@<vm-edge-ip>] [--deploy-key K]
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="$REPO/.env"
SSH_TARGET=""
# ${HOME:-/root}: robust under any env (set -u would die on a bare $HOME);
# /root/.srv is the canonical root-side home, and the flag overrides anyway.
DEPLOY_KEY="${SERVITOR_DEPLOY_KEY:-${HOME:-/root}/.srv/deploy_key}"
IMAGES_DIR="$REPO/deploy/guest/images"
MANIFEST="$IMAGES_DIR/manifest.json"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --env) ENV_FILE="$2"; shift 2 ;;
    --ssh) SSH_TARGET="$2"; shift 2 ;;
    --deploy-key) DEPLOY_KEY="$2"; shift 2 ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
done

[[ "$(id -u)" == "0" ]] || { echo "update runs as root (sudo make update): the gate's venv, the image build and the swap need it" >&2; exit 1; }
[[ -f "$ENV_FILE" ]] || { echo "env file not found: $ENV_FILE" >&2; exit 1; }
# update-run knobs: precedence is a NON-EMPTY .env line (a settled choice)
# > the caller's environment (the portcullis update tail passes
# UPDATE_DOCTOR via sudo) > the default below. The caller's values are
# captured BEFORE the source: an empty .env line means "unset here", and
# the source's empty assignment must not erase an explicit caller value
# (without the capture the re-assert would default it and, e.g., the
# host's UPDATE_DOCTOR=0 would be lost — the doctors would run anyway).
IN_UPDATE_DOCTOR="${UPDATE_DOCTOR:-}"
IN_IMAGE_MAX_AGE_DAYS="${IMAGE_MAX_AGE_DAYS:-}"
IN_SHUTDOWN_TIMEOUT="${SHUTDOWN_TIMEOUT:-}"
set -a; source <(grep -E '^[A-Za-z_][A-Za-z0-9_]*=' "$ENV_FILE"); set +a
# The defaults are re-asserted AFTER the source (${VAR:-d} catches a
# set-BUT-EMPTY line, not just an unset one — the caller's value sits
# behind the .env's) — without it the failures are SILENT, in the wrong
# directions: an empty UPDATE_DOCTOR is not "1" (the doctor is skipped),
# an empty IMAGE_MAX_AGE_DAYS evaluates to 0 in the age comparison
# (rebuild EVERY run), an empty SHUTDOWN_TIMEOUT to a 0 s ACPI budget
# (force-destroy on sight).
UPDATE_DOCTOR="${UPDATE_DOCTOR:-${IN_UPDATE_DOCTOR:-1}}"
IMAGE_MAX_AGE_DAYS="${IMAGE_MAX_AGE_DAYS:-${IN_IMAGE_MAX_AGE_DAYS:-7}}"
SHUTDOWN_TIMEOUT="${SHUTDOWN_TIMEOUT:-${IN_SHUTDOWN_TIMEOUT:-240}}"
: "${SERVITOR_PI_VERSION:?SERVITOR_PI_VERSION missing}"
: "${SERVITOR_PI_WEB_ACCESS_VERSION:=0.48.0}"
# the .env version lines are FLOORS: exact versions (X.Y.Z), never ranges —
# a range ("^1.5.0", "1.5.x", "latest") would hit the float rule's
# unparseable fail-safe (NO MOVEMENT), but its literal string still differs
# from what is deployed, so the run would gate, float the .env line to the
# range, and fail red at the guest's installed==env assert (a full rollback
# cycle for a typo-class mistake). The preflight turns that into a one-line
# error before anything moves (the rule itself is unit-tested:
# test/unit/test_update_resolve.py).
if ! python3 "$REPO/deploy/resolve_target.py" --exact "$SERVITOR_PI_VERSION" \
   || ! python3 "$REPO/deploy/resolve_target.py" --exact "$SERVITOR_PI_WEB_ACCESS_VERSION"; then
  echo "SERVITOR_PI_VERSION / SERVITOR_PI_WEB_ACCESS_VERSION must each be an exact version (X.Y.Z) — the line is a FLOOR the update floats in-major; a range, tag or prerelease is not accepted (pin the exact release; accepting a new major = editing the line to that major's release)" >&2
  exit 1
fi
# the PRE-FLOAT lines, captured before anything mutates the .env: the
# automatic rollback restores the .env (and the guest env file) to
# exactly these — what the world declared before this run, not a guest
# probe that can come back empty on a degraded guest.
PI_FLOOR_ORIG="$SERVITOR_PI_VERSION"
EXT_FLOOR_ORIG="$SERVITOR_PI_WEB_ACCESS_VERSION"
: "${SERVITOR_VM_IP:?SERVITOR_VM_IP missing in $ENV_FILE (the guest edge address)}"
: "${SERVITOR_PORT:=8787}"
SSH_TARGET="${SSH_TARGET:-root@${SERVITOR_VM_IP}}"
[[ -f "$DEPLOY_KEY" ]] || { echo "deploy key not found: $DEPLOY_KEY" >&2; exit 1; }
[[ -x "$REPO/.venv/bin/python" ]] || { echo "host checkout venv missing — sudo make venv (+ the slot1 user) per INSTALL.md §10: the update gate runs in it" >&2; exit 1; }
[[ -f "$MANIFEST" ]] || { echo "no image manifest at $MANIFEST — the first deploy is INSTALL.md (image + domain + first sync), not this command (sudo make guest builds the image and writes the manifest)" >&2; exit 1; }

# one update at a time (a manual run and a future timer would race the swap)
exec 9>/var/lock/servitor-update.lock
flock -n 9 || { echo "another servitor update is already running (lock: /var/lock/servitor-update.lock)" >&2; exit 1; }

SSH=(ssh -i "$DEPLOY_KEY" -o BatchMode=yes -o StrictHostKeyChecking=accept-new -o ConnectTimeout=10 "$SSH_TARGET")
run_guest() { "${SSH[@]}" "$@"; }

WORK="$(mktemp -d /tmp/servitor-update.XXXXXX)"
trap 'rm -rf "$WORK"' EXIT

jsonget() {
  python3 - "$MANIFEST" "$1" 2>/dev/null <<'EOF' || true
import json, sys
try:
    d = json.load(open(sys.argv[1]))
    for k in sys.argv[2].split("."):
        d = d[k]
    print(d)
except Exception:
    pass
EOF
}

# per-image field: the image NAME contains dots (servitor-pi1.5.2-<date>.qcow2),
# so it must arrive as a SINGLE argument — a dotted path like
# "images.<name>.built_at" cannot encode it (jsonget's split would break the
# name apart and the lookup would silently return nothing => age 0 => the
# freshness floor would never fire).
image_field() { # $1 = image name  $2 = field (e.g. built_at)
  python3 - "$MANIFEST" "$1" "$2" 2>/dev/null <<'EOF' || true
import json, sys
try:
    print(json.load(open(sys.argv[1]))["images"][sys.argv[2]][sys.argv[3]])
except Exception:
    pass
EOF
}

# package lookup by name: EXACT name first, then the first <name>-* variant.
# The kernel is the package named exactly `linux` — a bare startswith("linux-")
# would report `linux-api-headers` as the kernel, and list order (sorted) is
# no contract to rely on for the exact name to sort first.
manifest_pkg() {
  python3 - "$MANIFEST" "$1" "$2" 2>/dev/null <<'EOF' || true
import json, sys
try:
    pkgs = json.load(open(sys.argv[1])).get("images", {}).get(sys.argv[2], {}).get("packages", [])
    want = sys.argv[3]
    if want in pkgs:
        print(want)
    else:
        for p in pkgs:
            if p.startswith(want + "-"):
                print(p)
                break
except Exception:
    pass
EOF
}

# newest npm stable release in the declared version's MAJOR (the declaration
# is a floor, never a ceiling) — same major for everything: 1.5.2 -> 1.9.7
# (2.0.0 out); 0.48.0 -> 0.49.0 in (the 0.x extension floats across minors —
# stable across them in practice; a 1.0.0 would be reported, not applied).
# prints "<target> <latest-overall>"; target == latest-overall exactly when
# the newest release is in slot (or nothing exists). The rule itself is the
# unit-tested pure function deploy/resolve_target.py, and the npm JSON
# arrives at it via ARGV, never stdin: `cmd | python3 - <<EOF` gives the
# heredoc the stdin fight (stdin is the program, the pipe is discarded) and
# the versions list would never arrive — a silent degrade to exact-pin.
resolve_target() { # $1=pkg  $2=declared
  local json
  json="$(npm view "$1" versions --json 2>/dev/null || true)"
  python3 "$REPO/deploy/resolve_target.py" "$2" "$json"
}

# rewrite one .env line in place (mode kept, ATOMIC: temp + rename in the
# same dir — a crash mid-write must not corrupt the .env, the version
# source of truth; the same mkstemp+replace shape as the guest-side env
# re-injection in rollback_all). The file is never sourced as shell by
# this script's callers, only re-read.
float_env_line() { # $1=key $2=value
  python3 - "$ENV_FILE" "$1" "$2" <<'EOF'
import os, sys, tempfile
path, key, val = sys.argv[1:4]
lines = open(path).read().splitlines()
out, done = [], False
for l in lines:
    if not done and l.startswith(key + "="):
        out.append(key + "=" + val)
        done = True
    else:
        out.append(l)
if not done:
    out.append(key + "=" + val)
fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path) or ".")
try:
    with os.fdopen(fd, "w") as fh:
        fh.write("\n".join(out) + "\n")
    os.chmod(tmp, os.stat(path).st_mode & 0o777)
    os.replace(tmp, path)
except BaseException:
    try:
        os.unlink(tmp)
    except OSError:
        pass
    raise
EOF
}

# .env back to the PRE-RUN selections (float_env_line is atomic; a
# restore that changes nothing rewrites the identical lines). Used by
# every failure path that exits AFTER the .env was floated: a floated
# .env over a guest still on the old versions is ahead of the world —
# the next servitor-sync would push the floated line into the guest's
# 0600 env file (installed != env -> the doctor's versions.* red, and
# the next update's precheck would refuse to move). GUARDED: callers
# run in `||` contexts where set -e is suppressed — an unguarded
# failure here would be reported as a clean failure with the .env
# still floated.
restore_env_floors() {
  float_env_line SERVITOR_PI_VERSION "$PI_FLOOR_ORIG" \
    || { echo "    WARNING: could not restore SERVITOR_PI_VERSION in $ENV_FILE — re-set it to $PI_FLOOR_ORIG by hand (until then a servitor-sync pushes the floated line into the guest — installed != env, doctor red)" >&2; return 1; }
  float_env_line SERVITOR_PI_WEB_ACCESS_VERSION "$EXT_FLOOR_ORIG" \
    || { echo "    WARNING: could not restore SERVITOR_PI_WEB_ACCESS_VERSION in $ENV_FILE — re-set it to $EXT_FLOOR_ORIG by hand (until then a servitor-sync pushes the floated line into the guest — installed != env, doctor red)" >&2; return 1; }
  return 0
}

# the manifest's 'active' is the single source of "which image runs"
# (update.sh's OLD_IMAGE/age, the guest suite's boot image, the
# build's retention all read it) — every path that changes the running
# image re-points it ATOMICALLY (temp + rename): $1 = the new active,
# $2 = its retained_previous (the pair that is actually on disk).
# Without this after a swap failure the next run would read the
# NOT-running image's built_at (the freshness floor would go blind to
# the running image's age) and retention would keep the wrong pair.
manifest_set_active() { # $1 = new active image name  $2 = its retained_previous
  python3 - "$MANIFEST" "$1" "$2" <<'EOF'
import json, os, sys, tempfile
path, active, prev = sys.argv[1:4]
m = json.load(open(path))
if active in m["images"] and prev in m["images"]:
    m["images"][active]["retained_previous"] = m["images"][prev]
m["active"] = active
fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path) or ".")
with os.fdopen(fd, "w") as fh:
    json.dump(m, fh, indent=2, sort_keys=True)
os.replace(tmp, path)
EOF
}

# image swap: retarget the LIVE domain definition (the operator's sizing is
# preserved — the template is only the first-deploy input) to the named
# image, across a clean ACPI shutdown. The old image file is untouched on
# disk — it is the rollback target. The manifest name IS the file basename
# (including .qcow2 — build.sh writes the boot files as <stem>.vmlinuz /
# <stem>.initramfs.img alongside it).
swap_image() { # $1 = image name (manifest name, includes .qcow2)
  # This function is only called in conditional contexts (if ! swap_image …),
  # where set -e is suppressed for the WHOLE function body — every state
  # change below is therefore guarded explicitly. Invariant: the domain is
  # never left UNDEFINED (an undefined VM is unbootable and invisible to
  # virsh start and the panel's servitor-status) — the pre-undefine dump is
  # the restore source if the re-define fails.
  local name="$1" stem="${1%.qcow2}"
  local qcow2="$IMAGES_DIR/$name" kern="$IMAGES_DIR/${stem}.vmlinuz" initrd="$IMAGES_DIR/${stem}.initramfs.img"
  if [[ ! -f "$qcow2" || ! -f "$kern" || ! -f "$initrd" ]]; then
    echo "swap: image files missing for $name" >&2
    return 1
  fi
  # capture the autostart flag NOW: the virsh undefine below drops it (the
  # template does not set it — an operator may have; re-applied after define).
  # The query is `virsh dominfo`: `virsh autostart` has no --query option
  # (only --disable/--once), so a "query" through autostart always errored
  # and the flag was silently dropped on every swap.
  local autostart=0
  if virsh dominfo servitor 2>/dev/null | grep -q '^Autostart:.*enable'; then autostart=1; fi
  local state0
  state0="$(virsh domstate servitor 2>/dev/null || echo unknown)"
  case "$state0" in
    "shut off") : ;;
    unknown|"")
      echo "swap: no 'servitor' domain (virsh domstate: ${state0:-empty}) — nothing to swap (define it first: RUNBOOK → Servitor)" >&2
      return 1
      ;;
    crashed)
      echo "    WARNING: the VM is crashed — destroy before the swap" >&2
      virsh destroy servitor || { echo "swap: could not destroy the crashed VM — manual attention (virsh console servitor)" >&2; return 1; }
      ;;
    *)
      echo "    IMAGE SWAP: shutting down the VM (downtime; the app's abort-then-drain is up to 120 s, budget ${SHUTDOWN_TIMEOUT} s)"
      if ! virsh shutdown servitor; then
        # the REQUEST was refused (set -e is suppressed in this function —
        # an unguarded call would burn the full budget below with no trace
        # of why). Still wait: a refusal is not proof the guest will not
        # finish (a shutdown already in flight, a spurious libvirt error) —
        # the loop gives it the full ACPI budget, then the same force-destroy.
        echo "    WARNING: virsh shutdown was refused (its error is above) — waiting out the ACPI budget anyway, then forcing" >&2
      fi
      local t=0 state=""
      while (( t < SHUTDOWN_TIMEOUT )); do
        state="$(virsh domstate servitor 2>/dev/null || true)"
        if [[ "$state" == "shut off" ]]; then break; fi
        sleep 5; t=$((t + 5))
      done
      if [[ "$state" != "shut off" ]]; then
        echo "    WARNING: ACPI shutdown did not finish in ${SHUTDOWN_TIMEOUT} s — forcing (virsh destroy)" >&2
        virsh destroy servitor || { echo "swap: could not destroy the hung VM — manual attention (virsh console servitor)" >&2; return 1; }
      fi
      ;;
  esac
  # dump the XML only once the domain is "shut off": a running-domain dump
  # carries runtime-only state that should not ride into the redefinition.
  # The dump doubles as the restore source for the define below.
  virsh dumpxml servitor > "$WORK/domain.xml" \
    || { echo "swap: virsh dumpxml failed — the domain is untouched" >&2; return 1; }
  python3 - "$WORK/domain.xml" "$WORK/domain.new.xml" "$qcow2" "$kern" "$initrd" <<'EOF' \
    || { echo "swap: the domain XML rewrite failed (unexpected layout — missing os/kernel/initrd, or not exactly one qcow2 disk) — the domain is untouched" >&2; return 1; }
import sys, xml.etree.ElementTree as ET
src, dst, qcow2, kern, initrd = sys.argv[1:6]
# Match by LOCAL name, not by path: the domain root may or may not carry
# a default xmlns (libvirt keeps what the define XML declared — the repo's
# template is namespace-free, but an operator-defined domain can carry
# one), and ElementTree's "./os/kernel" lookup is namespace-EXACT: with a
# default xmlns it returns None and the rewrite would fail on a perfectly
# good domain. (Serialization re-declares any namespaces it finds, in
# whatever prefix form — virsh define accepts both.)
def local(tag):
    return tag.rsplit("}", 1)[-1]
t = ET.parse(src)
r = t.getroot()
os_el = next((c for c in r if local(c.tag) == "os"), None)
kern_el = next((c for c in os_el if local(c.tag) == "kernel"), None) if os_el is not None else None
initrd_el = next((c for c in os_el if local(c.tag) == "initrd"), None) if os_el is not None else None
devices = next((c for c in r if local(c.tag) == "devices"), None)
disks = [c for c in devices if local(c.tag) == "disk"] if devices is not None else []
if kern_el is None or initrd_el is None:
    sys.exit("missing os/kernel or os/initrd in the domain definition (this image boots via kernel/initrd, no bootloader)")
n = 0
for d in disks:
    if d.get("device") != "disk":
        continue
    s = next((c for c in d if local(c.tag) == "source"), None)
    if s is not None and (s.get("file") or "").endswith(".qcow2"):
        s.set("file", qcow2)
        n += 1
if n != 1:
    sys.exit(f"expected exactly one qcow2 root disk in the domain definition, found {n}")
kern_el.text = kern
initrd_el.text = initrd
t.write(dst, xml_declaration=True)
EOF
  # undefine only once the rewrite is verified: an undefined domain is
  # unbootable and invisible until re-defined.
  virsh undefine servitor \
    || { echo "swap: virsh undefine failed — the old definition is still in place" >&2; return 1; }
  virsh define "$WORK/domain.new.xml" > /dev/null \
    || { echo "swap: the re-definition failed — restoring the previous definition" >&2
        if virsh define "$WORK/domain.xml" > /dev/null 2>&1; then
          echo "    previous definition restored — the VM is shut off on the pre-swap image (re-run make update to retry, or virsh start servitor to run it as-is)" >&2
        else
          # the EXIT trap deletes $WORK — leave the dump at a stable path
          # (the latest failed swap's definition) for manual recovery.
          cp -f "$WORK/domain.xml" "$IMAGES_DIR/servitor-domain-restore.xml" 2>/dev/null || true
          echo "    RESTORE FAILED — the previous domain XML is at $IMAGES_DIR/servitor-domain-restore.xml (virsh define it)" >&2
        fi
        return 1; }
  if (( autostart )); then
    # warn, don't fail: the swap itself is done (the VM is about to start);
    # a lost flag is a boot-behavior gap, not a swap failure — but it must
    # not be lost SILENTLY (the unguarded call was how the capture above
    # used to be defeated).
    virsh autostart servitor \
      || echo "    WARNING: could not re-apply the autostart flag — run: virsh autostart servitor (the VM will not start on the next host boot until then)" >&2
  fi
  virsh start servitor \
    || { echo "swap: virsh start failed — the domain is defined on $name (check: virsh console servitor, journalctl -u libvirtd; re-run make update retries the swap)" >&2; return 1; }
  # every rebuilt image bakes FRESH ssh host keys (build.sh runs
  # ssh-keygen -A in the chroot), so the swap ROTATES the guest's host key —
  # and accept-new accepts only ABSENT entries, never CHANGED ones. Without
  # dropping the recorded key first, the wait loop below (and every later
  # ssh to the VM: sync, doctor, the next run's probes) would fail
  # host-key verification against a perfectly healthy guest. Dropping it
  # here — on every swap — covers both directions: the swap forward AND
  # the rollback swap-back to the old image's keys.
  ssh-keygen -R "$SERVITOR_VM_IP" >/dev/null 2>&1 || true
  echo "    waiting for the VM (ssh, then /healthz — up to 300 s each)…"
  local ok=0 t2=0
  while (( t2 < 300 )); do
    if run_guest "true" 2>/dev/null; then ok=1; break; fi
    sleep 5; t2=$((t2 + 5))
  done
  if [[ "$ok" != "1" ]]; then
    echo "swap: the VM did not come back over ssh within 300 s (check: virsh console servitor)" >&2
    return 1
  fi
  ok=0; t2=0
  while (( t2 < 300 )); do
    if curl -sf -m 5 "http://${SERVITOR_VM_IP}:${SERVITOR_PORT}/healthz" > /dev/null 2>&1; then ok=1; break; fi
    sleep 5; t2=$((t2 + 5))
  done
  if [[ "$ok" != "1" ]]; then
    echo "swap: /healthz did not answer within 300 s (check: virsh console servitor, journalctl -u servitor)" >&2
    return 1
  fi
  echo "    swapped to $name — the VM is up"
}

# --------------------------------------------------------------------------
echo "==> [1/7] state + resolve (.env floor -> newest in major)"
GUEST_PI="$(run_guest "node -e \"try{console.log(require('/usr/lib/node_modules/@earendil-works/pi-coding-agent/package.json').version)}catch(e){console.log('')}\"" || true)"
GUEST_EXT="$(run_guest "node -e \"try{console.log(require('/opt/pi-ext/pi-web-access/package.json').version)}catch(e){console.log('')}\"" || true)"
OLD_IMAGE="$(jsonget active)"
[[ -n "$OLD_IMAGE" ]] || { echo "manifest has no active image — the first deploy is INSTALL.md, not this command" >&2; exit 1; }
OLD_KERNEL="$(manifest_pkg "$OLD_IMAGE" linux)"
IMAGE_AGE_DAYS="$(python3 - "$(image_field "$OLD_IMAGE" built_at)" <<'EOF'
import datetime, sys
try:
    t = datetime.datetime.strptime(sys.argv[1], "%Y-%m-%dT%H:%M:%SZ")
    now = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
    print(max(0, int((now - t).total_seconds() // 86400)))
except Exception:
    print(0)
EOF
)"

PI_LINE="$(resolve_target "@earendil-works/pi-coding-agent" "$SERVITOR_PI_VERSION")"
PI_TARGET="${PI_LINE%% *}"; PI_LATEST="${PI_LINE##* }"
if [[ -n "$SERVITOR_PI_WEB_ACCESS_VERSION" ]]; then
  EXT_LINE="$(resolve_target "pi-web-access" "$SERVITOR_PI_WEB_ACCESS_VERSION")"
  EXT_TARGET="${EXT_LINE%% *}"; EXT_LATEST="${EXT_LINE##* }"
else
  EXT_TARGET=""; EXT_LATEST=""
fi

MAJOR_NOTES=()
if [[ "$PI_LATEST" != "$PI_TARGET" ]]; then
  MAJOR_NOTES+=("pi: $PI_LATEST available (out of major for $SERVITOR_PI_VERSION) — NOT applied; set SERVITOR_PI_VERSION to accept")
fi
if [[ -n "$EXT_TARGET" && "$EXT_LATEST" != "$EXT_TARGET" ]]; then
  MAJOR_NOTES+=("pi-web-access: $EXT_LATEST available (out of major for $SERVITOR_PI_WEB_ACCESS_VERSION) — NOT applied; set SERVITOR_PI_WEB_ACCESS_VERSION to accept")
fi

PI_MOVE=0;  if [[ "$PI_TARGET" != "$GUEST_PI" ]]; then PI_MOVE=1; fi
EXT_MOVE=0; if [[ -n "$EXT_TARGET" && "$EXT_TARGET" != "$GUEST_EXT" ]]; then EXT_MOVE=1; fi
AGE_OUT=0;  if (( IMAGE_AGE_DAYS > IMAGE_MAX_AGE_DAYS )); then AGE_OUT=1; fi
REBUILD=0;  if (( PI_MOVE || AGE_OUT )); then REBUILD=1; fi

echo "    pi:    ${GUEST_PI:-none} -> $PI_TARGET  (newest overall: $PI_LATEST)"
echo "    ext:   ${GUEST_EXT:-none} -> ${EXT_TARGET:-none}  (newest overall: ${EXT_LATEST:-none})"
if (( REBUILD )); then
  echo "    image: $OLD_IMAGE (built ${IMAGE_AGE_DAYS}d ago; freshness floor ${IMAGE_MAX_AGE_DAYS}d) -> REBUILD"
else
  echo "    image: $OLD_IMAGE (built ${IMAGE_AGE_DAYS}d ago; freshness floor ${IMAGE_MAX_AGE_DAYS}d) -> keep"
fi

# --------------------------------------------------------------------------
if (( REBUILD || EXT_MOVE )) && [[ "$UPDATE_DOCTOR" == "1" ]]; then
  echo "==> [2/7] precondition: deployed doctor must be green before anything moves"
  if ! bash "$REPO/deploy/doctor.sh" --env "$ENV_FILE" --ssh "$SSH_TARGET" --deploy-key "$DEPLOY_KEY"; then
    echo "doctor is not green — the failures above are pre-existing (nothing was touched); fix them, then re-run" >&2
    exit 1
  fi
else
  echo "==> [2/7] precondition doctor (skipped)"
  if (( ! REBUILD && ! EXT_MOVE )); then
    echo "    nothing versioned moves — a world-current run spends no live model run on a precheck"
  else
    echo "    UPDATE_DOCTOR=0"
  fi
fi

# --------------------------------------------------------------------------
if (( PI_MOVE || EXT_MOVE || AGE_OUT )); then
  echo "==> [3/7] gate: conformance suite against pi=$PI_TARGET ext=${EXT_TARGET:-none} (host-side; the guest is untouched until it passes)"
  GATE_LOG="$WORK/gate.log"
  set +e
  (
    cd "$REPO"
    SERVITOR_PI_VERSION="$PI_TARGET" \
    SERVITOR_PI_WEB_ACCESS_VERSION="${EXT_TARGET}" \
    "$REPO/.venv/bin/python" -m pytest test/conformance -q
  ) > "$GATE_LOG" 2>&1
  GATE_RC=$?
  set -e
  tail -15 "$GATE_LOG"
  if [[ "$GATE_RC" -ne 0 ]]; then
    # the EXIT trap deletes $WORK (and with it the log this message points
    # at) — preserve the RED gate's full log at a stable path.
    GATE_KEEP="/tmp/servitor-gate-$(date -u +%Y%m%d-%H%M%S).log"
    cp -f "$GATE_LOG" "$GATE_KEEP"
    echo "GATE RED (rc=$GATE_RC) — nothing deployed (full log: $GATE_KEEP; the suite needs the slot1 user + the host venv — INSTALL.md §10)" >&2
    exit 1
  fi
  echo "    gate GREEN"
  # float the .env lines NOW (bump before apply — portcullis order): the
  # rebuild reads the .env for the pi install, and the sync's key slice
  # carries these into the guest's 0600 env file (installed == env is
  # doctor-asserted).
  if (( PI_MOVE )); then
    float_env_line SERVITOR_PI_VERSION "$PI_TARGET"
    echo "    .env: SERVITOR_PI_VERSION -> $PI_TARGET"
  fi
  if (( EXT_MOVE )); then
    float_env_line SERVITOR_PI_WEB_ACCESS_VERSION "$EXT_TARGET"
    echo "    .env: SERVITOR_PI_WEB_ACCESS_VERSION -> $EXT_TARGET"
  fi
else
  echo "==> [3/7] gate (skipped: no versioned deploy — shape changes gate via the doctor)"
fi

# --------------------------------------------------------------------------
SWAPPED=0
NEW_IMAGE="$OLD_IMAGE"
# the rebuild's name is pi-version + UTC day: a same-day age-out rebuild
# is impossible to trigger (built-today means age 0, within any floor >= 0)
# and any rebuild that does run carries a fresh name — the running
# image's file is never overwritten in place.
if (( REBUILD )); then
  # retry path: a candidate from an earlier run with THIS run's exact
  # deterministic name (build.sh: servitor-pi<pi>-<UTC day>). The
  # manifest's active is the RUNNING image (the swap-failure exit
  # restores it), so a same-named, manifest-listed image is a
  # built-but-not-running candidate, never the running image's file —
  # reuse it: the swap is what the earlier run left unfinished (a rebuild
  # would just re-convert the same deterministic name — qemu-img convert
  # overwrites the existing target — re-paying the ~40-min build for an
  # image that already exists).
  # The manifest listing is required — a kill mid-convert leaves a
  # partial file at the final name with NO manifest entry (the build
  # writes the manifest only after the convert).
  CANDIDATE=0
  cand="servitor-pi${PI_TARGET}-$(date -u +%Y%m%d).qcow2"
  if [[ "$cand" != "$OLD_IMAGE" && -f "$IMAGES_DIR/$cand" \
       && -n "$(image_field "$cand" built_at)" \
       && -f "$IMAGES_DIR/${cand%.qcow2}.vmlinuz" \
       && -f "$IMAGES_DIR/${cand%.qcow2}.initramfs.img" ]]; then
    NEW_IMAGE="$cand"
    CANDIDATE=1
    echo "    reusing the previously built candidate $NEW_IMAGE (a swap failed after it built) — retrying the swap without a rebuild"
  else
    echo "==> [4/7] provision: image rebuild (the Arch packages re-resolve — kernel/node/python float to the current repo state, recorded in the manifest)"
    unshare -m bash "$REPO/deploy/guest/build.sh" --env "$ENV_FILE" --deploy-key "${DEPLOY_KEY}.pub" \
      || { echo "image build failed — nothing deployed (the VM is untouched; the .env is restored to the pre-run selections)" >&2
           restore_env_floors
           exit 1; }
    NEW_IMAGE="$(jsonget active)"
    if [[ -z "$NEW_IMAGE" || "$NEW_IMAGE" == "$OLD_IMAGE" ]]; then
      echo "build did not set the manifest's active to a new image (unexpected — the manifest must name the build's output) — aborting" >&2
      restore_env_floors
      exit 1
    fi
    echo "    new image: $NEW_IMAGE (previous retained: $OLD_IMAGE — the rollback target)"
  fi
  if ! swap_image "$NEW_IMAGE"; then
    # the manifest + .env must describe the world that IS running
    # again: active back to the old image (the freshness floor measures
    # the RUNNING image's age — see manifest_set_active) and the .env
    # back to the pre-run selections (see restore_env_floors). Then a
    # re-run reuses this very image and retries the swap.
    manifest_set_active "$OLD_IMAGE" "$NEW_IMAGE" \
      || echo "    WARNING: could not restore the manifest's active to $OLD_IMAGE — fix by hand (set 'active' to $OLD_IMAGE in $MANIFEST), or the next update measures the wrong image" >&2
    restore_env_floors
    echo "IMAGE SWAP FAILED — the old image is intact at $IMAGES_DIR/$OLD_IMAGE (the manifest's active + the .env are restored to it). Re-run make update: it reuses the built image and retries the swap; if the VM will not come up: virsh console servitor (RUNBOOK → Servitor)." >&2
    exit 1
  fi
  if (( CANDIDATE )); then
    # reuse path: the build that made the candidate pointed the manifest
    # at it once, and that run's swap-failure exit restored it —
    # re-point it at the image that now runs.
    manifest_set_active "$NEW_IMAGE" "$OLD_IMAGE" \
      || echo "    WARNING: could not set the manifest's active to $NEW_IMAGE — fix by hand (set 'active' to $NEW_IMAGE in $MANIFEST), or the next update measures the wrong image" >&2
  fi
  SWAPPED=1
else
  echo "==> [4/7] provision (skipped: no pi move and the image is within the freshness floor)"
fi

# --------------------------------------------------------------------------
echo "==> [5/7] deploy the shape (app/config/lib/extension/key slice — the sync's swap-based path)"
SYNC_LOG="$WORK/sync.log"
set +e
bash "$REPO/deploy/sync.sh" --env "$ENV_FILE" --ssh "$SSH_TARGET" --deploy-key "$DEPLOY_KEY" 2>&1 | tee "$SYNC_LOG"
SYNC_RC=${PIPESTATUS[0]}
set -e
SYNC_NOOP=0; SYNC_GREEN=0; SYNC_WAITING=0; SYNC_DEPLOYED=0
if grep -q "sync: NO-OP" "$SYNC_LOG"; then SYNC_NOOP=1; fi
if grep -q "doctor GREEN" "$SYNC_LOG"; then SYNC_GREEN=1; fi
if grep -q "update-WAITING" "$SYNC_LOG"; then SYNC_WAITING=1; fi
# "THIS run's sync changed (or staged) the shape" — the ONLY case the
# rollback's shape half is legitimate (the retained previous artifacts
# hold an EARLIER deploy's shape, not this run's, whenever this run's
# sync deployed nothing).
if [[ "$SYNC_RC" -eq 3 ]] || grep -q "shape deployed" "$SYNC_LOG" || (( SYNC_WAITING )); then
  SYNC_DEPLOYED=1
fi

# sync rc contract: 0 = NO-OP / deployed + doctor green / update-waiting;
# 3 = deploy LANDED but doctor red; 1+ = pre-deploy failure: nothing to
# roll back when the image did NOT swap; when it did, the guest is a
# consistent new-image + unchanged-shape state — the IMAGE rolls back
# anyway (a healthy but not-updated VM is the safe uniform end state);
# the shape rolls back only if THIS run's sync deployed it (above).
ROLLBACK_NEEDED=0
if [[ "$SYNC_RC" -eq 3 ]]; then
  ROLLBACK_NEEDED=1
elif [[ "$SYNC_RC" -ne 0 ]]; then
  if [[ "$SWAPPED" == "1" ]]; then
    ROLLBACK_NEEDED=1
  else
    echo "sync failed before deploy (rc=$SYNC_RC) — nothing to roll back on the guest" >&2
    # the .env lines may already be FLOATED (step 3 floats before the sync —
    # the extension-only move: no pi move, image within the floor): restore
    # the pre-run selections so no failure path leaves the world ahead of
    # what is running (a floated .env over an unchanged guest would leak
    # into the guest's env file on the next servitor-sync — installed !=
    # env, doctor red). A restore that changes nothing rewrites the
    # identical lines; a failed restore warns and exits non-zero (loud).
    restore_env_floors
    exit 1
  fi
fi

# --------------------------------------------------------------------------
FINAL_OK=1
if [[ "$ROLLBACK_NEEDED" == "0" && "$SWAPPED" == "1" && "$SYNC_GREEN" == "0" && "$SYNC_WAITING" == "0" && "$UPDATE_DOCTOR" == "1" ]]; then
  echo "==> [6/7] verify: doctor on the new image (the sync exited 0 without running one — after a pure swap it may have NO-OP'd: shape unchanged, nothing deployed, no doctor — the new image is certified here)"
  if ! bash "$REPO/deploy/doctor.sh" --env "$ENV_FILE" --ssh "$SSH_TARGET" --deploy-key "$DEPLOY_KEY"; then
    FINAL_OK=0
  fi
else
  echo "==> [6/7] verify (the doctor already ran on the deployed state, or nothing changed)"
fi

rollback_all() { # $1 = 1 when the image must go back too  $2 = 1 when THIS
  # run's sync deployed/staged a shape change — the only case the
  # guest-side shape rollback is legitimate (the retained previous
  # artifacts hold the PREVIOUS deploy's shape; reverting them when this
  # run's sync deployed nothing would roll an earlier deploy back a
  # generation and, via the re-stamp + floor re-injection below, leave
  # the doctor red on a healthy world)
  local img_back="$1" shape_back="${2:-0}"
  local what=""
  if [[ "$shape_back" == "1" ]]; then what="shape"; fi
  if [[ "$img_back" == "1" ]]; then what="${what:+$what + }image"; fi
  echo "update: AUTOMATIC ROLLBACK ($what)" >&2
  if [[ "$shape_back" == "1" ]]; then
  # guest-side shape rollback (the sync's retained previous artifacts).
  # single-quoted: every expansion happens on the GUEST.
  run_guest '
    set -e
    # previous extension (retained at /opt/pi-ext/.previous)
    if test -d /opt/pi-ext/.previous; then
      rm -rf /opt/pi-ext/pi-web-access
      mv /opt/pi-ext/.previous /opt/pi-ext/pi-web-access
    fi
    # re-stamp the registry to what is actually installed (the stamp is
    # doctor-asserted against the install; a new stamp over a restored
    # extension would keep the rollback red)
    if test -d /opt/pi-ext/pi-web-access; then
      # NO single quote may appear anywhere in this run_guest block: the
      # whole thing rides inside a host-side single-quoted string, where
      # any such character closes the host quote and the guest receives a
      # mangled script (silently). The JS and python string literals
      # therefore use \" — the backslash survives host-side
      # single-quoting literally, and the GUEST double-quoting turns it
      # back into a plain quote.
      V="$(node -e "try{console.log(require(\"/opt/pi-ext/pi-web-access/package.json\").version)}catch(e){console.log(\"\")}" || true)"
      if [ -n "$V" ]; then
        cd /opt/servitor/app && .venv/bin/python -c "
from app.registry import Registry
r = Registry(\"/var/lib/servitor/registry.db\")
r.set_version(\"pi_web_access\", \"$V\")
r.close()"
      fi
    fi
    # previous config version (recorded by the sync at swap time)
    PREV_CFG="$(cat /var/lib/servitor/config/.previous-active 2>/dev/null || true)"
    if [ -n "$PREV_CFG" ] && [ -d "/var/lib/servitor/config/$PREV_CFG" ]; then
      ln -s "$PREV_CFG" /var/lib/servitor/config/.active-tmp-rollback
      mv -T /var/lib/servitor/config/.active-tmp-rollback /var/lib/servitor/config/active
      echo "config rolled back to $PREV_CFG"
    else
      echo "WARN: no .previous-active marker — config pointer left as-is" >&2
    fi
    # previous app tree (retained by the sync, refreshed flat before every push)
    if test -d /opt/servitor/app.previous.app; then
      rm -rf /opt/servitor/app/app
      mv /opt/servitor/app.previous.app /opt/servitor/app/app
      if test -f /opt/servitor/app.previous.pyproject; then
        mv /opt/servitor/app.previous.pyproject /opt/servitor/app/pyproject.toml
      fi
      rm -f /opt/servitor/app.previous
    fi
    systemctl restart servitor || true
  ' || echo "    WARNING: the guest-side shape rollback hit an error (continuing; manual check needed)" >&2
  sleep 5
  else
    echo "    shape: left as-is (this run's sync deployed nothing — the retained previous artifacts hold an EARLIER deploy's shape, which is not this run's to roll back)" >&2
  fi
  if [[ "$img_back" == "1" ]]; then
    echo "    rolling the image back to $OLD_IMAGE (downtime)…" >&2
    swap_image "$OLD_IMAGE" || return 1
    # the manifest must describe the restored world: active = the image the
    # domain points at again (see manifest_set_active — the restored image's
    # retained_previous becomes the just-failed image: the pair that is
    # actually on disk).
    manifest_set_active "$OLD_IMAGE" "$NEW_IMAGE" \
      || { echo "    WARNING: could not restore the manifest's active image to $OLD_IMAGE — the next update would measure/keep the wrong image; fix by hand: set 'active' to $OLD_IMAGE in $MANIFEST" >&2; return 1; }
    # the shape-rollback re-stamp above ran on the NEW image: it stamped the
    # registry to the NEW image's installed extension. The registry lives on
    # the state disk, the extension dir on the root disk — and the swap-back
    # just restored the pre-run root disk (the old image FILE is the pre-run
    # live disk — the swap only retargets the domain, it never re-bakes — so
    # it carries the pre-run extension). Re-stamp to what is ACTUALLY
    # installed now. Without this, a run that moved the extension floor AND
    # the image leaves the stamp at the floated version over the pre-run
    # extension: versions.extension red on every doctor, the next update's
    # precheck refuses to move, and a bare servitor-sync NO-OPs (the shape
    # looks consistent — the stamp is not in its change detection).
    run_guest '
      if test -d /opt/pi-ext/pi-web-access; then
        V="$(node -e "try{console.log(require(\"/opt/pi-ext/pi-web-access/package.json\").version)}catch(e){console.log(\"\")}" || true)"
        if [ -n "$V" ]; then
          cd /opt/servitor/app && .venv/bin/python -c "
from app.registry import Registry
r = Registry(\"/var/lib/servitor/registry.db\")
r.set_version(\"pi_web_access\", \"$V\")
r.close()"
          echo "stamp: pi-web-access re-set to installed $V (the swap-back restored the pre-run extension)"
        fi
      fi
    ' || echo "    WARNING: could not re-stamp the extension registry after the swap-back — the doctor will read versions.extension red until the next sync that pushes the extension (manual check needed)" >&2
  fi
  # the guest's 0600 env file must describe the restored world again: the
  # failed sync's key slice wrote the FLOATED selections into it, and the
  # guest doctor asserts installed == env — a floated line over a
  # rolled-back guest stays red on every later run (and the next update's
  # precheck would refuse to move). The swap-back case already carries the
  # pre-update file from the old image; rewriting the same lines keeps one
  # uniform path. (Single-quoted heredoc on the guest side: the program is
  # literal, the values ride in as argv — the same stdin rule as
  # resolve_target above.)
  run_guest "
    cd /opt/servitor/app && .venv/bin/python - \"$PI_FLOOR_ORIG\" \"$EXT_FLOOR_ORIG\" <<'PYEOF'
import os, sys, tempfile
pi, ext = sys.argv[1], sys.argv[2]
path = '/etc/servitor/servitor.env'
out, seen = [], set()
for line in open(path).read().splitlines():
    key = line.split('=', 1)[0]
    if key == 'SERVITOR_PI_VERSION' and 'pi' not in seen:
        out.append('SERVITOR_PI_VERSION=' + pi); seen.add('pi')
    elif key == 'SERVITOR_PI_WEB_ACCESS_VERSION' and 'ext' not in seen:
        out.append('SERVITOR_PI_WEB_ACCESS_VERSION=' + ext); seen.add('ext')
    else:
        out.append(line)
fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path))
with os.fdopen(fd, 'w') as fh:
    fh.write('\n'.join(out) + '\n')
os.chmod(tmp, 0o600)
os.replace(tmp, path)
print('env: version lines re-floated to the restored world')
PYEOF" || { echo "    WARNING: could not re-inject the guest env version lines — servitor-sync will (until then the doctor reads versions.* red)" >&2; return 1; }
  # the .env must describe what is actually deployed again: restore the
  # pre-run lines (a floated .env over a rolled-back guest is red on every
  # later run — the guest doctor asserts installed == env). GUARDED: this
  # function runs in a `||` context, where set -e is suppressed — an
  # unguarded failure here would be reported as a successful rollback with
  # the .env still floated over a rolled-back guest.
  restore_env_floors || return 1
}

if [[ "$ROLLBACK_NEEDED" == "1" || "$FINAL_OK" == "0" ]]; then
  RB_OK=1
  rollback_all "$SWAPPED" "$SYNC_DEPLOYED" || RB_OK=0
  if [[ "$RB_OK" == "1" ]] && { [[ "$UPDATE_DOCTOR" != "1" ]] || bash "$REPO/deploy/doctor.sh" --env "$ENV_FILE" --ssh "$SSH_TARGET" --deploy-key "$DEPLOY_KEY"; }; then
    if [[ "$UPDATE_DOCTOR" == "1" ]]; then
      echo "update: ROLLED BACK — previous versions restored, service healthy, doctor GREEN"
    else
      echo "update: ROLLED BACK — previous versions restored (UPDATE_DOCTOR=0: the verify doctor was skipped — cert it: deploy/doctor.sh)"
    fi
    exit 0
  fi
  echo "update: ROLLBACK FAILED — manual intervention required (previous artifacts retained on the guest; the old image is at $IMAGES_DIR/$OLD_IMAGE)" >&2
  exit 1
fi

# --------------------------------------------------------------------------
echo "==> [7/7] report"
NEW_KERNEL="$(manifest_pkg "$NEW_IMAGE" linux)"
if (( SWAPPED )); then
  IMG_REPORT="$OLD_IMAGE -> $NEW_IMAGE (kernel: ${OLD_KERNEL:-?} -> ${NEW_KERNEL:-?})"
else
  IMG_REPORT="$OLD_IMAGE (unchanged; kernel ${OLD_KERNEL:-?}; built ${IMAGE_AGE_DAYS}d ago)"
fi
if (( SYNC_WAITING )); then
  echo "update: OK — update-WAITING (a run is active; the tick applies the rest at the first idle moment — nothing was interrupted)"
elif (( SYNC_NOOP )) && (( ! SWAPPED )) && (( ! PI_MOVE )) && (( ! EXT_MOVE )); then
  echo "update: NO-OP (world current: pi $PI_TARGET, ext ${GUEST_EXT:-none}, image age ${IMAGE_AGE_DAYS}d (floor ${IMAGE_MAX_AGE_DAYS}d), shape unchanged — nothing touched, no restart)"
else
  echo "update: OK — pi ${GUEST_PI:-none} -> $PI_TARGET; ext ${GUEST_EXT:-none} -> ${EXT_TARGET:-none}; image: $IMG_REPORT"
fi
if (( ${#MAJOR_NOTES[@]} )); then
  echo "update: MAJOR AVAILABLE — not applied (accept by editing the .env line to that major):"
  for n in "${MAJOR_NOTES[@]}"; do
    echo "    $n"
  done
fi
exit 0
