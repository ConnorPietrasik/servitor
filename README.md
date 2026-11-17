# Servitor

A home assistant with coding ability, not a pure coding agent: a
phone-first web chat UI driving Pi (`@earendil-works/pi-coding-agent`) in a
dedicated KVM VM. You hand it files — including far-beyond-context material
(photo binders, data dumps, video) — it writes and runs scripts to work on
them, searches the web, and runs as long as the task needs. A human can
steer or abort at any point.

## Architecture

- **One KVM guest VM is the whole system.** Arch-based qcow2 image,
  `servitor` systemd service in `servitor.slice`, no docker inside.
- **App** (Python 3.12+, FastAPI, one asyncio process, root on the guest):
  HTTP/SSE API + control plane on one published port. It authenticates
  nobody — the edge is the gate.
- **Agent**: one `pi --mode rpc` subprocess per session, spawned setuid to a
  slot uid from a static pool of 16. The app is privileged; the agent never
  is.
- **Isolation**: per-session overlay mounts on a fixed-size state volume
  (`/var/lib/servitor`, real ext4 — never overlay/container storage);
  config, `lib/`, and the toolkit are read-only to the agent; uploads are
  content-addressed; all cleanup goes through one `release()` that sweeps
  by owner.
- **Edge contract**: one reverse proxy routes one hostname to the VM's
  ingress port and is the only auth gate. Production: the owner's portcullis
  (Traefik file-provider + Authelia 2FA). Standalone (laptop, tests, bare
  VM): loopback behind a local reverse proxy. No code change between them.
- **Config** (`agent-config/`) and the library (`lib/`) are synced one-way
  from this repo to the guest — versioned, doctor-gated, swap-based with
  rollback.

## Invariants (load-bearing — a build that breaks these is wrong)

Privilege model: **the app is privileged (root on the guest); the agent
never is.** Each session's pi runs as a **slot uid** from a static pool of
16. The agent's uid is never the app's uid — that is what makes the
read-only config real (root could remount it rw; the agent cannot be
root). Code and deploy comments cite these by number (`invariant N`).

1. **Prompt injection is the dominant threat.** Search results, fetched
   pages, and uploaded files steer an agent that holds credentials and a
   network position. Mitigation ladder: VM boundary (no egress filtering
   by design — exfiltration to the open internet is the *accepted
   residual risk*), read-only where injection could persist (config,
   `lib/`), content-addressed uploads, context-file discovery disabled,
   launch-line trust flags asserted in CI, per-session uid + overlay +
   owner-sweep, `lib/` writable only by a distinct human action. One
   further accepted residual with a warning: baseline extension discovery
   means an extension an agent plants in its own tree loads on the next
   respawn — visible (new-extension warning), not blocked; it adds no
   reach the slot uid doesn't already have, only an unattended trigger
   point.
2. **No caps:** no path allow-lists, no iteration/wall-clock/vision
   limits. Reach is bounded only by the slot uid and read-only areas.
   Controls are meters (run active, elapsed, current tool, subprocess
   count, scratch usage, outbox size) + human `steer()`/`abort()`.
3. **The API authenticates nobody.** Anything that can reach the socket
   can drive the agent; reachability is the control. Bind the ingress
   interface, admit the port from the edge subnet only; standalone binds
   loopback.
4. **The producer never awaits a client.** Dropped phone connections
   never cancel, slow, or stall a run. Delivery is a separate concern
   from the run.
5. **The key is env-only per subprocess.** `OMNISSIAH_API_KEY` is passed
   to each pi subprocess by env only — never on argv (visible in
   `/proc/*/cmdline` to every slot), never in the synced config, never
   in a transcript or app log.
6. **Session existence is the app's fact, not pi's.** The registry owns
   sessions and records pi's `sessionFile`; empty sessions are never
   persisted by pi, so the app must not guess.
7. **One process per session file at a time** — live run, respawn, or
   probe.
8. **Release is the only cleanup path**, used by all four triggers, and
   a slot is never handed out before its sweep verifies clean.
9. **The run does not need a client.** Zero subscribers ⇒ the log keeps
   growing; no stall, no cancel. Abort is an explicit POST, never a side
   effect of a socket dying.
10. **Config reaches a session only at spawn.** A live session keeps the
    config it started with (copy-up/overlay semantics) — do not "fix"
    hot-reload; pi does not support it.
11. **Artifacts leave through the outbox/download endpoint or `lib/`** —
    and only "save this script" into `lib/` is ever a human UI action.
    There is no agent-reachable write to any durable artifact.
12. **No git credentials in the VM.** The host does all git
    reconciliation.
13. **pi's session JSONL is opaque payload.** The app never
    reconstructs the active branch or compaction state by parsing it —
    that is pi's contract.

## Repo layout

| path | what |
|---|---|
| `app/` | the guest-side app: API, lifecycle, isolation core, pi rpc driver, doctor |
| `ui/` | chat UI (React/Vite, static, served by the app on the same port) |
| `agent-config/` | agent system prompt, model config, skills — synced read-only |
| `lib/` | durable human-curated library — synced read-only to the agent |
| `deploy/` | guest image build, systemd units, firewall, sync/update/doctor, portcullis install |
| `test/` | the gate stack: contract lint, unit, conformance, smoke, guest |
| `compose/` | interface contract (linted by `make check`, not a runtime container) |

## Dev loop (laptop, root)

```bash
cp .env.example .env   # fill in per deploy/README.md
make venv
make check             # contract lint (no root)
make test              # unit + conformance vs the mock router (no root)
sudo make test-stage2  # lifecycle drills over the stdio app (root)
sudo make test-api     # HTTP/SSE + delivery, real subprocess app (root)
sudo make sandbox-smoke && sudo make vm-smoke   # isolation halves (root)
sudo make guest        # build the guest image (root)
sudo make test-guest   # full guest suite: boots the built VM (~12 min, root)
```

The guest suite additionally needs the laptop host prerequisites in
`deploy/README.md` (ufw rules, disk-backed pytest basetemp).

## Deploy

- **Standalone** (laptop / bare VM): `deploy/README.md`, §Standalone.
- **Production** (portcullis server): one-time install in
  `deploy/portcullis/INSTALL.md`; day-2 ops in `deploy/portcullis/RUNBOOK.md`.
- **Updates**: `make update` — resolve versions → conformance gate →
  swap-based deploy → doctor → automatic rollback on failure. Config-only
  pushes: `make sync`.
