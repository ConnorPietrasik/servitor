# Servitor → portcullis server: one-time install

Everything the servitor side needs, in order. The checkout must contain
`deploy/guest/build.sh` and this file (the image-build tooling). Reading
order: this file, then `deploy/README.md` (the contract) and
`deploy/portcullis/RUNBOOK.md` (day-2 ops).

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
# the edge network's gateway = the host's bridge IP (the host's OWN ssh to
# the VM sources it — the guest admits 22 from exactly this /32)
sudo docker network inspect edge -f '{{(index .IPAM.Config 0).Gateway}}'
# the server LAN subnet (the ops/workstation SSH path)
ip -4 addr show
```

Pick `SERVITOR_VM_IP`: an unused address inside the edge subnet (the laptop
convention is `.15`; the gateway is the subnet's `.1` by build convention).

## 1. Host packages

```bash
sudo pacman -S --needed libvirt qemu nodejs npm git e2fsprogs rsync zstd curl
sudo systemctl enable --now libvirtd
```

- `libvirt` — the hypervisor (none on the server at first deploy).
- `qemu` — provides `qemu-img` (state disk) and the emulator.
- `nodejs` + `npm` — `deploy/sync.sh` stages the pi-web-access extension with
  host npm and reads its resolved version with `node -e` (same command the
  conformance gate uses), and builds the chat UI (vite) for the image and
  for `ui/`-changing syncs.
- `e2fsprogs` — `mkfs.ext4` for the state disk.
- `rsync` + `zstd` + `curl` — `build.sh` needs them (rsync for the app/ui
  trees into the chroot, `tar --zstd` for the pinned base, the base
  download). CachyOS usually has all three; the line is idempotent.

## 2. Checkout + deploy key

```bash
sudo git clone git@github.com:ConnorPietrasik/servitor.git /opt/servitor-repo
cd /opt/servitor-repo
# VM root ssh: single-purpose key (NOT your laptop agent key). build.sh bakes
# the PUBLIC half into the image's /root/.ssh/authorized_keys.
#
# The keypair lives in ROOT's home (/root/.srv): the panel-do actions run with
# a scrubbed env (no $HOME), so they pass this exact path via --deploy-key
# (the scripts' ${HOME:-/root} default resolves the same path as a backstop).
# Keep the key there — moving it breaks the panel actions, not just the scripts.
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
# secrets (from the laptop .env or the secret store)
OMNISSIAH_API_KEY=<router bearer key>
EXA_API_KEY=<exa key>

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
NAME=$(python3 -c "import json;print(json.load(open('$IMG/manifest.json'))['active'])")
# -> e.g. servitor-pi1.5.2-20261116

BRIDGE=$(sudo docker network inspect edge -f '{{(index .Options "com.docker.network.bridge.name")}}')

sed -e "s|{{KERNEL}}|$IMG/$NAME.vmlinuz|" \
    -e "s|{{INITRD}}|$IMG/$NAME.initramfs.img|" \
    -e "s|{{ROOT_QCOW2}}|$IMG/$NAME.qcow2|" \
    -e "s|{{STATE_QCOW2}}|/opt/servitor-disk/servitor-state.raw|" \
    -e "s|{{BRIDGE}}|$BRIDGE|" \
    # sizing defaults (24 vCPU / 30 GB ceiling / 16 GB initial) — change
    # these three if scaling down for a weaker box (template header, Sizing)
    -e "s|{{VCPU}}|24|" \
    -e "s|{{RAM_MAX}}|30|" \
    -e "s|{{RAM_CURRENT}}|16|" \
    /opt/servitor-repo/deploy/guest/servitor-domain.xml | sudo virsh define -

sudo virsh start servitor
sudo virsh console servitor   # watch the first boot (serial); Ctrl-] to leave
sudo virsh autostart servitor  # survive a host reboot — the domain template
                               # ships WITHOUT autostart (a stopped VM is
                               # otherwise just a WARN), and the update's
                               # image swap preserves the flag once set
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

The first sync is NOT a no-op by design. It pushes the app tree (when the sha
differs), the chat UI's built dist (built host-side when the `ui/` source
moved since the last build), stages the content-addressed config, pushes
`lib/`, installs pi-web-access 0.48.0 (host npm stage → self-contained dir
on the guest), re-injects the key slice into the guest's 0600 env file,
mirrors any host `/etc/hosts` pins (hairpin NAT — none on a clean server),
self-heals the edge gateway, writes the update-pending flag, waits for the
app's tick to restart at idle, then runs the doctor (now including one
live run that makes the agent search the web — a real model call, the
VM → router → model and public-egress proof; that is why the panel's
`servitor-doctor`/`servitor-sync` budget is 10 min).

**Success marker:** `sync: OK — shape deployed, doctor GREEN` (exit 0).
On the first sync — and any shape change that triggers the live doctor —
the realistic outcome is instead `sync: OK — update-WAITING` (exit 0): the
staged update takes TWO in-guest doctor runs (pre-restart cert +
post-restart cert, each with a 300 s live-run cap) plus the restart itself
— longer than the sync's 150 s staging wait, so the sync exits and the
app's tick finishes the job. Certify with `deploy/doctor.sh` (§9) once it
settles.
Exit 3 = deploy landed but doctor red (the `update.sh` path owns automatic
rollback); exit 1 = pre-deploy failure, nothing landed.

## 8. Merge into portcullis (the edge side)

The portcullis side is **already in the portcullis repo** (already merged):
the four `servitor-*` panel-do actions (with the `checkManagedScript`
validation of this checkout before every exec), the doctor's tri-state
servitor section (absent = skip; no libvirt / no domain / stale binary =
fail; VM stopped or endpoint dead = warn — planned downtime never red-lights
the update gate), the RUNBOOK section, and the hardening/troubleshooting
entries. If the server's portcullis clone pre-dates that merge, pull it first.

