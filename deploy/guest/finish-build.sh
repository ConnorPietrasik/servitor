#!/usr/bin/env bash
# Servitor — one-shot recovery for the build interrupted on 2026-11-16.
#
# The interrupted run (deploy/guest/build.sh, workdir Ls1YD7) completed
# steps [1/7]..[5/7] and the boot-file extract of [6/7], then died at the
# `umount -R` (busy devtmpfs bind) before the qcow2 convert + manifest.
# The raw root disk is complete and still mounted (loop0).
#
# This script finishes it:
#   1. find the leftover workdir (/srv/servitor-guest-build/*/root.raw)
#   2. hotpatch the on-disk image to match the FIXED build.sh (the
#      interrupted build predates three fixes — same edits, applied
#      explicitly, so the finished image == a clean rebuild's image):
#        a. bake the agent-config source at /opt/servitor/agent-config
#           (first-boot config source; the FRESH state disk mounts over
#            /var/lib/servitor, so the source must live on the root fs)
#        b. unit ExecStart rewritten in full: -m app.main --state
#           /var/lib/servitor --config-src /opt/servitor/agent-config
#           (the interrupted build's sed pointed at
#           /var/lib/servitor/config/active — missing at first boot,
#           the app crash-looped; the app.api.main module in the old
#           template does not exist at all)
#        c. baked env: SERVITOR_PI_NODE_BIN=/usr/sbin/node (Arch's node
#           is in /usr/sbin, not /usr/bin — every pi spawn would ENOENT)
#   3. unmount the leftover tree (lazy where busy), detach the loop device
#   4. convert raw -> compressed qcow2 (stem = the already-extracted boot
#      files; they are unaffected by the patches — /boot is untouched)
#   5. write images/manifest.json (same schema as build.sh step [7/7];
#      the python is duplicated by design: one-shot recovery tool)
#   6. remove the workdir
#
# Root only. Run:  sudo deploy/guest/finish-build.sh
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
GUEST_DIR="$REPO/deploy/guest"
IMAGES_DIR="$GUEST_DIR/images"
PINS="$GUEST_DIR/pins.json"
BUILD_ROOT=/srv/servitor-guest-build
ENV_FILE="$REPO/.env"

[[ "$(id -u)" == "0" ]] || { echo "needs root (mounts, losetup, qemu-img)" >&2; exit 1; }
command -v qemu-img >/dev/null || { echo "qemu-img not found (pacman -S qemu)" >&2; exit 1; }

jsonget() { python3 - "$PINS" "$1" <<'EOF'
import json, sys
d = json.load(open(sys.argv[1]))
for k in sys.argv[2].split("."):
    d = d[k]
print(d)
EOF
}

# ---- 1. find the leftover workdir ---------------------------------------
ROOT_RAW="$(ls -d "$BUILD_ROOT"/*/root.raw 2>/dev/null | head -1 || true)"
[[ -n "$ROOT_RAW" ]] || { echo "no leftover build workdir with root.raw under $BUILD_ROOT" >&2; exit 1; }
WORK="$(dirname "$ROOT_RAW")"
DISK="$WORK/disk"
echo "==> [1/6] workdir: $WORK"

# make sure the raw disk is mounted (it may have survived the crash, as it
# did here: /dev/loop0 -> .../disk)
if ! mountpoint -q "$DISK"; then
  LOOP="$(losetup -j "$WORK/root.raw" 2>/dev/null | head -1 | cut -d: -f1 || true)"
  if [[ -z "$LOOP" ]]; then
    LOOP="$(losetup -f --show "$WORK/root.raw")"
  fi
  mkdir -p "$DISK"
  mount "$LOOP" "$DISK"
fi

# ---- 2. hotpatch the image tree (before umount) --------------------------
echo "==> [2/6] hotpatch: agent-config bake + unit + node bin"
rsync -a --delete "$REPO/agent-config/" "$DISK/opt/servitor/agent-config/"
sed -i 's|^ExecStart=.*|ExecStart=/opt/servitor/app/.venv/bin/python -m app.main --state /var/lib/servitor --config-src /opt/servitor/agent-config|' \
  "$DISK/etc/systemd/system/servitor.service"
if ! grep -q -- '-m app.main --state /var/lib/servitor --config-src /opt/servitor/agent-config' \
    "$DISK/etc/systemd/system/servitor.service"; then
  echo "FATAL: unit ExecStart did not land (unexpected template)" >&2; exit 1
fi
if grep -q 'app\.api\.main' "$DISK/etc/systemd/system/servitor.service"; then
  echo "FATAL: unit still references app.api.main (no such module)" >&2; exit 1
fi

NODE_BIN=""
for c in /usr/sbin/node /usr/bin/node; do
  [[ -x "$DISK$c" ]] && NODE_BIN="$c" && break
done
[[ -n "$NODE_BIN" ]] || { echo "FATAL: node not found on the built disk" >&2; exit 1; }
sed -i "s|^SERVITOR_PI_NODE_BIN=.*|SERVITOR_PI_NODE_BIN=$NODE_BIN|" "$DISK/etc/servitor/servitor.env"
echo "    node bin -> $NODE_BIN"

