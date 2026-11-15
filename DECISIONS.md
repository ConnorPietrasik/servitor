# Servitor — decisions log

Owner-ratified decisions and council assumptions, as they settle. This is the decision
surface; the build order (PLAN.md, the consolidation pass) consolidates it with
AGENT-BRIEF.md + report.md (agreed T-items + standing N-notes).

Status legend: **ratified** = owner confirmed · **proposed** = answer derived, owner
should confirm or veto · **open** = no default yet.

All items closed as of 2026-11-14 (see D1/D2, the portcullis-review block, and the
ratified block below); D8 ratified in the plan-review session. Remaining unknowns
are micro-details flagged inline (TBD).

---

## D1 (H5) — Service runtime: systemd on the guest, not a container — **ratified** (2026-11-14)

The Servitor app runs as a **systemd service on the guest OS** (`servitor.service` +
`servitor.slice`, GC in-process — consolidated in PLAN §5), serving the React static
UI from the same process and port. No docker in the VM. The guest image (qcow2/cloud-init, with the
toolkit baked into the rootfs) is the reproducible artifact.

Why (against the owner's portcullis system, `github.com/ConnorPietrasik/portcullis`):
- Portcullis's own rule: privileged API runs on the host as systemd (panel-api); only
  static UIs containerize. The guest is a dedicated host whose only API is Servitor.
- A privileged container (CAP_SYS_ADMIN + setuid + chown + mounts) is root-equivalent
  in the guest anyway — the container boundary adds no isolation but adds the docker
  daemon as a root-equivalent surface. Portcullis deleted its docker-socket-proxy for
  exactly this reason.
- §9 is already systemd-shaped (slice MemoryHigh=25G/MemoryMax=30G, CPUWeight, GC
  timer); T19's drain-then-abort maps to TimeoutStopSec + ExecStop (grace <
  TimeoutStopSec, per the council's derivation).
- T29 (state volume must be real ext4, not container storage) becomes a plain mount.
- Guest containers would be invisible to the panel's Services view (panel-do lists
  host containers only); a guest systemd service at least gets journald, Restart=,
  OOM accounting, and doctor parity.

Costs (accepted): brief §4's `compose/docker-compose.yml` "(service)" becomes a
**contract-only** artifact (env var names, port, bind, mount layout); the runtime
definition is the guest's systemd units in this repo. "Toolkit baked into the image"
= baked into the guest image, not a container image.

## D2 (new) — Standalone-first; portcullis is a deploy target, not a dependency — **ratified** (2026-11-14)

Servitor builds and runs as a **self-contained system with no import/call dependency on
portcullis**. The "edge" is an abstract contract: *a reverse proxy that routes one
hostname to the VM's ingress IP:port, and is the only auth gate* (brief §5). Portcullis
(Traefik + Authelia) is one implementation of that contract; the standalone/loopback
deploy is another. No code change between them.

Motivation: dev loop. The coding agent gets **root on the owner's laptop** and builds
against the real router from the LAN, with zero risk to the production server:
- App + RPC conformance suite (T23's `sandbox-smoke` half) run natively on the laptop
  as root — no VM needed.
- `vm-smoke` (uid-drop, real sweep) also runs on the laptop: root has CAP_SYS_ADMIN,
  so overlay/setuid/spawn gates execute locally before anything touches the server.
- Full guest test = the qcow2 under QEMU/KVM on the laptop, when needed.
- Server deploy is then the portcullis-style gated step: versioned artifacts,
  doctor pre/post, rollback.

Consequence: **nothing in the servitor deploy may require a portcullis-specific
service to exist** (no SearXNG, no panel-api, no host .env access at build time).
Every runtime input is servitor's own `.env` (see A3).

## D3 (new) — Extension loading: baseline, no `--no-extensions`, warn on new
extensions — **ratified** (2026-11-14)

The launch line keeps **baseline extension loading**: built-ins load naturally
(codemode's extension loads too but its tool is **not declared** — off per D5; the
verified mechanics in PLAN §7 are the re-enable path), pi-web-access loads by
explicit `-e` path. `--no-extensions`
is dropped: the owner judged the council's persistence-channel concern (an
agent-planted extension in its own tree auto-loading on the next respawn) a small
risk not worth the flag/gate hassle. Compensating control = **visibility, not
blocking** (consistent with Q8): the spawn path records the loaded-extension set
per spawn (`get_commands` delta + the agent-upper file list); anything beyond the
image baseline ⇒ warning in panel + doctor + session event feed. The run is
unaffected; the warning is the control. Accepted residual: a planted extension can
execute with the slot uid on respawn — no new capability beyond what the agent
already holds (arbitrary code execution + the key are accepted per the brief),
session-scoped, dies at release, now visible.