1. **Route (the only file to add):** copy
   `deploy/portcullis/traefik-dynamic/servitor.yml` into the portcullis
   clone's `compose/traefik/dynamic/`, substituting the VM ingress IP +
   port 8787 (same value as `SERVITOR_VM_IP` in this repo's `.env` — the
   route file is the second place that value lives; keep them in sync). The
   portcullis deploy prunes deployed route files absent from its repo, so
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
   The compose contract publishes exactly `${SERVITOR_PORT}` (8787); the
   route targets the VM's published port — that is the whole integration.

## 9. Verify (the gate list)

```bash
virsh domstate servitor                      # running
curl -s http://$SERVITOR_VM_IP:8787/healthz  # ok (from the host)
deploy/doctor.sh                             # RESULT: GREEN
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

## 10. Host prerequisites for the first update (the update gate)

`deploy/update.sh` gates every versioned deploy with the conformance suite,
and that suite runs **host-side** (root) — so the server's checkout needs
what the suite spawns into, none of which §1–§9 created:

```bash
cd /opt/servitor-repo
sudo make venv                                        # the gate runs the repo venv
sudo useradd -u 9001 -m -s /usr/sbin/nologin slot1    # the gate's spawn uid
# also (for `make test` / `make sandbox-smoke`, not the gate itself):
sudo useradd -u 9002 -m -s /usr/sbin/nologin slot2
```

The guest image creates its own slot0–16; the HOST gate needs its own — the
conformance harness setuid-spawns a real pi as `slot1` and keeps its scratch
under `/home/slot1`. `slot2` is only wanted if you also run the wider suites
on the server: `make test`'s unit half is in-process (no spawn) but
`test/unit/test_slots.py` still resolves BOTH `slot1` and `slot2` from passwd,
and `make sandbox-smoke` spawns as `slot2`. Without `slot1` the gate is RED
on the first versioned update: nothing deploys. That is the gate working as
intended, but the fix is two commands, not a debug session.

---

## Appendix A — what the image already contains (do NOT re-provision)

slots slot0–16 (uid 9000–9016, 0700 homes; slot0 = the doctor probe slot,
outside the app pool), the app tree + venv at `/opt/servitor/app`, the chat
UI's built dist at `/opt/servitor/app/ui/dist` (host vite build at image
time; sync.sh refreshes it for `ui/`-only changes), the toolkit at
`/opt/toolkit`, pi 1.5.2 at the pinned global npm path, nftables
default-deny (edge-only 8787, LAN-only 22), resolved with the public
upstream, systemd units (`servitor`, `servitor-firewall`), the 0600 env file
with the non-key deploy defaults, and the deploy key in root's
`authorized_keys`. **Not** in the image, by design: the API keys, the
pi-web-access extension, and any state volume — the first sync + the state
disk provide those.

## Appendix B — failure modes seen on the laptop (and their cures)

| symptom | cause | cure |
|---|---|---|
| VM up, no default route | gateway line wrong in `10-servitor.network` (the 5-octet bug in pre-fix builds) | sync.sh self-heals the Gateway line on next sync; or fix by hand + `systemctl restart systemd-networkd` |
| public names don't resolve in the VM | resolved disabled / stale resolv.conf | image patch #4; `systemctl enable --now systemd-resolved`, check `resolvectl status` |
| guest → router prompts return empty turns | hairpin NAT (router's public IP dead from behind the host NAT) | pin `*.pietrasik.top` → a routable address in the guest's `/etc/hosts` (sync.sh mirrors the host's panel-hosts block); on the server the edge-internal router address is usually routable directly — verify with a guest-side curl before the first sync |
| sync env file grows unbounded | (fixed) old rewriter doubled blank lines | the current sync.sh normalizes + self-heals a bloated file |
| service crash-loop `ModuleNotFoundError: app` | unit pointed at `app.api.main` (no such module) | the template is fixed (`-m app.main`); rebuild or sed the unit |
| panel `servitor-sync`/`-doctor` → "deploy key not found" | the key is not at `/root/.srv/deploy_key` — panel-do's scrubbed env has no `$HOME`, so it passes the path explicitly via `--deploy-key` (the scripts' `${HOME:-/root}` default resolves the same path as a backstop) | keypair at `/root/.srv/deploy_key` (0600, root), per §2; the public half must also be the one baked into the image |
| server can't ssh the VM (first sync dies/times out at ssh, doctor can't reach the guest) | the host's ssh to the VM sources the host's EDGE BRIDGE IP; the guest admits 22 from `SERVITOR_HOST_BRIDGE_IP`/32 + the ops subnet — a wrong or stale bridge IP in the guest's 0600 env locks the host out of its own VM | nbd-patch `/etc/servitor/servitor.env` — `SERVITOR_HOST_BRIDGE_IP` — and `systemctl restart servitor-firewall`, or rebuild the image (§4) |
| panel `servitor-sync`/`-doctor` → "group/other-writable" / "owned by the panel user" | `/opt/servitor-repo` (or a directory above it) lost its modes/owner — the `checkManagedScript` anti-swap gate refuses to exec | `chmod go-w` the offending path (the gate names it); never `chown panel` the checkout |
| VM up, no public egress (search/model calls die) | missing host egress rule — the VM is not a docker container, so docker's FORWARD rules never see its traffic | `sudo ufw route allow from <edge subnet>` (INSTALL.md §6; portcullis `make doctor` checks it) |
