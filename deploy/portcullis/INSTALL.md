# Servitor → portcullis server: one-time install

Everything the servitor side needs, in order. The checkout must contain
`deploy/guest/build.sh` and this file (the image-build tooling). Reading
order: this file, then `deploy/README.md` (the contract) and
`deploy/portcullis/RUNBOOK.md` (day-2 ops).

**The shape:** one libvirt VM (2 disks + 1 NIC on the portcullis edge bridge),
provisioned once by `deploy/sync.sh`. Two gates, both on the traefik
route: the portcullis auth chain (Authelia 2FA) authenticates the user,
and the `servitor-edge` middleware injects the edge credential
(`X-Servitor-Edge-Key`) which the app re-verifies on every route — the
guest firewall admits the port from the WHOLE edge subnet, so the
credential is what stops a neighbor container from driving the agent
unauthed.

```
┌─ portcullis server ─────────────────────────────────────────────┐
│  traefik (docker "edge" net) ──br-<edge>──► servitor VM         │
│        :443 (2FA + edge key)  VM_IP:8787  (nft: edge subnet;    │
│                                app key-gated)                   │
│  host checkout /opt/servitor-repo ──ssh(deploy key)──► VM root  │
│        deploy/sync.sh · doctor.sh · update.sh                   │
└─────────────────────────────────────────────────────────────────┘
```

---

## 0. Preflight (decide these first)

```bash
# the portcullis edge subnet + its bridge device (the VM joins this segment)
sudo docker network inspect edge -f '{{range .IPAM.Config}}{{.Subnet}}{{end}}'
# bridge device = br-<first 12 hex of the network ID> (the Options map does
# not always carry com.docker.network.bridge.name — empty output means you
# would define a domain with an empty bridge; cross-check with `ip -br link`)
br-$(sudo docker network inspect edge -f '{{.Id}}' | cut -c1-12)
# the edge network's gateway = the host's bridge IP (the host's OWN ssh to
# the VM sources it — the guest admits 22 from exactly this /32)
sudo docker network inspect edge -f '{{(index .IPAM.Config 0).Gateway}}'
# the server LAN subnet (the ops/workstation SSH path)
ip -4 addr show
```