## D4 (new) — Delivery/ops parameters are not capability caps — **ratified**
(2026-11-14)

Owner constraint: **no constraints on capability; a 20-minute test run is a
legitimate normal run.** Consequences (all in PLAN.md §7/§13):
- Coalesce (1000 ms default) is wire batching only — the run is byte-identical;
  per-subscriber override.
- Extension dialog (**amended same day, owner**): **wait indefinitely — no
  deadline, no auto-decline.** A dialog pauses the run and waits for the human; a
  returning client is re-offered the pending dialog, answers it, and the run
  resumes. The wait is a live run (idle-killer/GC/pressure never touch it), the
  meters show "waiting on extension dialog (since …)", and abort remains. Rationale
  (owner): auto-decline would let the run continue *unsteered* to completion past a
  point that wanted input — for a steered assistant, pausing at a human decision
  point is the correct behavior. The wait spins nothing (no model call in flight)
  and costs one visible, abortable slot. N14's "run hangs indefinitely" failure
  mode is thereby a designed, visible state (meters) rather than a bug.

## D5 (new) — Codemode off by default — **ratified** (2026-11-14)

Codemode is not declared: the synced `settings.json` keeps the default tools
(read/bash/edit/write only; no `+codemode`). Owner's point: the agents in practice
write Python scripts via the bash tool anyway, and Python/bash already reach
everything the env + toolkit allow — codemode would add no reach, only a ~3k
token/call prompt cost for a tool the model mostly doesn't reach for. Enabling
later = `"+codemode"` in the synced settings + one sync (verified mechanics in
PLAN §7); the stage-1 tools-array gate asserts the declared set either way (v1:
the four pi-web-access tools present, `codemode` absent).

## D6 (new) — Version selection is declared, not tracked — **ratified**
(2026-11-14)

`SERVITOR_PI_VERSION` and `SERVITOR_PI_WEB_ACCESS_VERSION` are npm semver specs
(exact by default; a range or `latest` is allowed). The **conformance suite +
doctor are the upgrade control; the value is only where the choice is recorded** —
one field in `.env`, read by laptop dev, the image build, and doctor alike.
**Not bumping is a supported steady state**: the guest runs the last-verified
version indefinitely and nothing auto-updates (an unattended surprise upgrade is
exactly what the gate exists to prevent). pi-web-access updates are a sync (one
string → sync → doctor); pi updates are the image loop (string → image build →
suite → deploy). The earlier version+shasum stamp is dropped — the registry
records the installed version read from the installed `package.json`; a corrupt or
half-installed extension is caught by spawn failure / the tools-array assert /
doctor's live-search check.

## D7 (new) — One update command — **ratified** (2026-11-14)

Updates are a single host-side command (`servitorctl update` / `make update`):
resolve the `.env` specs → run the conformance suite against the **resolved**
versions host-side (scratch pi prefix + staged extension — the guest is not
touched yet) → provision only what changed (image / extension / config) →
swap-based deploy with previous artifacts retained → doctor → **automatic
rollback** on doctor failure. The conformance suite is version-parameterized
(stage 1) precisely so the gate runs before the deploy. `sync` remains the fast
config-only push for daily edits. Same command in the dev loop, aimed at the
laptop QEMU target (target = `.env` selection).
- The 1 h idle kill fires **only after `agent_settled`** — a run in progress,
  however long, is never eligible.
- Drain grace (90 s default, `.env`) applies **only at app shutdown/restart**
  (interrupted = recorded + resumable); during normal operation nothing fires.
