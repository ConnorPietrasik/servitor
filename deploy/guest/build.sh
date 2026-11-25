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
# Determinism: pinned base (pins.json: url + sha256); the base is then fully
# upgraded (pacman -Syu) and the whole package set re-resolves from the Arch
# repos at build time, every installed version recorded in the manifest. The
# pins + manifest reproduce an image; they do not freeze one.
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
# the edge credential: baked into the image env file so the
# FIRST boot (before any sync) is already gated — with the key set, the
# app 403s every keyless request; rotation re-injects via the sync's key
# slice
: "${SERVITOR_EDGE_KEY:?SERVITOR_EDGE_KEY missing in $ENV_FILE (the edge credential — set it in portcullis .env too)}"
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
# mount table: private from here on (a mid-build crash must leave no host
# mounts behind, and the mount churn stays out of the host mount table)
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
# -Syu, never -Sy. `pacman -Sy <pkgs>` is a PARTIAL upgrade: the pinned
# bootstrap's glibc/pam/systemd stay at the pin date while the requested set
# and its deps come from today's repo. Arch does not support that, and the
# failures are the quiet kind — logind/sshd refusing to start on a PAM or
# libsystemd-shared skew, in an image that otherwise boots happily. A full
# upgrade is also what makes the header's promise true for the base and not
# just the added set: "the Arch packages re-resolve, kernel/node/python float
# to the current repo state". --noconfirm (plus /dev/null stdin) answers any
# conflict/selection prompt with pacman's default, never on the operator's
# terminal.
pacman -Syu --noconfirm --needed "${PKGS[@]}" > /tmp/pacman.log 2>&1 \
  || { tail -30 /tmp/pacman.log; exit 1; }
pacman -Qq > /tmp/pkglist.txt
CHROOT_EOF
cp "$WORK/chroot-packages.sh" "$WORK/disk/chroot-packages.sh"
# in-chroot scripts get /dev/null stdin: nothing in the build may read the
# operator's tty (a stray interactive prompt would hang the whole build —
# the same class of hang the firstboot mask closes at boot)
env LC_ALL=C TZ=UTC PACKAGES="$PACKAGES" chroot "$WORK/disk" /usr/bin/bash /chroot-packages.sh < /dev/null
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
# (nodejs is a required host package) with npm's DEFAULT script policy — no
# --ignore-scripts, unlike the pinned pi stage. Nothing here may DEPEND on a
# script running: npm's allowScripts policy can block one (esbuild's
# postinstall is blocked on current npm) and the build is still correct,
# because esbuild ships its binary as the @esbuild/<platform> optional
# dependency. What is load-bearing is the artifact, so the finished dist is
# certified — a dist whose index.html points at missing assets is a 200 and a
# black screen, and a dist-less image is a 404 behind the gate with nothing
# saying why. sync.sh keeps it current on the guest for UI-only changes (no
# image rebuild).
( cd "$REPO/ui" && npm ci --no-audit --no-fund >/dev/null && npm run build >/dev/null )
bash "$REPO/deploy/check_ui_dist.sh" "$REPO/ui/dist"
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
# is NOT on the agent's PATH, and bare python is /usr/bin/python. Every
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
# state volume dirs (root fs; the state DISK mounts over /var/lib/servitor):
# derived from the app's STATE_AREAS — the image creates the WHOLE set at
# build time (the service also creates each area on first use, so a new
# area is never missing; deriving keeps this line and the tuple in lockstep
# instead of drift apart)
AREAS="\$(/opt/servitor/app/.venv/bin/python -c 'import sys; sys.path.insert(0, "/opt/servitor/app"); from app.iso.layout import STATE_AREAS; print(" ".join(STATE_AREAS))')"
[ -n "\$AREAS" ] || { echo "FATAL: could not derive the state areas from the app" >&2; exit 1; }
for a in \$AREAS; do
  mkdir -p "/var/lib/servitor/\$a"
done
# /opt/pi-ext: empty — the sync pipeline installs pi-web-access (it is NOT
# in the image)
mkdir -p /opt/pi-ext
chmod 0755 /opt/pi-ext
CHROOT_EOF
cp "$WORK/chroot-setup.sh" "$WORK/disk/chroot-setup.sh"
env LC_ALL=C TZ=UTC chroot "$WORK/disk" /usr/bin/bash /chroot-setup.sh < /dev/null
rm -f "$WORK/disk/chroot-packages.sh" "$WORK/disk/chroot-setup.sh"

