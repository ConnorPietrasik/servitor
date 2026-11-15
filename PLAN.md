# Servitor — implementation plan

**Handoff document for the implementing agent.** This file is the single decision
authority: it inlines every requirement, invariant, verified fact, and stage gate
needed to build the system. `AGENT-BRIEF.md` (design rationale), `report.md`
(deliberation record + evidence for every verified fact), and `DECISIONS.md`
(owner ratifications, 2026-11-14) are background reading — where they disagree with
this file, **this file wins**.

Working rules:

1. Build in stage order (§8). Each stage's gate is its acceptance criterion; do not
   merge a stage whose gate is red.
2. **No capability caps.** No wall-clock, iteration, token, vision-call, or file-size
   limiters anywhere. The controls are a human (steer/abort) and meters. If you find
   yourself wanting to add a cap, stop and re-read §3.
3. The prior review (report.md) found a dozen places where the *obvious* assumption
   about pi was wrong (silent empty sessions, eventless failures, non-interpolated
   `baseUrl`, context-file injection from ancestor dirs, …). Every pi behavior
   stated here was verified against **pi 1.5.2** with citations. Re-verify anything
   you plan to rely on; when the pi pin changes, the conformance suite (§8, stage 1)
   must pass green before the bump ships. That suite is permanent, not scaffolding.
4. The system must be buildable and runnable **entirely on a dev laptop with root**
   (DECISIONS D2) before anything touches the production server. The production
   deploy is one extra gated step, not a different system.

---

## 1. What is being built

A home assistant with coding ability, not a pure coding agent: a browser chat UI
fronting Pi (`@earendil-works/pi-coding-agent` 1.5.2, MIT) in a dedicated VM. A human
hands it files (including files beyond context — e.g. photos of a binder of Pokémon
cards), it writes and runs scripts to work on them, searches the web, runs as long
as it needs, and can be steered or aborted by the human at any point.

Components (one repo — `servitor/`):

| component | where | notes |
|---|---|---|
| Chat UI (React/Vite, static) | guest, served by the app | phone-first; same port as the API |
| HTTP/SSE API + control plane (Python 3.12+, FastAPI, single asyncio process) | guest, systemd service | **no end-user auth of its own** — the edge is the gate |
| Agent harness: one `pi --mode rpc` subprocess per session | guest | spawned setuid to a slot uid |
| Synced agent config (SYSTEM.md, skills, tool defs, `models.json`) + `lib/` | guest, read-only to the agent | one-way sync from this repo |
| pi-web-access (0.47.0; semver spec from `.env`, exact by default; sync-delivered) | guest `/opt/pi-ext/pi-web-access` | web search; keyed Exa by default |
| `deploy/` — guest image, units, firewall, sync script, portcullis artifacts | this repo, host-side | the deploy is versioned here |

**Runtime (DECISIONS D1):** the service is a **systemd service on the guest OS** —
`servitor.service` in `servitor.slice`, GC in-process. **No docker in the VM.** The
guest image (qcow2) is the reproducible artifact.

**Edge (DECISIONS D2):** the service is edge-agnostic. Contract: *one reverse proxy
routes one hostname to the VM ingress IP:port and is the only auth gate.*
Production: the owner's portcullis (Traefik file-provider + Authelia 2FA). Standalone
(laptop, tests, bare VM): bind loopback behind a local reverse proxy — same guarantee,
no code change. The app binds `SERVITOR_BIND` (ingress address in production,
127.0.0.1 standalone) on `SERVITOR_PORT` — both only ever from `.env`.

---

## 2. Environments

**Host** (reference: the owner's server, CachyOS, Threadripper Pro 9965WX, 256 GB,
managed by portcullis — Traefik file-provider only, no docker.sock mounted, Authelia
2FA, `make update` gated by `make doctor` with snapshot + auto-rollback).
Hypervisor: libvirt/KVM — **none is installed on the server yet**; host-side install + domain definition is stage-6 work (§13). The LLM router is the
host's existing OpenAI-compatible llm fleet (e.g. `https://27b.pietrasik.top/v1`,
bearer key = the host's `LLM_API_KEY`). The router does **not** hairpin: every LAN
machine that talks to it by name pins `*.pietrasik.top` → host LAN IP in
`/etc/hosts` (portcullis ships `tools/panel-hosts.sh` for this; the guest gets the
pin at provisioning, the laptop via that tool).

**Guest VM** (provisioned once, then synced):
- Arch-based minimal (CachyOS for host parity), recent kernel (overlay behavior
  verified on 7.3.3-1-cachyos).
- 24 vCPU, **no quota**, low host-side scheduling weight (a long run should own the
  idle machine).
- RAM: ceiling 30 GB, not a reservation — host: virtio-balloon + free-page-reporting
  so idle memory returns; guest slice: `MemoryHigh=25G` / `MemoryMax=30G` so the
  guest throttles before OOM; zram absorbs the rest.
- Disk: fixed-size qcow2, ~200 GB, sized at creation — no quota mechanism; near-full
  disk is a GC trigger (§5).
- **No GPU.** Model vision is a capability of the router's model, not guest work.
  GPU-compute tool calls (OCR/ASR/embeddings) are a future extension, out of scope.
- Network: LAN-reachable; firewall admits `SERVITOR_PORT` from the host's edge
  subnet only (plus loopback, established, and ops SSH from the LAN — see §9).
- The **state volume is the guest's ext4 rootfs area `/var/lib/servitor`** — a real
  filesystem, never container/overlay storage: overlayfs refuses `upperdir`/`workdir`
  on overlayfs (T29). Boot asserts it: `findmnt -T`/`stat -f` on the scratch path
  must not report overlay.

**Laptop (dev loop, DECISIONS D2):** root access. Runs: the conformance suite
(real pi from the pinned npm package + a local mock router — no server), the
`sandbox-smoke` half (root has CAP_SYS_ADMIN: overlay mounts, EROFS, sweep walks),
the `vm-smoke` half (setuid spawn, chown handoff, cross-uid EPERM, real sweep),
the app natively with standalone loopback bind, and the full guest under QEMU/KVM
when needed. The router is reachable by public URL off-LAN or via the hosts pin on-LAN.

---

## 3. Invariants (load-bearing — a build that breaks these is wrong)

Privilege model: **the app is privileged (root on the guest); the agent never is.**
Each session's pi runs as a **slot uid** from a static pool of 16. The agent's uid is
never the app's uid — that is what makes the read-only config real (root could
remount it rw; the agent cannot be root).

1. **Prompt injection is the dominant threat.** Search results, fetched pages, and
   uploaded files steer an agent that holds credentials and a network position.
   Mitigation ladder: VM boundary (no egress filtering by design — exfiltration to
   the open internet is the *accepted residual risk*), read-only where injection
   could persist (config, `lib/`), content-addressed uploads, context-file discovery
   disabled, launch-line trust flags asserted in CI, per-session uid + overlay +
   owner-sweep, `lib/` writable only by a distinct human action. One further
   accepted residual with a warning (owner decision D3): baseline extension
   discovery means an extension an agent plants in its own tree loads on the next
   respawn — visible (new-extension warning), not blocked; it adds no reach the
   slot uid doesn't already have, only an unattended trigger point.
2. **No caps** (§8/§6 of the brief): no path allow-lists, no iteration/wall-clock/
   vision limits. Reach is bounded only by the slot uid and read-only areas. Controls
   are meters (run active, elapsed, current tool, subprocess count, scratch usage,
   outbox size) + human `steer()`/`abort()`.
3. **The API authenticates nobody.** Anything that can reach the socket can drive the
   agent; reachability is the control. Bind the ingress interface, admit the port
   from the edge subnet only; standalone binds loopback.
4. **The producer never awaits a client.** Dropped phone connections never cancel,
   slow, or stall a run. Delivery is a separate concern from the run (§6).
5. **The key is env-only per subprocess.** `OMNISSIAH_API_KEY` is passed to each pi
   subprocess by env only — never on argv (visible in `/proc/*/cmdline` to every
   slot), never in the synced config, never in a transcript or app log.
6. **Session existence is the app's fact, not pi's.** The registry owns sessions and
   records pi's `sessionFile`; empty sessions are never persisted by pi, so the app
   must not guess.
7. **One process per session file at a time** — live run, respawn, or probe.
8. **Release is the only cleanup path**, used by all four triggers, and a slot is
   never handed out before its sweep verifies clean.
