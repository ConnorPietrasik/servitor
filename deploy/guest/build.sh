#!/usr/bin/env bash
# Servitor guest image build.
#
# The image is the versioned artifact:
#   images/servitor-pi<pi-version>-<YYYYMMDD>.qcow2  (+ images/manifest.json)
# deploy/update.sh rebuilds it when a pi bump lands or the userland outgrows
# the freshness floor (IMAGE_MAX_AGE_DAYS): the rebuild re-resolves the Arch
# packages, so kernel/node/python float with it (recorded in the manifest).
# Retention: the NEW active image + exactly the previous one (the update's
# automatic rollback target) — every older image is pruned here (qcow2 +
# extracted kernel/initramfs + manifest entry): an image triple is multi-GB
# and rebuilds are routine.
#
# Determinism: pinned base (pins.json: url + sha256); the package set comes
# from the Arch repos at build time and is recorded in the manifest.
#
# Requirements: root (mkfs/loop mounts/chroot), qemu (qemu-img), network
# (Arch repos + npm). Run from the repo root:
#   sudo unshare -m deploy/guest/build.sh [--env .env] [--deploy-key ~/.srv/deploy_key.pub]
#
# unshare -m keeps the build's loop/mount activity in a PRIVATE mount
# namespace: a mid-build crash leaves no host mounts behind, and the heavy
# mount churn stays out of the host mount table entirely.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
GUEST_DIR="$REPO/deploy/guest"
IMAGES_DIR="$GUEST_DIR/images"
CACHE_DIR="$IMAGES_DIR/cache"
PINS="$GUEST_DIR/pins.json"
ENV_FILE="$REPO/.env"
DEPLOY_KEY="${HOME}/.srv/deploy_key.pub"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --env) ENV_FILE="$2"; shift 2 ;;
    --deploy-key) DEPLOY_KEY="$2"; shift 2 ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
done

[[ "$(id -u)" == "0" ]] || { echo "build needs root (mkfs, loop, chroot)" >&2; exit 1; }
command -v qemu-img >/dev/null || { echo "qemu-img not found (pacman -S qemu)" >&2; exit 1; }
mkdir -p "$IMAGES_DIR" "$CACHE_DIR"

# ---- read the pinned inputs + the .env selections ----------------------
jsonget() { python3 - "$PINS" "$1" <<'EOF'
import json, sys
d = json.load(open(sys.argv[1]))
for k in sys.argv[2].split("."):
    d = d[k]
print(d)
EOF
}
BASE_URL="$(jsonget arch_bootstrap.url)"
BASE_SHA="$(jsonget arch_bootstrap.sha256)"
PACKAGES="$(python3 -c "import json; print(' '.join(json.load(open('$PINS'))['packages']))")"

# .env: KEY=VALUE lines (no export needed; we export what we use)
if [[ -f "$ENV_FILE" ]]; then
  set -a; source <(grep -E '^[A-Za-z_][A-Za-z0-9_]*=' "$ENV_FILE"); set +a