echo "==> [5/7] systemd units + firewall + ssh + fstab"
# node lives at /usr/sbin/node on this Arch (the build's chroot PATH sees
# it; record the REAL path, never hardcode /usr/bin/node)
NODE_BIN="$(cat "$WORK/disk/tmp/node-bin.txt" 2>/dev/null || true)"
[[ -x "$WORK/disk$NODE_BIN" ]] || NODE_BIN=""
[[ -n "$NODE_BIN" && -x "$WORK/disk$NODE_BIN" ]] || NODE_BIN="/usr/sbin/node"
[[ -x "$WORK/disk$NODE_BIN" ]] || NODE_BIN="/usr/bin/node"
[[ -x "$WORK/disk$NODE_BIN" ]] || { echo "FATAL: node not found on the built disk" >&2; exit 1; }
# the env file (0600): the model/search key slice (OMNISSIAH_API_KEY /
# EXA_API_KEY) is owned by the sync pipeline (rotation re-injects) and is
# NOT in the image; the edge key (SERVITOR_EDGE_KEY) IS baked — the first
# boot, before any sync, must already be gated: the build
# refuses to run without it, and the app enforces it on every route while
# set; the rest are the non-key deploy defaults
mkdir -p "$WORK/disk/etc/servitor"
cat > "$WORK/disk/etc/servitor/servitor.env" <<EOF
SERVITOR_PORT=$SERVITOR_PORT
# the guest serves the edge directly (no local reverse proxy): bind the edge
# address so the host/proxy reach it at SERVITOR_VM_IP:SERVITOR_PORT.
# The firewall admits the port from the edge subnet only.
SERVITOR_BIND=$SERVITOR_VM_IP
# edge credential: the app verifies X-Servitor-Edge-Key
# against this on every route; must equal the portcullis middleware value
SERVITOR_EDGE_KEY=$SERVITOR_EDGE_KEY
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
# no `|| true`, no stderr sink: `systemctl enable` is all-or-nothing per
# invocation (one unresolvable name and every later one in the list is
# skipped), so a swallowed failure here ships a guest with NOTHING enabled.
# That guest still reaches multi-user and prints a login prompt; the only
# symptom is ssh timing out 300 s into a swap.
systemctl enable sshd servitor-firewall servitor systemd-networkd systemd-resolved
ssh-keygen -A >/dev/null
# first-boot hygiene: the bootstrap ships an UNPOPULATED machine-id (empty or
# the literal "uninitialized"), so systemd-firstboot.service would drop into
# an interactive prompt and HANG boot, and logind refuses to start without a
# valid id. The build writes the id itself, not via systemd-machine-id-setup:
# that tool's behavior floats with the repo systemd this build just upgraded
# (current releases treat "uninitialized" as terminal — exit 0, no write),
# and its chroot detection (comparing /proc/1/root with /) is blind in this
# build (host /proc, no pid namespace), so when it DOES write it reuses the
# host's own id from the bound /run. Mask firstboot too; both are certified
# after the filesystem is quiesced (see the [6/7] artifact gate).
head -c 16 /dev/urandom | od -An -v -tx1 | tr -d ' \n' > /etc/machine-id
chmod 0444 /etc/machine-id
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
CHROOT_EOF
cp "$WORK/chroot-systemd.sh" "$WORK/disk/chroot-systemd.sh"
chroot "$WORK/disk" /usr/bin/env GUEST_ADDR="$GUEST_ADDR" GUEST_GW="$GUEST_GW" /usr/bin/bash /chroot-systemd.sh < /dev/null
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
# unmount in reverse mount order, one by one. The pseudo mounts (the host
# dev/devpts/run binds, the tmpfs visible under /dev) MAY legitimately refuse:
# lazy is fine for them because none of their data is in the image.
for m in "$WORK/disk/dev/shm" "$WORK/disk/dev/pts" "$WORK/disk/dev/mqueue" \
         "$WORK/disk/dev/hugepages" "$WORK/disk/proc" "$WORK/disk/sys" \
         "$WORK/disk/run" "$WORK/disk/dev"; do
  if mountpoint -q "$m" 2>/dev/null; then
    umount "$m" 2>/dev/null || umount -l "$m" 2>/dev/null || true
  fi