9. **The run does not need a client.** Zero subscribers ⇒ the log keeps growing; no
   stall, no cancel. Abort is an explicit POST, never a side effect of a socket dying.
10. **Config reaches a session only at spawn.** A live session keeps the config it
    started with (copy-up/overlay semantics) — do not "fix" hot-reload; pi does not
    support it.
11. **Artifacts leave through the outbox/download endpoint or `lib/`** — and only
    "save this script" into `lib/` is ever a human UI action. There is no
    agent-reachable write to any durable artifact.
12. **No git credentials in the VM.** The host does all git reconciliation.
13. **pi's session JSONL is opaque payload.** The app never reconstructs the active
    branch or compaction state by parsing it — that is pi's contract.

---

## 4. Verified pi facts (pinned 1.5.2 — cite, don't re-research; re-run conformance on any pin change)

RPC protocol (`docs/rpc.md`):
- JSONL over stdio, one subprocess per conversation. **LF-only framing** — do not use
  Node `readline` (splits on `U+2028`/`U+2029`, legal inside JSON strings), and the
  Python equivalent: read bytes, split on `\n` only; text-mode universal-newline
  `readline()` splits on embedded CRLF *and rewrites it*, corrupting JSON strings.
  (report.md N18.)
- Correlate by `id`. Subscribe to events **before** prompting. stdout is protocol-only;
  diagnostics on stderr (log it, never parse it).
- `prompt` response `disposition`: `started` | `queued` | `handled`. `handled` =
  consumed, no run started — do **not** wait for `agent_settled`. Otherwise wait for
  `agent_settled` (not `agent_end`).
- **A failed `prompt` response (`success: false`) is terminal.** Some failure modes
  emit **zero events** (no `agent_start`, no `turn_start`, no `agent_settled`;
  `get_state` still returns `success: true` and the process idles — the system looks
  healthy while inference is permanently dead). End the run with the sanitized
  reason, surface it in the meters ("failed, not settled"), arm no settle timer. No
  automatic retry (a dead credential is a config fault; retrying burns the slot).
  The no-API-key repro is a permanent conformance regression test.
- Orderly shutdown on stdin EOF waits for pending work before exiting — that is the
  first rung of every kill ladder. Once a shutdown has started (from stdin EOF or a
  signal), a further single signal does not interrupt it; a **second** signal exits
  at once (rpc.md) — the ladder's SIGKILL rung is what guarantees exit. Commands
  travel only over stdin, so no RPC (including `abort`) can be sent after the EOF —
  the §5 drain sends the abort first.
- Steer/follow-up/abort are RPC commands (`abort`, `steer`, `follow_up`) — no custom
  protocol needed. `get_entries(since)` gives a durable entry-id cursor across
  client restarts.

Sessions (`docs/session-format.md`, `docs/cli.md`):
- `--session-id <id>` (first spawn) resolves per-cwd and **silently creates an empty
  session when the cwd differs**; resume must use `--session <path>` with the
  `sessionFile` captured verbatim from `get_state`. A missing recorded workdir makes
  pi exit 1 with **no response** ("Stored session working directory does not exist")
  — mount before spawn/respawn and treat it as a fatal spawn error routed to
  release, never a retry loop.
- Empty sessions (never prompted) are **never persisted** — a registry row without a
  transcript is "idle, nothing to resume", not an error.
- `--session-dir` overrides `PI_CODING_AGENT_SESSION_DIR`; use **exactly one
  mechanism** — the `--session-dir` flag, identically at spawn/respawn/probe — and
  never set the env var (passing both means one silently lies). With an explicit
  session dir, transcripts land flat.
- After **every** spawn/respawn, assert identity from the protocol:
  `get_state.sessionId` == the minted id, and (respawn) `get_state.sessionFile` ==
  the recorded path — else fail closed and route to release. Never detect bad
  respawns from stderr: the "No project session found … creating a new session"
  warning also prints on legitimate first spawns.

Config & credentials (`docs/models.md`, `docs/custom-provider.md`,
`docs/environment-variables.md`):
- `PI_CODING_AGENT_DIR` must be **writable and per-session**: pi `mkdir`s
  `<agent-dir>/sessions/…`, `trust.json.lock`, and writes `auth.json` /
  `models-store.json` at startup. A read-only agent dir is either a startup crash
  (`EROFS … trust.json.lock`, exit 1, no response) or — with default project trust
  off — a **healthy-looking, permanently unable-to-run** session (`prompt` →
  `success:false`, zero events). Both are negative-gate failures (report.md N42).
- `models.json` is read **only from the agent dir**. `apiKey` supports `"$ENVVAR"`
  interpolation; **`baseUrl` does not** (`$VAR` stays literal → `Invalid URL`).
  So: literal router URL in the synced config, `apiKey: "$OMNISSIAH_API_KEY"`, and
  the key only ever in the subprocess env.
- **Never ship `auth.json`/`models-store.json` in the image or synced config** —
  credential precedence puts a stored credential above the provider env var, so a
  stale one wins and "env-only" is quietly false.
