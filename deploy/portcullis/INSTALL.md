# Servitor → portcullis server: one-time install

Everything the servitor side needs, in order. The repo must be at **stage 5
commit or later** (the stage-5 commit message: `Stage 5: guest image +
standalone deploy + doctor (gate stack green)`). Reading order: this file,
then `deploy/README.md` (the contract) and `deploy/portcullis/RUNBOOK.md`
(day-2 ops).

**The shape:** one libvirt VM (2 disks + 1 NIC on the portcullis edge bridge),
provisioned once by `deploy/sync.sh`. The app authenticates nobody — the
only gate is the portcullis auth chain (Authelia 2FA) on the traefik route.

```
┌─ portcullis server ─────────────────────────────────────────────┐
│  traefik (docker "edge" net) ──br-<edge>──► servitor VM         │
│        :443 (2FA)                 VM_IP:8787  (nft: edge only)  │
│  host checkout /opt/servitor-repo ──ssh(deploy key)──► VM root  │
│        deploy/sync.sh · doctor.sh · update.sh                   │
└─────────────────────────────────────────────────────────────────┘
```

---

## 0. Preflight (decide these first)

```bash
# the portcullis edge subnet + its bridge device (the VM joins this segment)
sudo docker network inspect edge -f '{{range .IPAM.Config}}{{.Subnet}}{{end}}'
sudo docker network inspect edge -f '{{(index .Options "com.docker.network.bridge.name")}}'
# the server LAN subnet (ops SSH is admitted from it only)
ip -4 addr show
```

