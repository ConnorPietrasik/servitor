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
  no user; when `SERVITOR_EDGE_KEY` is set (always in production) it
  admits only requests carrying the edge credential the proxy injects
  (invariant 3).
- **Agent**: one `pi --mode rpc` subprocess per session, spawned setuid to a
  slot uid from a static pool of 16. The app is privileged; the agent never
  is.
- **Isolation**: per-session overlay mounts on a fixed-size state volume
  (`/var/lib/servitor`, its own local disk — never overlayfs, which would
  refuse the per-session overlay uppers);
  config, `lib/`, and the toolkit are read-only to the agent; uploads are
  hash-verified and delivered by atomic rename (one copy, all-or-nothing
  from the agent's view); all cleanup goes through one `release()` that
  sweeps by owner.
- **Edge contract**: one reverse proxy routes one hostname to the VM's
  ingress port and is the only auth gate — it authenticates the user
  (Authelia 2FA) AND injects the edge credential on every request it
  proxies, which the app re-verifies (invariant 3). Production: the
  owner's portcullis (Traefik file-provider + Authelia 2FA). Dev runs
  bind loopback only, same code — the credential gate is active only
  when `SERVITOR_EDGE_KEY` is set: leave it empty in a dev `.env` (no
  gate, the bind is the control) or set it to exercise the same check.
- **Config** (`agent-config/`) and the library (`lib/`) are synced one-way
  from this repo to the guest — versioned, doctor-gated, swap-based with
  rollback.

## Invariants (load-bearing — a build that breaks these is wrong)

Privilege model: **the app is privileged (root on the guest); the agent
never is.** Each session's pi runs as a **slot uid** from a static pool of
16. The agent's uid is never the app's uid.

1. **Prompt injection is the dominant threat.** Search results, fetched
   pages, and uploaded files steer an agent that holds credentials and a
   network position. Mitigation ladder: VM boundary (no egress filtering
   by design — exfiltration to the open internet is *accepted
   residual risk*), read-only where injection could persist (config,
   `lib/`), hash-verified uploads (staged, sha256-checked, atomically
   renamed — all-or-nothing), context-file discovery disabled,
   launch-line trust flags asserted in CI, per-session uid + overlay +
   owner-sweep, `lib/` writable only by a distinct human action. One
   further accepted residual with a warning: baseline extension discovery
   means an extension an agent plants in its own tree loads on the next
   respawn — visible (new-extension warning), not blocked; it adds no
   reach the slot uid doesn't already have, only an unattended trigger
   point.
2. **No caps:** no path allow-lists, no iteration/wall-clock/vision
   limits. Reach is bounded only by the slot uid and read-only areas.
   Controls are meters (run state, current tool, outbox) + human
   `steer()`/`abort()`.
3. **The API authenticates no user — the edge is the gate, and it
   shows a credential.** When `SERVITOR_EDGE_KEY` is set, every request
   must carry `X-Servitor-Edge-Key` with the value the Traefik
   `servitor-edge` headers middleware injects after the auth chain
   (overwriting anything a client sends); the app constant-time-verifies
   it on every route (and on websocket upgrades — fail-closed — while
   lifespan passes through). The gate is active only when the key is set —
   unset/empty authenticates nobody (dev/standalone; the bind is the
   control) — while production is always keyed: baked into the image at
   build (first boot already gated), re-injected by the sync's key
   slice, certified by the doctor's keyed/keyless probes. Why the
   credential: the guest firewall
   admits the port from the WHOLE edge subnet (the proxy's source IP
   is indistinguishable from a neighbor's), and that subnet carries the
   public gpu fleet — a compromised neighbor must not be able to drive
   the agent or read its files directly. Scope, honestly: it stops no
   internet attacker (Authelia 2FA + admins-only stays the human gate)
   and no host-side actor — its population is unauthenticated in-subnet
   callers. Bind the ingress interface, admit the port from the edge
   subnet only; dev binds loopback (gate off unless the dev `.env`
   sets a key).
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
12. **No git credentials in the VM — the agent can never write to a
    repo.** The guest ships git (clone/fetch for reading) but no keys,
    tokens, or credential helpers; all git reconciliation is host-side.
    The threat this closes is not exfiltration (an accepted residual,
    invariant 1) — it is that a prompt-injected agent must never be
    able to push to, or otherwise modify, your repositories.
13. **The live process is the transcript's authority.** While a session's
    process is alive the app reads the transcript only through pi RPC;
    it parses the JSONL itself only when the process is dead — the boot
    crash signature (a run that never settled) and the dead-process
    reattach (the active branch from the file's tail). Compaction state
    is never reconstructed from the file — that is pi's contract.

## Repo layout

| path | what |
|---|---|
| `app/` | the guest-side app: API, lifecycle, isolation core, pi rpc driver, doctor |
| `ui/` | chat UI (React/Vite, static, served by the app on the same port) |
| `agent-config/` | agent system prompt, model config, skills — synced read-only |
| `lib/` | durable human-curated library — synced read-only to the agent |
| `deploy/` | guest image build, systemd units, firewall, sync/update/doctor, portcullis install |
| `test/` | the gate stack: contract lint, unit, conformance, stage2, sandbox-smoke |

## Dev loop

Dev happens in a plain checkout (no root needed):

```bash
cp .env.example .env   # fill in per deploy/README.md
make venv
make check             # contract lint
make test              # unit + conformance vs the mock router
                       # (conformance loud-skips without root; pi +
                       # extension versions come from .env — sourced by
                       # the Makefile, no default pin)
```

The root suites run on the server (the host the VM lives on):

```bash
sudo make test-stage2   # lifecycle drills over the stdio app
sudo make sandbox-smoke # isolation gates (overlays, sweep, protocol)
sudo make guest         # build the guest image
```

## Deploy

- **Production** (portcullis server): one-time install in
  `deploy/portcullis/INSTALL.md`; day-2 ops in `deploy/portcullis/RUNBOOK.md`.
- **Updates**: `make update` — float pi/extension to the newest release in
  the same major (out-of-major releases are reported, not applied), rebuild
  the guest image when the userland outgrows the freshness floor (the guest
  kernel updates with it), conformance-gate the resolved versions
  host-side, deploy swap-based (image swap included) → doctor → automatic
  rollback on failure. Config-only pushes: `make sync`.