# manifest inputs (read while mounted)
PI_VERSION="$(cat "$DISK/tmp/pi-version.txt" 2>/dev/null || echo unknown)"
NODE_VERSION="$(cat "$DISK/tmp/node-version.txt" 2>/dev/null || echo unknown)"
PKGLIST="$WORK/pkglist.txt"
[[ -f "$PKGLIST" ]] || { echo "FATAL: $PKGLIST missing (step [6/7] did not reach the pkglist copy?)" >&2; exit 1; }

# ---- 3. unmount + detach ---------------------------------------------------
echo "==> [3/6] unmount + losetach"
for m in "$DISK/dev/shm" "$DISK/dev/pts" "$DISK/dev/mqueue" "$DISK/dev/hugepages" \
         "$DISK/proc" "$DISK/sys" "$DISK/run" "$DISK/dev" "$DISK"; do
  if mountpoint -q "$m" 2>/dev/null; then
    umount "$m" 2>/dev/null || umount -l "$m" 2>/dev/null || true
  fi
done
losetup -j "$WORK/root.raw" 2>/dev/null | cut -d: -f1 | xargs -r -n1 losetach 2>/dev/null \
  || echo "warn: loop device still attached (busy); the convert reads the raw file directly"

# ---- 4. convert raw -> qcow2 -----------------------------------------------
echo "==> [4/6] qcow2 convert"
VMLINUX="$(ls "$IMAGES_DIR"/*.vmlinuz 2>/dev/null | head -1 || true)"
[[ -n "$VMLINUX" ]] || { echo "FATAL: no extracted .vmlinuz in $IMAGES_DIR" >&2; exit 1; }
STEM="$(basename "$VMLINUX" .vmlinuz)"
[[ -f "$IMAGES_DIR/${STEM}.initramfs.img" ]] || { echo "FATAL: initramfs missing for $STEM" >&2; exit 1; }
IMAGE_NAME="${STEM}.qcow2"
[[ -f "$IMAGES_DIR/$IMAGE_NAME" ]] && rm -f "$IMAGES_DIR/$IMAGE_NAME"
qemu-img convert -c -f raw -O qcow2 "$WORK/root.raw" "$IMAGES_DIR/$IMAGE_NAME"
IMAGE_SHA="$(sha256sum "$IMAGES_DIR/$IMAGE_NAME" | cut -d' ' -f1)"
IMAGE_SIZE="$(stat -c %s "$IMAGES_DIR/$IMAGE_NAME")"

# ---- 5. manifest (previous image retained) ----------------------------------
echo "==> [5/6] manifest"
BASE_URL="$(jsonget arch_bootstrap.url)"
BASE_SHA="$(jsonget arch_bootstrap.sha256)"
if [[ -f "$ENV_FILE" ]]; then
  set -a; source <(grep -E '^[A-Za-z_][A-Za-z0-9_]*=' "$ENV_FILE"); set +a
fi
: "${SERVITOR_PI_WEB_ACCESS_VERSION:=}"
python3 - "$IMAGES_DIR/manifest.json" "$IMAGE_NAME" "$IMAGE_SHA" "$IMAGE_SIZE" \
  "$BASE_URL" "$BASE_SHA" "$PI_VERSION" "$NODE_VERSION" "$PKGLIST" \
  "$SERVITOR_PI_WEB_ACCESS_VERSION" <<'EOF'
import json, os, sys, time
(manifest_path, name, sha, size, base_url, base_sha, pi, node,
 pkglist, ext_spec) = sys.argv[1:11]
manifest = {"images": {}}
if os.path.exists(manifest_path):
    manifest = json.load(open(manifest_path))
prev = manifest.get("active")
packages = []
if os.path.exists(pkglist):
    packages = [l.strip() for l in open(pkglist) if l.strip()]
entry = {
    "name": name,
    "sha256": sha,
    "size": int(size),
    "built_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    "base": {"url": base_url, "sha256": base_sha},
    "pi": {"package": "@earendil-works/pi-coding-agent", "version": pi, "node": node},
    "pi_web_access_spec": ext_spec,
    "packages": packages,
    "note": "finished by deploy/guest/finish-build.sh from an interrupted build.sh run "
            "(steps 1-5 complete); hotpatched: agent-config bake, unit ExecStart "
            "(app.main + --config-src /opt/servitor/agent-config), SERVITOR_PI_NODE_BIN "
            "— identical to the fixed build.sh output",
}
if prev and prev != name:
    entry["retained_previous"] = manifest["images"].get(prev, {"name": prev})
manifest["images"][name] = entry
manifest["active"] = name
json.dump(manifest, open(manifest_path, "w"), indent=2, sort_keys=True)
print(f"image: {name}")
print(f"sha256: {sha}")
print(f"size: {int(size) // (1024*1024)} MB")
print(f"pi: {pi} (node {node})")
print(f"manifest: {manifest_path}")
EOF

# ---- 6. cleanup --------------------------------------------------------------
echo "==> [6/6] cleanup"
rm -rf "$WORK" 2>/dev/null || echo "warn: workdir $WORK could not be fully removed (leftover busy mounts?)"
echo "FINISH OK: $IMAGES_DIR/$IMAGE_NAME"
echo
echo "Next: the FIRST sync from the current repo is NOT a no-op by design — it"
echo "injects the key slice, installs the extension at the .env selection, and"
echo "re-stages the config under the content-addressed name (boot staged v1)."
echo "After that, an unchanged sync is a deterministic NO-OP."
