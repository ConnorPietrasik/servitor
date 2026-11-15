# Servitor deployment

Two deployments, one system (DECISIONS D2): **standalone** (dev laptop / bare
VM — loopback behind a local reverse proxy) and **production** (the portcullis
edge — Traefik file-provider + Authelia 2FA forward-auth). No code change
between them: the app binds `SERVITOR_BIND` on `SERVITOR_PORT`, both from the
0600 env file; the edge is an abstract contract (one reverse proxy routes one
hostname to the VM ingress IP:port and is the only auth gate).

Servitor never reaches into portcullis, and portcullis never reaches into
servitor at runtime (A1/A2).

## Layout

```
deploy/
├── guest/          # guest image inputs: nftables script, systemd slice+unit,
│                   #   hosts-pin template (image build script lands here in stage 5)
├── portcullis/     # operator-merged into the portcullis repo:
│   ├── traefik-dynamic/servitor.yml   # the route (compose/traefik/dynamic/)
│   └── panel-do/servitor_actions.go   # panel-do ACTIONS entries
├── sync.sh         # host-side one-way sync (stage 5): config/lib push,
│                   #   key slice, extension install, update-waiting flag (D8)
├── update.sh       # host orchestrator (stage 5): resolve -> host-side
│                   #   conformance gate -> provision -> swap -> doctor ->
│                   #   auto-rollback
└── doctor.sh       # host wrapper -> guest `servitorctl doctor` (stage 5)
```

## The env file (single source of truth)

`.env.example` names every env var the app and deploy scripts read. The
guest's copy lives at `/etc/servitor/servitor.env`, mode 0600, consumed by
`servitor.service` (`EnvironmentFile=`). The sync pipeline owns the *key
slice* of that file (re-injects `OMNISSIAH_API_KEY`/`EXA_API_KEY` on rotation;
see Key rotation below).

## Standalone (laptop dev loop)

1. `cp .env.example .env`; set `SERVITOR_BIND=127.0.0.1`,
   `SERVITOR_PORT=8787`, `SERVITOR_ROOT=<path on ext4, never overlay>`,
   `SERVITOR_PI_VERSION`, `SERVITOR_PI_WEB_ACCESS_VERSION`, the two keys,
   and the intended model values (A4: discover live from the router —
   off-LAN the public URL works; on-LAN pin `*.pietrasik.top` to the host
   LAN IP in `/etc/hosts`, the same workaround portcullis ships).
2. `make venv && make test` (mock router + conformance suite, no server).
3. Run the app with root (iso/rpc need CAP_SYS_ADMIN: overlays, setuid):
   `sudo -E set -a; . ./.env; set +a; .venv/bin/python -m app.api.main`.
   Reach it on loopback behind a local reverse proxy (e.g.
   `python -m http.server` is not enough — use any proxy with
   `proxy_buffering off` for SSE).

## Production (portcullis)

1. **Host work (once, stage 6):** install libvirt/KVM (none exists on the
   server yet), build the guest qcow2 (`make guest`), define + start the VM
   (`virsh define/start`), record the VM ingress IP.
2. Merge `deploy/portcullis/traefik-dynamic/servitor.yml` (substituting the
   VM ingress IP + port) into the portcullis repo and `make update` there
   (gated, with snapshot + auto-rollback). Authelia's
   `*.pietrasik.top` two_factor wildcard makes it private by default.
3. `deploy/sync.sh` from a host checkout: versioned config swap, `lib/` push,
   key slice into the 0600 env file, pi-web-access install at the `.env`
   selection, service restart (or the update-waiting flag when a run is
   active — D8, never blocks, always exit 0), then `deploy/doctor.sh`.
4. `doctor` green is the deploy gate: no-prompt spawn resolves the intended
   provider/baseUrl, `model.id != "unknown"`, launch line == the asserted
   constant, state volume is real ext4, firewall probe, key material absent
   from the synced tree/session files/app logs, live search works, one live
   prompt end-to-end.

## Key rotation (PLAN A2 — portcullis's rotation does NOT reach the VM)

1. Edit the host `.env` (`LLM_API_KEY` / Exa).
2. portcullis `make update` (for the host-side consumers).
3. `deploy/sync.sh` on the host — re-injects the key slice into the guest's
   0600 env file.
4. Service restart (orderly: abort-then-drain) — the key is env-only per
   subprocess (invariant 5), so a restart re-reads it; no on-disk key to
   scrub.

## Updates

`make update` / `servitorctl update` is the single versioned path: resolve
the `.env` specs → conformance suite against the *resolved* versions
host-side (the guest is untouched until the gate passes) → provision only
what changed (image / extension / config) → swap-based deploy with previous
artifacts retained → doctor → automatic rollback on doctor failure.
`sync` remains the fast config-only push for daily edits. A run active at
the restart step sets the update-waiting flag instead of restarting (D8);
image-level swaps are always explicit operator downtime. Not bumping is a
supported steady state — the guest runs the last-verified pi indefinitely.

## Runbook

See `deploy/portcullis/RUNBOOK.md` (provisioning, sync, release, GC, drills).
