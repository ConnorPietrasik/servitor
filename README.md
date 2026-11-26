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
- **App** (Python 3.14+, FastAPI, one asyncio process, root on the guest):
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
  config, `lib/`, `inbox/` (a session's uploads), and the toolkit are
  read-only to the agent; uploads are hash-verified, stored once in a
  content-addressed blob store, and delivered by hardlink — one copy of the
  bytes, all-or-nothing from the agent's view, and no session can rewrite or
  remove what a human sent; all cleanup goes through one `release()` that
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
  rollback. A config version is useful only as long as some session runs
  on it (config reaches a session only at spawn, invariant 9): unreferenced
  versions are pruned — by `release()` as a session's last lower goes, and
  by the GC tick for orphans — keeping `active`, the recorded rollback
  target (`.previous-active`), and every version a live session's mount or
  registry row pins.

## Invariants (load-bearing — a build that breaks these is wrong)

Privilege model: **the app is privileged (root on the guest); the agent
never is.** Each session's pi runs as a **slot uid** from a static pool of
16. The agent's uid is never the app's uid.

1. **No caps:** no path allow-lists, no iteration/wall-clock/vision
   limits. Reach is bounded only by the slot uid and read-only areas.
   Controls are meters (run state, current tool, outbox) + human
   `steer()`/`abort()`.
2. **The API authenticates no user — the edge is the gate, and it
   shows a credential.** When `SERVITOR_EDGE_KEY` is set, every request
   must carry `X-Servitor-Edge-Key` with the value the Traefik
   `servitor-edge` headers middleware injects after the auth chain
   (overwriting anything a client sends); the app constant-time-verifies
   it on every route (and on websocket upgrades — fail-closed — while
   lifespan passes through). The gate is active only when the key is set —
   unset/empty authenticates nobody (dev/standalone; the bind is the
   control) — while production is always keyed: baked into the image at
   build (first boot already gated), re-injected by the sync's key
   slice, certified by the doctor's keyed/keyless probes.
3. **The producer never awaits a client.** Dropped phone connections
   never cancel, slow, or stall a run. Delivery is a separate concern
   from the run.
4. **The key is env-only per subprocess.** `OMNISSIAH_API_KEY` is passed
   to each pi subprocess by env only — never on argv (visible in
   `/proc/*/cmdline` to every slot), never in the synced config, never
   in a transcript or app log.
5. **Session existence is the app's fact, not pi's.** The registry owns
   sessions and records pi's `sessionFile`; empty sessions are never
   persisted by pi, so the app must not guess.
6. **One process per session file at a time** — live run, respawn, or
   probe.
7. **Release is the only cleanup path**, used by all four triggers, and
   a slot is never handed out before its sweep verifies clean.
8. **The run does not need a client.** Zero subscribers ⇒ the log keeps
   growing; no stall, no cancel. Abort is an explicit POST, never a side
   effect of a socket dying.
9. **Config reaches a session only at spawn.** A live session keeps the
    config it started with (copy-up/overlay semantics) — do not "fix"
    hot-reload; pi does not support it. The version a session booted from
    is recorded on its registry row at spawn (a respawn — including one
    after a reboot, when the mount is gone — resolves the same lower),
    and the version is kept exactly until the session's last release
    (invariant 7's prune keeps it while the row or the mounted lower
    pins it; nothing older is retained).
10. **Artifacts leave through the outbox/download endpoint, a human keep,
    or `lib/`** — and only "keep this" / "save this into `lib/`" are ever
    human UI actions (as are rename, delete, and attach). There is no
    agent-reachable write to any durable area: the agent's writable areas are
    its overlay workdir, its `outbox/` bind, and `/tmp`. A keep is a hardlink
    to the app's copy of the bytes, so it costs nothing and no session can
    rewrite what was kept.
11. **The live process is the transcript's authority.** While a session's
    process is alive the app reads the transcript only through pi RPC;
    it parses the JSONL itself only when the process is dead — the boot
    crash signature (a run that never settled) and the dead-process
    reattach (the active branch from the file's tail). Compaction state
    is never reconstructed from the file — that is pi's contract.
12. **A session's files are its own, and root reads them only through that
    fact.** The app runs as root and CAN read any inode on the volume; every
    list/read/delete path requires `st_uid == slot.uid` — or, for a delivered
    upload (root-owned by design, which is what makes it un-deletable by the
    agent), that the inode *is* the blob for the sha the app computed at
    upload time. `find / -user` does not follow hardlinks and neither does
    "the path is inside the directory": without this gate, one `ln /etc/shadow
    outbox/x` would advertise, hash, and serve a file the session never wrote.
    (A session whose slot this host cannot resolve has no uid to bind to:
    interactive reads keep a dev/no-slot fallback, and the background sweep
    skips it rather than read unbounded.)
13. **A transfer is resumable, and an archive is streamed.** Uploads arrive in
    chunks written at the offset the client names — a lost response costs one
    chunk, not the file, and a resume that would splice two different files is
    refused. Directory downloads are deflated straight out of the session's
    gated listing with no staging copy: an archive must not need room for the
    archive. Both are byte-metered and cancellable, because the phone is on a
    metered link.
14. **The UI pays for change, not for waiting.** No route is polled: the
    session's stream carries file deltas, the app-wide stream carries
    lifecycle/run state, listings are ETag'd, per-version sha/MIME are
    computed once, and a hidden tab closes its stream. Byte meters exist to
    be read, never to limit.
15. **A strict session is a launch-line boundary, set at creation.** The
    per-session strict opt-in pins the launch line: `--tools` restricted to
    the read-only builtins (`read,grep,find,ls`), `--exclude-tools mcp__*`,
    the replacement `--system-prompt <config>/STRICT_SYSTEM.md`, no search
    extension, no EXA/web-cache env — all on the argv (the one surface the
    agent cannot rewrite), recorded in the registry row, and re-applied
    identically on every respawn and probe. The model-inference connection
    is infrastructure, not session reach: with no bash and no web tool the
    slot has no code path that opens a socket.

## Repo layout

| path | what |
|---|---|
| `app/` | the guest-side app: API, lifecycle, isolation core, pi rpc driver, delivery (blob store, keep shelf, library save, file watch, byte meters), doctor |
| `ui/` | chat UI (React/Vite, static, served by the app on the same port) |
| `agent-config/` | agent system prompt, model config, skills — synced read-only |
| `lib/` | durable human-curated library — synced read-only to the agent, mounted RO in every session's workdir; the UI can save an outbox/upload file there |
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
  kernel updates with it), deploy swap-based (image swap included) → doctor
  → automatic rollback on failure (a versioned deploy is certified by that
  doctor; the conformance suite stays on-demand — `make test`, e.g. before
  accepting an out-of-major bump). Config-only pushes: `make sync`.