- 500 MB size warning is a UI nudge (no cap, per H3).
- Added: SSE keepalive ping (~15 s) so a long silent gap (a 20-minute tool call
  emits no events) doesn't idle-timeout the proxy; a cut connection is still
  harmless by contract (reattach = fetch).

## D8 (new) — Update mid-run: the update-waiting flag — stage, defer the restart,
never block — **ratified** (owner, plan-review session)

Amends D7. `sync`/`update` may be invoked from other scripts (cron, pipelines) at
any time — including mid-run. They **never wait on a run and never error**:

- Everything non-disruptive proceeds as usual: host-side gate, versioned config swap
  (active pointer — new spawns take the new version at spawn; live sessions keep
  theirs), extension stage + rename, key slice, and the image rebuild. The service
  restart is the only disruptive act, and the only deferred act.
- Run active ⇒ the script sets the **update-waiting** flag (a state-volume marker
  mirrored into the registry), skips restart + doctor, prints one status line, and
  **exits 0**. Repeated invocations are idempotent; the newest staged version wins.
- The in-process tick consumes the flag: no live run, staged ≠ running ⇒ the app
  triggers an orderly service restart (the existing abort-then-drain stop path —
  instant when nothing is in flight); the after-start doctor certifies it (red ⇒
  D7's automatic rollback). A no-op flag (staged == running) clears silently.
- **Image-level changes (qcow2 swap) are never auto-applied** — a pi bump staged
  mid-run stays *image-swap pending* until an explicit operator invocation, as a
  planned downtime. No automatic downtime, ever.
- The flag is visible everywhere: panel, `GET /meters`, `servitorctl doctor` (while
  set: certifies the running state and reports the staged target as a warning —
  never red while the VM serves), and the session event feed.

Rationale (owner): the update scripts are called by other scripts; a hard refusal
or a blocking wait would turn an update into a failure or a hang exactly when the
system is busy. A visible, self-healing deferred state keeps D7's guarantee (never
a half-updated, unverified state) without touching D4's: a run is ended only by
finishing, by the human, or by an explicit app stop. Consolidated in PLAN.md §5
("Update waiting"), §9 (doctor + update path), §11 (`/meters`), §12, §13, and the
stage-5 gate (update-waiting drill).

---

## Answered by the portcullis review — **ratified in principle** (deploy-plan statement,
2026-11-14: "real deploy plan is to add it to Portcullis; I don't mind having to update
Portcullis too for the integration")

### A1 (H4) — Single implementation repo, self-contained
`/dockerx` (the servitor repo) holds everything: app, UI, agent config, contract
artifacts, deploy scripts. Portcullis integration lives in this repo under
`deploy/portcullis/` (route yml, panel-do action source, runbook excerpt) and is
**merged into the portcullis repo by the operator** — servitor never reaches into
portcullis, and portcullis never reaches into servitor at runtime. This matches D2 and
the council's N37 assumption; the standalone constraint confirms it.

### A2 (H2) — Two independent syncs, each with its own doctor
- **Servitor sync (repo → guest)**: operator script in this repo (`deploy/sync.sh`):
  rsync prompts/skills/tool-defs/lib into the guest's read-only config tree (atomic
  versioned-dir swap per T36), inject the key slice into the guest's reduced env file
  (portcullis's `.panel-api.env` 0600 pattern), restart the service if config shape
  changed, run `servitor doctor`. Runs identically on the laptop and the server.
  In the portcullis deploy it is optionally wrapped in a panel-do action
  (`servitor-sync`) so the panel can trigger it; manual invocation is always supported.
- **Edge route (servitor repo → portcullis repo → edge)**: the route file is versioned
  **in the portcullis repo** (`compose/traefik/dynamic/`), applied by portcullis's
  `make update`. Not by the servitor sync. (Verified: `stack_sync_configs` in
  `setup/common.sh` *prunes* deployed traefik files absent from the portcullis repo —
  a hand-placed route file dies on the next `make update`/daily autoupdate.)
- **Key rotation**: portcullis's `make update` secret rotation does NOT reach the VM.
  Rotation = edit host `.env` → `make update` → `servitor-sync` (re-injects the key
  slice) → service restart. Documented in the runbook. `OMNISSIAH_API_KEY` is the
  host's `LLM_API_KEY` (the llm fleet's bearer).

