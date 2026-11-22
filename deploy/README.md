# Servitor deployment

One system, one deployment: **production** (the portcullis edge — Traefik
file-provider + Authelia 2FA forward-auth). The app binds `SERVITOR_BIND`
on `SERVITOR_PORT`, both from the 0600 env file; the edge is an abstract
contract (one reverse proxy routes one hostname to the VM ingress IP:port,
authenticates the user via forward-auth, AND injects the edge credential
the app re-verifies on every route while it is set). Dev
runs bind loopback only, same code — the gate is off unless the dev
`.env` sets `SERVITOR_EDGE_KEY` (production is always keyed: baked into
the image, the key slice, the doctor's probes).

Servitor never reaches into portcullis, and portcullis never reaches into
servitor at runtime.

## Layout

```
deploy/
├── guest/          # guest image build: build.sh + inputs (systemd unit/slice,
│                   #   nftables, pins, domain template); built images under images/
├── portcullis/     # operator-merged into the portcullis repo:
│   ├── traefik-dynamic/servitor.yml   # the route (→ compose/traefik/dynamic/)
│   └── panel-do/servitor_actions.go   # record of the merged panel-do actions
│                                      #   (PORTCULLIS's main.go is canonical)
├── sync.sh         # host-side one-way sync: config/lib push,
│                   #   key slice, extension install, update-waiting flag
├── update.sh       # host orchestrator: resolve -> float -> provision ->
│                   #   swap -> doctor -> auto-rollback
└── doctor.sh       # host wrapper -> guest `servitorctl doctor`
```

## The env file (single source of truth)

`.env.example` names every env var the app and the host deploy scripts read
(the host-side deploy section at its bottom is never read by the app and
never synced into the guest). The
guest's copy lives at `/etc/servitor/servitor.env`, mode 0600, consumed by
`servitor.service` (`EnvironmentFile=`). The sync pipeline owns the *key
slice* of that file (re-injects `OMNISSIAH_API_KEY`/`EXA_API_KEY`/
`SERVITOR_EDGE_KEY` on rotation; see Key rotation below). The edge key is
also baked into the image at build time so the FIRST boot is already
gated; the slice is what keeps it current after rotation.

## Production (portcullis)

Step-by-step: **`deploy/portcullis/INSTALL.md`** (one-time server install:
host packages, `.env` template, image, state disk, the libvirt domain from
`deploy/guest/servitor-domain.xml`, first sync, portcullis merge, verify).

1. **Host work (once):** install libvirt/KVM (none exists on the
   server yet), build the guest qcow2 (`sudo make guest`), create the state disk,
   define + start the VM (the domain template in
   `deploy/guest/servitor-domain.xml`) — the VM ingress IP is CHOSEN in the
   host `.env` before the build (it is baked into the image).
2. Merge `deploy/portcullis/traefik-dynamic/servitor.yml` (substituting the
   VM ingress IP + port) into the portcullis repo and `make update` there
   (gated, with snapshot + auto-rollback). Access is admins-only: the
   portcullis repo's Authelia config carries the `servitor.pietrasik.top`
   two_factor + `group:admins` rule pair above the `*.pietrasik.top`
   wildcard (same pattern as the panel) — ships in the same `make update`.
3. `deploy/sync.sh` from a host checkout: versioned config swap, `lib/` push,
   key slice into the 0600 env file, pi-web-access install at the `.env`
   selection, service restart (or the update-waiting flag when a run is
   active — never blocks, always exit 0), then `deploy/doctor.sh`.
4. `doctor` green is the deploy gate: no-prompt spawn resolves a usable
   model (provider/baseUrl listed, `model.id != "unknown"` — no id/context
   pin), launch line == the asserted
   constant, state volume is a local non-overlay fs, firewall probe, key material absent
   from the synced tree/session files/app logs, live search works, one live
   prompt end-to-end.

## Key rotation (portcullis's rotation does NOT reach the VM)

Model/search keys (`LLM_API_KEY` / Exa):

1. Edit the host `.env` (`LLM_API_KEY` / Exa).
2. portcullis `make update` (for the host-side consumers).
3. `deploy/sync.sh` on the host — re-injects the key slice into the guest's
   0600 env file.
4. Service restart (orderly: abort-then-drain) — the key is env-only per
   subprocess, so a restart re-reads it; no on-disk key to scrub.

Edge key (`SERVITOR_EDGE_KEY`) rotates on BOTH sides, because
the middleware's value and the guest's are separate copies that must agree
(a drift 403s the whole UI — portcullis `make doctor` cross-checks them):

1. Edit portcullis's `.env` (the middleware's source) AND the servitor host
   `.env` (the guest's copy) with the SAME new value.
2. portcullis `make update` (re-seds the deployed middlewares.yml).
3. `deploy/sync.sh` — re-injects the key slice into the guest's 0600 env
   file; the app restarts with the new key at the next idle moment.
4. `deploy/doctor.sh` — its keyed/keyless probes certify the guest matches
   the new value before you consider it done.

## Updates

`make update` / `deploy/update.sh` is the single day-2 command (portcullis
parity): it FLOATS — it does not ask. Each `.env` version line is an exact
version (X.Y.Z) and a floor — a range or tag fails the preflight (it could
never round-trip a deploy: installed == env is doctor-asserted); the
update resolves the newest npm release in the same major for everything
(the 0.x extension floats across minors; a real 1.0.0 is a major) and
deploys it. A newer out-of-major release is REPORTED (`MAJOR AVAILABLE`)
and never applied — accepting one is editing the `.env` line to that
major's release.

The guest userland is part of the update: when the image's manifest age
outgrows the freshness floor (default 7 days, `IMAGE_MAX_AGE_DAYS`), the
image is rebuilt — which re-resolves the Arch packages, so the guest
kernel floats with it — and swapped in across a clean ACPI shutdown
(explicit downtime, loudly reported). Retention keeps exactly the new
image + the previous one (the automatic rollback target); the build
prunes every older image, so `deploy/guest/images/` stays at two image
triples. The swap rotates the guest's ssh host key (each image bakes
fresh keys) and `accept-new` never accepts a CHANGED key, so the swap
drops the recorded known_hosts entry and re-accepts on first contact
(both directions: swap forward and rollback swap-back).