- `get_state.model.id == "unknown"` is the single no-usable-model signal (models.json
  absent *or* a declared model's credential failing to resolve) — the spawn path
  fails closed on it (config error, session not runnable) before any prompt.
- Env: pass an **explicit allowlist** (never inherit the app's environment).

Trust & context files (`docs/security.md`, verified behavior):
- pi loads `AGENTS.md`/`CLAUDE.md`/`.pi` context files from the cwd **and every
  ancestor directory, with no project-trust prompt** — content-addressed upload
  naming does not stop it (the agent can copy an upload into its own workdir).
  Reproduced: planted `AGENTS.md` in the workdir *and* one ancestor level up both
  reach `messages[0].role=system`; the launch flag removes both. So: **always
  `--no-context-files`**, and the session tree root must have an ancestor chain free
  of those filenames (never nest sessions inside a git checkout; doctor asserts).
- `--no-approve` always; `--approve` never appears in the launch line (with
  `--approve`, generated code could write `.pi/SYSTEM.md` in the agent-owned overlay
  and rewrite the system prompt of the *next* spawn after the 1 h idle-kill
  respawn).
- Extension dialogs (`select`/`confirm`/`input`/`editor`) **block the child until
  answered**; with no client attached a run can hang indefinitely. Fire-and-forget
  records (`notify`/`setStatus`/`setWidget`/`setTitle`/`set_editor_text`) expect no
  response — a stray reply desynchronizes the sub-protocol.
- Pi ships **without** sub-agents, plan mode, a built-in sandbox, or approval gates.
  SDK sessions do not auto-load `codemode`/`tool_search`/MCP; CLI sessions load the
  built-in extensions (codemode among them) by default, but the `codemode` *tool*
  is only declared when `defaultTools` includes `+codemode` — servitor does **not**
  declare it (off per D5; verified mechanics §7).

Wire contract (`docs/json.md` — drop-safe by contract):
- `text_end`, `thinking_end`, `toolcall_end` carry the completed content;
  `message_end.message` carries the whole message. **Deltas are disposable.**
  `thinking_start/thinking_delta/thinking_end` exist even for non-reasoning configs
  (thinking was observed streaming with reasoning off) — the verbosity filter runs
  on every run.
- Thinking is persisted in `AssistantMessage.content` — never streaming it loses
  nothing; the UI can fetch any block later.

Search extension: pi-web-access (MIT), version-selected from `.env` (npm semver
spec — exact by default, `latest` if you want to track; the conformance suite +
doctor are the control, the value is just the record) and **delivered by the sync
pipeline** (host-side npm fetch at the selected version into a staging prefix →
stage + rename onto the guest; the guest never touches the registry — zero
start-time contact holds, and a version bump is a sync, never an image rebuild);
loaded by explicit
`-e` path — no `packages:` entry in the synced config (exact pins are not re-checked
against npm at start; unpinned configured packages are). Why `-e` rather than an
`extensions:`/`packages:` entry in the synced settings: that file is shadowable in
the agent's own overlay upper (Q8 class) — a shadowed settings could silently
un-load the extension on the next respawn; the `-e` declaration lives on argv, the
one surface the agent cannot rewrite, so the provisioned search capability cannot
disappear on its own. (The stage-1 gate and doctor assert the tools array anyway,
catching a pi regression or a shadow regardless of mechanism.) Baseline extension
discovery is otherwise **on** (owner decision D3) — the per-spawn new-extension
warning is the compensating control (§7). A version bump takes effect at the next
spawn (the extension loads at spawn — same semantics as the config swap). Its fetched-content cache default lives in the
shared agent dir — relocate per session with `PI_WEB_ACCESS_CACHE_ROOT` into
`scratch/<session>/`. Key plumbing — **verified against the pinned 0.47.0 dist
(2026-11-15), stage-1 open question closed**: the config lookup is
**agent-dir-based** (`getWebSearchConfigDir()` returns `PI_CODING_AGENT_DIR` when
set — which servitor always sets — before the XDG/homedir fallbacks), and the Exa
credential resolves **env-first**: `EXA_API_KEY` in the subprocess env beats any
`web-search.json` literal (`resolveCredential` falls through to
`environmentValue ?? configuredValue`). So the key rides the spawn env allowlist
like `OMNISSIAH_API_KEY` (invariant 5), servitor never writes a key file, and
nothing credential-shaped ever enters the synced config tree or a session file.
Do not set `PI_OFFLINE` in the launch line — verified in the 1.5.2 dist, it only
disables model-catalog network refresh (`modelNetworkEnabled`, the remote catalog
fetch), not inference or the extension's fetch path, so it buys nothing for a
static-`models.json` deploy; the pin already prevents npm re-checks.

---

## 5. Storage, isolation, lifecycle

### Areas (state volume `/var/lib/servitor`, guest ext4)

| area | contents | owner | lifetime |
|---|---|---|---|
| `config/v<N>/` | synced agent config, versioned dirs; active pointer | root (0755 — slot uids cannot write) | until superseded by sync; a live session's lower is never mutated |
| `lib/` | scripts the user chose to keep — the only durable, reviewable artifact | root (0755; app writes via save-script) | forever; **never GC'd** |
| `inbox/<s>/` | what the user sent (content-addressed names) | app writes, chowned to slot uid | until session release |
| `scratch/<s>/` | all generated junk: overlay `upper`/`work` dirs, agent-dir upper, venv, web cache | slot uid | until session release |
| `sessions/<s>/` | pi transcripts (`--session-dir`, flat) | slot uid | until session release |
| `outbox/` **inside the session workdir** | files the agent is handing to the user (convention, §7 UI) | slot uid | until session release |
| `/home/slotN` | slot uid's `$HOME` (caches, config) — follows the uid, never an env var | slot uid | swept at release |

"Read-only to the agent" = root-owned, slot-uid-unwritable. The real mounts are:
per-session overlay (workdir), per-session overlay (agent dir), the `inbox/<s>`
bind into the workdir, and the single-file read-only bind of `models.json` (§4,
T34 — verified EROFS on write, EBUSY on unlink, even as root).

### Slots

- **16 static slot uids** (`slot1`..`slot16`), fixed uids, real passwd homes
  (`/home/slotN`, 0700), created at guest image build. `$HOME` follows the uid:
  a shared uid with per-session `$HOME` leaks the first time a script forgets to
  export it, and concurrent sessions collide in `~/.cache`, uv/pip/npm caches,
  `__pycache__`. Signals are uid-gated, not cgroup-gated: generated code does dumb
  global things (`pkill -f python`, `kill -9 -1`) and a distinct uid keeps one
  session from killing another's tooling or the (Python) app. Owner-based sweep is
  the clean-handover mechanism. `DynamicUser=` rejected: recycled uids break
  "look at it tomorrow" and a uid change triggers recursive re-chown.
- 16 slots = 16 *live* sessions (running **or** awaiting release), not 16
  simultaneous runs. A **stable** session (user mark) pins its slot until closed:
  never GC'd, revisitable, junk intact until release. 16 stable sessions block new
  ones until one is closed — the panel says so.

### Per-session mounts (mounted at allocate, before **any** spawn/respawn)

- **Workdir**: overlay, lower = `/opt/toolkit` (baked toolkit, §6), upper/work =
  `scratch/<s>/{upper,work}`, merged at the stable per-session workdir path — that
  path is the recorded cwd. `inbox/<s>` bind-mounted into it as `./inbox`.
- **Agent dir**: overlay, lower = `config/v<N>` (the version this session booted
  from — a plain subdirectory, **never a mount** — overlayfs won't show a nested
  mount inside a lowerdir), upper/work = `scratch/<s>/{agent-upper,agent-work}`.
  `PI_CODING_AGENT_DIR` = the merged path. Single-file read-only bind of
  `models.json` over the merged dir (the agent could otherwise shadow it in the
  upper and steer the *next* run's endpoint — the bearer key rides along).
- Per-session venv under the workdir (`uv`, `--system-site-packages` so the baked
  Pillow/numpy are visible — otherwise every session reinstalls the world); dies
  with the session.
- Mounts live for the **session** lifetime, not the process lifetime: pi runs a
  session in its recorded cwd, so idle-kill never unmounts — only release does.

### Subprocess lifecycle

- Spawn: pinned absolute pi path + the launch line (§7), cwd = merged workdir,
  explicit env allowlist, setuid to the slot uid (helper asserts the child's uid).
  First spawn `--session-id <uuid>`; respawn/probe `--session <recorded path>`.
- **Idle kill after 1 h**, only when truly idle: not streaming, not compacting,
  `pendingMessageCount == 0`, no subscribers. The kill fires **only after
  `agent_settled`** — a run in progress, however long (a 20-minute test script is
  a single turn), is never eligible. Ladder: stdin EOF → SIGTERM grace → SIGKILL.
  Never unmount or delete.
- **Respawn on pickup** (any user action: prompt/steer/follow_up/abort; opening a
  session for reads is a probe, §below — but the read path must be able to wake a
  dead process for user actions). Respawn only when the registry says a transcript
  exists. No live process for the slot uid (proc scan) — a crash-orphan still
  running is killed first (ladder); then spawn. Post-spawn identity assert (§4);
  missing cwd ⇒ fatal ⇒ release.
- **Probe** (reads of sessions with no live process): short-lived RPC spawn —
  `--session <path>` → `get_messages`/`get_session_stats`/`get_tree` → stdin EOF,
  ~2 s, no model call. Runs as the **slot uid with the exact run launch path**
  (same mounts, same flags) — a probe as the app uid would drop root-owned files
  into slot-owned dirs and break the sweep invariant. One process per session file.
  Never the first process to touch a session's agent dir (assert: registry says a
  transcript exists). Results briefly cached. **The session list reads the registry
  only — O(1), zero pi processes, always.** A passive phone poll of 15 dead
  sessions wakes nothing.
- **App restart/upgrade**: children cannot be adopted (closing the stdio pipe's read
  end gives the orphan EPIPE on its next stdout write — demonstrated). So:
  **abort-then-drain, with cause** — stop GC/pressure triggers, refuse new prompts,
  then send RPC `abort` with recorded cause to every run-active child **while its
  stdin is still open** (commands travel only over stdin — once it is closed the
  abort is unsendable, so the abort must precede it), mark the session
  `interrupted`, and only then close stdin (orderly shutdown; the run is already
  aborted, the session is idle, pi exits promptly). Bounded grace for settle + exit
  (from `.env`, default 90 s — **strictly less than the unit's `TimeoutStopSec`**,
  or systemd escalates to SIGKILL mid-drain and the recorded cause lies); any
  child still alive after the grace is finished by the ladder (SIGTERM → SIGKILL —
  SIGKILL never first, never before the abort was sent). Session files and mounts
  survive; next pickup respawns. This is **app-shutdown behavior only** — during
  normal operation (a 20-minute test included) nothing here fires; the run ends
  only by finishing, by the human, or by an explicit app stop (interrupted +
  resumable). A supervisor-socket layer for live-migrate is a separate, costed
  decision — not v1.
- **Unclean restart (crash / `kill -9`): the drain never runs.** The live child is
  orphaned and — by orderly shutdown on stdin EOF — **keeps running to
  `agent_settled`, still writing the session file** (a 20-minute test included).
  No release trigger fires for it: release is the *session's* cleanup path, and
  this session must survive the restart (transcript + slot intact — the stage-2
  crash drill expects a respawn, not a release). Boot reconciliation (stage 2)
  closes the window: proc-scan for live slot-uid processes, kill strays (the
  ladder — release step 3's primitive), mark the corresponding sessions
  `interrupted`, **before any respawn**; the respawn path re-checks its slot uid
  for a live process and kills it first if present — invariant 7 is enforced at
  the spawn boundary, not assumed.

### Update waiting (deferred restart — sync/update never blocks, never fails)

`deploy/sync.sh` / `servitorctl update` may be called by other scripts at any
time — including mid-run. They never wait on a run and never error:

- **Everything non-disruptive proceeds normally**: the host-side gate, the
  versioned config swap (stage + rename, active pointer — new spawns take the
  new version at spawn; live sessions keep theirs, invariant 10), the extension
  stage + rename, the key slice, and (for a pi bump) the image rebuild. The
  service restart is the only disruptive act, and the only deferred act.
- **Run active ⇒ flag, don't restart.** If any session has a live run (the §7
  "live run" set — streaming / compacting / pending / dialog), the script sets
  the **update-waiting** flag (a state-volume marker the app mirrors into the
  registry), skips restart + doctor, prints one status line (`update staged;
  restart pending (run active)`), and **exits 0**. Repeated calls are
  idempotent; the newest staged version wins.
- **The in-process tick consumes the flag**: flag set, staged version ≠ running
  version, and no session has a live run ⇒ the app triggers an orderly service
  restart (`systemctl restart` from the app — the unit's stop path is the
  existing abort-then-drain stop path, instant when nothing is in flight). The new
  process comes up on the new config and is certified by the §9 doctor before it
  serves (red ⇒ the §9 rollback: restore the retained previous artifact,
  restart, doctor). The flag clears only on green doctor; a no-op flag (staged
  == running) clears silently without a restart.
- **Image-level changes are never auto-applied** (a qcow2 swap needs VM
  downtime): a pi bump staged mid-run leaves the flag marked *image-swap
  pending*; it applies only by explicit operator action (a `servitorctl update`
  re-invocation) as a planned downtime. No automatic downtime, ever.
- Visible wherever state is: panel (`update waiting — staged 14:02, applies when
  idle`), `GET /meters`, `servitorctl doctor` (while set: reports staged vs
  running version as a warning and certifies the running state, not the `.env`
  target — never a failure while the VM serves), and the session event feed.

### Release (the single cleanup path — UI close, GC timer, slot pressure, disk pressure)

1. Mark `draining` in the registry **before** side effects (a crash mid-release must
   not hand out an unswept slot).
2. Kill the subprocess (ladder) if alive. Slot/disk pressure **never releases a
   session with a live run** — skip and report. The idle GC likewise.
3. Kill strays by uid (everything the slot uid still owns).
4. Unmount (reverse mount order).
5. **Sweep by owner**: `find / -user slotN` — its home, `/tmp`, everything dropped
   outside `scratch/` — and delete.
6. Delete `inbox/<s>/`, `scratch/<s>/` (overlay uppers included), `sessions/<s>/`.
7. Free the slot only when the sweep is **verified empty** (state: allocated →
   cleaning → free). A slot that fails its sweep is **quarantined, visible in the
   panel, retryable, never reused**.

GC trigger: in-process timer (every few minutes) releases sessions idle >
`gc_idle_days` (default **14**, `.env`) that are not stable and not running.
Disk pressure is a **co-equal** trigger (200 GB is finite and the card-photo
workload generates bulk): `statvfs` on the scratch filesystem (not `/`) near full ⇒
release oldest non-stable until headroom. Slot pressure: no free slot ⇒ release the
oldest non-stable non-running session and take its slot; all 16 running ⇒ the new
session **waits** and the panel says so. Junk size is reported to the panel —
unbounded mess is only dangerous when invisible.

### Uploads

Received by the privileged app: **streamed to a temp file while hashing** (never
buffer a >context upload in memory), sha256 **content-addressed name** (client
filename discarded — and content-addressed names stay forever; restoring client
filenames is the trap), **MIME by magic** (python-magic), atomic rename into
`inbox/<s>/`, **chowned to the slot uid before the agent can see it**, no
root-owned partial survives an aborted upload. Later app writes into the
agent-writable inbox use `O_NOFOLLOW|O_EXCL` from a dirfd. An upload literally
named `AGENTS.md` must never load as a context file (gate: sentinel test, §8).
Prompt attachments: user-attached **images** are inlined via the RPC `images`
field; other files are referenced by path in the message.

### "Save this script" → `lib/`

A **distinct human UI action** — never agent-callable, never triggered by agent
output (an agent-reachable write to the one durable artifact is the persistence
channel). Source: a path the human picked from the session's file listing,
resolved inside the session's merged tree via dirfd-relative opens with
`O_NOFOLLOW`, regular file only — the app runs as root and the tree is written by
the slot uid, so the agent can plant symlinks to anywhere; the app's own privilege
is what makes this dangerous. Destination name normalized, collision-appended with
a short hash, never taken verbatim from agent text. Copy as the app uid, with a
provenance sidecar (session id, source path, sha256, size, timestamp) the human
reviews. `lib/` stays read-only to the agent; git reconciliation happens
host-side (the host pulls `lib/` back; no push credential in the VM).

**Every app-side read of an agent-owned tree** (transcript of a dead session, lazy
tool-result bodies, outbox/file listing + download, save-script source) follows the
same confinement: dirfd-relative open, `O_NOFOLLOW`, refuse symlinks and
non-regular files, never accept a resolved path from the client.

---

## 6. Guest image & toolkit

Built by `deploy/guest/` (qcow2 + cloud-init or arch-chroot script; the image is
the versioned artifact — images are named by pi version + date, and the previous
image is retained for update rollback, §9):

- Base: Arch-based minimal; kernel current.
- `node` (LTS, pinned) + **pi from `SERVITOR_PI_VERSION`** (npm semver spec,
  exact by default; `npm` install `--ignore-scripts`, absolute CLI path constant
  in the harness — never PATH resolution). Not bumping is a supported steady
  state: the guest runs the last-verified version indefinitely, nothing
  auto-updates.
- Python 3.12+ + `uv`; app dependencies (FastAPI/uvicorn, python-magic → libmagic).
- **Toolkit at `/opt/toolkit`** (root-owned 0755 = read-only to slots; a plain
  directory — the overlay lower, never a mount): python env with **Pillow, numpy**,
  `uv`, plus system **ffmpeg, ImageMagick, yt-dlp, ripgrep**. The toolkit root must
  contain **no** `AGENTS.md`/`SYSTEM.md`/`APPEND_SYSTEM.md`/`.pi` — it is the
  overlay lower of every session's cwd.
- **No pi-web-access in the image** — `/opt/pi-ext/pi-web-access` is created by
  the sync pipeline (version from `SERVITOR_PI_WEB_ACCESS_VERSION`, stage +
  rename, installed version stamped in the registry — §4);
  loaded by explicit path (baseline discovery otherwise on, D3).
- 16 slot users (fixed uids, 0700 homes); the app runs as root.
- `/var/lib/servitor/{config,lib,inbox,scratch,sessions}` pre-created.
- systemd: `servitor.slice` (`CPUWeight` low — avoid starving other processes;
  `MemoryHigh=25G`, `MemoryMax=30G`), `servitor.service` (root, `EnvironmentFile`
  0600, `TimeoutStopSec=120` (above the default 90 s drain grace), `Restart=on-failure`),
  GC in-process. zram enabled.
- nftables: default-deny input (except loopback/established/ops-SSH from LAN);
  admit `SERVITOR_PORT` from `SERVITOR_EDGE_SUBNET` only (derived on the host:
  `docker network inspect edge -f Subnet` — traffic from the Traefik container
  arrives NATed through the host).
- `/etc/hosts` pins for the router names → host LAN IP (no-hairpin workaround).
- **No** docker, **no** git credentials, **no** `auth.json`/`models-store.json`
  anywhere, no context files anywhere under the session root's ancestors.

---

## 7. Launch line & delivery contract

### Launch line (one constant; CI asserts the exact argv by lint/AST on the spawn
path, plus the sentinel tests as a standing regression — an argv check alone would
not catch a future pi default change)

```
<node> <absolute pi cli path> \
  --mode rpc \
  --no-approve --no-context-files \
  --session-dir <state>/sessions/<s> \
  (--session-id <minted-uuid>   # first spawn
  |  --session <recorded sessionFile>   # respawn / probe) \
  -e /opt/pi-ext/pi-web-access
```

cwd = merged workdir. Env allowlist: `PATH`, `HOME`, `USER`, `LANG`,
`PI_CODING_AGENT_DIR` (merged agent dir), `OMNISSIAH_API_KEY`, `EXA_API_KEY`
(search extension key — verified env-first in the 0.47.0 dist, §4; no key file is
rendered), `PI_WEB_ACCESS_CACHE_ROOT` (`scratch/<s>/web-cache`). Never `--api-key`. Never `PI_CODING_AGENT_SESSION_DIR`. Never
`--approve`. Never `PI_OFFLINE`. Never `--no-extensions` (D3 — baseline extension
loading is deliberate; the compensating control is the new-extension warning, §7).

`models.json` (in the synced config lower): `api: "openai-completions"`, provider
+ per-model `baseUrl` **literal**, `apiKey: "$OMNISSIAH_API_KEY"`, the image-capable
model(s). `.env` carries the key (and the Exa key) — nothing else secret. No
per-spawn rendering step.

Per-spawn/respawn assertions (fail closed): `get_available_models` lists the
intended provider with the intended `baseUrl` and declared tool set;
`get_state.model.id != "unknown"`; identity asserts (§4). A live session's config
version is stamped in the registry row and shown in the panel; a respawn under a
newer config is recorded in the session event feed (there is no RPC surface to
inject a system-level note into a resumed transcript — the version change is
panel/feed metadata, not transcript content). Sync swaps in
a **new versioned config dir** (stage + rename; a live lower is never mutated).

`settings.json` (synced): default tools only (read/bash/edit/write — **codemode
off**, D5), compaction on; plus the `skills` resource array when the owner adds
skills (§4).
**Codemode mechanics — verified 2026-11-14** (mock router; the captured request's
`tools` array is authoritative — the system prompt string is a red herring, it
mentions "codemode" in its docs index even when the tool is gone): the tool is
declared only by `+codemode` in settings (or `--tools +codemode`); adding
`--no-extensions` removes the built-in codemode extension and `+codemode` alone
does not restore it (silently ignored); `-e builtin:codemode` would.
**Owner decisions (D3 + D5): run baseline (no `--no-extensions`) but do not
declare the tool — codemode stays off.** The built-in extension loads naturally and
simply stays undeclared (no ~3k token/call prompt cost, and the agent scripts via
Python/bash anyway — same reach, §4 residual analysis). Enabling later is one line
in the synced settings (`+codemode`) + a sync; these verified mechanics are the
reference for that and for any pi upgrade. The launch line only pins the search
extension explicitly. Accepted residual (documented, owner 2026-11-14):
baseline discovery also loads an extension planted in the session's own agent
dir/workdir — it fires on the next respawn. Compensating control is a **warning,
not a block**: the spawn path records the loaded-extension set per spawn (`get_
commands` delta + the already-surfaced agent-upper file list, Q8 policy); anything
beyond the image baseline ⇒ warning in the panel, in `servitorctl doctor`, and in
the session event feed — the run is unaffected. The exposure is session-scoped
(dies at release; the sweep takes the planted file) and adds no reach the slot uid
doesn't already have (arbitrary code execution + the key are accepted residuals
per §3) — only an unattended trigger point, now visible. Gate: assert the captured
request's `tools` array contains the four pi-web-access tools (`web_search`,
`source_check`, `fetch_content`, `get_search_content`) and **no `codemode`** (off
per D5 — the gate asserts the declared set; re-enabling flips this assertion).