### A3 (H6) — Search: keyed Exa, owned by servitor's own .env
The council's assumption stands, and D2 *strengthens* it: the Exa key is a machine
cred in **servitor's** `.env` (`.env.example` names it), rendered into the
per-session agent dir at spawn (per the council's finding that pi-web-access's config
lookup is homedir-based). The owner's live env already runs keyed Exa, so the key
exists. pi-web-access pinned at 0.47.0, loaded by explicit `-e <baked path>`
(baseline discovery otherwise on per D3), `PI_WEB_ACCESS_CACHE_ROOT` into
`scratch/<session>/`.

Rejected for v1: pointing search at portcullis's SearXNG agent API
(`search.pietrasik.top`, gated by LLM_API_KEY). It works from the VM (LAN, name pinned
per A5) but **violates D2** — a servitor built against it can't run on the laptop
without portcullis. Kept as a post-v1 optional backend (needs verification that
pi-web-access can target a SearXNG-style endpoint at all).

### A4 (H14) — Router model: runtime discovery + deploy-time assertion
No change from the council: don't hardcode. `servitor doctor` (T35's no-prompt spawn)
asserts the resolved provider, baseUrl, model id, and context window against the
intended values at deploy time and after every sync. The sandbox-verified model
(Qwen3.8-27B, 250k context, image input, parallel tool calls) is the *expected* value
on the owner's network, not a constant.

### A5 (new) — LAN name-pinning is a deploy step, with a known gotcha
The host's router doesn't hairpin and the apex A record follows the public IP, so
**every LAN machine that talks to the router by name** (the guest, the laptop, any
future LAN client) must pin `*.pietrasik.top` to the host LAN IP in its own
`/etc/hosts` — the same workaround portcullis ships as `tools/panel-hosts.sh` (it
rewrites only its own marked block; the guest gets the equivalent pin at
provisioning). From off-LAN, the public URL works as-is (the llm route is
internet-facing by design). Affects: the synced `models.json` baseUrl (literal per
T16 — same literal works for LAN-pinned and public depending on where the guest
lives), pi-web-access fetches, and nothing else.

### A6 (new) — Panel integration surface (portcullis side, operator-merged)
- Route: `servitor.pietrasik.top` (name TBD) → `http://<vm-ingress>:<port>`,
  `middlewares: [auth-chain@file]`, in portcullis's `compose/traefik/dynamic/`.
  No new Authelia rule — the `*.pietrasik.top` two_factor wildcard already makes it
  private-by-default (any authenticated user). If the owner later wants admins-only,
  it's one Authelia rule above the wildcard.
- No new host UFW rule — the route rides the existing 443. The **VM's** firewall
  admits the API port from the host's edge-network source subnet, derived, not
  hardcoded (`docker network inspect edge -f Subnet`, same method as portcullis's
  UFW rule 3).
- panel-do actions (new entries in the Go ACTIONS registry, per portcullis RUNBOOK
  "Add a privileged action"): `servitor-status`, `servitor-vm-restart`,
  `servitor-sync`, `servitor-doctor` — thin wrappers over `virsh` + `deploy/sync.sh`
  + `servitor doctor`. Source ships in `deploy/portcullis/`.
- VM lifecycle: portcullis has no VM management today (verified). v1 treats the VM as
  provisioned once (qcow2 + virsh define by the servitor deploy script); the panel-do
  actions give day-2 control. Full panel-managed VM creation is a portcullis
  enhancement, out of scope.

---

## Ratified this session (2026-11-14)

### H3 — Artifact egress: download endpoint + an explicit outbox — **ratified**
- **Endpoint (yes, ship it)**: read-only list + download from the session's
  scratch tree, per T27/T31 — dirfd-relative opens, `O_NOFOLLOW`, regular files
  only, session-scoped, edge-gated, dies at release. **No size cap** (owner,
  2026-11-14: the disk is the bound, the fetch is an intentional 2FA-gated human
  action, and the size is visible before the click — a cap is a §6 capability cap
  protecting nothing). What replaces it: (1) the app **streams in chunks** — memory
  is O(chunk), never O(file), even under the 30 GB `MemoryMax` (the cap was
  accidentally covering a naive read-into-memory implementation; streaming is the
  real requirement); (2) the listing shows size before download (outbox design
  above); (3) a soft UI warning above a threshold — a meter, not a limiter (§6);
  (4) **HTTP Range** support, so a big download interrupted on a bad connection
  resumes instead of restarting (§7 applied to egress). Full workdir browsing stays
  available as the secondary surface.