Order, after the resolve: precondition doctor (deployed state must be
green) → the `.env` lines are floated to the targets (bump before apply:
the build and the sync's key slice both read the `.env`) → provision only
what moved → the sync's swap-based deploy → doctor → AUTOMATIC ROLLBACK on
failure. The rollback restores the image,
the manifest's `active` and the `.env` to what is actually running —
always; the shape back to the retained previous too, but only when THIS
run's sync deployed it. No failure path leaves the world ahead of what is
running: a BUILD failure restores the floated `.env` lines, and a SWAP
failure additionally restores the manifest's `active` to the still-running
image. Re-run `make update` either way — a built-but-not-swapped image is
reused (the swap retried), never rebuilt in place.

The on-demand root suites (`make test`, `make sandbox-smoke`) run
host-side (root) and need the host checkout's own `make venv` plus the
slot users they spawn into (`slot1` for the conformance half, `slot2` for
sandbox-smoke — the unit half stands in passwd and needs no real users) —
the guest
image's slots are a different namespace (INSTALL.md §10 for the server). A
run active at the restart step sets the update-waiting flag instead of
restarting (never blocks).

A world-current run (no in-major release newer than the deployed one, the
image within the floor, the shape unchanged) touches nothing, restarts
nothing, and reports NO-OP: the update ran and had nothing to change.

Run as the portcullis tail (`08-update.sh`'s best-effort tail after the
host gates), a shut-off VM is SKIPPED — the tick never starts a VM the
operator stopped on purpose (the swap would); the float then lands on the
next tick after the VM is back, or via a manual run above.

## Runbook

See `deploy/portcullis/RUNBOOK.md` (provisioning, sync, release, GC, drills).