Pick `SERVITOR_VM_IP`: an unused address inside the edge subnet (the laptop
convention is `.15`; the gateway is the subnet's `.1` by build convention).

## 1. Host packages

```bash
sudo pacman -S --needed libvirt qemu nodejs npm git e2fsprogs
sudo systemctl enable --now libvirtd
```

- `libvirt` — the hypervisor (none on the server at first deploy).
- `qemu` — provides `qemu-img` (state disk) and the emulator.
- `nodejs` + `npm` — `deploy/sync.sh` stages the pi-web-access extension with
  host npm and reads its resolved version with `node -e` (same command the
  conformance gate uses).
- `e2fsprogs` — `mkfs.ext4` for the state disk.

## 2. Checkout + deploy key

```bash
sudo git clone git@github.com:ConnorPietrasik/servitor.git /opt/servitor-repo
cd /opt/servitor-repo
# VM root ssh: single-purpose key (NOT your laptop agent key). build.sh bakes
# the PUBLIC half into the image's /root/.ssh/authorized_keys.
mkdir -p ~/.srv && chmod 700 ~/.srv
ssh-keygen -t ed25519 -N '' -f ~/.srv/deploy_key
```

The path `/opt/servitor-repo` is the one `deploy/portcullis/panel-do/
servitor_actions.go` expects (`servitorRepoPath` const) — keep it.

## 3. The server `.env` (the contract; chmod 600)

```bash
sudo cp /opt/servitor-repo/.env.example /opt/servitor-repo/.env
sudo chmod 600 /opt/servitor-repo/.env
```

Set (everything else may stay at defaults):

```bash
# secrets (from the laptop .env or the secret store)
OMNISSIAH_API_KEY=<router bearer key>
EXA_API_KEY=<exa key>

# network (from §0)
SERVITOR_EDGE_SUBNET=<edge subnet, e.g. 172.28.0.0/20>
SERVITOR_OPS_SUBNET=<server LAN subnet>
SERVITOR_VM_IP=<chosen VM IP inside the edge subnet>
SERVITOR_PORT=8787
# host-side probe target (panel-do servitor-status curls this)
SERVITOR_BIND=<SERVITOR_VM_IP>

# guest DNS (no DHCP on the edge bridge; public resolvers ride the host NAT).
# QUOTED — multi-word values are sourced by sync.sh; an unquoted value makes
# bash execute the second token.
SERVITOR_DNS_UPSTREAM="1.1.1.1 9.9.9.9"

# version selections (must match the image you install)
SERVITOR_PI_VERSION=1.5.2
SERVITOR_PI_WEB_ACCESS_VERSION=0.47.0

# deploy-time intent (doctor asserts live against the router)
SERVITOR_INTENDED_MODEL_ID=qwen35
SERVITOR_INTENDED_CONTEXT_WINDOW=250000
```

## 4. The guest image

**Primary path — build on the server (designed path, ~20–40 min, needs
network):**

```bash
cd /opt/servitor-repo
make guest     # = sudo unshare -m deploy/guest/build.sh --env .env \
               #        --deploy-key ~/.srv/deploy_key.pub
```

The bake reads `SERVITOR_VM_IP` / `SERVITOR_EDGE_SUBNET` / `SERVITOR_OPS_SUBNET`
/ `SERVITOR_DNS_UPSTREAM` from this `.env`, so the built image is correct for
THIS server's network by construction. Output (in `deploy/guest/images/`):
`servitor-pi1.5.2-<date>.qcow2` + `.vmlinuz` + `.initramfs.img` +
`manifest.json` (sha256 recorded; the previous image is retained for the
update rollback).

**Fast path — reuse the laptop image (off-contract, ~10 min, see Appendix A).**

## 5. The state disk (the contract: ext4, LABEL=servitor-state)

Loop-free, exactly like the test harness:

```bash
sudo mkdir -p /opt/servitor-disk
sudo qemu-img create -f raw /opt/servitor-disk/servitor-state.raw 200G
sudo mkfs.ext4 -q -L servitor-state /opt/servitor-disk/servitor-state.raw
```

200 G is sparse on the host until written. The image's fstab mounts
`LABEL=servitor-state` at `/var/lib/servitor` with `nofail` (boot tolerates
its absence; the doctor fails loudly when it is missing — it must be present
for the app to work).

## 6. Define + start the VM

```bash
IMG=/opt/servitor-repo/deploy/guest/images
NAME=$($IMG/../images/manifest.json >/dev/null; \
  python3 -c "import json;print(json.load(open('$IMG/manifest.json'))['active'])")
# -> e.g. servitor-pi1.5.2-20261116

BRIDGE=$(sudo docker network inspect edge -f '{{(index .Options "com.docker.network.bridge.name")}}')

sed -e "s|{{KERNEL}}|$IMG/$NAME.vmlinuz|" \
    -e "s|{{INITRD}}|$IMG/$NAME.initramfs.img|" \
    -e "s|{{ROOT_QCOW2}}|$IMG/$NAME.qcow2|" \
    -e "s|{{STATE_QCOW2}}|/opt/servitor-disk/servitor-state.raw|" \
    -e "s|{{BRIDGE}}|$BRIDGE|" \
    /opt/servitor-repo/deploy/guest/servitor-domain.xml | sudo virsh define -

sudo virsh start servitor
sudo virsh console servitor   # watch the first boot (serial); Ctrl-] to leave
```

While waiting for sshd, verify from the host:

```bash
ping -c1 $SERVITOR_VM_IP
ssh -i ~/.srv/deploy_key -o BatchMode=yes root@$SERVITOR_VM_IP \
    'ip -4 addr show; ip route; systemctl is-active servitor'
```

Expected: the VM has `SERVITOR_VM_IP` up on its interface, a default route via
the subnet `.1`, and `servitor` active. (The service runs pre-provisioning:
it serves, but has no keys/extension yet — that is the design, not a defect.)

**Egress (the proven laptop lesson — the NAT masquerade alone is not enough
when ufw is enabled):**

```bash
sudo ufw route allow from <SERVITOR_EDGE_SUBNET>   # guest public egress
# then, from INSIDE the guest:
ssh -i ~/.srv/deploy_key root@$SERVITOR_VM_IP \
    'curl -sS -m 10 https://1.1.1.1/cdn-cgi/trace | head -3'
```

(Docker's default iptables already masquerade the edge bridge subnet; this
ufw line is what lets the traffic pass the host FORWARD chain. If the server
has no ufw, verify the curl only.)

## 7. First sync = provisioning (the whole deploy)

```bash
cd /opt/servitor-repo
deploy/sync.sh
```

The first sync is NOT a no-op by design. It pushes the app tree (when the sha
differs), stages the content-addressed config, pushes `lib/`, installs
pi-web-access 0.47.0 (host npm stage → self-contained dir on the guest),
re-injects the key slice into the guest's 0600 env file, mirrors any host
`/etc/hosts` pins (hairpin NAT — none on a clean server), self-heals the
edge gateway, writes the D8 update-pending flag, waits for the app's tick to
restart at idle, then runs the doctor.

**Success marker:** `sync: OK — shape deployed, doctor GREEN` (exit 0).
Exit 3 = deploy landed but doctor red (the `update.sh` path owns automatic
rollback); exit 1 = pre-deploy failure, nothing landed.

## 8. Merge into portcullis (the edge side)

1. **Route:** copy `deploy/portcullis/traefik-dynamic/servitor.yml` into
   portcullis's `compose/traefik/dynamic/`, substituting
   `VM_INGRESS_IP=<SERVITOR_VM_IP>` and `SERVITOR_PORT=8787`. `make update`
   in the portcullis repo (gated by its doctor + snapshot/auto-rollback).
   No new UFW rule: the route rides the existing 443; the VM's own firewall
   admits 8787 from the edge subnet only. Authelia's `*.pietrasik.top`
   two-factor wildcard makes it private by default.
2. **Panel actions:** merge `deploy/portcullis/panel-do/servitor_actions.go`
   into portcullis's Go ACTIONS registry (per the portcullis RUNBOOK "Add a
   privileged action"): `servitor-status`, `servitor-vm-restart`,
   `servitor-sync`, `servitor-doctor`. Keep `servitorRepoPath =
   "/opt/servitor-repo"`.

## 9. Verify (the gate list)

```bash
virsh domstate servitor                      # running
curl -s http://$SERVITOR_VM_IP:8787/healthz  # ok (from the host)
deploy/doctor.sh                             # RESULT: GREEN
```

Then, from a phone/LAN browser: `https://servitor.pietrasik.top` → Authelia
2FA → the chat UI → send one live prompt → a real assistant answer comes back
(this exercises VM → router → model end-to-end; it is THE integration gate).
Finally: panel → `servitor-status` (vm: running + healthz ok) and
`servitor-doctor` (green).

---

## Appendix A — fast path: reuse the laptop image (off-contract)

The laptop's `servitor-pi1.5.2-20261116.qcow2` is the proven artifact (guest
suite green), but it bakes **10.42.0.15/24 + the laptop's deploy key**. If the
server's edge subnet differs (it will, unless the server edge is literally
10.42.0.0/24), patch three things before first boot. Takes ~10 min; the cost
is that the manifest sha no longer matches the file (the manifest is
harness-only — `virsh` never reads it; a later `make guest` on the server
supersedes it cleanly).

