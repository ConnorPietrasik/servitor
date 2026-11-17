# Servitor deployment

Two deployments, one system: **standalone** (dev laptop / bare
VM — loopback behind a local reverse proxy) and **production** (the portcullis
edge — Traefik file-provider + Authelia 2FA forward-auth). No code change
between them: the app binds `SERVITOR_BIND` on `SERVITOR_PORT`, both from the
0600 env file; the edge is an abstract contract (one reverse proxy routes one
hostname to the VM ingress IP:port and is the only auth gate).

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
├── update.sh       # host orchestrator: resolve -> host-side
│                   #   conformance gate -> provision -> swap -> doctor ->
│                   #   auto-rollback
└── doctor.sh       # host wrapper -> guest `servitorctl doctor`
```

## The env file (single source of truth)

`.env.example` names every env var the app and the host deploy scripts read
(the host-side deploy section at its bottom is never read by the app and
never synced into the guest). The
guest's copy lives at `/etc/servitor/servitor.env`, mode 0600, consumed by
`servitor.service` (`EnvironmentFile=`). The sync pipeline owns the *key
slice* of that file (re-injects `OMNISSIAH_API_KEY`/`EXA_API_KEY` on rotation;
see Key rotation below).

## Standalone (laptop dev loop)

1. `cp .env.example .env`; set `SERVITOR_BIND=127.0.0.1`,
   `SERVITOR_PORT=8787`, `SERVITOR_ROOT=<path on ext4, never overlay>`,
   `SERVITOR_PI_VERSION`, `SERVITOR_PI_WEB_ACCESS_VERSION`, the two keys,
   and the intended model values (discover live from the router —
   off-LAN the public URL works; on-LAN pin `*.pietrasik.top` to the host
   LAN IP in `/etc/hosts`, the same workaround portcullis ships).
2. `make venv && make test` (mock router + conformance suite, no server).
3. Run the app with root (iso/rpc need CAP_SYS_ADMIN: overlays, setuid):
   `sudo -E set -a; . ./.env; set +a; .venv/bin/python -m app.api.main`.
   Reach it on loopback behind a local reverse proxy (e.g.
   `python -m http.server` is not enough — use any proxy with
   `proxy_buffering off` for SSE).

### Laptop host prerequisites (re-create after a host reimage)

Not in the repo — host firewall + host `.env`:

- **ufw** (the laptop default-drops routed and incoming traffic):
  ```bash
  sudo ufw route allow from 10.42.0.0/24   # guest egress NAT
  sudo ufw allow in from 10.42.0.0/24 to any port 48700:48799 proto tcp
  ```
  Rule 2 admits the test mock router: it binds the host's bridge IP and
  allocates ports from that range so the guest can reach it.
- **`/tmp` is a small tmpfs on the laptop** — run the guest/api suites with
  a disk-backed basetemp (`--basetemp=~/.cache/servitor-pytest/<name>`); a
  full /tmp also breaks `make check`.
- **`/etc/hosts`**: the panel-hosts block pins `*.pietrasik.top` to the
  server's LAN IP (managed by portcullis's `tools/panel-hosts.sh`);
  `sync.sh` mirrors those pins into the guest (hairpin-NAT workaround).
- Multi-word `.env` values (e.g. `SERVITOR_DNS_UPSTREAM`) must be QUOTED —
  `sync.sh` sources the whole file.

## Production (portcullis)

Step-by-step: **`deploy/portcullis/INSTALL.md`** (one-time server install:
host packages, `.env` template, image, state disk, the libvirt domain from
`deploy/guest/servitor-domain.xml`, first sync, portcullis merge, verify).

1. **Host work (once):** install libvirt/KVM (none exists on the
   server yet), build the guest qcow2 (`make guest`), create the state disk,
   define + start the VM (the domain template in
   `deploy/guest/servitor-domain.xml`) — the VM ingress IP is CHOSEN in the
   host `.env` before the build (it is baked into the image).
2. Merge `deploy/portcullis/traefik-dynamic/servitor.yml` (substituting the
   VM ingress IP + port) into the portcullis repo and `make update` there
   (gated, with snapshot + auto-rollback). Authelia's
   `*.pietrasik.top` two_factor wildcard makes it private by default.
3. `deploy/sync.sh` from a host checkout: versioned config swap, `lib/` push,
   key slice into the 0600 env file, pi-web-access install at the `.env`
   selection, service restart (or the update-waiting flag when a run is
   active — never blocks, always exit 0), then `deploy/doctor.sh`.
4. `doctor` green is the deploy gate: no-prompt spawn resolves the intended
   provider/baseUrl, `model.id != "unknown"`, launch line == the asserted
   constant, state volume is real ext4, firewall probe, key material absent
   from the synced tree/session files/app logs, live search works, one live
   prompt end-to-end.

## Key rotation (portcullis's rotation does NOT reach the VM)

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
the restart step sets the update-waiting flag instead of restarting;
image-level swaps are always explicit operator downtime. Not bumping is a
supported steady state — the guest runs the last-verified pi indefinitely.

## Runbook

See `deploy/portcullis/RUNBOOK.md` (provisioning, sync, release, GC, drills).