done
# holders of the built disk: every pid rooted in, or cwd'd into, $WORK/disk,
# as TAB-separated "pid  where  cmdline". Match on the /proc/<pid>/root and
# /cwd paths, never by name: the pid namespace is the host's (unshare -m
# only), so a name match would kill host processes.
disk_holders() {
  local p r c where
  for p in /proc/[0-9]*; do
    r="$(readlink "$p/root" 2>/dev/null)" || continue
    c="$(readlink "$p/cwd" 2>/dev/null)" || continue
    where=""
    case "$r" in "$WORK/disk"|"$WORK/disk"/*) where="root=$r" ;; esac
    case "$c" in "$WORK/disk"|"$WORK/disk"/*) where="${where:+$where, }cwd=$c" ;; esac
    [[ -n "$where" ]] || continue
    printf '%s\t%s\t%s\n' "${p#/proc/}" "$where" \
      "$(tr '\0' ' ' < "$p/cmdline" 2>/dev/null)"
  done
}
# sweep every process still rooted in, or cwd'd into, the built disk: a
# daemon spawned in-chroot outlives the chroot shell (pacman-key's
# gpg-agent is the known one — gpg auto-starts it, it detaches, and it
# holds cwd on the rootfs; that open reference is what makes the root
# umount EBUSY)
while IFS=$'\t' read -r pid where cmd; do
  echo "    killing lingering build process $pid — $cmd ($where)"
  kill -9 "$pid" 2>/dev/null || true
done < <(disk_holders)
# a SIGKILLed agent does not unlink its own socket; do not ship one
rm -f "$WORK/disk"/var/lib/pacman/gpg/S.gpg-agent* "$WORK/disk"/etc/pacman.d/gnupg/S.gpg-agent* 2>/dev/null || true
# THE ROOT MOUNT IS NOT A PSEUDO MOUNT. Lazy-unmounting it leaves the ext4
# mounted and dirty while losetup -d reports success (the loop driver defers
# the detach over open holders), so the convert below would read root.raw
# straight out of a live page cache: the image silently lacks everything the
# final seconds wrote, and is still perfectly bootable.
sync
if mountpoint -q "$WORK/disk"; then
  umount -R "$WORK/disk" 2>/dev/null || umount "$WORK/disk" 2>/dev/null \
    || { echo "FATAL: $WORK/disk will not unmount cleanly (something holds it busy) — refusing to image a dirty filesystem (evidence below, then re-run)" >&2
         # print the evidence BEFORE the EXIT trap removes the workdir:
         # leftover child mounts, then every process still rooted in / cwd'd
         # into / holding an open file on the disk (a holder that only shows
         # in /proc/*/fd is exactly how this class of bug hides)
         findmnt -R --noheadings "$WORK/disk" 2>/dev/null | sed 's/^/    mount: /' >&2
         while IFS=$'\t' read -r pid where cmd; do
           echo "    holder: pid $pid $where cmd=$cmd" >&2
         done < <(disk_holders)
         find /proc/[0-9]*/fd -lname "$WORK/disk/*" 2>/dev/null | sed 's/^/    fd: /' >&2
         exit 1; }
fi
# and prove the loop is really gone, not merely pending: one byte of
# root.raw is read only after nothing can still write it
losetup -d "$LOOP" 2>/dev/null || true
for _ in $(seq 1 60); do
  losetup -j "$WORK/root.raw" | grep -q . || break
  sleep 1
done
if losetup -j "$WORK/root.raw" | grep -q .; then
  echo "FATAL: $WORK/root.raw is still attached to a loop device (open holders) — refusing to image it (losetup -l; lsof \"$WORK/root.raw\")" >&2
  exit 1
fi
LOOP=""

# --- artifact gate -------------------------------------------------------
# Certify the QUIESCED filesystem read-only before it is shipped: the shape
# every later step depends on, asserted against the bytes the guest will
# actually boot. A guest that boots with nothing enabled is indistinguishable
# from a healthy one until a swap's ssh wait expires, so this is the only
# cheap place to catch it — before the image gets a name, a manifest entry,
# and a domain definition pointing at it.
echo "    certifying the image (read-only mount of the quiesced raw)"
CHK="$WORK/verify"; mkdir -p "$CHK"
RO_LOOP=""
if ! RO_LOOP="$(losetup -f --show -r "$WORK/root.raw")" || ! mount -o ro,norecovery "$RO_LOOP" "$CHK"; then
  [[ -n "$RO_LOOP" ]] && losetup -d "$RO_LOOP" 2>/dev/null
  echo "FATAL: $WORK/root.raw cannot be mounted read-only — the filesystem is dirty or corrupt, nothing is shipped" >&2
  exit 1