Pick `SERVITOR_VM_IP`: an unused address inside the edge subnet (the
gateway is the subnet's `.1` by build convention).

## 1. Host packages

```bash
sudo pacman -S --needed libvirt qemu nodejs npm git e2fsprogs rsync zstd curl
sudo systemctl enable --now libvirtd
```

- `libvirt` — the hypervisor (none on the server at first deploy).
- `qemu` — provides `qemu-img` (state disk) and the emulator.
- `nodejs` + `npm` — `deploy/sync.sh` stages the pi-web-access extension with
  host npm and reads its resolved version with `node -e`, and builds the chat UI (vite) for the image and
  for `ui/`-changing syncs.
- `e2fsprogs` — `mkfs.ext4` for the state disk.
- `rsync` + `zstd` + `curl` — `build.sh` needs them (rsync for the app/ui
  trees into the chroot, `tar --zstd` for the pinned base, the base
  download). CachyOS usually has all three; the line is idempotent.

## 2. Checkout + deploy key

```bash
sudo git clone git@github.com:ConnorPietrasik/servitor.git /opt/servitor-repo
cd /opt/servitor-repo
# VM root ssh: single-purpose key (NOT your workstation agent key). build.sh
# bakes the PUBLIC half into the image's /root/.ssh/authorized_keys.
#
# The keypair lives in ROOT's home (/root/.srv): the panel-do actions run
# with a scrubbed env (no $HOME), so they pass this exact path via
# --deploy-key. Keep the key there — moving it breaks the panel actions,
# not just the scripts.
mkdir -p /root/.srv && chmod 700 /root/.srv
ssh-keygen -t ed25519 -N '' -f /root/.srv/deploy_key
```

The path `/opt/servitor-repo` is the one the merged panel-do actions expect
(`servitorRepoPath` const in portcullis's `panel-do/main.go`) — keep it.

## 3. The server `.env` (the contract; chmod 600)

```bash
sudo cp /opt/servitor-repo/.env.example /opt/servitor-repo/.env
sudo chmod 600 /opt/servitor-repo/.env
```

Set (everything else may stay at defaults):

```bash
# secrets (from the secret store)
OMNISSIAH_API_KEY=<router bearer key>
EXA_API_KEY=<exa key>
# the edge credential: generate ONE value and set it in BOTH
# places — here (the guest's copy: the image build bakes it in, the sync's
# key slice re-injects it) and portcullis's .env (the servitor-edge
# middleware's source). A mismatch 403s the whole UI; portcullis `make
# doctor` cross-checks the two.
SERVITOR_EDGE_KEY=<openssl rand -hex 32>

# network (from §0)
SERVITOR_EDGE_SUBNET=<edge subnet, e.g. 172.28.0.0/20>
# the edge network's gateway (from §0): the host's OWN ssh to the VM
# sources this address (the kernel picks the output-interface address for
# the connected route) — the guest admits 22 from exactly this /32
SERVITOR_HOST_BRIDGE_IP=<edge gateway, e.g. 172.28.0.1>
# ops subnet for the workstation SSH path. The host's OWN ssh to the VM
# does NOT use this: it sources the edge-bridge IP above. This one is the
# LAN subnet for LAN-workstation access (host forward rule, no SNAT — the
# workstation keeps its LAN source address; RUNBOOK §Daily ops).
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
SERVITOR_PI_WEB_ACCESS_VERSION=0.48.0
```

## 4. The guest image

Build on the server (~20–40 min, needs network):

```bash
cd /opt/servitor-repo
sudo make guest   # = unshare -m deploy/guest/build.sh --env .env
                  #        --deploy-key /root/.srv/deploy_key.pub
                  # (the target runs as the invoking user — the build needs
                  # root, so the sudo is on `make`, and sudo's HOME=/root is
                  # where the deploy key's pub half lives by §2)
```

The bake reads `SERVITOR_VM_IP` / `SERVITOR_EDGE_SUBNET` /
`SERVITOR_HOST_BRIDGE_IP` / `SERVITOR_OPS_SUBNET` / `SERVITOR_DNS_UPSTREAM`
from this `.env`, so the built image is correct for
THIS server's network by construction. Output (in `deploy/guest/images/`):
`servitor-pi1.5.2-<date>.qcow2` + `.vmlinuz` + `.initramfs.img` +
`manifest.json` (sha256 recorded; the previous image — the update rollback
target — is retained, older images are pruned).

## 5. The state disk (the contract: a local disk, LABEL=servitor-state — ext4 is what you create, any local non-overlay fs works)

Loop-free (a raw file directly, no loop device — loop churn correlates
with host flakiness):

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
# the manifest's `active` INCLUDES the .qcow2 extension — the boot files are
# named <stem>.vmlinuz / <stem>.initramfs.img, so strip it for the sed below
NAME=$(python3 -c "import json;print(json.load(open('$IMG/manifest.json'))['active'])" | sed 's/\.qcow2$//')
# -> e.g. servitor-pi1.5.2-20261116

# the bridge device is br-<first 12 hex of the network ID> — the Options map
# does not always carry com.docker.network.bridge.name (empty output = the
# sed below would define a domain with bridge=''): derive from .Id and
# cross-check against `ip -br link`
BRIDGE="br-$(sudo docker network inspect edge -f '{{.Id}}' | cut -c1-12)"

sed -e "s|{{KERNEL}}|$IMG/$NAME.vmlinuz|" \
    -e "s|{{INITRD}}|$IMG/$NAME.initramfs.img|" \
    -e "s|{{ROOT_QCOW2}}|$IMG/$NAME.qcow2|" \
    -e "s|{{STATE_QCOW2}}|/opt/servitor-disk/servitor-state.raw|" \
    -e "s|{{BRIDGE}}|$BRIDGE|" \
    # sizing defaults (24 vCPU / 64 GB ceiling / 16 GB initial) — change
    # these three if scaling down for a weaker box (template header, Sizing)
    -e "s|{{VCPU}}|24|" \
    -e "s|{{RAM_MAX}}|64|" \
    -e "s|{{RAM_CURRENT}}|16|" \
    /opt/servitor-repo/deploy/guest/servitor-domain.xml > /tmp/servitor-domain.xml
# define from the file (not stdin — some libvirt builds reject `virsh define -`);
# the file is yours to grep before defining
grep -E '<kernel|<initrd|<vcpu|<memory|bridge=|source file' /tmp/servitor-domain.xml
sudo virsh define /tmp/servitor-domain.xml

sudo virsh start servitor
sudo virsh console servitor   # watch the first boot (serial); Ctrl-] to leave

# Boot persistence: `virsh autostart` is NOT the mechanism here — it fires
# when libvirtd initializes, BEFORE docker has created the edge bridge the
# domain is wired to (libvirt does not create external bridges), so the
# early start attempt fails and the domain sits "shut off" after a host
# reboot. The host-side unit owns boot-start instead: ordered after
# docker.service, idempotent, loud on failure (unit file header carries the
# full contract). The domain template ships WITHOUT autostart (a
# stopped VM is otherwise just a WARN in the portcullis doctor):
sudo cp /opt/servitor-repo/deploy/servitor-vm-boot.service \
    /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable servitor-vm-boot
```

While waiting for sshd, verify from the host (as root — the key is in root's
home; `sudo` also gives the manual scripts their `$HOME=/root`, which is how
they find the key and the root-owned `.env` by default):

```bash
ping -c1 $SERVITOR_VM_IP
sudo ssh -i /root/.srv/deploy_key -o BatchMode=yes root@$SERVITOR_VM_IP \
    'ip -4 addr show; ip route; systemctl is-active servitor'
```

Expected: the VM has `SERVITOR_VM_IP` up on its interface, a default route via
the subnet `.1`, and `servitor` active. (The service runs pre-provisioning:
it serves, but has no keys/extension yet — that is the design, not a defect.)

**Egress (the NAT masquerade alone is not enough when ufw is enabled):**

```bash
sudo ufw route allow from <SERVITOR_VM_IP>/32 comment 'servitor VM egress'
# (the VM's exact address; portcullis `make doctor` checks it against the
# route file's upstream IP)
# then, from INSIDE the guest:
ssh -i ~/.srv/deploy_key root@$SERVITOR_VM_IP \
    'curl -sS -m 10 https://1.1.1.1/cdn-cgi/trace | head -3'
```

(Docker's default iptables already masquerade the edge bridge subnet; this
ufw line is what lets the traffic pass the host FORWARD chain. If the server
has no ufw, verify the curl only. The curl runs from INSIDE the guest as the
guest's own uid — no key needed.)

## 7. First sync = provisioning (the whole deploy)

```bash
cd /opt/servitor-repo
sudo deploy/sync.sh     # or the panel's servitor-sync action, once §8 is done
```

Run it as root (or via the panel action): the `.env` and the deploy key are
root-owned, and root's `$HOME` is exactly where both live by default — a
non-root run would need `--deploy-key /root/.srv/deploy_key` plus read access
to the `.env`, which it does not have.

The first sync is NOT a no-op by design. It pushes the app tree (when the
sha differs), the chat UI's built dist, stages the content-addressed
config, pushes `lib/`, installs pi-web-access at the `.env` selection,
re-injects the key slice into the guest's 0600 env file, mirrors any host
`/etc/hosts` pins, self-heals the edge gateway, writes the update-pending
flag, waits for the app's tick to restart at idle, then runs the doctor
(including one live run that makes the agent search the web — a real
model call, the VM → router → model and public-egress proof; that is why
the panel's `servitor-doctor`/`servitor-sync` budget is 10 min).

**Success marker:** `sync: OK — shape deployed, doctor GREEN` (exit 0).
On the first sync — and any shape change that triggers the live doctor —
the realistic outcome is instead `sync: OK — update-WAITING` (exit 0):
the staged update takes TWO in-guest doctor runs (pre-restart +
post-restart cert, each with a 300 s live-run cap) plus the restart —
longer than the sync's 150 s staging wait, so the sync exits and the app's
tick finishes the job. Certify with `deploy/doctor.sh` (§9) once it
settles.
Exit 3 = deploy landed but doctor red (the `update.sh` path owns
automatic rollback); exit 1 = pre-deploy failure, nothing landed.

## 8. Merge into portcullis (the edge side)

The portcullis side is **already in the portcullis repo** (already merged):
the four `servitor-*` panel-do actions (with the `checkManagedScript`
validation of this checkout before every exec), the doctor's tri-state
servitor section (absent = skip; no libvirt / no domain / stale binary =
fail; VM stopped or endpoint dead = warn — planned downtime never red-lights
the update's precheck), the RUNBOOK section, and the hardening/troubleshooting
entries. If the server's portcullis clone pre-dates that merge, pull it first.

1. **Route (the only file to add):** copy
   `deploy/portcullis/traefik-dynamic/servitor.yml` into the portcullis
   clone's `compose/traefik/dynamic/`, substituting the VM ingress IP +
   port 8787 (same value as `SERVITOR_VM_IP` in this repo's `.env` — the
   route file is the second place that value lives; keep them in sync).
   The route's second middleware (`servitor-edge@file`, the edge
   credential) lives in portcullis's
   `compose/traefik/dynamic/middlewares.yml`, its value sedded from
   portcullis's `.env` (`SERVITOR_EDGE_KEY` — the one you set in §3);
   if the clone pre-dates it, pull portcullis first. The portcullis
   deploy prunes deployed route files absent from its repo, so
   the file must be committed in the clone, not hand-placed in /opt/panel.
   **Ordering trap:** do not commit the route BEFORE the domain exists —
   the portcullis doctor's servitor section then reads `bad` (route defined,
   no domain) and the very `make update` that ships the route fails its
   gate and auto-rolls-back. §6 (define + start) comes first on purpose.
2. **Panel-do binary:** `make panel-do` in the portcullis clone (rebuilds,
   reinstalls, runs the negative-test suite). Only needed if the installed
   binary pre-dates the servitor actions — `make doctor` fails explicitly
   (`registry missing servitor-status`) when it does.
3. **No new UFW rule for INGRESS** — the route rides the existing 443; the
   VM's own firewall admits 8787 from the edge subnet only. The egress line
   from §6 is the one host firewall change; doctor checks it.
4. `make update` in the portcullis clone (gated by its doctor + snapshot /
   auto-rollback). The server re-pins `servitor.pietrasik.top` itself as
   part of the update; client machines re-run their `tools/panel-hosts.sh`.
   The app binds `SERVITOR_PORT` (8787) on the VM; the route targets that
   address:port — that is the whole integration.

## 9. Verify (the gate list)

```bash
virsh domstate servitor                      # running
curl -s -H "X-Servitor-Edge-Key: $SERVITOR_EDGE_KEY" \
     http://$SERVITOR_VM_IP:8787/healthz     # ok (from the host)
curl -s -o /dev/null -w '%{http_code}' \
     http://$SERVITOR_VM_IP:8787/healthz     # 403 (keyless — the gate is on)
deploy/doctor.sh                             # RESULT: GREEN (incl. both key probes)
```

Then, from a phone/LAN browser: `https://servitor.pietrasik.top` → Authelia
2FA → the chat UI → send one live prompt → a real assistant answer comes back
(this exercises VM → router → model end-to-end; it is THE integration gate —
and it is now automated: the doctor's own `spawn.live_prompt` / `spawn.live_search`
checks do exactly this, key-gated). Finally: panel →
`servitor-status` (vm: running + healthz ok) and
`servitor-doctor` (green). Portcullis `make doctor` now reports its own
servitor section (VM running, endpoint answering, egress rule present,
actions in the registry) — a stopped VM shows as WARN there by design.

---

## 10. Host prerequisites for the on-demand root suites

`make update` needs none of these — a versioned deploy is certified by the
post-deploy doctor (red => automatic rollback, image included). These are
for the on-demand root suites — `make test` (unit + conformance) and
`make sandbox-smoke` — which run **host-side** (root); e.g. to run the
conformance suite before accepting an out-of-major bump. None of them was
created in §1–§9:

```bash
cd /opt/servitor-repo
sudo make venv                                        # the suites run the repo venv
sudo useradd -u 9001 -m -s /usr/sbin/nologin slot1    # conformance's spawn uid
sudo useradd -u 9002 -m -s /usr/sbin/nologin slot2    # sandbox-smoke's spawn uid
```

The guest image creates its own slot0–16; the HOST suites need their own —
the conformance harness setuid-spawns a real pi as `slot1` and keeps its
scratch under `/home/slot1` (the unit half stands in passwd — no real
users needed). Without `slot1` the conformance half of
`make test` fails loud (never silently green) — the fix is a line in
the block above, not a debug session.

---

## Appendix A — what the image already contains (do NOT re-provision)

slots slot0–16 (uid 9000–9016, 0700 homes; slot0 = the doctor probe slot,
outside the app pool), the app tree + venv at `/opt/servitor/app`, the chat
UI's built dist at `/opt/servitor/app/ui/dist` (host vite build at image
time; sync.sh refreshes it for `ui/`-only changes), the toolkit at
`/opt/toolkit`, pi 1.5.2 at the pinned global npm path, nftables
default-deny (edge-subnet 8787, LAN-only 22 — and the app is
key-gated on every route on top), resolved with the public
upstream, systemd units (`servitor`, `servitor-firewall`), the 0600 env
file with the deploy defaults INCLUDING the baked edge key (first boot is
already gated; rotation re-injects via the sync's key slice), and the
deploy key in root's `authorized_keys`. **Not** in the image, by
design: the model/search keys, the pi-web-access extension, and any state
volume — the first sync + the state disk provide those.
