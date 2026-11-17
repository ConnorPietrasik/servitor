# Servitor — runbook excerpt (day-2 ops + drills)

Full operational reference for the servitor VM. The one-time server install
lives in `deploy/portcullis/INSTALL.md`; the portcullis-side canon (route,
actions, doctor checks, host firewall) is the portcullis RUNBOOK section
"Servitor (agent VM)". The short version: the VM is provisioned once; day-2
control is the four panel actions (`servitor-status`, `servitor-vm-restart`,
`servitor-sync`, `servitor-doctor`); version bumps are `deploy/update.sh`
from the host checkout (SSH, gated, auto-rollback); config edits are
`deploy/sync.sh` or the panel's `servitor-sync` (fast path, never blocks on
a run). Naming: `servitorctl` is the GUEST-side command (the app's modules,
run inside the VM as `python -m app.<module>`); host-side, the entry points
are the `deploy/*.sh` scripts — there is no host `servitorctl`.

## Provisioning (once)

1. Host: install libvirt/KVM + the deploy key + the one egress ufw rule —
   INSTALL.md §1–§2 and §6 (none of it exists on a fresh box).
2. Host: `make guest` in a servitor checkout — builds the qcow2 (name =
   pi version + date; the previous image is retained for rollback). The VM's
   edge IP (`SERVITOR_VM_IP` in the host `.env`) is CHOSEN BEFORE the build
   — it is baked into the image.
3. Host: `virsh define` + `virsh start` with the domain definition from
   `deploy/guest/servitor-domain.xml` (24 vCPU no quota, balloon, ~200 GB
   state disk, NIC on the portcullis EDGE bridge, no GPU) + the state disk
   (ext4, LABEL=servitor-state). Watch the serial console on first boot.
4. Guest: the image ships with slots 1-16, the toolkit, the app, nftables
   applied (8787 from the edge subnet only, 22 from the LAN subnet, default
   deny), systemd-resolved with a public upstream.
5. Host: `sudo deploy/sync.sh` — provisioning (config + keys + extension +
   doctor). Success marker: "sync: OK — shape deployed, doctor GREEN".
6. Portcullis: commit the route (this repo's
   `deploy/portcullis/traefik-dynamic/servitor.yml`, VM IP + 8787 substituted)
   into the portcullis clone's `compose/traefik/dynamic/`; `make panel-do`
   (only if the installed binary pre-dates the servitor actions); `make
   update` there.
7. Verify: browser → `servitor.pietrasik.top` (2FA) → chat works,
   `servitor-doctor` green, portcullis `make doctor` servitor section green.

## Daily operations

| task | how |
|---|---|
| change prompts/skills/lib | edit `agent-config/` / `lib/` in the host checkout, panel `servitor-sync` (or `sudo deploy/sync.sh`) |
| bump pi / pi-web-access | edit `.env` specs, `sudo deploy/update.sh` (SSH — long, network-bound; the conformance suite gates the resolved versions host-side before anything touches the guest) |
| rotate keys | host `.env` → `sudo deploy/sync.sh` (re-injects the key slice into the guest's 0600 env file, sets the restart flag) — the portcullis stack's copy of the key floats with its own `make update` as usual |
| ssh into the VM | from the server: `sudo ssh -i /root/.srv/deploy_key root@$SERVITOR_VM_IP`. From a LAN workstation additionally needs a host forward rule (`sudo ufw route allow from <LAN subnet> to $SERVITOR_VM_IP port 22`) — not added by default; the designed flows (panel + server) don't need it |
| release a session | UI close, GC timer (idle >14 d, non-stable), slot/disk pressure — all through the single `release()` (sweep by owner before the slot is reused; sweep-fail ⇒ quarantined, visible, retryable) |
| back up `lib/` | host-side git pull (no git credentials in the VM, invariant 12) |

## Drills

- **app-crash restart**: `kill -9` the app mid-run — the orphan child keeps
  running to settle (by design); restart ⇒ boot reconciliation kills
  crash-orphans via the ladder, marks the session interrupted, no double
  slot allocation; pickup respawns from the transcript as the sole process.
- **slot-sweep failure**: inject an unwritable file owned by a slot uid
  (chattr +i) ⇒ release quarantines the slot; visible in the panel and
  `GET /meters`; retryable; never reused before clean.
- **disk pressure**: fill `scratch/` ⇒ oldest non-stable non-running
  sessions released until headroom; `lib/` untouched.
- **slot pressure**: 16 live sessions ⇒ oldest non-stable non-running
  released; 16 running ⇒ new session waits, panel says so.
- **GC timer**: idle >14 d non-stable released; stable never.
- **soak**: long run with the phone disconnected/locked mid-run — nothing
  cancelled (the run does not need a client); reattach = fetch.
- **card-binder pass**: upload photos → scripts + web research → report in
  `outbox/` → download (Range-resumable).
