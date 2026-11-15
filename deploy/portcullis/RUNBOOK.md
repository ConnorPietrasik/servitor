# Servitor — runbook excerpt (operator-merged into the portcullis repo, A1/A6)

Full operational reference for the servitor VM. The short version: the VM is
provisioned once; day-2 control is the four panel-do actions
(`servitor-status`, `servitor-vm-restart`, `servitor-sync`, `servitor-doctor`);
updates are `servitorctl update` (gated, rollbacks automatically); config
edits are `servitorctl sync` (fast path, never blocks on a run).

## Provisioning (once)

1. Host: install libvirt/KVM (none on the server at first deploy — stage-6
   documented host work).
2. Host: `make guest` in a servitor checkout — builds the qcow2 (name =
   pi version + date; the previous image is retained for rollback).
3. Host: `virsh define` + `virsh start` with the domain definition from
   `deploy/guest/` (24 vCPU no quota, balloon + free-page-reporting,
   ~200 GB disk, LAN-reachable, no GPU). Record the VM ingress IP.
4. Guest: the image ships with slots 1-16, the toolkit, the app, nftables
   applied (port from the edge subnet only), `/etc/hosts` pins for
   `*.pietrasik.top` → host LAN IP (no-hairpin workaround, A5).
5. Host: `deploy/sync.sh` (config + keys + extension + doctor).
6. Portcullis: merge the route (this repo's
   `deploy/portcullis/traefik-dynamic/servitor.yml`, VM IP substituted) and
   `make update`.
7. Verify: browser → `servitor.pietrasik.top` (2FA) → chat works, `doctor`
   green.

## Daily operations

| task | how |
|---|---|
| change prompts/skills/lib | edit `agent-config/` / `lib/` in the host checkout, `servitorctl sync` |
| bump pi / pi-web-access | edit `.env` specs, `servitorctl update` (suite gates the resolved versions host-side first) |
| rotate keys | host `.env` → portcullis `make update` → `servitorctl sync` → restart (sync does the re-inject + flag) |
| release a session | UI close, GC timer (idle >14 d, non-stable), slot/disk pressure — all through the single `release()` (sweep by owner before the slot is reused; sweep-fail ⇒ quarantined, visible, retryable) |
| back up `lib/` | host-side git pull (no git credentials in the VM, invariant 12) |

## Drills (stage-6 gate set)

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