`SYSTEM.md` (synced): explains the workdir layout — `inbox/` = user uploads,
`outbox/` = files for the user ("put anything the user should receive in `outbox/`;
everything else in the workdir is scratch"), `lib/` = saved scripts (read-only) —
and the beyond-context workflow (process large uploads with scripts + web research,
not by reading them into context). Contains a marker string used by the config gate.

### Skills (future workflow — no code change; verified against pi 1.5.2 docs)

`--no-context-files` disables **only** `AGENTS.md`/`CLAUDE.md` discovery (cli.md)
— skills are a separate subsystem with their own `--no-skills` flag, which
servitor does **not** use: directories containing `SKILL.md`, each advertised by
name + description in the system prompt, full body loaded on demand by the model
when a task matches (an explicit `/skill:name` forces it in interactive use).
To add skills later: write `config/skills/<name>/SKILL.md` (+ bundled
scripts/references) into this repo, add `"skills": ["skills"]` to the synced
`settings.json` (user settings resolve from the agent dir), `servitorctl sync` —
every session gets them. Skills an agent plants in its own home or workdir
(`.agents/skills/`) load too — the accepted D3/Q8 class (self-steering, visible,
session-scoped). This is also why `-nc` stays: `AGENTS.md`/`CLAUDE.md` are exactly
the filenames an injection can use — an upload named `AGENTS.md` that the agent
copies into its workdir would otherwise silently rewrite the next spawn's system
prompt; the channels above are the durable, human-authored ones.

### Extension dialogs

An extension dialog (`select`/`confirm`/`input`/`editor`) blocks the child until
answered. Policy — **never auto-allow** (a self-answered "allow dangerous command?"
is the persistence path), never a value, and **no deadline** (owner decision, D4
amendment 2026-11-14):

- **A dialog pauses the run and waits for the human indefinitely.** A dialog is a
  steer point, not a failure: the child blocks, no model call is in flight, so the
  wait spins nothing and costs one slot. The session counts as a **live run** — the
  idle killer is settled-only and GC/pressure never release a live run — so it
  cannot be swept out from under the user; the pinned slot is visible in the panel.
- **A returning client is re-offered the pending dialog.** On (re)attach, after the
  state + transcript fetch, any still-unanswered request is forwarded to the new
  subscriber, so the user answers it and the run resumes.
- The meters show "waiting on extension dialog (since 14:02)" as the current
  activity — a designed, visible state, never a mysterious stall. `abort` remains
  available for a dialog the user never wants to answer.

Every request + answer is recorded in the session event feed (the history shows the
ask and, whenever the user got around to it, the answer). Reply only to
`select`/`confirm`/`input`/`editor` with a matching `id`; never answer
fire-and-forget records (`notify`/`setStatus`/`setWidget`/`setTitle`/
`set_editor_text`) — a stray reply desynchronizes the sub-protocol.

### Wire contract (the §7 delivery spec)

- One asyncio producer per session drains pi events and publishes to
  **per-subscriber bounded queues that drop and coalesce on overflow and never
  block**. Dropped delta classes: classify by the `docs/json.md` contract, not by
  size.
- **Coalesce** thinking/text deltas into configurable flushes (default ~1 s;
  per-subscriber `?coalesce=`): per-token SSE over LTE is mostly framing overhead.
  **Flush the buffer before** `tool_execution_start`/`*_end`/`message_end` or the
  client renders a tool call ahead of the text that preceded it.
- **Heartbeat**: the SSE stream emits a keepalive ping (SSE comment line) at a
  fixed interval (default ~15 s, `.env`), so a long silent gap — a 20-minute bash
  call emits no events at all — does not idle-timeout the proxy connection. A cut
  connection is still harmless by contract (reattach is a fetch); the heartbeat
  just keeps the meters live instead of stalling.
- **Server-side** `show_thinking` per subscriber (a client that merely doesn't
  render still pays for the bytes if they're sent):
  - `none` — drop `thinking_start`/`thinking_delta`; the app synthesizes one
    compact "thinking… 12 s" line per block from start→end timing (per-subscriber
    state machine; handle the block whose end never arrives — abort).
  - `final` — emit only the authoritative `thinking_end` block (thinking is
    persisted in `AssistantMessage.content` anyway; the UI can fetch any block
    later).
  - `all` — everything (desktop default).
  The run is identical in all three — "thinking optional" means not sending it.
  Model thinking level is out of scope.
- **Collapse** tool progress into one status line per call; **omit tool result
  bodies** — the UI fetches them lazily by tool-call id.
- **Reattach is a fetch, not a replay.** On (re)connect the client gets: (1)
  authoritative state (run active/settled, elapsed, current tool — the meters),
  (2) the transcript (via RPC reads — `get_messages`/probe — never re-parsed
  JSONL branches), (3) re-subscribes to the live tail with an entry-id cursor. No
  client is expected to have seen every delta.
- **Abort** = `POST /sessions/{id}/abort` → RPC `abort`. A dead socket, a
  backgrounded tab, a dropped phone — never cancels a run.

### Meters (computed by the pump/OS — never client-reported)

Run active (`isStreaming`/`isCompacting` + events), elapsed (pump clock), current
tool (`tool_execution_start`), subprocess count (proc scan for the slot uid —
counts actual active conversations, not viewers), scratch usage (async `du`,
cached, so the session list stays O(1)), last activity, pending queue
(`pendingMessageCount` — "queued while you were away"), junk size per session,
outbox file count + size, config version, slot state, wait state. Never derive
"run failed" from settle — a dead router still settles; failure comes from the
prompt response / exit classification. A child OOM-killed by `MemoryMax` is
classified distinctly: shown as "killed by OOM", not hung or successful.

---

## 8. Stages & gates

Gate vocabulary: `make check` (contract lint) · `make test` (unit + conformance vs
mock router) · `make sandbox-smoke` (CI/laptop-root runnable: overlay mounts,
EROFS, whiteouts, upper paths, teardown order, sweep walk, config/credential/
context-file gates, the whole protocol suite) · `make vm-smoke` (guest/laptop-root
only: setuid spawn, chown handoff, cross-uid EPERM, real `find -user` sweep —
**loud-skip and merge-blocking** in CI, never silently green; every gate states
which half it actually ran) · `make doctor` (guest-side certification, §9).

**Mock router** (stage 1, permanent): a small OpenAI-compatible server
(test/) that records every request (headers, body) and returns scripted
completions — including thinking blocks (on every run, per the verified behavior),
parallel `tool_calls`, error responses, and long gaps. The entire driver is testable
without inference; **gates assert router-side** (a request arrived, with the right
Bearer/model/content) — a gate that only checks `disposition: started` or an RPC
response passes while inference is dead.

### Stage 0 — repo scaffold + interface contract
Build: repo layout (§10); `compose/docker-compose.yml` as **contract-only**
artifact (env var names, port, bind, mount layout); `deploy/portcullis/` route
file; `.env.example` (names every secret/config: `OMNISSIAH_API_KEY`,
`EXA_API_KEY`, `SERVITOR_BIND`, `SERVITOR_PORT`, `SERVITOR_EDGE_SUBNET`,
`SERVITOR_ROOT`, `SERVITOR_GC_IDLE_DAYS`, `SERVITOR_PI_VERSION`, `SERVITOR_PI_WEB_ACCESS_VERSION` (0.47.0) — both npm
semver specs, exact by default; the gate, not the value, is the control; coalesce
default (1000 ms),
`SERVITOR_DRAIN_GRACE_SECS` (90), heartbeat
interval (~15 s)); nftables script; deploy docs.
Gate: `make check` — every env var the app reads is named in `.env.example`;
nothing hardcodes an address/port/path; route yml parses; one published port.

### Stage 1 — mock router + RPC driver + isolation core + conformance suite (permanent, upgrade-gated)
Build: `app/rpc/` — spawn (cwd/uid/env-controlled), LF-only binary framer,
id-correlated request/response futures, always-on event dispatcher (subscribed
before any prompt), typed wrappers (prompt/steer/follow_up/abort/get_state/
get_messages/get_entries/get_tree/get_session_stats), extension-UI responder,
stderr capture (logged, never parsed), idle-kill + shutdown ladder, `success:false`
terminal handling, `model.id=="unknown"` fail-closed. `app/iso/` — state-volume layout, 16-slot pool (allocate/free, stable
pinning, pressure policy), per-session overlay mounts (workdir, agent dir) + inbox
bind + `models.json` single-file RO bind, chown-to-slot handoff, setuid spawn
helper (asserts child uid), owner-sweep walk, the single `release()` (§5),
versioned config swap (stage + rename). Conformance suite encoding
every §4 fact. The suite is **version-parameterized**: it takes a pi version +
extension path (default: the `.env` selections), installs the resolved pi into a
scratch prefix, and runs the full suite against that — this is the host-side
preflight of `servitorctl update` (§9).
Gate (green on the dev laptop, no server; vm-smoke half needs laptop-root): framing tests (U+2028/2029, CRLF, LF-only);
disposition matrix (`started`/`queued`/`handled`); settle-wait correctness;
abort/steer/follow_up during a live run; **T25 no-event failure** (unset key ⇒
`success:false`, zero events, idle process — driver fails the run, does not hang);
no-API-key repro as regression; `--session-id` first spawn + `--session <path>`
resume returning the transcript; missing-cwd exit-1-no-response ⇒ fatal-spawn
handling; launch-line constant asserted; **tools-array assert: the four
pi-web-access tools present and `codemode` absent in the captured mock request**
(declared set per D5 — standing regression either way); pi-web-access
0.47.0 loads via explicit `-e` with **zero** registry contact (assert); its key
plumbing recorded as a conformance fact (0.47.0 dist verified: config lookup is
`PI_CODING_AGENT_DIR`-based, `EXA_API_KEY` env preferred over a file literal — no
key file is ever written); **config/credential
gate** (runs offline — its isolation primitives are in-stage): (1) mock receives
`Authorization: Bearer <env value>` — the key travelled env→models.json→request,
nothing credential-shaped on disk or argv; (2) the mock-recorded leading system
message contains the SYSTEM.md marker and the declared skills block; (3) sentinel
`AGENTS.md` planted in workdir, inbox, and one ancestor level up is **absent** from
that system message; (4) **negative gate**: pointing `PI_CODING_AGENT_DIR` at a
read-only layout must fail (prompt `success:false` or startup crash; no request
reaches the router) so nobody "simplifies" the layout back into a broken one that
still passes `get_state`. Isolation gates: `sandbox-smoke` green (overlay
lower+upper/work on one fs; writes land in upper; whiteouts; EROFS on the RO
binds incl. `models.json` unlink-EBUSY; mount-before-spawn; teardown order;
sweep walk; config swap atomicity — no live lower mutated; state-fs-type assert
per T29) and `vm-smoke` green on laptop/guest (setuid spawn asserts child uid;
chown handoff; cross-uid `kill()` EPERM; real sweep-by-owner; full `release()`
path; sweep-fail ⇒ quarantine). Isolation is proven **before** stage 2 builds on
it.

### Stage 2 — session registry + lifecycle
Build: SQLite registry (id, display name, slot, state machine
`allocated|live|draining|cleaning|free`, stable flag, `sessionFile`, config
version, timestamps, junk-size cache, wait state, update-waiting flag) — the only source of session
truth; boot reconciliation (proc-scan for live slot-uid processes ⇒ kill
crash-orphans via the ladder + mark the corresponding sessions `interrupted` —
before any respawn); probe (§5); idle-kill (1 h, settled-only, ladder);
respawn (transcript-exists guard, identity asserts); abort-then-drain on app
shutdown (grace < `TimeoutStopSec`); GC + disk-pressure evaluation in-process
through `release()`; per-spawn extension recording — loaded set vs image baseline
⇒ new-extension warning (registry/panel/doctor/event feed; D3 — warn, never block).
Gate: app-crash/restart drill (kill -9 the app mid-run with the mock's long
tool call still in flight — the orphan keeps running; restart ⇒ boot
reconciliation kills the orphan via the ladder, registry reconciles, no double
slot allocation, session marked interrupted, transcript not interleaved; pickup
respawns from transcript as the sole process); probe rules (list = registry only,
O(1); probe as slot
uid with run mounts; one process per session file; probe never first to touch an
agent dir); idle-kill never fires mid-run/mid-compaction and never unmounts — a 20-minute
synthetic run (long scripted tool call via the mock) is never killed; respawn of a
transcript-less row = "idle, nothing to resume", not an error; planted-extension
test (drop an extension into the agent upper, respawn, assert the warning lands in
panel/doctor/event feed and the run is unaffected).

### Stage 3 — control plane: HTTP/SSE API + delivery
Build: all endpoints (§11); per-session producer → per-subscriber bounded queues
(discard/coalesce, never block); the wire contract verbatim (§7); meters; upload
pipeline (§5); outbox + file list/download (chunked streaming, HTTP Range, soft
UI warning threshold, **no size cap**); save-script; extension-dialog responder;
OOM-kill and prompt-failure classification surfaced in meters.
Gate (byte-level — assert on raw SSE bytes, not decoded objects): `none` = zero
thinking bytes + one compact line per block; `final` = only `thinking_end`;
`all` = everything; coalesce flush ~1 s ± config; flush-before-tool-ordering;
run unaffected by zero subscribers and by a subscriber that never reads;
reattach = state → transcript → live tail (entry-id cursor); abort via POST only
(close the socket, run continues); upload gates (content-addressing; `AGENTS.md`-
named upload absent from the system message — assert via the mock-recorded
request; MIME-by-magic; chown-before-visible; atomic; no root partial on abort);
file-endpoint confinement (planted symlink → other session's inbox, symlink →
`/var/lib/servitor/...`, and a fifo: all refused/handled without hang; Range
resumes a large file); save-script (agent-cannot-trigger: no tool, no RPC surface,
no magic-directory convention; source confined; provenance sidecar present);
dialog wait: a pending dialog survives a subscriber disconnect — kill the client,
reconnect, the dialog is re-offered and answerable, the run resumes on the answer,
and no auto-decline ever fires; ask + answer recorded in the event feed; router
outage mid-run surfaces
as a run error (pi's retry/compaction events visible), never a hang or harness
crash.

### Stage 4 — React UI (phone-first; served statically by the app, same port)
Build: session list (meters, stable pins, slot usage, wait state, junk size,
config version, quarantined slots); chat view (authoritative messages, thinking
per the client's mode, collapsed tool status lines, lazy tool-result expansion);
composer (send = steer while a run is active, explicit follow-up; file upload with
per-file status; show_thinking + coalesce settings); always-visible **abort**
button; **outbox view** (first-class: name, size, mtime, sha256, download — with
the soft size warning); close-with-confirm (aborts first when a run is active);
stable toggle; reattach on load (fetch, never replay). **Last slice: fork/branch**
— `get_tree` read-only view, `get_fork_messages` + `fork` ("edit that message"),
`switch_session` navigation — built after everything above so it cannot delay the
phone-critical path.
Gate: full interaction pass at phone viewport (narrow, slow): start a run, lock
"the phone" (kill the connection) mid-run, come back → state + transcript + live
tail, nothing lost; abort from the list and from chat; upload >context-sized file
with per-file status; outbox download of a multi-GB file resumes after interrupt;
meters update without a live socket (poll).

### Stage 5 — guest image + standalone deploy + doctor
Build: `deploy/guest/` image build (§6); `servitorctl doctor`; `deploy/sync.sh`
(host-side: stage+rename versioned config swap, `lib/` push, key slice injection
into the 0600 env file, pi-web-access install — host npm at the
`SERVITOR_PI_WEB_ACCESS_VERSION` selection into a staging prefix, stage + rename
to `/opt/pi-ext/pi-web-access`, installed version (read from the installed
package.json) stamped in the registry — service restart on shape change, doctor;
update-waiting flag when a run is active, §5);
standalone mode
(loopback bind, no code change).
Gate: image builds reproducible; on a fresh guest (laptop QEMU or the real VM):
`doctor` green — no-prompt spawn (~1.5 s, no model call) asserts
`get_available_models` lists the intended provider + `baseUrl`; model id + context
window match the intended values (runtime discovery — the sandbox-verified
Qwen3.8-27B/250k is the *expected* value, not a constant); `model.id !=
"unknown"`; state-fs-type assert; ancestor context-file-free assert; launch-line
assert; firewall probe (port open from edge subnet, closed from a second address);
`sync.sh` clean run; **live search works** (keyed Exa via `EXA_API_KEY` env; key
material absent from synced tree, session files, app logs, and no
`web-search.json` exists anywhere); one **live prompt** against the real router
end-to-end (mock no longer in the loop); standalone: reachable on loopback behind
a local reverse proxy; **`update` drills**: no-op run when selections are
unchanged (no restart); extension-spec bump → update → doctor green → new version
at next spawn; doctor fault-injected mid-deploy ⇒ automatic rollback restores
the previous versions, service healthy; **`update-waiting` drill**: with a run
active, `sync` stages everything non-disruptive, sets the flag, and exits 0 (no
hang, no error); the in-process tick applies the restart + doctor at the first
idle moment; an image-swap-pending flag survives until explicit apply.

### Stage 6 — portcullis integration + server deploy + ops hardening
Build: operator merges `deploy/portcullis/` into the portcullis repo (route file →
`compose/traefik/dynamic/`; panel-do actions `servitor-status`, `servitor-vm-
restart`, `servitor-sync`, `servitor-doctor` → Go ACTIONS registry); portcullis
`make update` green; host-side libvirt/KVM installed (**none exists on the server
yet** — documented host work, like the portcullis route) + VM provisioned (qcow2 +
`virsh define/start`); runbook
(provisioning, sync, release, GC, **key rotation** = host `.env` edit → portcullis
`make update` → servitor sync (re-injects key slice) → service restart; `lib/`
backup via host-side git pull; ops drills: slot pool full, session broken, disk
pressure, pi upgraded); pi/extension version bumps = `servitorctl update` (§9)
— the suite gates the resolved versions host-side before anything touches the
guest; not bumping is a supported steady state — the guest runs the
last-verified version indefinitely, nothing auto-updates).
Gate: route live through the edge with 2FA (SSE survives: no response buffering,
read timeout above the longest LLM gap, `flushInterval` set — an SSE stream cut by
a proxy read-timeout is the §7 dropped-connection bug we are forbidden to
reproduce); forward-auth re-evaluated per reconnect (reconnect is a fresh GET);
panel shows the servitor actions; the full failure-mode drill set: app-crash
restart (registry reconciliation, resume on pickup, no double slot), slot-sweep
failure (quarantined, visible, retryable, never reused), disk pressure (fill
scratch ⇒ oldest non-stable released ⇒ headroom restored, `lib/` untouched), slot
pressure (16 live ⇒ oldest non-stable released; 16 running ⇒ wait state shown),
GC timer (idle >14 d non-stable released; stable never), **soak**: long run with
phone disconnect/lock mid-run (nothing cancelled), and a real card-binder pass:
upload photos → scripts + web research → report in `outbox/` → download it.

---

## 9. `servitorctl doctor` (deploy gate; runs on the guest, invoked from the host
by `deploy/doctor.sh` and optionally a panel-do action)

Certifies, read-only, with a pass/fail report:
- no-prompt spawn resolves: intended provider + `baseUrl` in
  `get_available_models`; `model.id != "unknown"`; model id/context window match
  intended (A4: assert at deploy, never hardcode);
- launch line == the asserted constant; `--api-key` absent from any recorded argv;
  loaded extension set == image baseline (drift = warning, D3);
- state volume is real ext4 (not overlay); session-root ancestors context-file-free;
- `models.json` RO bind holds (write ⇒ EROFS, unlink ⇒ EBUSY);
- config version on disk == active pointer; per-live-session stamped version sane;
- key material absent from synced tree, session files, and app logs; no
  `auth.json`/`models-store.json` outside per-session scratch uppers;
- slot pool state (free/cleaning/quarantined); no root-owned files inside
  slot-owned trees;
- firewall: `SERVITOR_PORT` admitted from edge subnet only;
- applied slice properties (`CPUWeight`/`MemoryHigh`/`MemoryMax`) read from the
  live unit — not from a drop-in file;
- outbox/scratch sizes, disk headroom; pi version == the `.env` selection;
  installed pi-web-access version matches the `.env` selection (registry stamp).
Doctor must be green before the service starts serving and after every sync.
While the update-waiting flag is set (§5), doctor certifies the running state
and reports the staged target as a warning; the full green run happens after the
deferred restart.

### `servitorctl update` — the single update path (host-side)

One command takes the guest from its current state to "matches the repo + the
`.env` version selections", verified. The guest is not touched until the gates
pass:

1. **Resolve**: `.env` specs (or CLI overrides) → concrete versions (`npm view`
   on the host). No-op when nothing changed (no restart).
2. **Gate (host-side)**: the conformance suite runs against the *resolved*
   versions — pi installed into a scratch prefix, the extension staged at the
   resolved version — full suite (framing, disposition matrix, tools array,
   zero-registry contact, config gate). Red ⇒ stop; nothing is deployed.
3. **Provision** (only what changed): pi ⇒ image rebuild (reads the same
   `.env`; the previous image is retained); extension ⇒ reuse the gate's
   staging; config/lib ⇒ versioned swap artifacts.
4. **Deploy** (swap-based): image disk swap on the VM target (virsh) or the
   laptop QEMU instance (same command — the target is a `.env` selection);
   extension stage + rename; config versioned swap; service restart.
5. **Verify**: doctor. **Doctor fails ⇒ automatic rollback** — swap back the
   retained previous artifacts, restart, re-run doctor, report. The host is
   never left in a half-updated, unverified state.

**Run active at the restart step ⇒ the §5 update-waiting path, not a
failure**: steps 1-3 proceed (all non-disruptive), the flag is set instead of
the restart + doctor, and the command exits 0 — the in-process tick applies the
restart + doctor at the first idle moment; an image-level swap stays an explicit
operator downtime.

Live sessions keep the old versions until respawn (pi and the extension load at
spawn). `sync` remains the fast config-only push for daily edits; `update` is
the versioned path.

---

## 10. Repo layout

```
servitor/
├── PLAN.md · AGENT-BRIEF.md · report.md · DECISIONS.md   # decision surface
├── compose/docker-compose.yml                            # contract-only (D1): env names, port, bind, mounts
├── .env.example                                          # every name, no values
├── app/                                                  # Python 3.12+ (FastAPI, single asyncio process)
│   ├── rpc/        # driver: framer, futures, dispatcher, dialog responder, ladders
│   ├── iso/        # slots, overlays, chown handoff, setuid spawn, sweep, release()
│   ├── registry/   # SQLite: the only source of session truth
│   ├── delivery/   # producers, per-subscriber queues, wire contract, meters
│   ├── api/        # HTTP/SSE routes + static UI serving
│   └── doctor.py   # servitorctl doctor
├── ui/             # React + Vite → app/static
├── agent-config/   # synced RO: SYSTEM.md, skills/, settings.json, models.json (literal URL + $KEY)
├── deploy/
│   ├── guest/      # qcow2 build, systemd units + slice, nftables, hosts pins
│   ├── sync.sh     # host-side one-way sync (config/lib → guest, key slice, restart, doctor)
│   ├── update.sh   # host orchestrator: resolve → host-side gate → provision → swap → doctor → auto-rollback
│   ├── doctor.sh   # host wrapper → guest doctor
│   └── portcullis/ # route yml + panel-do action source + runbook excerpt (operator merges)
├── test/           # mock router, conformance suite, sandbox-smoke, vm-smoke, byte-level SSE gates
└── Makefile        # check · test · sandbox-smoke · vm-smoke · guest · sync · update · doctor · deploy
```

Single implementation repo (DECISIONS A1). Nothing imports or calls portcullis;
portcullis never reaches into this repo at runtime (D2).

---

## 11. API surface (all edge-gated, no auth of its own)

```
POST /sessions                     create (mint id, allocate slot, mounts, first spawn)
                                   pool full ⇒ 503 + wait state
GET  /sessions                     list — registry only, O(1), wakes nothing
GET  /sessions/{id}                authoritative state + meters (+ transcript on open)
POST /sessions/{id}/prompt         {text, files?, mode?: auto|steer|follow_up}
                                   auto = prompt when idle, steer while a run is active
POST /sessions/{id}/abort          explicit — the only abort
POST /sessions/{id}/stable         toggle (pins the slot until closed)
POST /sessions/{id}/close          ⇒ release()
POST /sessions/{id}/uploads        multipart; streamed, content-addressed, chowned
GET  /sessions/{id}/events         SSE; ?show_thinking=none|final|all &coalesce=<ms>
GET  /sessions/{id}/transcript     authoritative (RPC reads / probe — never JSONL parse)
GET  /sessions/{id}/tool-results/{toolCallId}   lazy body (confined read)
GET  /sessions/{id}/files          outbox first, then full workdir listing (name,size,mtime,sha256)
GET  /sessions/{id}/files/…        download: chunked, HTTP Range, no cap (soft UI warning)
POST /sessions/{id}/save-script    {path} — human UI action only; provenance sidecar
GET  /sessions/{id}/tree           (fork/branch slice)
POST /sessions/{id}/fork           {messageId}   (fork/branch slice)
GET  /meters                       global: slot pool, disk headroom, subprocess count, update-waiting flag
GET  /healthz
```

---

## 12. Non-goals & rejected alternatives (do not "improve" these)

- **No** end-user auth inside the service (edge is the only gate). **No** egress
  filtering inside the VM (the VM boundary is the mitigation; exfiltration is the
  accepted residual). **No** capability/iteration/token/wall-clock/vision/size
  limiters (§3). **No** docker in the VM (D1). **No** dependency on portcullis at
  runtime (D2). **No** per-session VMs/containers (brief §8). **No** in-process Pi
  SDK (uid boundary collapses). **No** WebSockets (SSE + fetch-not-replay is the
  design). **No** replay ring buffer / Last-Event-ID resume (transcript is the
  authoritative store; deltas are disposable). **No** `DynamicUser=` / shared-uid
  `$HOME`. **No** offline JSONL parsing for rendering. **No** agent-callable
  save-to-lib. **No** hot-reload of config into live sessions. **No** `--approve`,
  `--api-key`, `auth.json` in image/synced config, `$VAR` in `baseUrl`,
  `PI_CODING_AGENT_SESSION_DIR`, `PI_OFFLINE`, `--no-extensions` (D3 — baseline
  loading + new-extension warning, §7) in the launch line. **No** git
  credentials in the VM. **No** GPU in the VM. **No** sync/update that blocks,
  waits, or errors while a run is active (update-waiting flag, §5 — any script
  may call it at any time).

## 13. Settled values (all micro-details closed 2026-11-14)

| item | settled as |
|---|---|
| subdomain | `servitor.pietrasik.top` |
| codemode | **off** (D5) — default tools only; enable later = `"+codemode"` in synced settings + sync (§7 verified mechanics) |
| `SERVITOR_PORT` | **8787** — the VM's port space is dedicated; unique inside the VM is all that's needed |
| hypervisor | libvirt/KVM. **None is installed on the server yet** — host-side install + domain definition is stage-6 work, documented like the portcullis route |
| guest distro | CachyOS — host parity, and the kernel generation where the overlay behavior was verified |
| `OMNISSIAH_API_KEY` | the host's `LLM_API_KEY` (verified 2026-11-14, high confidence: this sandbox — the owner's live env — is accepted by `27b.pietrasik.top`, and portcullis gates every `llm-<name>` container with that key). Sync reads the value from the host `.env` (single source of truth; rotation stays in sync); the doctor's live-prompt gate at first deploy is the backstop |
| Exa key | **`EXA_API_KEY` in the spawn env allowlist** — verified against the 0.47.0 dist (2026-11-15): config lookup is `PI_CODING_AGENT_DIR`-based and the env credential beats any `web-search.json` literal, so no key file is rendered or synced (§4, §7) |
| Authelia group | default (any authenticated user) — out of plan scope; admins-only later = one rule above the wildcard |
| coalesce default | **1000 ms** — delivery batching only; the run is byte-identical; per-subscriber `?coalesce=` overrides |
| SSE heartbeat | **~15 s** keepalive ping (`.env`) — carries long silent gaps (a 20-minute tool call emits no events) through proxy idle timeouts; a cut connection is still harmless (reattach = fetch) |
| dialog wait | **indefinite** (owner D4, 2026-11-14) — a dialog pauses the run and waits for the human; no deadline, no auto-decline; a returning client is re-offered the pending dialog; the wait is a live run (idle/GC/pressure never touch it); meters show it; abort remains (§7) |
| drain grace / `TimeoutStopSec` | **90 s / 120 s** (`SERVITOR_DRAIN_GRACE_SECS`; grace strictly < stop timeout) — **app-shutdown behavior only**; during normal operation nothing here fires |
| sync/update mid-run | **update-waiting flag** — everything non-disruptive stages immediately; the service restart + doctor defers to the first idle moment (in-process tick); an image swap is always explicit operator downtime; scripts get exit 0, never a block (§5) |
| soft size warning | **500 MB** — a UI nudge on download; no cap |

**None of the delivery/ops parameters is a capability cap** (owner constraint,
2026-11-14): a run is ended only by finishing, by the human (abort/close), or by an
explicit app stop (abort-then-drain, recorded, resumable). A 20-minute test is a
normal run — the 1 h idle kill fires only after `agent_settled`, so a run in
progress, however long, is never eligible.

---

## 14. Source map (background, for the curious or the stuck)

- AGENT-BRIEF.md — owner intent, threat model, §6–§9 policy, §10 verified-fact list.
- report.md — the council deliberation: agreed T1–T27 (decisions), T28–T36 (unseen
  but codified here), N1–N45 (evidence, incl. retractions), Q1–Q14 (planner Q&A).
  Every "verified" claim in §4 of this file traces to a note there.
- DECISIONS.md — owner ratifications: D1 (systemd guest), D2 (standalone-first +
  laptop dev loop), A1–A6 (portcullis integration), H3+outbox (no size cap), Q8,
  Q11.