fi
: "${SERVITOR_PI_VERSION:?SERVITOR_PI_VERSION missing (set in .env)}"
: "${SERVITOR_PI_WEB_ACCESS_VERSION:=}"
: "${SERVITOR_PORT:=8787}"
: "${SERVITOR_EDGE_SUBNET:?SERVITOR_EDGE_SUBNET missing in $ENV_FILE}"
: "${SERVITOR_OPS_SUBNET:?SERVITOR_OPS_SUBNET missing in $ENV_FILE}"
: "${SERVITOR_HOST_BRIDGE_IP:?SERVITOR_HOST_BRIDGE_IP missing in $ENV_FILE (the host edge-bridge IP: source address of the host ssh to the VM)}"
: "${SERVITOR_VM_IP:?SERVITOR_VM_IP missing in $ENV_FILE (the guest edge address)}"
# NOTE: no apostrophes in the :? message below — a single quote inside the
# word of ${var:?...} (even within double quotes) is a bash syntax error that
# makes the whole script unparseable.
: "${SERVITOR_DNS_UPSTREAM:?SERVITOR_DNS_UPSTREAM missing in $ENV_FILE (public DNS upstreams for the guest systemd-resolved; no DHCP on the edge bridge)}"
# the guest's static edge config, derived from the .env (no literals here)
GUEST_ADDR="${SERVITOR_VM_IP}/${SERVITOR_EDGE_SUBNET##*/}"
# gateway = the subnet's .1 (the host bridge IP — the edge gateway). NOT
# "${SUBNET%%/*}.1": for a /24's zero-address base that yields a 5-octet
# invalid string which systemd-networkd silently drops — the guest then
# boots with no default route and every live-router gate dies.
GUEST_GW="$(cut -d. -f1-3 <<<"${SERVITOR_EDGE_SUBNET%%/*}").1"
[[ -f "$DEPLOY_KEY" ]] || {
  echo "deploy key not found: $DEPLOY_KEY" >&2
  echo "  ssh-keygen -t ed25519 -N '' -f ${DEPLOY_KEY%.pub}" >&2
  exit 1; }

mkdir -p /srv/servitor-guest-build
WORK="$(mktemp -d /srv/servitor-guest-build/XXXXXX)"
LOOP=""
cleanup() {
  set +e
  # unmount everything under the workdir, deepest first; lazy where the
  # kernel refuses (busy devtmpfs bind, leftover pseudo mounts). Never
  # touch mounts outside the workdir.
  awk -v p="$WORK/disk" '$2 ~ "^"p"(/|$)" {print $2}' /proc/mounts \
    | sort -r | while read -r m; do
      umount "$m" 2>/dev/null || umount -l "$m" 2>/dev/null || true
    done
  [[ -n "$LOOP" ]] && losetach "$LOOP" 2>/dev/null
  # fallback: detach by backing file (resumed builds, lost $LOOP)
  losetup -j "$WORK/root.raw" 2>/dev/null | cut -d: -f1 | xargs -r -n1 losetach 2>/dev/null
  rm -rf "$WORK"
}
trap cleanup EXIT

# the workdir's mounts must NOT propagate to (or be disturbed by) the host
# mount table: private from here on (2026-11-16 umount -R failure)
mount --make-rprivate /srv/servitor-guest-build 2>/dev/null || true

echo "==> [1/7] base userland (pinned)"
TARBALL="$CACHE_DIR/$(basename "$BASE_URL")"
if [[ ! -f "$TARBALL" ]]; then
  curl -fsSL -o "$TARBALL.part" "$BASE_URL"
  echo "$BASE_SHA  $TARBALL.part" | sha256sum -c - >/dev/null
  mv "$TARBALL.part" "$TARBALL"
else
  echo "$BASE_SHA  $TARBALL" | sha256sum -c - >/dev/null
fi
mkdir -p "$WORK/rootfs"
tar --zstd -xf "$TARBALL" -C "$WORK/rootfs"
mv "$WORK/rootfs/root.x86_64" "$WORK/rootfs.tmp"
rm -rf "$WORK/rootfs"
mv "$WORK/rootfs.tmp" "$WORK/rootfs"

echo "==> [2/7] root disk (24G ext4, label servitor-root)"
# NOTE: mkfs must see the full size — a sparse qcow2 container is ~20 KB
# on disk, so the image is built RAW (loop-friendly) and converted to a
# compressed qcow2 at the end (the qcow2 is the shipped artifact).
qemu-img create -f raw "$WORK/root.raw" 24G >/dev/null
mkfs.ext4 -q -L servitor-root "$WORK/root.raw"
LOOP="$(losetup -f --show "$WORK/root.raw")"
ROOT_UUID="$(blkid -s UUID -o value "$LOOP")"
mkdir -p "$WORK/disk"
mount "$LOOP" "$WORK/disk"
rsync -a --delete "$WORK/rootfs/" "$WORK/disk/"
mkdir -p "$WORK/disk/run" "$WORK/disk/dev/pts"
mount --bind /dev "$WORK/disk/dev"
mount --bind /dev/pts "$WORK/disk/dev/pts"
mount -t proc proc "$WORK/disk/proc"
mount -t sysfs sys "$WORK/disk/sys"
mount --bind /run "$WORK/disk/run"
# keep every mount made under the disk local to it (private propagation)
mount --make-rprivate "$WORK/disk"
# the chroot shares the host netns but not its DNS config (the bootstrap
# ships a placeholder): borrow the host's resolv.conf
if grep -q "^nameserver" /etc/resolv.conf; then
  cp /etc/resolv.conf "$WORK/disk/etc/resolv.conf"
else
  # no usable host config (e.g. systemd-resolved stub only): the stub's
  # own resolver is reachable via the loopback DNS on the host
  cp /run/systemd/resolve/resolv.conf "$WORK/disk/etc/resolv.conf" 2>/dev/null \
    || cp /etc/resolv.conf "$WORK/disk/etc/resolv.conf"
fi

echo "==> [3/7] chroot: package set (Arch repos, versions -> manifest)"
# the in-chroot scripts are written to the host workdir and copied in —
# no nested-quote gymnastics
cat > "$WORK/chroot-packages.sh" <<'CHROOT_EOF'
set -euo pipefail
# the bootstrap ships no mirrorlist (by design). Mirrors of the official
# repo (pacman fails over per file between the listed servers): the
# package set + versions are recorded in the manifest, so the image is
# reproducible from pins + manifest regardless of mirror.
printf 'Server = https://mirror.de.leaseweb.net/archlinux/$repo/os/$arch\nServer = https://mirror.23m.com/archlinux/$repo/os/$arch\n' > /etc/pacman.d/mirrorlist
# pacman GPG trust store (the bootstrap ships none): init + archlinux keys
mkdir -p /var/lib/pacman/gpg
pacman-key --init >/dev/null 2>&1 || true
pacman-key --populate archlinux >/dev/null 2>&1 || true
rm -f /var/lib/pacman/db.lck
read -r -a PKGS <<< "$PACKAGES"
pacman -Sy --noconfirm --needed "${PKGS[@]}" > /tmp/pacman.log 2>&1 \
  || { tail -30 /tmp/pacman.log; exit 1; }
pacman -Qq > /tmp/pkglist.txt
CHROOT_EOF
cp "$WORK/chroot-packages.sh" "$WORK/disk/chroot-packages.sh"
env LC_ALL=C TZ=UTC PACKAGES="$PACKAGES" chroot "$WORK/disk" /usr/bin/bash /chroot-packages.sh
echo "    installed: $(tr '\n' ' ' < "$WORK/disk/tmp/pkglist.txt" | cut -c1-160)..."

# app code from the repo (the synced artifact layout) BEFORE the venv
mkdir -p "$WORK/disk/opt/servitor/app"
rsync -a --delete "$REPO/app/" "$WORK/disk/opt/servitor/app/app/"
cp "$REPO/pyproject.toml" "$WORK/disk/opt/servitor/app/pyproject.toml"
# never ship rollback remnants: an in-build sync leaves app.previous* — a
# stale previous tree baked into the image poisons the FIRST update
# rollback (it restores pre-build code)
rm -rf "$WORK/disk/opt/servitor/app.previous" \
       "$WORK/disk/opt/servitor/app.previous.app" \
       "$WORK/disk/opt/servitor/app.previous.pyproject"

# The chat UI (static): the app serves ui/dist on the same port
# (app/api/app.py resolves <app root>/ui/dist — on the guest that is
# /opt/servitor/app/ui/dist). Built host-side at image time with vite
# (nodejs is a required host package). NO --ignore-scripts here, unlike
# the pi stage: esbuild (a vite dep) needs its install script. A dist-less
# image is API-only (a 404 behind the 2FA gate, nothing explaining why) —
# so this is a hard build step, not a convenience. sync.sh keeps it
# current on the guest for UI-only changes (no image rebuild).
( cd "$REPO/ui" && npm ci --no-audit --no-fund >/dev/null && npm run build >/dev/null )
rm -rf "$WORK/disk/opt/servitor/app/ui"
mkdir -p "$WORK/disk/opt/servitor/app/ui"
rsync -a --delete "$REPO/ui/dist/" "$WORK/disk/opt/servitor/app/ui/dist/"

echo "==> [4/7] pi ($SERVITOR_PI_VERSION) + app venv + toolkit + slots"
# note: unquoted heredoc — $SERVITOR_PI_VERSION expands at write time;
# the in-chroot shell variables stay escaped
cat > "$WORK/chroot-setup.sh" <<CHROOT_EOF
set -euo pipefail
export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
# locate node + npm (diagnostic on failure — Arch's nodejs package layout)
echo "    node: \$(command -v node || echo MISSING)  \$(node --version 2>/dev/null || true)"
echo "    npm:  \$(command -v npm || echo MISSING)"
if ! command -v npm >/dev/null 2>&1; then
  echo "FATAL: npm not found in the chroot; nodejs package files:" >&2
  ls -la /usr/bin/ | grep -E '^(l)?r.*node|npm|npx' || true
  ls -la /usr/lib/node_modules 2>/dev/null | head || true
  exit 1
fi
# pi: --ignore-scripts; the CLI path is an absolute constant (never PATH)
npm install --ignore-scripts --no-audit --no-fund --global \
    "@earendil-works/pi-coding-agent@${SERVITOR_PI_VERSION}" > /tmp/npm.log 2>&1 \
  || { tail -30 /tmp/npm.log; exit 1; }
node --version > /tmp/node-version.txt
command -v node > /tmp/node-bin.txt
PI_VER=\$(node -e "console.log(require('/usr/lib/node_modules/@earendil-works/pi-coding-agent/package.json').version)")
echo "\$PI_VER" > /tmp/pi-version.txt
echo "    pi installed: \$PI_VER"
# app venv (the service runs /opt/servitor/app/.venv/bin/python -m app.main)
python -m venv /opt/servitor/app/.venv
/opt/servitor/app/.venv/bin/pip install --no-cache-dir --disable-pip-version-check --quiet \
    fastapi "uvicorn[standard]" python-magic httpx
# the app package is the tree itself: a .pth on the venv sys.path
/opt/servitor/app/.venv/bin/python - <<'PYEOF'
import site
with open(site.getsitepackages()[0] + '/servitor.pth', 'w') as f:
    f.write('/opt/servitor/app\n')
PYEOF
python --version > /tmp/python-version.txt
# toolkit: root-owned 0755, a plain directory (the overlay lower), and NO
# context files at its root (doctor asserts this). The toolkit is the
# LOWER of every session's workdir overlay, so the agent sees bin/python
# (the venv python — the sole toolkit entry that differs from /usr/bin,
# by Pillow/numpy) as ./bin/python in its working directory: /opt/toolkit
# is NOT on the agent's PATH, and bare `python` is /usr/bin/python. Every
# other tool resolves via /usr/bin, which is already on the agent's PATH.
# A new packaged tool is a pins.json + SYSTEM.md change; a new Python library
# goes on the pip line below (uv comes from the Arch package, not here).
mkdir -p /opt/toolkit/bin
python -m venv /opt/toolkit/.venv
/opt/toolkit/.venv/bin/pip install --no-cache-dir --disable-pip-version-check --quiet pillow numpy
ln -sf /opt/toolkit/.venv/bin/python /opt/toolkit/bin/python
chmod 0755 /opt/toolkit
# slot users, fixed uids, 0700 homes: slot0 (uid 9000) is the DOCTOR PROBE
# slot — outside the app's pool (SLOT_NAMES = slot1-16), so the app's
# uid-gated kills can never touch the probe's pi child; slot1-16 (uids
# 9001-9016) are the app's session slots
for i in \$(seq 0 16); do
  useradd -u \$((9000 + i)) -m -s /usr/sbin/nologin "slot\$i" 2>/dev/null || true
  chmod 0700 "/home/slot\$i"
done
# state volume dirs (root fs; the state DISK mounts over /var/lib/servitor)
mkdir -p /var/lib/servitor/{config,lib,inbox,scratch,sessions}
# /opt/pi-ext: empty — the sync pipeline installs pi-web-access (it is NOT
# in the image)
mkdir -p /opt/pi-ext
chmod 0755 /opt/pi-ext
CHROOT_EOF
cp "$WORK/chroot-setup.sh" "$WORK/disk/chroot-setup.sh"
env LC_ALL=C TZ=UTC chroot "$WORK/disk" /usr/bin/bash /chroot-setup.sh
rm -f "$WORK/disk/chroot-packages.sh" "$WORK/disk/chroot-setup.sh"

echo "==> [5/7] systemd units + firewall + ssh + fstab"
# node lives at /usr/sbin/node on this Arch (the build's chroot PATH sees
# it; record the REAL path, never hardcode /usr/bin/node)
NODE_BIN="$(cat "$WORK/disk/tmp/node-bin.txt" 2>/dev/null || true)"
[[ -x "$WORK/disk$NODE_BIN" ]] || NODE_BIN=""
[[ -n "$NODE_BIN" && -x "$WORK/disk$NODE_BIN" ]] || NODE_BIN="/usr/sbin/node"
[[ -x "$WORK/disk$NODE_BIN" ]] || NODE_BIN="/usr/bin/node"
[[ -x "$WORK/disk$NODE_BIN" ]] || { echo "FATAL: node not found on the built disk" >&2; exit 1; }
# the env file (0600): the key slice (OMNISSIAH_API_KEY / EXA_API_KEY) is
# owned by the sync pipeline (rotation re-injects); the image ships the
# non-key deploy defaults
mkdir -p "$WORK/disk/etc/servitor"
cat > "$WORK/disk/etc/servitor/servitor.env" <<EOF
SERVITOR_PORT=$SERVITOR_PORT
# the guest serves the edge directly (no local reverse proxy): bind the edge
# address so the host/proxy reach it at SERVITOR_VM_IP:SERVITOR_PORT. The
# firewall admits the port from the edge subnet only.
SERVITOR_BIND=$SERVITOR_VM_IP
SERVITOR_EDGE_SUBNET=$SERVITOR_EDGE_SUBNET
SERVITOR_HOST_BRIDGE_IP=$SERVITOR_HOST_BRIDGE_IP
SERVITOR_OPS_SUBNET=$SERVITOR_OPS_SUBNET
SERVITOR_PI_VERSION=$SERVITOR_PI_VERSION
SERVITOR_PI_WEB_ACCESS_VERSION=$SERVITOR_PI_WEB_ACCESS_VERSION
SERVITOR_PI_NODE_BIN=$NODE_BIN
SERVITOR_PI_CLI_JS=/usr/lib/node_modules/@earendil-works/pi-coding-agent/dist/cli.js
SERVITOR_PI_EXT_DIR=/opt/pi-ext/pi-web-access
SERVITOR_TOOLKIT_DIR=/opt/toolkit
EOF
chmod 0600 "$WORK/disk/etc/servitor/servitor.env"

# agent config source baked into the root fs. NOT under /var/lib/servitor
# (the state disk mounts over that and is FRESH on first boot): the app's
# _stage_config stages this source into the state volume's versioned
# config/ at first boot; the sync pipeline owns versions from then on.
rsync -a --delete "$REPO/agent-config/" "$WORK/disk/opt/servitor/agent-config/"

install -m 0644 "$GUEST_DIR/servitor.slice" "$WORK/disk/etc/systemd/system/servitor.slice"
sed "s|ExecStart=.*|ExecStart=/opt/servitor/app/.venv/bin/python -m app.main --state /var/lib/servitor --config-src /opt/servitor/agent-config|" \
  "$GUEST_DIR/servitor.service" > "$WORK/disk/etc/systemd/system/servitor.service"
chmod 0644 "$WORK/disk/etc/systemd/system/servitor.service"
install -m 0755 "$GUEST_DIR/firewall-nftables.sh" "$WORK/disk/usr/local/sbin/servitor-firewall.sh"
cat > "$WORK/disk/etc/systemd/system/servitor-firewall.service" <<'EOF'
[Unit]
Description=Servitor nftables (default-deny, edge subnet only)
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
RemainAfterExit=yes
EnvironmentFile=/etc/servitor/servitor.env
ExecStart=/usr/local/sbin/servitor-firewall.sh

[Install]
WantedBy=multi-user.target
EOF
cat > "$WORK/chroot-systemd.sh" <<'CHROOT_EOF'
set -euo pipefail
systemctl enable sshd servitor-firewall servitor systemd-networkd systemd-resolved 2>/dev/null || true
ssh-keygen -A >/dev/null
# first-boot hygiene: the bootstrap ships an EMPTY machine-id, so
# systemd-firstboot.service would drop into an interactive prompt and HANG
# boot. Set a machine-id and mask firstboot.
systemd-machine-id-setup >/dev/null 2>&1 || true
ln -sf /dev/null /etc/systemd/system/systemd-firstboot.service
cat > /etc/zram-generator.conf <<'CONF'
[zram0]
zram-size = 4G
swap-size = 100%
CONF
# the zram generator runs at boot whenever the config file exists (no
# enable step): systemd-zram-setup@0 starts from it
# static edge address (no DHCP on the edge bridge): the host owns
# the edge subnet with itself at its .1; the guest is SERVITOR_VM_IP
cat > /etc/systemd/network/10-servitor.network <<NET
[Match]
Name=en* eth*

[Network]
Address=${GUEST_ADDR}
Gateway=${GUEST_GW}
NET
# DNS: no DHCP on the edge bridge, so systemd-resolved must be enabled with
# an explicit upstream (.env SERVITOR_DNS_UPSTREAM; public resolvers work
# over the host's egress NAT). Stub listener stays on the standard loopback
# stub.
mkdir -p /etc/systemd/resolved.conf.d
cat > /etc/systemd/resolved.conf.d/10-servitor.conf <<DNS
[Resolve]
DNS=${SERVITOR_DNS_UPSTREAM}
DNSStubListener=yes
DNSSEC=no
DNSOverTLS=no
DNSStubRRAAAA=no
DNS
ln -sf /run/systemd/resolve/stub-resolv.conf /etc/resolv.conf
systemctl enable systemd-resolved 2>/dev/null || true
CHROOT_EOF
cp "$WORK/chroot-systemd.sh" "$WORK/disk/chroot-systemd.sh"
chroot "$WORK/disk" /usr/bin/env GUEST_ADDR="$GUEST_ADDR" GUEST_GW="$GUEST_GW" /usr/bin/bash /chroot-systemd.sh
rm -f "$WORK/disk/chroot-systemd.sh"

# fstab: root by UUID + the state disk by label (the operator labels it
# servitor-state at VM creation, INSTALL.md §5; nofail so boot tolerates
# its absence — doctor fails loudly when it is missing). The state line is
# fstype `auto` (not ext4): the label is the contract, and the doctor only
# requires a local non-overlay fs — the install creates ext4, but a
# xfs/btrfs state disk must mount too.
cat > "$WORK/disk/etc/fstab" <<EOF
# / etc/fstab
UUID=$ROOT_UUID  /  ext4  defaults  0 1
LABEL=servitor-state  /var/lib/servitor  auto  defaults,nofail  0 2
EOF
# ssh: root via the deploy key only
mkdir -p "$WORK/disk/root/.ssh"
cp "$DEPLOY_KEY" "$WORK/disk/root/.ssh/authorized_keys"
chmod 0700 "$WORK/disk/root/.ssh"
chmod 0600 "$WORK/disk/root/.ssh/authorized_keys"
echo servitor > "$WORK/disk/etc/hostname"

echo "==> [6/7] finalize (extract boot files, umount, compress, manifest)"
cp "$WORK/disk/tmp/pkglist.txt" "$WORK/disk/opt/servitor/pkglist.txt"
cp "$WORK/disk/tmp/pkglist.txt" "$WORK/pkglist.txt"
rm -f "$WORK/disk/tmp/pacman.log" "$WORK/disk/tmp/npm.log"
PI_VERSION="$(cat "$WORK/disk/tmp/pi-version.txt")"
NODE_VERSION="$(cat "$WORK/disk/tmp/node-version.txt")"
BUILD_DATE="$(date -u +%Y%m%d)"
IMAGE_NAME="servitor-pi${PI_VERSION}-${BUILD_DATE}.qcow2"
# the libvirt domain boots with <kernel>/<initrd> (no bootloader in the
# image on purpose): ship the boot files alongside the qcow2
STEM="${IMAGE_NAME%.qcow2}"
cp "$WORK/disk/boot/vmlinuz-linux" "$IMAGES_DIR/${STEM}.vmlinuz"
cp "$WORK/disk/boot/initramfs-linux.img" "$IMAGES_DIR/${STEM}.initramfs.img"
# unmount in reverse mount order, one by one; lazy fallback for a busy
# devtmpfs bind (umount -R on nested dev mounts is unreliable)
for m in "$WORK/disk/dev/shm" "$WORK/disk/dev/pts" "$WORK/disk/dev/mqueue" \
         "$WORK/disk/dev/hugepages" "$WORK/disk/proc" "$WORK/disk/sys" \
         "$WORK/disk/run" "$WORK/disk/dev" "$WORK/disk"; do
  if mountpoint -q "$m" 2>/dev/null; then
    umount "$m" 2>/dev/null || umount -l "$m" 2>/dev/null || true
  fi
done
losetup -d "$LOOP"; LOOP=""

qemu-img convert -c -f raw -O qcow2 "$WORK/root.raw" "$IMAGES_DIR/$IMAGE_NAME"
IMAGE_SHA="$(sha256sum "$IMAGES_DIR/$IMAGE_NAME" | cut -d' ' -f1)"
IMAGE_SIZE="$(stat -c %s "$IMAGES_DIR/$IMAGE_NAME")"

echo "==> [7/7] manifest (active + previous retained, older images pruned)"
python3 - "$IMAGES_DIR/manifest.json" "$IMAGE_NAME" "$IMAGE_SHA" "$IMAGE_SIZE" \
  "$BASE_URL" "$BASE_SHA" "$PI_VERSION" "$NODE_VERSION" "$WORK/pkglist.txt" \
  "$SERVITOR_PI_WEB_ACCESS_VERSION" <<'EOF'
import json, os, sys, tempfile, time
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
}
if prev and prev != name:
    entry["retained_previous"] = manifest["images"].get(prev, {"name": prev})
manifest["images"][name] = entry
manifest["active"] = name
# retention: the new active + exactly the previous (the rollback target)
# only. Everything older is trash — files AND manifest entries. The
# previous is NEVER pruned here: it is the update's rollback target, and
# the NEXT build supersedes it, one generation at a time. Files go first,
# the manifest after: a crash in between leaves orphan files (visible
# trash), not a manifest pointing at pruned rollback targets. A file that
# cannot be deleted keeps its manifest entry — the next build retries.
dir = os.path.dirname(manifest_path) or "."
pruned = []
for n in sorted(n for n in manifest["images"] if n not in ({name} | ({prev} if prev and prev != name else set()))):
    stem = n[: -len(".qcow2")] if n.endswith(".qcow2") else n
    ok = True
    for f in (os.path.join(dir, n),
              os.path.join(dir, stem + ".vmlinuz"),
              os.path.join(dir, stem + ".initramfs.img")):
        try:
            os.unlink(f)
        except FileNotFoundError:
            pass
        except OSError as e:
            print(f"WARNING: could not prune {f}: {e} — it stays listed (the next build retries the prune)", file=sys.stderr)
            ok = False
    if ok:
        pruned.append(n)
for n in pruned:
    del manifest["images"][n]
# atomic write (temp + rename): a crash here must not corrupt the manifest
# — it is the source of "which image runs".
fd, tmp = tempfile.mkstemp(dir=dir)
with os.fdopen(fd, "w") as fh:
    json.dump(manifest, fh, indent=2, sort_keys=True)
os.replace(tmp, manifest_path)
print(f"image: {name}")
print(f"sha256: {sha}")
print(f"size: {int(size) // (1024*1024)} MB")
print(f"pi: {pi} (node {node})")
for n in pruned:
    print(f"pruned: {n} (older than active + previous — trash)")
print(f"manifest: {manifest_path}")
EOF

echo "BUILD OK: $IMAGES_DIR/$IMAGE_NAME"