- **Outbox**: `outbox/` inside the session workdir (overlay upper, slot-uid-writable).
  It is a **convention in SYSTEM.md, not new machinery**: "Files the user should
  receive go in `outbox/` in your working directory; everything else is scratch." The
  UI surfaces the outbox first-class (per-session list: name, size, mtime, sha256, +
  download), so inbox/outbox are symmetric in the user's mental model: what I sent you
  / what you're giving me.
  - Why a workdir convention rather than a new top-level area: zero new mounts or
    lifecycle state (fits §6 "no caps"); agent paths stay natural; the real control is
    T31's app-side read confinement, which applies identically either way (the agent
    owns those bytes regardless of where the dir sits).
- **Lifetime**: dies at release with the workdir — same as inbox (H15 position: no
  hidden persistence). Durable paths stay exactly two: download now, or "save this
  script" → `lib/` (T30; accepts outbox or any scratch source, unchanged).
- **Meters**: outbox file count + size on the session list/panel (cached, same pattern
  as T7's du cache). Unbounded mess is only dangerous when invisible.
- **Amends brief §8**: areas table gains a row — `outbox/ (in workdir) | files the
  agent is handing to the user | until session release`.

### Q8 — Agent shadows `settings.json`/`SYSTEM.md` in its own overlay upper — **ratified**
Visibility-only: agent-upper file list in the panel + doctor, logged at release; no
auto-restore (would race a live process). Bounded: session-scoped, dies at release,
gives the slot uid no new reach. The launch-line contract (T26) is the hard control;
this is the visible-ness layer.

### Q11 — Transcript probes reuse the exact run launch path — **ratified**
Same slot uid, same mounts, same flags as a real run; only the lifetime differs. A
probe running as the app uid would drop root-owned files into slot-owned dirs and break
the ownership invariant the owner-sweep depends on.

---

## Verified this session (2026-11-14, closes the council's unanswered Q8 part 2)

- **Codemode mechanics** — settled empirically (mock router, launch-line variants,
  judged by the `tools` array of the captured request — the system prompt string is
  a red herring): the `codemode` tool is declared only by `+codemode` in settings
  (or `--tools +codemode`); `--no-extensions` removes the built-in codemode
  extension and `+codemode` alone does not restore it (silently ignored);
  `-e builtin:codemode` would. **Owner decisions (same day) supersede the
  council's `-ne` design**: D3 — run baseline, no `--no-extensions`, explicit
  `-e` for the search extension only; D5 — codemode **off** by default (no
  `+codemode`; the agents script via Python/bash anyway, which has the same reach
  at zero prompt-token cost). The verified mechanics remain the reference for the
  stage-1 tools-array gate (v1 asserts `web_search`/`source_check`/
  `fetch_content`/`get_search_content` present, `codemode` absent), for
  re-enabling codemode, and for any pi upgrade.
- **`OMNISSIAH_API_KEY` = the host's `LLM_API_KEY`** — verified by inference,
  high confidence: this sandbox (the owner's live pi environment) calls
  `27b.pietrasik.top/v1` successfully, and portcullis gates every `llm-<name>`
  container with `LLM_API_KEY`. The sync script reads the value from the host
  `.env` regardless, and the doctor's live-prompt gate catches a mismatch.
- `pi-web-access` 0.47.0 confirmed installed in the owner's live env, manifest
  `pi.extensions: ["./dist"]` (loadable via package path), four tools as above.

## Unchanged from council assumptions (N37) — ride along unless vetoed

Single repo (= A1) · keyed Exa (= A3) · runtime model discovery (= A4) ·
operator-run sync (= A2) · artifact download + outbox (= H3, ratified) · fork/branch
UI ships in v1 as the last UI slice (H1, answered-in-place; vetoable as a scope cut).

---

## Stage 3 verified (2026-11-15) — API, SSE, delivery; gate stack green

Findings that shaped the stage-3 code (all reproduced and fixed):

- **httpx `iter_raw(n)` re-chunks into FIXED-size pieces.** With a chunk size,
  the final partial piece (which can hold the tail of the stream — including
  `agent_settled`) is held back until more bytes arrive or the stream closes;
  an SSE stream never closes (heartbeats), so the tail never surfaces. Test
  clients must use `iter_raw()` without a chunk size (raw passthrough) or
  `iter_lines()`. (Cost: ~8 debugging sessions; the kernel was innocent —
  raw-socket and asyncio 20 MB loopback transfers complete in <1 s.)
- **Deep kernel receive backlogs wedge TCP on this host's kernel (7.3.4-1-
  cachyos).** Measured: client socket `skmem d=114 rb=6.5MB` under a slow
  reader; a dedicated reader thread that drains the socket at line rate keeps
  the backlog small and the wedge disappears. The backpressure gate test
  therefore reads in a reader thread and throttles the PARSER — the pause is
  driven by the hub's position (ack'd ids), never by TCP socket state.
