# AGENTS.md — working rules for this repo

**Servitor** is a home assistant with coding ability: a phone-first web chat
UI (React/Vite, served by the app) driving `pi` (`@earendil-works/pi-coding-agent`)
in a dedicated KVM VM. One Python 3.14 asyncio process is the whole system:
HTTP/SSE API + control plane + isolation core, one `pi --mode rpc` subprocess
per session, setuid to a slot uid from a static pool of 16.

## Gates (run these; a gate is never silently green where root is unavailable)

```bash
cp .env.example .env   # if no .env; fill in per deploy/README.md
make venv              # file-backed venv (stamped; rebuilds only on dep/interpreter change)
make check             # contract lint — run after ANY change; fast, no root
make test              # unit + conformance vs the mock router
```

UI changes: `cd ui && npm ci && npm run build` (build is `tsc --noEmit && vite build`).
`ui/dist/` and `ui/.ui-src-sha` are git-ignored build outputs; `deploy/sync.sh`
rebuilds the dist host-side when the source hash moves. Do not hand-edit `dist/`.

## Hard invariants — a build that breaks these is wrong

The full list with rationale is README §Invariants. The ones that bite code
changes:

1. **No caps.** No path allow-lists, no iteration/wall-clock/vision limits.
   Reach is bounded by slot uid + read-only areas; controls are meters plus
   human `steer()`/`abort()`.
2. **The API authenticates no user.** The edge proxy is the gate; when
   `SERVITOR_EDGE_KEY` is set, every request (incl. websocket upgrades —
   fail-closed) must carry the credential the proxy injected, verified
   constant-time. Empty/unset key = gate off (dev only; production is always
   keyed).
3. **The producer never awaits a client.** Dropped connections never cancel,
   slow, or stall a run. Abort is an explicit POST, never a socket side effect.
4. **Keys are env-only per subprocess** — never on argv, never in synced
   config, never in a transcript or log.
5. **The registry is the session truth**, not pi. One process per session file
   at a time. `release()` is the *only* cleanup path (all four triggers go
   through it); a slot is never handed out before its sweep verifies clean.
6. **Config reaches a session only at spawn.** No hot-reload — pi does not
   support it. Do not "fix" this.
7. **The live process is the transcript's authority.** Read the transcript via
   pi RPC while the process is alive; parse the JSONL yourself only when it is
   dead. Never reconstruct compaction state from the file.
8. **Root reads a session file only if the session owns it.** Every
    list/read/delete path requires `st_uid == slot.uid`, or (for a delivered
    upload, root-owned by design) that the inode *is* the blob for the sha the
    app computed at upload. `resolve()`+containment does not reject hardlinks.
    One named exception: a session whose slot this host cannot resolve has no
    uid to bind to — interactive API reads keep a dev/no-slot fallback (gate
    inert), and the background sweep skips such sessions entirely rather than
    read unbounded.
9. **The UI pays for change, never for waiting.** No interval polling
    anywhere: session file deltas and app-wide lifecycle events ride the
    existing SSE, listings are ETag'd, sha/MIME are per-version caches only,
    and a hidden tab closes its stream.
10. **A strict session is a launch-line boundary.** Create-time opt-in, set
    in the registry row, carried by the launch line itself: `--tools
    read,grep,find,ls`, `--exclude-tools mcp__*`, `--system-prompt
    STRICT_SYSTEM.md`, no search extension, no EXA key — the one surface
    the agent cannot rewrite, re-applied on every respawn/probe (README §15).

## Coding contracts (enforced by `make check`, an AST + parse lint)

- **Env vars**: app code reads env *only* through `app/env.py` — never
  `os.environ`/`os.getenv` directly. A new var must be added to `.env.example`
  **and** exactly one of the name tuples in `app/env.py`: `ENV_NAMES` (app),
  `HOST_DEPLOY_ENV_NAMES` (host scripts only), or `TEST_SCOPED_ENV_NAMES`
  (suite-internal, test code only). The name sets must stay disjoint and equal
  to `.env.example`'s.
- **Nothing hardcodes an address/port/path.** No IPv4 literals in `app/` or
  `deploy/` code; no loopback/any-address literal in `app/`. Bind/port/subnet
  come from env, full stop. (The UI vite dev-proxy is exempt — `ui/` is not
  linted.)
- **`agent-config/` sanity**: `models.json` has a literal `baseUrl` (no `$VAR`
  interpolation), `apiKey: "$OMNISSIAH_API_KEY"` (env-only), no
  `auth.json`/`models-store.json` (a stale stored credential silently beats the
  env var), no context-file names, and `SYSTEM.md` carries the config-gate
  marker.
- **The portcullis route yml** (`deploy/portcullis/traefik-dynamic/`) must
  parse with the expected shape (one router, auth-chain middleware, the
  servitor-edge credential middleware, SSE flush setting, no `readTimeout`).

## Style

- Module docstrings state the design contract ("the registry is the only
  session truth", "one per LIVE session", …); keep them accurate when the
  design moves. Comments are terse and load-bearing, not narration.
- Docstrings and comments explain *invariants and why*, not what the next line
  does. Avoid any history or wordy prose. Bug history explanations are forbidden; if you see a date in a comment, it should probably be deleted.
- Version selections (pi, extensions) are floors in `.env` — no default pins
  in code; `deploy/update.sh` floats them in-major.

## Layout

| path | what |
|---|---|
| `app/` | the guest-side app: `api/` (FastAPI, SSE, transcript), `iso/` (mounts, slots, spawn, sweep, release, config swap), `rpc/` (pi driver, framer, launch), `delivery/` (staging→blob store, keep shelf, file watch, byte meters), `lifecycle.py`, `registry.py`, `doctor.py` |
| `ui/` | chat UI (React 18 / Vite / TS), static, served by the app same-port; `/api`, `/panel`, `/healthz` same-origin |
| `agent-config/` | system prompt, model config, skills — synced **read-only** to the guest, spawn-time only |
| `lib/` | durable human-curated library — synced RO to the agent, never agent-writable, never GC'd |
| `deploy/` | guest image build, systemd units, firewall, `sync.sh`/`update.sh`/`doctor.sh`, portcullis install/runbook |
| `test/` | `check_contract.py` (the lint), `unit/`, `conformance/` (mock router), `stage2/` (root, stdio lifecycle), `sandbox_smoke/` (root, isolation) |

## Deploy

- `.env` is the single source of truth (guest copy: `/etc/servitor/servitor.env`,
  0600). The host-deploy section at the bottom of `.env.example` is never read
  by the app and never synced.
- Day-2: `make sync` (config/lib push, key slice) or `make update` (float +
  doctor-certified deploy + auto-rollback, image included).
- `make pull` mirrors the guest's kept shelf (`/var/lib/servitor/keep`) to the
  host — one-way, host-side, over the deploy key. Kept bytes are the only
  files with no session attached and no expiry; this is their backup path.
- `make deploy` deliberately prints instructions instead of running a script —
  the one-time install (`deploy/portcullis/INSTALL.md`) keeps operator
  decisions visible. Do not add a deploy script.
- `deploy/portcullis/` files are operator-merged into a separate repo; keep
  them in their record form.

## Never

- Put credentials in the guest or on any argv.
- Add capability caps, path allow-lists, or rate/iteration limits.
- Give the app a way to trust a client-sent edge credential when a key is set.
- Let session cleanup bypass `release()`.
- Parse the transcript JSONL while a session process is alive.
- Chown a shared (nlink > 1) inode to a slot, or read/serve a session file the
  session's uid does not own.
- Poll from the UI, or re-hash/re-sniff a file version a cache already knows.