fi
BAD=()
want_file() { [[ -s "$CHK$1" ]] || BAD+=("$1 missing/empty — $2"); }
want_grep() { grep -q -- "$2" "$CHK$1" 2>/dev/null || BAD+=("$1 carries no '$2' — $3"); }
want_unit() { # a symlink under /etc/systemd/system/*.wants (multi-user vs sysinit differs per unit)
  compgen -G "$CHK/etc/systemd/system/*.wants/$1*" >/dev/null \
    || BAD+=("$1 not enabled — the guest boots with nothing running (the swap's ssh wait expires)")
}
# step [5/7]: the boot-critical system configuration
want_unit sshd; want_unit servitor; want_unit servitor-firewall
want_unit systemd-networkd; want_unit systemd-resolved
want_file /etc/hostname "the guest boots as the bootstrap's hostname"
want_file /etc/fstab "root + the state disk never mount"
want_grep /etc/fstab "servitor-state" "/var/lib/servitor never mounts (state.mount red)"
[[ -L "$CHK/etc/systemd/system/systemd-firstboot.service" ]] \
  || BAD+=("systemd-firstboot not masked — first boot prompts on a serial console nobody watches")
grep -Eq '^[0-9a-f]{32}$' "$CHK/etc/machine-id" 2>/dev/null \
  || BAD+=("/etc/machine-id is not a valid id — logind fails to start, firstboot prompts")
[[ -n "$(ls "$CHK"/etc/ssh/ssh_host_*_key 2>/dev/null)" ]] \
  || BAD+=("no ssh host keys — sshd cannot answer, and every swap rotates them anyway")
want_file /root/.ssh/authorized_keys "there is no other way in: password auth is off"
want_file /etc/systemd/network/10-servitor.network "no static edge address — no ssh, no sync, no doctor"
want_grep /etc/systemd/network/10-servitor.network "^Gateway=" "no default route — live router and searches die"
want_file /etc/systemd/resolved.conf.d/10-servitor.conf "no public resolver"
want_file /etc/zram-generator.conf "the swap the slice sizing assumes"
# the app's own contract: the unit's EnvironmentFile must carry the bind and
# the edge credential (the first boot is gated BEFORE any sync has run)
want_file /etc/servitor/servitor.env "the unit's EnvironmentFile: the app cannot start"
want_grep /etc/servitor/servitor.env "^SERVITOR_BIND=" "ConfigError: the bind address has no default"
want_grep /etc/servitor/servitor.env "^SERVITOR_EDGE_KEY=." "an unkeyed first boot (invariant 3: fail closed)"
# the synced/spawned shape the doctor certifies
want_file /opt/servitor/app/.venv/bin/python "the unit's interpreter is gone"
want_file /usr/lib/node_modules/@earendil-works/pi-coding-agent/dist/cli.js "SERVITOR_PI_CLI_JS points at nothing: every spawn fails"
want_file /opt/servitor/app/ui/dist/index.html "a UI-less image is a 404 behind the gate, with nothing saying why"
want_file /opt/servitor/agent-config/SYSTEM.md "first-boot config staging has no source"
want_file /opt/servitor/pkglist.txt "a step-[6/7] write is absent: the image was read while still mounted"
# the verifier holds the same hazard it exists to catch: a lazy release here
# would put a mounted filesystem back in front of the convert
umount "$CHK" 2>/dev/null || umount -R "$CHK" 2>/dev/null \
  || { echo "FATAL: $CHK will not unmount — nothing is shipped (a verifier that cannot release the image is not a gate)" >&2; exit 1; }
losetup -d "$RO_LOOP" 2>/dev/null || true
if losetup -j "$WORK/root.raw" | grep -q .; then
  echo "FATAL: $WORK/root.raw is still attached after certification — refusing to image it" >&2
  exit 1
fi
if (( ${#BAD[@]} )); then
  echo "FATAL: the built image is not certifiable — $IMAGE_NAME is NOT shipped and the manifest is untouched:" >&2
  for b in "${BAD[@]}"; do echo "    FAIL  $b" >&2; done
  exit 1
fi
echo "    image certified (enabled units, boot config, machine-id, host keys, env contract)"

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