- **Test state roots must live on the shared 0755 tree (`/srv/
  servitor-test`), never under a slot home.** The slot homes are 0700; a state
  root under slot1's home is invisible to slot2/slot3 (stat EACCES on their
  OWN pre-created session dirs — `existsSync` false, `mkdirSync` EACCES, pi
  exits 1 at spawn). Production shape: state is a dedicated volume, never in
  a slot home.
- **`find -path` is a whole-path GLOB, not a prefix.** Bare prefixes prune
  nothing; sweep exclusions need both `P` and `P/*`. The owner-sweep was
  deleting files inside the wholesale-deleted session areas (merged overlay
  views) and erroring on them.
- **Sweep tolerates ENOENT.** A file vanishing between find and unlink (the
  agent's exit-time cleanup, e.g. pi-web-access dropping
  `last-seen-version.json`) is the desired outcome, not an error; the
  verification pass (uid owns nothing outside excludes) is the real gate.
- **Sweep lazy-unmounts stale mountpoints.** A mount left by a dead run
  (crashed app, killed test) must not quarantine the slot forever; `umount -l`
  then sweep the directory like any other.
- **Released sessions are gone.** API treats FREE rows as 404 everywhere; the
  session list shows live sessions only (`include_released=False`).
- **SSE replay is a snapshot, not a live view.** `subscribe` returned a lazy
  generator over the ring deque; the generator yields lazily while the
  publisher mutates the deque → `RuntimeError: deque mutated during
  iteration` (any subscribe with a non-empty replay, i.e. a reconnect or a
  stream opened after events exist). Snapshot list at subscribe time
  (synchronous ⇒ atomic w.r.t. the event loop).
- **Backpressure test calibration (measured, pi 1.5.2 + mock).** The mock's
  `chunk_count` is a PIECE count (not bytes); 20 tool rounds × 32 KB prefix ×
  2048 pieces ≈ 42 000 small `message_update`s ≈ 12–16 MB of SSE at the
  mock's ~300 KB/s natural pace. A consumer at ~150 KB/s (0.25 s sleep per
  200 parsed frames) crosses the 1 MB cap, the run pauses (driver stdout
  gate), the drain resumes it, and the run settles complete. Pause episodes
  observed: 14–23 per run; the slow consumer stretches the run ~3×.
- **Crash-drill uploads must be throttled client-side.** 16 MB over loopback
  completes in <1 s; an unthrottled "mid-upload" kill lands after completion.
  The test paces the body (~250 KB/s) so the kill is deterministically
  mid-stream.

## Stage 4 verified (2026-11-15) — transcript fetch, fork/branch, static UI; gate stack green

- **pi 1.5.2 fork semantics (verified live).** `fork` on a user entry
  creates a NEW session FILE (same `sessions/<s>/` dir, new session id,
  timestamped name) and the process switches to it. The forked-from
  message is NOT re-appended: the new session starts at the fork point
  with only the config preamble (model_change/thinking_level_change/
  system) — the client re-sends the (edited) message as the fork's first
  prompt (that's the "edit that message" flow; the API returns the
  forked text exactly for the composer prefill). The original file
  stays on disk — the session's full branch picture = every file in the
  session dir.