```bash
# 1. from the laptop (1.5 G + 35 M):
scp deploy/guest/images/servitor-pi1.5.2-20261116.{qcow2,vmlinuz,initramfs.img} \
    server:/opt/servitor-disk/
scp deploy/guest/images/manifest.json server:/opt/servitor-disk/

# 2. on the server — nbd-patch (same mechanism the image was patched with):
sudo modprobe nbd
sudo qemu-nbd -c /dev/nbd0 /opt/servitor-disk/servitor-pi1.5.2-20261116.qcow2
sudo vi /dev/nbd0/etc/systemd/network/10-servitor.network
#    [Network]
#    Address=<SERVITOR_VM_IP>/<edge prefix>
#    Gateway=<edge subnet .1>
sudo vi /dev/servitor...   # the 0600 env file:
sudo vi /dev/nbd0/etc/servitor/servitor.env
#    SERVITOR_BIND=<SERVITOR_VM_IP>
#    SERVITOR_EDGE_SUBNET=<edge subnet>
#    SERVITOR_OPS_SUBNET=<LAN subnet>
# 3. ssh access: EITHER copy the laptop's keypair to the server
#    (scp ~/.srv/deploy_key{,.pub} server:~/.srv/ — then skip this step),
#    OR add the server's public key:
sudo bash -c 'cat ~/.srv/deploy_key.pub >> /dev/nbd0/root/.ssh/authorized_keys'
sudo sync && sudo qemu-nbd -d /dev/nbd0
```

Then continue at §5 (state disk) and §6 (point `{{ROOT_QCOW2}}` at
`/opt/servitor-disk/servitor-pi1.5.2-20261116.qcow2`, kernel/initrd likewise).

## Appendix B — what the image already contains (do NOT re-provision)

slots slot0–16 (uid 9000–9016, 0700 homes; slot0 = the doctor probe slot,
outside the app pool), the app tree + venv at `/opt/servitor/app`, the toolkit
at `/opt/toolkit`, pi 1.5.2 at the pinned global npm path, nftables
default-deny (edge-only 8787, LAN-only 22), resolved with the public
upstream, systemd units (`servitor`, `servitor-firewall`), the 0600 env file
with the non-key deploy defaults, and the deploy key in root's
`authorized_keys`. **Not** in the image, by design: the API keys, the
pi-web-access extension, and any state volume — the first sync + the state
disk provide those.

## Appendix C — failure modes seen on the laptop (and their cures)

| symptom | cause | cure |
|---|---|---|
| VM up, no default route | gateway line wrong in `10-servitor.network` (the 5-octet bug in pre-fix builds) | sync.sh self-heals the Gateway line on next sync; or fix by hand + `systemctl restart systemd-networkd` |
| public names don't resolve in the VM | resolved disabled / stale resolv.conf | image patch #4; `systemctl enable --now systemd-resolved`, check `resolvectl status` |
| guest → router prompts return empty turns | hairpin NAT (router's public IP dead from behind the host NAT) | pin `*.pietrasik.top` → a routable address in the guest's `/etc/hosts` (sync.sh mirrors the host's panel-hosts block); on the server the edge-internal router address is usually routable directly — verify with a guest-side curl before the first sync |
| sync env file grows unbounded | (fixed) old rewriter doubled blank lines | the current sync.sh normalizes + self-heals a bloated file |
| service crash-loop `ModuleNotFoundError: app` | unit pointed at `app.api.main` (no such module) | the template is fixed (`-m app.main`); rebuild or sed the unit |