- **pi creates the fork file lazily.** At fork time the new file does
  not exist yet (created on the first write). The registry's
  `session_file` is therefore synced to the new file ONLY when it exists
  (immediately after fork/switch, and lazily on every live transcript
  fetch). The registry must never point at a missing file: the probe's
  first-touch rule, respawn, and the dead-process disk path all trust it.
- **The probe survives the fork's lazy file creation.** The registry
  only follows the new sessionFile once the file EXISTS, so between the
  fork and the fork's first write the registry still points at the
  original transcript — the probe (which requires an existing
  transcript by its first-touch rule) keeps working, and respawn in that
  window reattaches to the original file (the fork is a new session
  anyway, so nothing is lost). Once the fork file lands, the next
  sync (immediate post-fork/switch check or any live fetch) moves the
  registry.
- **`/messages` reattach protocol: fetch, never replay.** Response =
  `{leaf_id, cursor, messages}`. `cursor` = the SSE hub's monotonic event
  id read BEFORE the entries snapshot (cursor-first: a concurrent event
  in between duplicates an entry the snapshot already contains — the
  client dedupes by (role, text, thinking) fingerprint; the reverse
  order would silently skip the tail). The UI subscribes to
  `/events?after=cursor` and follows only NEW events; the browser
  EventSource re-sends `Last-Event-ID` on every (re)connect, so a
  dropped phone connection resumes exactly where it stopped. A `gap`
  (ring evicted past our position) triggers a full re-fetch + re-attach —
  never partial data.
- **Fingerprint dedup, not entry-id dedup.** pi's message events carry
  no entry ids (ids are assigned at transcript append). Snapshot/live
  dedup keys on role+text+thinking; after `agent_settled` the UI
  re-fetches `/messages` anyway, so live partials (synthetic ids) are
  replaced by the authoritative transcript with real entry ids.
- **`/tree` is a forest, not a tree.** The response is the union of ALL
  session files in the session dir (per-file pi get_tree shape +
  `files[]`), because forks create new files rather than in-file
  branches. Uniform across live/dead: the files are written as events
  happen, so the disk IS the live view.
- **Outbox sha256 is lazy.** `list_outbox` returns `sha256: null` while a
  background thread hashes the file (1 MB chunks), cached per
  (session, path, size, mtime) — a multi-GB artifact is never re-hashed
  on every listing; the UI polls and the hash appears when ready.
- **Static UI served by the app, same port.** `ui/dist` (Vite+React,
  hashed assets) is mounted by `build_app` when present: `/assets/*` via
  StaticFiles, everything else falls through to the SPA index. API/panel
  routes are registered first and win; the catch-all is
  `include_in_schema=False` so the OpenAPI route set (and the versioned
  contract) is unchanged. `ui/dist` and `ui/node_modules` are gitignored
  (reproducible: `npm ci && npm run build` in `ui/`).
- **UI design (phone-first, single origin).** Dark, max-width 720 px
  column, 42 px tap targets, system fonts, `100dvh` chat layout with
  sticky composer (safe-area padded). Send = steer while a run is
  active, prompt otherwise; explicit follow-up button queues a turn in
  the current run. Abort is always visible while running. Close =
  confirm dialog (aborts first when a run is active) → release.
  Settings (show_thinking, coalesce ms) persist to localStorage and
  affect rendering only. Tool lines collapse; tool results expand
  lazily (4 KB preview). Meters (session list) poll every 5 s — no live
  socket needed on the list screen.
- **Gate wiring.** `make test-stage2`, `make test-api` (each suite in its
  own pytest invocation — the stage-2 and api test dirs both define a
  `conftest` imported by bare name and cannot share one collection),
  and `make test-all` = check + unit/conformance + stage2 + api +
  sandbox-smoke + vm-smoke. Stage 4 full stack: check 31/31, unit+
  conformance 56, stage2 5, api 20 (incl. fork/branch + 110 s
  backpressure), sandbox+vm 15.
