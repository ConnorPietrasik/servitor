# The deliberation record

- project: `/dockerx`
- config: `/home/crazybot/.config/council/council.toml` · council `plan`
- mode: plan · outcome: **aborted** · rounds: 3
- date: 2026-11-14 10:15:36
- sandbox: `sandbox_mib = 32768` shared by 5 members (4 seats + the chair)
- project trust: approve · verbosity: quiet (stderr only, under `agents/`)
- pi: `/usr/lib/node_modules/@earendil-works/pi-coding-agent/dist/bundle/cli.js` (1.5.2)
- questions: none — the council was never asked
- aborted: interrupted by SIGINT 6992.7 s in (round 3 · verdicts)

## Cut short by SIGINT

Nothing below is settled: SIGINT arrived 6992.7 s in, with the run at round 3 · verdicts.

- in flight: Senior (Agent2) — `bash` python3 t31.py 2>&1 (783.2 s, idle 659.1 s)

## Roster

| ID | name | model | phase-0 plans | backing |
|---|---|---|---|---|
| Agent1 | Metic | `Qwen3.8-27B` | 1 | Agent4 |
| Agent2 | Senior | `Qwen3.8-Flash-Next` | 1 | Agent4 |
| Agent3 | Pro | `Qwen3.8-Flash-Next` | 1 | Agent4 |
| Agent4 | Coworker | `Qwen3.8-Flash-Next` | 1 | Agent3 |
| — | Chair (chair) | `Qwen3.8-27B` | — | — |

## Chosen direction

The council did not settle a direction.

## Decision points (34)

### T1 `[agreed]` What is built first (the plan's entry point)
- closed agreed in round 1
- raised by: Chair (chair)
Agent2: throwaway spikes first (isolation + RPC bridge), then the
API/delivery/UI/GC hung off SessionManager + RpcBridge; Agent3: the
privileged FastAPI control plane first, owning slots, storage areas, and
SSE fan-out; Agent4: the RPC driver plus its protocol-contract tests first,
then registry, guest-level isolation, delivery; Agent1: not stated in the
provided summary (whole-service build in 8 stages, each gate runnable
without the real router until final deploy)
- Metic (Agent1): agree (round 1) — I take Agent4's direction: contract + skeleton first, then the RPC driver with its protocol-contract tests, then registry, then guest isolation, then delivery. My phase-0 order (isolation before RPC) is revised: N17 shows the isolation gates cannot be validated in this sandbox (setuid EPERM, overlay refused), so starting there stalls verifiable progress, while the RPC contract is fully testable now and N16/N18 show its details are exactly where naive translations fail. Control-plane-first (Agent3) would build the session lifecycle on driver semantics not yet proven by tests.
- Senior (Agent2): agree (round 1) — Risk-first, but the split the chair summarized is smaller than it looks: Agent3's own plan puts the control plane at stage 5 too. The real ordering fact is that the uid/sweep gates cannot run in this CI at all, so isolation must start early and be allowed to finish on a guest run while the RPC driver is built against a mock router.
- Pro (Agent3): agree (round 1) — Agree with protocol-first, but two things come before it and one thing must not be deferred to the end: (0) the §4 contract skeleton (compose + traefik dynamic + .env) is this repo's own deliverable and costs an hour, so it lands first and stays the single source of ports/paths; (1) the RPC layer is second because every trap found so far lives there and every other layer depends on it; (2) the isolation primitives (overlay, chown handoff, owner sweep, uid drop) are proven before SessionManager exists, because that is the one place the sandbox cannot substitute for a guest and the one place a wrong assumption invalidates the registry design. Agent2's 'throwaway spike' framing is right for isolation and wrong for the protocol layer (see T2).
- Coworker (Agent4): agree (round 1) — I accept the merged order (see my revised plan): contract artifacts first, the RPC contract spine as the spine, and the guest isolation spike starting day one *in parallel* rather than as my predecessor. I still reject isolation-first as a *sequential* stage: everything after it (session store, meters, delivery, UI) is typed by the RPC event model, so a late RPC surprise is the expensive one.
- history: agreed in round 1

### T2 `[agreed]` Is the early pi-integration work disposable proof or a permanent, upgrade-gated contract
- closed agreed in round 1
- raised by: Chair (chair)
Agent2: throwaway spikes — disposable once isolation and the bridge are
proven; Agent4: permanent — pi's RPC/session format is a versioned external
contract with a conformance suite that must pass on every pi upgrade
- Metic (Agent1): agree (round 1) — Permanent, upgrade-gated. The conformance suite encodes the exact facts naive assumptions got wrong (N16: four corrections; N18: Python universal-newlines trap). pi is a pinned external dependency (1.5.2) with a version-specific protocol; the suite is the upgrade gate and its tests already exist for the driver, so 'permanent' costs nothing extra. Throwaway spikes (Agent2) discard precisely the knowledge N16 shows is expensive.
- Senior (Agent2): agree (round 1) — Agent4 wins against me here. My phase-0 'throwaway spike' framing was wrong: the protocol surface is versioned (brief §10 pins 1.5.2 and cites docs/rpc.md, docs/json.md, docs/session-format.md), so the framing/disposition/dialog/abort tests must re-run green on every pi upgrade. Cost of my original framing: an upgrade silently moves `disposition` or the event taxonomy and nothing fails.
- Pro (Agent3): agree (round 1) — Agent4 is right: permanent and upgrade-gated, not disposable. Everything I discovered this round is undocumented behaviour, which is exactly the material that regresses silently on a pi bump; it must be a conformance suite that re-runs on every pin change. Nuance by layer: the protocol suite is permanent, the isolation spike is throwaway-but-rewritten (the spike's assertions survive as `make vm-smoke`, its glue code does not).
- Coworker (Agent4): agree (round 1) — Merged, and I think the disagreement is nominal: the spike *code* is disposable, the *assertions* are not. Agent1's own Stage-8 gate ('a pi minor bump re-runs the hermetic e2e gate green') already is a conformance suite; Agent2's ops stage already has a pin/upgrade procedure. Make it one permanent CI target, not two artifacts.
- history: agreed in round 1

### T3 `[agreed]` Agent's web-search capability (pi-web-access or custom) — core to the problem statement, named in no plan
- closed agreed in round 1
- raised by: Chair (chair)
- Metic (Agent1): agree (round 1) — pi-web-access, pinned at 0.47.0 (the version in the owner's live env), loaded via explicit -e path, keyed Exa backend rendered per-spawn from .env (the owner's env already runs keyed Exa — a keyless default would deviate from the working setup). Cache rooted per-session via PI_WEB_ACCESS_CACHE_ROOT (verified in the package's dist code).
- Senior (Agent2): agree (round 1) — Decide it as: pi-web-access, version-pinned, `npm ci`'d and baked into the image, loaded by explicit `-e /opt/pi-ext/pi-web-access` plus `--no-extensions`, declared nowhere that triggers a registry call at session start. Which provider/key it uses stays open (H6); ship the keyless chain and document the override.
- Pro (Agent3): agree (round 1) — Ship pi-web-access, pinned to an exact version at image build, loaded from the read-only config with `-ne -e <path>` rather than `pi install` (a configured package is re-checked against npm at every start, and we spawn a process per session and per resume). Search keys go in the seeded per-session agent dir (`web-search.json`); set `PI_WEB_ACCESS_CACHE_ROOT` per session inside scratch (the default cache lives in the shared config dir and concurrent processes share it); default `workflow: none`; ffmpeg+yt-dlp baked in.
- Coworker (Agent4): agree (round 1) — Build it, and specifically: ship pi-web-access pinned at image build, loaded with an explicit `-e <pinned path>` while `--no-extensions` keeps *discovery* off; keyless provider chain unless the owner has a key (H6); `PI_WEB_ACCESS_CACHE_ROOT` pointed into `scratch/<session>/` so the cache dies with the session; and the extension-UI responder is not optional — it ships in the same stage as the extension.
- history: agreed in round 1
- note: **N30** (Coworker (Agent4)): Retracting my H20 — decided from evidence instead: keep the read-only sync mount for prompts/skills, seed a writable per-session agent dir, and point the web extension's cache root into `scratch/<session>/` so the cache dies with the session and does not fight the read-only mount. Loading the extension by explicit path also keeps `packages: []`, so nothing npm-installs at session start.

### T4 `[agreed]` Concrete edge integration: Traefik dynamic route, VM firewall admitting the API port from the edge subnet only, standalone/loopback fallback, .env machine creds (Agent1 commits to edge-as-gate as a principle; no plan details the work)
- closed agreed in round 1
- raised by: Chair (chair)
- Metic (Agent1): agree (round 1) — Owned by stage 1 (contract) + stage 7 (deployment): one published port, one dynamic route to http://<vm>:<port>, nftables admitting that port from the edge subnet only, standalone = loopback behind a local reverse proxy with no code change. Port/bind/edge-subnet live in exactly one .env shared by compose and the Traefik file.
- Senior (Agent2): agree (round 1) — Add the SSE-specific edge work nobody has claimed: Traefik read timeout ≥ the longest LLM gap so the stream is not cut, no response buffering, and forward-auth re-evaluated per reconnect (reconnect is a fresh GET by design). Standalone fallback = bind loopback, same code path.
- Pro (Agent3): agree (round 1) — Add the two details the item names but no plan spelled: bind address and port come only from env (`SERVITOR_BIND`/`SERVITOR_PORT`) so the standalone case is loopback behind a local proxy with no code change (§5), and the dynamic route must state SSE intent explicitly (no buffering middleware, `responseForwarding.flushInterval`, no response timeout) — an SSE stream cut by a proxy read-timeout looks like the §7 dropped-connection bug we are explicitly forbidden to reproduce.
- Coworker (Agent4): agree (round 1) — Add three specifics to whoever takes it: the SSE router must disable response buffering with an idle timeout above the heartbeat; bind from `.env` defaulting to loopback (standalone mode needs no code change); and do not add egress filtering as 'hardening' — inference is HTTPS out to the router.
- history: agreed in round 1

### T5 `[agreed]` Upload pipeline: content-addressed names, MIME by magic, client filename discarded, chown to the slot uid before the agent can see the file
- closed agreed in round 1
- raised by: Chair (chair)
- Metic (Agent1): agree (round 1) — Content-addressed names (sha256), MIME by magic, client filename discarded, streamed to temp then atomic rename, chown to slot uid before the agent can see it. I adopt Agent4's gate detail: an upload literally named AGENTS.md must be stored under its content-addressed name and never load as a context file — my phase-0 project-scope-precedence trap, formalized as a test (assert via the leading system message in the transcript).
- Senior (Agent2): agree (round 1) — Agree with the brief verbatim, plus: stream to a temp file hashing as it writes then atomically rename (never buffer a >context upload in memory or in container storage), and no root-owned partial survives an aborted upload. Only user-attached images may be inlined, via the RPC `images` field.
- Pro (Agent3): agree (round 1) — Agree, plus one gate that is not in anyone's list: an uploaded file must not become a context file. Content-addressing defeats the filename, but see my new item on parent-directory AGENTS.md discovery, which content-addressing does not defeat.
- Coworker (Agent4): agree (round 1) — Agree as written (§8:159-161 verbatim) with one addition: after the chown the inbox is agent-writable, so every *later* app write into it must be `O_NOFOLLOW|O_EXCL` from a dirfd, and content-addressed names must stay forever — restoring client filenames is the trap.
- history: agreed in round 1

### T6 `[agreed]` §7 wire contract: show_thinking none/final/all, ~1s coalesced flushes, collapsed tool progress, lazy fetch of tool-result bodies, reattach = authoritative state → transcript → live tail (all plans have a delivery component; none names these semantics)
- closed agreed in round 1
- raised by: Chair (chair)
- Metic (Agent1): agree (round 1) — All of §7: show_thinking none/final/all filtered server-side per subscriber, ~1s coalesced flushes, collapsed tool status lines, tool-result bodies omitted with lazy fetch by toolCallId, reattach = state → transcript → live tail (fetch, not replay). Addition from N8: thinking was streamed even with reasoning configured off, so the filter runs on every run and the gate's stub emits thinking blocks on every run, with byte-level assertions (none = zero thinking bytes + one compact line per block; final = only thinking_end).
- Senior (Agent2): agree (round 1) — Agree, and pin three implementation semantics nobody stated: the verbosity filter is a per-subscriber state machine (the 'thinking… 12 s' line needs start→end timing per subscriber, so it cannot be a shared predicate); assert on bytes, not decoded objects; and the snapshot's transcript comes from RPC reads, never from re-parsing JSONL branches.
- Pro (Agent3): agree (round 1) — Agree, and insist the gate is byte-level, not object-level: assert on the raw SSE bytes that `show_thinking=none` carries zero thinking_delta/thinking_start and one compact 'thinking… Ns' line, because §7's whole point is that a client which merely does not render still pays the bytes. Also assert the run is unaffected by zero subscribers and by a subscriber that never reads. Reattach cursor = pi entry id (`get_entries since`), which docs/rpc-commands.md documents as durable across client restarts — better than a home-made sequence.
- Coworker (Agent4): agree (round 1) — This is §7 implemented literally (brief:104-133). Two things no plan states: classify droppable records by the contract, not by size; and flush any coalescing buffer before `tool_execution_start`/`*_end`/`message_end` or the client renders a tool call ahead of the text that preceded it. `none`'s 'thinking… 12 s' line must be synthesized by the app from thinking_start→thinking_end and must handle the block whose end never arrives (abort).
- history: agreed in round 1

### T7 `[agreed]` §6 meters in the UI (run active, elapsed, current tool, subprocess count, scratch usage) plus the explicit abort control
- closed agreed in round 1
- raised by: Chair (chair)
- Metic (Agent1): agree (round 1) — Meters (run active, elapsed, current tool, subprocess count, scratch usage) computed by the harness/pump from the registry and pi events — never client-reported — plus the explicit abort POST. Junk size reported to the panel per §8 ('unbounded mess is only dangerous when invisible'), with du caching so the sessions list stays O(1) in junk size.
- Senior (Agent2): agree (round 1) — Agree. Meters come from the pump and the OS, not from clients: current tool from `tool_execution_start`, subprocess count by scanning /proc for the session's slot uid (which is also what the sweep needs), scratch usage sampled asynchronously and cached — a synchronous `du` on the request path stalls the same loop that must never await a client.
- Pro (Agent3): agree (round 1) — Agree. Source the meters from the pump, not from clients: run active = `isStreaming`/`isCompacting` from get_state + events, current tool from `tool_execution_start`, subprocess count and scratch usage from psutil/du on the slot uid, and cache the du per session so the session list stays O(1). `get_state` also gives messageCount/pendingMessageCount, so the 'queued while you were away' meter is free.
- Coworker (Agent4): agree (round 1) — Meters must be computed by the pump, not read from pi: `get_state` carries no elapsed time and no current tool. Also do not derive 'run failed' from settle — a dead router still settles.
- history: agreed in round 1

### T8 `[agreed]` GC/pressure policy: gc_idle_days default, slot pressure (release oldest non-stable; all 16 running ⇒ wait and say so), disk-pressure release, stable-session marking that pins a slot
- closed agreed in round 1
- raised by: Chair (chair)
- Metic (Agent1): agree (round 1) — gc_idle_days default 14, GC timer and disk pressure both first-class triggers through the single release() path, stable sessions pinned and never GC'd, slot pressure releases the oldest non-stable non-running session, all-16-running ⇒ new session waits and the panel says so, lib/ never touched. H21 is answered from the brief: it names both triggers independently, so 14 days stays the default and disk pressure is not merely a backstop.
- Senior (Agent2): agree (round 1) — Agree, with two additions: one `release()` used by all four triggers and nothing else (Agent1's point), and a slot that fails its sweep is quarantined and visible rather than reused — a slot is never handed out unclean. Disk pressure is a co-equal trigger, not a backstop, because 200 GB is finite and the card-photo workload generates bulk.
- Pro (Agent3): agree (round 1) — Agree, with the mechanics pinned: one `release()` used by all four triggers; slots move allocated → cleaning → free and are never reusable until the sweep is verified empty; the idle GC never releases a session with a live run; all-16-running is a wait state shown in the panel, not an error; junk size is a first-class meter because unbounded mess is only dangerous when invisible.
- Coworker (Agent4): agree (round 1) — One `release()` for all four triggers, plus a `draining` state recorded *before* side effects so a crash mid-release cannot hand out an unswept slot, plus: never release a session with a live run under slot/disk pressure (skip and report), and evaluate the threshold with `statvfs` on the scratch filesystem, not `/`.
- history: agreed in round 1
- note: **N29** (Coworker (Agent4)): Retracting my H21 — AGENT-BRIEF.md:172 fixes `gc_idle_days` at 14 and :178 with :214-215 already make near-full disk a GC trigger. Keep 14 as the default and wire disk pressure as the trigger that actually protects the box; the timer is the backstop. No human answer needed.

### T9 `[agreed]` Subprocess idle lifecycle: kill after 1 h idle, respawn from the session file when picked up again
- closed agreed in round 1
- raised by: Chair (chair)
- Metic (Agent1): agree (round 1) — Kill after 1 h idle only when settled, orderly ladder (stdin EOF → SIGTERM grace → SIGKILL), respawn on pickup via --session <path> with the sessionFile captured from get_state (N6/N16.3 — not --session-id, which silently creates an empty session on cwd mismatch), and from N7 the overlay must be mounted BEFORE any spawn/respawn attempt: a missing recorded workdir makes pi exit 1 with no response, which the harness treats as a fatal spawn error, not a retry loop. Idle-kill never unmounts or deletes — only release does.
- Senior (Agent2): agree (round 1) — Agree, with the consequence nobody stated: after an idle kill the session still has to answer reads, so opening a session *is* a pickup — the read path must be able to wake a dead process. The idle timer may only fire on a settled run, and the kill ladder is stdin EOF → grace (pi waits for pending work) → SIGTERM → SIGKILL.
- Pro (Agent3): agree (round 1) — Agree, with three additions: define idle precisely (not streaming, not compacting, pendingMessageCount == 0, no subscriber, ≥1 h) so the killer never fires mid-compaction; keep the session's overlay/bind mounts for the session lifetime, not the process lifetime, because pi runs a session in its recorded cwd and Agent2's N7 shows a missing cwd is a fatal spawn error; and respawn only when a transcript actually exists, since an empty session is never persisted.
- Coworker (Agent4): agree (round 1) — Agree, with the respawn mechanism nailed: use `--session <path>` with `sessionFile` captured from `get_state`, never `--session-id` (silent empty session when cwd differs), mount before respawn, and treat a failed `prompt` *response* as terminal — some failures emit no events at all.
- history: agreed in round 1
- note: **N28** (Coworker (Agent4)): Retracting my H24 (app restart vs in-flight runs) — it is now decidable: once the app closes the read end of the stdio pipe, the orphaned pi child gets EPIPE on its next stdout write (demonstrated: child exited on EPIPE). So children cannot be adopted after an app restart, and 'survive the upgrade' means adding a per-session supervisor layer with sockets instead of pipes. v1 should drain active runs, confirm, and mark them interrupted; the supervisor layer is a separate decision with a real cost.

### T10 `[agreed]` Toolkit-baked image (Pillow, numpy, ffmpeg, ImageMagick, uv) as the read-only overlay lowerdir, with per-session venv installs dying with the session
- closed agreed in round 1
- raised by: Chair (chair)
- Metic (Agent1): agree (round 1) — Toolkit (Pillow, numpy, ffmpeg, ImageMagick, uv) baked into the image and materialized as the read-only overlay lowerdir (docker export at deploy); per-session venv installs under the workdir die with the session. The lowerdir root must ship no AGENTS.md/SYSTEM.md/.pi (verified precedence rule), and N7 confirms lower-RO + upper/work-on-same-fs overlay mounts work on this kernel (7.3.3-1-cachyos).
- Senior (Agent2): agree (round 1) — Agree. Gate additions: the toolkit lowerdir must contain no `.pi`, `AGENTS.md`, `SYSTEM.md` or `APPEND_SYSTEM.md` entry (it is the overlay lower of every session's cwd), and spawn with `--no-context-files` because context files load regardless of project trust — an AGENTS.md extracted from an upload into the workdir is otherwise read into the prompt.
- Pro (Agent3): agree (round 1) — Agree, with the one exception that makes it work: the session's venv must be created with `--system-site-packages`, otherwise the baked Pillow/numpy are invisible to the venv and every session reinstalls the world into its own upper dir. Keep the toolkit root free of AGENTS.md/SYSTEM.md/.pi entries so the read-only lowerdir cannot inject instructions through the overlay.
- Coworker (Agent4): agree (round 1) — Agree, and Agent1's check that the toolkit lowerdir contains no `AGENTS.md`/`SYSTEM.md`/`.pi` is now load-bearing rather than tidy — context files are read from the working directory and its parents, and they go into the system prompt. Keep both belt and braces: clean lowerdir *and* `--no-context-files`.
- history: agreed in round 1

### T11 `[agreed]` One-way sync of prompts/skills/tool defs/lib into the VM; lib/ read-only with 'save this script' as a distinct write-through
- closed agreed in round 1
- raised by: Chair (chair)
- Metic (Agent1): agree (round 1) — One-way sync into the VM via a host-side operator script shipped in this repo (the host holds the git credentials; the VM never does). 'Save this script' is a distinct app-uid write-through to the VM's lib/; a separate step of the same host-side script commits the VM's lib/ into the agent repo — preserving the one-way direction of the into-the-VM sync while giving kept scripts a git path without in-VM push credentials. lib/ stays read-only to the agent.
- Senior (Agent2): agree (round 1) — Agree: `rsync` push run by the operator from this repo (H2/H19 owner unknown), config mounted `:ro`, `lib/` `:ro`, and 'save this script' is a distinct privileged endpoint that copies scratch→lib staging as the app uid with git push staying outside the VM. No git credential inside the VM.
- Pro (Agent3): agree (round 1) — Agree. Sync runs host-side and one-way (repo → VM), shipped as a script in this repo; `lib/` stays read-only to the agent and 'save this script' is an app-side copy from scratch → lib (never an agent write, and no git credential inside the VM — the host pulls lib back out).
- Coworker (Agent4): agree (round 1) — Agree on one-way sync, and correct the mechanism: the synced target cannot *be* PI_CODING_AGENT_DIR. `SYSTEM.md`, skills and prompt templates load fine from a read-only mount; `settings.json` and the provider config do not, because pi refuses config it cannot lock. So: RO mount for prompt/skills/templates, plus a per-session writable agent dir seeded by copy with `models.json`+`settings.json`, and put the security-critical switches on the CLI so a silently-rejected settings.json cannot open a hole.
- history: agreed in round 1
- note: **N27** (correction, Coworker (Agent4)): N16: my point (1) had the right conclusion and the wrong mechanism, and my round-0 test was invalid because I ran as root so `chmod 555` never blocked writes. Redone with `mount -o ro,bind`: a read-only PI_CODING_AGENT_DIR crashes startup only when the session dir is not redirected (`ENOENT … mkdir <agent-dir>/sessions/--<cwd>--` from `getDefaultSessionDir`); with the redirect pi starts, warns `Invalid settings file … settings.json.lock` on stderr only, refuses the provider config, and `prompt` fails with `success:false "No API key found for the selected model"` and **no events at all**. Agent2's N4 and my N16 are the same experiment run on both sides of `--session-dir`; the merged fact is stronger than either and is why my Stage 5 has a negative gate for this exact layout.

### T12 `[agreed]` API key plumbing: OMNISSIAH_API_KEY env-only per pi subprocess, never on disk, never in a transcript
- closed agreed in round 1
- raised by: Chair (chair)
- Metic (Agent1): agree (round 1) — Key is env-only per pi subprocess, never on disk, never in a transcript — with two corrections to my phase 0: models.json must be rendered per-spawn into the writable agent dir (baseUrl is NOT env-interpolated, N16.2, and models.json is read only from the agent dir), and only apiKey uses interpolation ($OMNISSIAH_API_KEY — the exact mechanism in the owner's live models.json, N12). Never the --api-key flag (visible in ps to every slot). Same pattern for the Exa key via rendered web-search.json (N9: agent-readable = accepted residual, stated in the security checklist). Gates assert the key string is absent from the state volume, session files, and app logs.
- Senior (Agent2): agree (round 1) — Agree, plus two things that silently break env-only: never pass `--api-key` (visible in /proc/<pid>/cmdline to every slot), and ship NO `auth.json` in the image or in the synced config — credential precedence puts a stored auth.json credential *above* the provider env var, so a stale one wins and the 'env-only' claim is quietly false.
- Pro (Agent3): agree (round 1) — Agree, and refine Agent4's conclusion: env interpolation works for `apiKey`, so the key needs no rendering; what needs a literal is `baseUrl`, which is not a secret. So 'render models.json per spawn' is optional — baking the router URL into the synced config is enough, and the seeded per-session agent dir copy is where it lands. Never `--api-key` on argv (visible in `ps` to every slot uid).
- Coworker (Agent4): agree (round 1) — Agree. Three consequences no plan lists: pass `env=` as an explicit allowlist (never inherit), never use `--api-key`, and expect `auth.json`/`models-store.json` to appear in the agent dir — so those files live in the per-session dir and get swept, and `HOME` must be the per-session dir too. Also sanitize pi's error text before it reaches the transcript: provider errors embed install paths.
- history: agreed in round 1

### T13 `[agreed]` VM/resource provisioning: 24 vCPU with CPUWeight, virtio-balloon + MemoryHigh 25G / MemoryMax 30G, zram, ~200 GB fixed disk
- closed agreed in round 1
- raised by: Chair (chair)
- Metic (Agent1): abstain (round 1) `declined`
- Senior (Agent2): agree (round 1) — Agree; this is host config shipped as `vm/` provisioning + doctor assertions, not app code. One app-side consequence: `MemoryMax=30G` can OOM a pi child mid-run, so the pump must classify a child exit distinct from `agent_settled` and show it as 'killed by OOM', not as a hung or successful run.
- Pro (Agent3): agree (round 1) — Agree, and scope it honestly in the plan: the repo ships the guest-side slice (CPUWeight low, MemoryHigh=25G/MemoryMax=30G), zram setup, and the numbers/notes for the hypervisor side (vCPU count, ballooning with free-page-reporting, 200 GB fixed disk), which the human applies on the host — the plan must not claim a gate it cannot run.
- Coworker (Agent4): agree (round 1) — Agree, scoped honestly: provisioning is host work the repo can only *assert*. Make `servitor-doctor` read the applied unit properties instead of trusting a drop-in file, and note that these knobs are coupled to H5 — if the service ends up in a container, MemoryHigh/CPUWeight live on that container's unit, not on a hand-written slice.
- history: agreed in round 1

### T14 `[agreed]` PI_CODING_AGENT_DIR vs PI_CODING_AGENT_SESSION_DIR split onto different volumes
- closed agreed in round 1
- raised by: Chair (chair)
- Metic (Agent1): abstain (round 1) `declined`
- Senior (Agent2): agree (round 1) — Agree on the split, but the item's premise needs correcting: PI_CODING_AGENT_DIR cannot be the read-only synced mount at all — it must be per-session and writable (overlay upper in scratch), so the split is really 'agent dir in scratch, session dir on the state volume'. And use exactly one session-dir mechanism: `--session-dir` overrides PI_CODING_AGENT_SESSION_DIR.
- Pro (Agent3): agree (round 1) — Agree on the split, but the agent dir is per-session and WRITABLE, seeded from the read-only synced config; the synced config reaches pi through --system-prompt/--append-system-prompt/--skill/--prompt-template/-e, never as PI_CODING_AGENT_DIR. Use exactly one mechanism for the session dir — the `--session-dir` flag, identically at spawn and respawn (it outranks the env var), and record the returned sessionFile. With an explicit session dir the transcript path came back flat (no `--<cwd>--` grouping), so per-session session dirs make transcript deletion one directory delete.
- Coworker (Agent4): agree (round 1) — Agree on the split, and adopt Agent1's N2 with the mechanism pinned: use the `--session-dir` **flag only** and never set PI_CODING_AGENT_SESSION_DIR, because the flag wins over the env var and passing both means one of them silently lies. Also note the layouts differ: with `--session-dir` files land flat, without it they land in `<agent-dir>/sessions/--<cwd>--/`, so neither path is reconstructable — always store `sessionFile`.
- history: agreed in round 1

### T15 `[agreed]` The per-session Pi agent dir is an overlay (lower = read-only synced config, upper = `scratch/<s>/agent-upper`), not the synced mount itself and not a per-spawn seeded copy
- closed agreed in round 2
- raised by: Senior (Agent2) (also reported by Coworker (Agent4))
Admits: (a) overlay, reusing the same mount primitive as the workdir —
always current with the sync, no copy cost, all of pi's writes land in
scratch and die with the session; (b) seeded writable copy per spawn —
works, but staleness mid-session and a copy on every wake; (c) symlinks —
insufficient, because pi creates siblings (`settings.json.lock`,
`auth.json`, `models-store.json`) in the dir itself.
- Metic (Agent1): agree (round 2) — I change my position from round 1 (where I raised the flag-based alternative) and now agree with the overlay: lower = read-only synced config, upper = scratch/<s>/agent-upper. Evidence that decided it: N5 (SYSTEM.md/skills/prompts load from the read-only lower — skill:demo-skill appeared in get_commands) and N20 (overlay with RO lower + writable upper verified working, pi writing auth.json/models-store.json into the upper). The shadowing risk I raised (agent writes settings.json/SYSTEM.md/skills into the upper, persisting across in-session idle-kill respawns) is real but bounded: it is session-scoped (dies at release), gives the slot uid no new reach (it already owns its territory and can read the config), and is made visible (agent-upper file list in the panel + doctor; release logs it). The operational wins are substantial: no per-spawn rendering of models.json/settings.json (T16 removes the baseUrl rendering need entirely), config stays diffable and always current with the sync, and the agent dir behaves exactly like the owner's real environment. Compensating controls: agent-upper contents surfaced in panel/doctor, lower stays ro, release sweeps it. Note: the overlay subsumes T14's split (config on a separate ro volume as the lower, session data in the per-session upper on the state volume), so the settled invariant holds without PI_CODING_AGENT_SESSION_DIR or the --session-dir flag.
- Senior (Agent2): agree (round 2) — Confirmed (T24 folded here — naming the survivor). The overlay variant is not merely viable, it is strictly cheaper than flag-enumeration: skills/prompts/SYSTEM.md load from the read-only lower with **zero** `--skill/--prompt-template` flags, and pi's own writes land in the upper. Seeded-copy-per-spawn is acceptable but must be re-seeded to stay current with the sync; symlinks remain insufficient.
- Pro (Agent3): agree (round 2) — (a) overlay. Same mount primitive as the workdir, no copy per wake, config current at mount time, and every write pi makes (auth.json, models-store.json, settings.json.lock, trust.json.lock, tmp/) lands in scratch and dies at release. Two consequences to write down, not discover: overlayfs will not show a nested mount inside a lowerdir, so the lower must be a plain subdirectory of the sync mount and never a mount itself; and copy-up means the session keeps the config it started with even if the sync updates mid-run — desirable (no system prompt changing under a running session), but it must be stated so nobody 'fixes' it. (c) is correctly rejected: pi creates lock siblings in the dir itself.
- Coworker (Agent4): agree (round 2) — Take (a) overlay — but fix its rationale and add two conditions. The freshness argument is void (see evidence), so the real reasons are: no copy step, and every write pi makes lands in the session's upper and dies with it. Conditions: the sync must swap in a new versioned config dir rather than mutating a lower that a live overlay still references; and `models.json` must be pinned read-only inside the merged dir (my new item), because the overlay upper lets the agent shadow it.
- history: agreed in round 2
- note: **N31** (Metic (Agent1)): T15: I am recording my position change explicitly — in round 1 I held the flag-based config-loading design (explicit --system-prompt/--skill/-e + minimal per-spawn rendering) and objected to the overlay on shadowing grounds. I now agree with the overlay. What moved me: N5/N20 verified that pi loads SYSTEM.md/skills from the read-only lower and handles its writable-state writes in the upper correctly (so the flag design's complexity buys little), and my reach analysis of the residual shadowing risk (session-scoped, no new capability for the slot uid, inspectable via the agent-upper panel listing) showed it is a visibility problem, not a boundary problem. The flag design is retired, not lost: the launch line still asserts --no-approve --no-context-files (T26) and the ancestor-context-file-free invariant (T20), which close the workdir/ancestor channel regardless of the agent-dir mechanism.
- note: **N33** (Senior (Agent2)): T15: the per-session overlay agent dir is proven end-to-end offline — synced SYSTEM.md/skills load from the read-only lower with no resource flags at all, the env key reaches the router as `Bearer <value>`, and pi's own `auth.json`/`models-store.json` writes land in the scratch upper and die with the session.
- note: **N43** (Coworker (Agent4)): New hole for the plan, independent of T15's copy-vs-overlay choice: pi must write `auth.json`/`models-store.json`/lock dirs into the agent dir, so the slot uid owns that directory, so it can replace `models.json` there — and since the subprocess is respawned after every idle kill, the substituted endpoint is used on the *next* run, taking the bearer key with it. Two cheap closings: a read-only bind of that single file (verified EROFS/EBUSY), and a per-spawn assertion of provider/baseUrl/tools read back from `get_available_models`.

### T16 `[agreed]` `models.json` baseUrl must be a literal string; only `apiKey`/headers interpolate from env
- closed agreed in round 2
- raised by: Senior (Agent2)
Admits: (a) keep the router URL literal in the synced config (it is not a
secret) and .env carries only the key — no rendering step, config stays
diffable; (b) render models.json per session into the writable agent-dir
upper if the owner insists the URL live in .env. Never `$VAR` in baseUrl.
- Metic (Agent1): agree (round 2) — Literal router URL in the synced config, $VAR interpolation only for apiKey/headers, .env carries the key only, never $VAR in baseUrl. Under the T15 overlay this needs no rendering step at all — the lower's models.json (literal URL + apiKey "$OMNISSIAH_API_KEY") is exactly the shape of the owner's live models.json, which I inspected this round.
- Senior (Agent2): agree (round 2) — Confirms, and removes Agent4's follow-on consequence: with `baseUrl` literal in the synced models.json there is **no** per-spawn rendering step at all — the only interpolated value is the key, and it resolves through the overlay. `render models.json per spawn` is work we do not have to build.
- Pro (Agent3): agree (round 2) — (a): literal router URL in the synced config, `.env` carries only the key. If the owner later insists the URL is deployment-specific, render models.json **once per session at mount time** into the overlay upper — not per spawn — so the config stays diffable and the launch path stays identical.
- Coworker (Agent4): agree (round 2) — Take (a): literal router origin in the synced config. And delete the `.env` variable for the router origin entirely — keeping a `SERVITOR_ROUTER_BASE_URL` that nothing reads is exactly the drift trap; `.env` then carries only the key, which is where the secret-only rule already puts it.
- history: agreed in round 2

### T17 `[agreed]` Policy for `extension_ui_request` dialogs when nobody is attached: forward to an attached client, otherwise auto-`cancelled:true` after a short deadline and record it in the event feed
- closed agreed in round 2
- raised by: Senior (Agent2)
Admits: (a) auto-decline only (safest, but a legitimate prompt-for-input
breaks); (b) forward-with-timeout-decline (preferred — no approval gate is
introduced, and nothing hangs); (c) auto-allow (never: an extension asking
'allow dangerous command?' would self-answer yes). This is not covered by
any docket item and any third-party extension hits it.
- Metic (Agent1): agree (round 2) — Forward-with-timeout-decline (b): an attached client gets the dialog; with no client (or on deadline) auto-cancel and record the request+disposition in the session's event feed so the user can see what was declined. Never auto-allow (c) — a self-answered 'allow dangerous command?' is the failure mode. This also keeps N14's hang case closed for third-party extensions.
- Senior (Agent2): agree (round 2) — Unchanged, and still covered by no other item: forward to an attached client, else `cancelled:true` after a deadline, recorded in the feed; never auto-allow.
- Pro (Agent3): agree (round 2) — (b) forward-with-timeout-decline, with three additions: the decline is `cancelled:true` and never a value; the request and its outcome are recorded in the session's event feed so a phone user who returns sees 'an extension asked X at 14:02, declined at 14:03'; and the deadline is per-session config, not a magic constant. (c) is unacceptable — an 'allow dangerous command?' dialog self-answering yes is exactly the persistence path §3's threat model is about.
- Coworker (Agent4): agree (round 2) — Take (b). Specify the responder by the documented contract so it cannot be got wrong: reply **only** to `select`/`confirm`/`input`/`editor`, with a matching `id`, and `cancelled: true` as the decline; never answer `notify`/`setStatus`/`setWidget`/`setTitle`/`set_editor_text` (they expect no response, and a stray reply desynchronises the sub-protocol). Requests already carrying `timeout` need no client-side timer. Two additions: keep the deadline short (a couple of seconds, not 30 — an unanswered dialog looks like a stalled run in the §6 meters), and forward the fire-and-forget records into the event feed, because in a headless deployment that is the only UI an extension has.
- history: agreed in round 2

### T18 `[agreed]` Reads of sessions with no live process go through a short-lived RPC probe; the app never reconstructs the active branch by parsing session JSONL
- closed agreed in round 2
- raised by: Senior (Agent2)
Admits: (a) probe spawn (`--session <path>` →
get_messages/get_session_stats/get_tree → stdin EOF) — ~2 s, no model call,
and branch/compaction semantics stay Pi's; (b) always-respawn-on-open
(Agent1) — same thing, kept alive; (c) offline JSONL parsing — forbidden
for rendering: it must re-derive the parent chain and compaction
`firstKeptEntryId` and abandoned branches, which is Pi's contract, not
ours. Enforce one process per session file so the probe cannot race the
live run.
- Metic (Agent1): agree (round 2) — Agree with (a): reads of sessions with no live process go through a short-lived RPC probe (--session <path> → get_messages/get_session_stats/get_tree → stdin EOF), never (c) offline JSONL parsing (active-branch/compaction derivation is pi's contract), with one process per session file enforced so a probe cannot race a live run. Refinement I add: no respawn-on-view. A live process exists only for user actions (prompt/steer/follow_up/abort of an active run) — i.e. the brief's 'picked up' — so the session LIST is registry-only (O(1), no pi processes), the §6 'subprocess count' meter stays a true count of active conversations, and a passive phone poll never wakes anything. This is the design-out Agent2 asked for in Q2.
- Senior (Agent2): agree (round 2) — Probe reads stand, and Agent4's EPIPE finding strengthens them: since an orphan dies on its next stdout write, a short-lived probe we start and stop ourselves is the only safe shape — never leave a readerless child around.
- Pro (Agent3): agree (round 2) — (a), and (c) must be forbidden outright — reconstructing the active branch is pi's compaction/tree contract. Three constraints T18 does not yet state: the probe must run as the **slot uid with the same mounts** as a real run (a root-run probe creates slot-owned-path files as root, breaks the ownership invariant the sweep depends on, and can write into a session file the agent owns); one process per session file at a time, so a probe cannot race a live run; and the list view reads the registry only (Agent2's Q2 failure mode — never wake 15 sessions to render a list), with probe results cached briefly per opened session.
- Coworker (Agent4): agree (round 2) — Take (a). One corollary to make an assertion out of it: a probe must never be the first process to touch a session's agent dir, because pi creates `auth.json`/`models-store.json` there — a probe running first would leave root-owned files in a directory the slot uid must write. T22's rule (probe only when the registry holds a recorded transcript) already prevents it, so assert it rather than assume it.
- history: agreed in round 2

### T19 `[agreed]` In-flight runs at app restart/upgrade: drain (stop accepting prompts, wait a bounded grace for `agent_settled`), then abort with a recorded cause and mark the session interrupted; never SIGKILL first
- closed agreed in round 2
- raised by: Senior (Agent2) (also reported by Pro (Agent3))
Admits: (a) drain-then-abort-with-cause (preferred — the whole point of the
system is long runs, and §7 forbids a connection event from cancelling a
run; an app restart is not a user action either); (b) die with the
container and let the operator re-run (acceptable only for an emergency
stop); (c) live-migrate the child — impossible, the child owns stdio pipes.
Session files and mounts always survive; respawn on next pickup.
- Metic (Agent1): agree (round 2) — Drain-then-abort-with-cause: stop accepting prompts, close stdin (orderly shutdown waits for pending work per docs/rpc.md), bounded grace for agent_settled, then explicit abort with a recorded cause, session marked interrupted; never SIGKILL first. N28's EPIPE demonstration confirms children cannot be adopted after an app restart, so drain is the only honest v1 story; the supervisor-socket layer is a separate, costed decision, not smuggled in.
- Senior (Agent2): agree (round 2) — T21 folded here — survivor T19. Upgrade this from 'preferred' to 'forced': N28's EPIPE demonstration means a crashed or restarted app cannot leave runs alive, so drain-then-abort-with-cause is the only v1 behaviour and 'die with the container' is not an option anyone chooses. A supervisor/socket layer is a separate, costed decision.
- Pro (Agent3): agree (round 2) — (a). Sequence: stop the GC/pressure triggers, refuse new prompts, send RPC `abort` with a recorded cause after a bounded grace (stdin EOF is the orderly-shutdown mechanism, never SIGKILL first), mark the session `interrupted` in the panel, and let the next pickup respawn from the transcript. Agent4's N28 settles the durability question for me — once the app closes the read end of the pipe the child gets EPIPE on its next write, so adoption is not a thing without a socket supervisor layer, which is a separate decision with a real cost.
- Coworker (Agent4): agree (round 2) — Take (a). Make the grace window derived rather than invented: it must be strictly less than the unit's `TimeoutStopSec`, or systemd escalates to SIGKILL mid-drain and the recorded cause lies. And adopt is off the table by physics, not preference — once the app's read end of the stdio pipe is closed, the child gets EPIPE on its next stdout write, so there is nothing to re-attach to.
- history: agreed in round 2

### T20 `[agreed]` Context-file discovery is an injection channel that content-addressed uploads do not close: pi loads AGENTS.md/CLAUDE.md from the working directory AND every parent directory, with no project-trust prompt, so the ancestor chain of every per-session workdir must be context-file-free and/or the spawn must pass `-nc`.
- closed agreed in round 2
- raised by: Pro (Agent3)
Two defences, both cheap and both asserted: (a) root the session tree at a
path whose ancestors contain no AGENTS.md/CLAUDE.md (never inside a git
checkout), and (b) spawn with `-nc/--no-context-files`, taking standing
instructions only from SYSTEM.md/APPEND_SYSTEM.md or
`--append-system-prompt`. Gate: plant `AGENTS.md` in the uploads dir, in
the workdir, and one ancestor level up; assert the leading system message
in the transcript contains none of them (Agent4's vm-smoke already asserts
the workdir case; extend it to parents).
- Metic (Agent1): agree (round 2) — Both defenses: (a) the session tree is rooted so no ancestor of any per-session workdir contains AGENTS.md/CLAUDE.md (the state-volume layout is asserted context-file-free at image build — never nest sessions inside a git checkout), and (b) the launch line carries -nc/--no-context-files so standing instructions come only from the synced SYSTEM.md. Gate plants sentinels in the workdir, the inbox, and one ancestor level up and asserts none reaches the leading system message (extending Agent4's vm-smoke workdir case to parents).
- Senior (Agent2): agree (round 2) — Reproduced both halves of the claim in one run, including the ancestor-directory case that content-addressed naming cannot reach. The fix is free: `-nc` removes the injected context files and still leaves our own SYSTEM.md in place.
- Pro (Agent3): agree (round 2) — Agent4's T26 evidence closes Agent2's Q1: content-addressed naming protects only the inbox, because the agent owns its workdir and can recreate any filename there, and context-file discovery needs no trust. So the defence is the launch flag (`-nc`), with the clean-ancestor-chain rule as belt-and-braces; standing instructions then come only from SYSTEM.md/APPEND_SYSTEM.md (not context files) in the overlay lower, which is what we want anyway.
- Coworker (Agent4): agree (round 2) — Agree, and the ancestor half is no longer an inference — it reproduces. Consequence worth writing into the path contract: `SERVITOR_ROOT` must not sit inside a git checkout, because a repo-level `AGENTS.md`/`CLAUDE.md` would then inject into every session's system prompt; `servitor doctor` should assert that no ancestor of the session root contains either filename, alongside `-nc`.
- history: agreed in round 2
- note: **N35** (Senior (Agent2)): T20/T26: measured, not argued — without `--no-context-files` a planted AGENTS.md in the workdir AND one in its parent directory both reach `messages[0]` (request body grew 4225→4601 bytes); with `-nc` both vanish and our SYSTEM.md remains. `-nc` is not optional and `--approve` never appears in the launch line.

### T22 `[agreed]` Session existence is the app's fact, not pi's: the registry owns it and records `get_state.sessionFile`; never respawn with `--session <path>` for a session that was never prompted.
- closed agreed in round 2
- raised by: Pro (Agent3)
An empty session is not persisted, so a respawn path can legitimately not
exist yet. Rules: first spawn mints the id and uses `--session-id`; respawn
uses `--session <recorded path>` only if the registry says a transcript
exists; a missing transcript with a live registry row is 'idle, nothing to
resume', not an error; a missing cwd is a fatal spawn error that routes to
release, not a retry loop.
- Metic (Agent1): agree (round 2) — Session existence is the app's fact: the registry owns it and records get_state.sessionFile verbatim; first spawn mints the id with --session-id; respawn uses --session <recorded path> only when the registry says a transcript exists; a registry row without a transcript is 'idle, nothing to resume' (not an error, since empty sessions are never persisted); a missing cwd at spawn is a fatal spawn error that routes to release with a recorded cause, never a retry loop.
- Senior (Agent2): agree (round 2) — Agree. Add the failure mode my run exposes: pi logs a warning and silently creates a fresh session for a missing id, so a respawn that guesses 'the transcript should be there' can fork a second lineage under the same session id with only a stderr line as evidence. The registry flag, not the filesystem, decides which of `--session-id` and `--session <path>` is legal.
- Pro (Agent3): agree (round 2) — One refinement under T18's probe rule: 'respawn' and 'probe' become one launch path with a lifetime difference, so the guard belongs there — no `--session <path>` unless the registry says a transcript exists; a live row with no transcript is 'idle, nothing to resume', not an error; a missing cwd is fatal-to-session and routes to release.
- Coworker (Agent4): agree (round 2) — Agree, with one hardening detail: do not try to detect a bad respawn from stderr. The warning 'No project session found with id …; creating a new session with that id.' is printed on a *legitimate first* spawn too, so it is not evidence of a cwd mismatch; assert identity from the protocol instead — after every spawn, `get_state.sessionId` must equal the minted id and `get_state.sessionFile` must equal the recorded path, else fail closed and route to release.
- history: agreed in round 2

### T23 `[agreed]` Gate split by capability: overlay/mount/sweep mechanics are testable in this sandbox; uid-drop is not — so the isolation suite must split into `make sandbox-smoke` and `make vm-smoke`, with the latter loud-skipped and merge-blocking, not silently green.
- closed agreed in round 2
- raised by: Pro (Agent3)
Overlayfs (lower + upper/work on one fs), the read-only bind, the EROFS
assertions and the owner-sweep walk all run here; only the setuid/setgid
spawn needs a guest or privileged runner. Every stage gate that touches
isolation must say which half it ran and refuse to certify the whole from
CI.
- Metic (Agent1): agree (round 2) — Split the isolation gates by capability: make sandbox-smoke (overlay lower+upper/work on one fs, writes landing in upper, EROFS on the ro bind, sweep-walk, teardown order — all runnable in this sandbox per N25/N23's unshare -Urm proxy) and make vm-smoke (setuid/setgid spawn, cross-slot EPERM, real sweep — guest/privileged runner only), with the vm half loud-skipping and merge-blocking so CI can never certify the whole from the sandbox. This corrects my N19, which over-claimed that isolation is wholly untestable here (retracted).
- Senior (Agent2): agree (round 2) — Right call, and the boundary is now measured rather than guessed — the CI half is bigger than Agent4 said and the guest half is exactly 'anything needing a second uid': setuid, setgid, chown-to-slot, cross-uid `kill()` EPERM, and sweep-by-owner with real owners.
- Pro (Agent3): agree (round 2) — Adopt Agent2's N23 as the refinement of my own note: the overlay half is CI-smokable two ways (direct `mount -t overlay` where the runner has CAP_SYS_ADMIN, or inside `unshare -Urm` where it does not), and the uid-drop half is guest-only (`setpriv --reuid` to an unmapped uid → EINVAL, and `unshare --map-users` rejects a multi-uid map). Keep my point that a skipped guest run must be loud and merge-blocking, and that every gate says which half it actually executed.
- Coworker (Agent4): agree (round 2) — Agree, and my N17 is the thing being corrected: overlay is CI-testable here, so `sandbox-smoke` can certify overlay/whiteout/upper-path/EROFS/teardown-order and the sweep walk; only uid-drop and sweep-by-owner need the guest, loudly skipped and merge-blocking.
- history: agreed in round 2

### T25 `[agreed]` The harness must treat a failed `prompt` response as terminal, because some failure modes emit no events at all — waiting for `agent_settled` hangs the run instead of failing it.
- closed agreed in round 2
- raised by: Coworker (Agent4)
Rule: `success:false` on the prompt response ends the run with that reason
(sanitized), surfaces it in the meters, and does not arm a settle timer.
`disposition:handled` remains the only success-without-run case.
- Metic (Agent1): agree (round 2) — A failed prompt response (success:false) is terminal: end the run with the sanitized reason, surface it in the meters, and arm no settle timer — some failure modes emit zero events (Agent4's no-API-key repro: no agent_start, no turn_start, no agent_settled, process idle), so waiting for agent_settled hangs instead of failing. disposition:handled remains the only success-without-run case. The conformance suite must include the no-API-key repro as a regression.
- Senior (Agent2): agree (round 2) — Reproduced independently, and it is worse than 'no events': `get_state` still returns success, so a health check and a meters panel both look healthy while inference is permanently dead. Treat `success:false` as terminal and also surface it on the session, not just the response.
- Pro (Agent3): agree (round 2) — Confirmed independently, and it is the nastiest item in this round because the naive driver hangs instead of failing. Add to the same rule set: the reason is sanitized before it reaches the browser (the example error text contains package paths and `/login` instructions meant for a terminal); no automatic retry (an unresolvable credential is a config fault, and retrying it burns the slot); the meters show 'failed, not settled'; and `get_state.model.id == "unknown"` is checked at spawn so the common cause is caught before any prompt is sent.
- Coworker (Agent4): agree (round 2) — Agree, and the case for it got stronger: `--no-approve` turns a broken-config crash (exit 1) into exactly the silent, eventless failure this rule is for. Without it, misconfiguration is loud; with it, misconfiguration is a spinner. Either way the only correct handler is the response level.
- history: agreed in round 2
- note: **N34** (Senior (Agent2)): T25: the same failure leaves the system looking healthy — `get_state` returns `success:true` and the process stays idle while `prompt` is permanently rejected. Any panel that only polls state will show a live session that can never run.
- note: **N38** (Pro (Agent3)): T25 is worse than 'handle the error': I reproduced it with a writable agent dir and an unset key — `prompt` returned `success:false` and the process emitted zero event records (2 records total, both responses), so a driver that arms a settle timer on any accepted-looking prompt hangs the session forever. The cheap upstream guard is at spawn: `get_state.model.id == "unknown"` means no usable model — and it appears even when models.json declares a model, if that model's credential does not resolve, which is the narrower reading N16(4) had.

### T26 `[agreed]` The launch line must carry `--no-approve` and `--no-context-files` and must never carry `--approve`; assert the exact flag set in CI, not by review.
- closed agreed in round 2
- raised by: Coworker (Agent4)
Because the agent owns its overlay and the session is respawned after every
idle kill, `--approve` lets generated code write `.pi/SYSTEM.md` and
rewrite the system prompt of the next spawn; `AGENTS.md` (or an upload
named `AGENTS.md`) does the same on every default launch.
- Metic (Agent1): agree (round 2) — The launch line carries --no-approve and --no-context-files and never --approve, and CI asserts the exact flag set (a lint/AST check on the spawn path, not code review). Rationale stands: the agent owns its overlay and the session respawns after every idle kill, so --approve would let generated code write .pi/SYSTEM.md and rewrite the next spawn's system prompt; AGENTS.md in the workdir does the same on any default launch. Combined with T20 this is one launch-line contract: --mode rpc --no-approve --no-context-files plus the session-selection flag and nothing else that changes trust.
- Senior (Agent2): agree (round 2) — Agree, and make it two assertions rather than one: pin the argv (the driver records its own launch line and CI diffs it against an allow-list) and keep the sentinel test from arm C as a permanent test, because an argv check alone would not catch a future pi default change.
- Pro (Agent3): agree (round 2) — Assert the exact flag set as one constant with a CI test, and add two flags to the same assertion so nobody re-litigates them per-spawn: `--session-dir` as the only session-dir mechanism (N2) and the no-extension/explicit-extension pair (`--no-extensions -e <baked path>`, N22). Also assert `--api-key` is never present, since argv is world-readable to every slot uid.
- Coworker (Agent4): agree (round 2) — Agree, and the flags are now load-bearing for a second reason: the settings file they can override sits in a directory the agent owns, so the CLI is the only trusted surface. Verified that CLI wins: `--tools read` over a settings file asking for `read,bash,edit,write` declared only `['read']`, and a shadowed `defaultProjectTrust: "always"` did not re-enable a cwd `AGENTS.md` because `--no-context-files` was on the command line. Keep `--approve` banned outright and assert the exact set in CI.
- history: agreed in round 2

### T27 `[agreed]` There is no path for generated files to leave the VM; the card-binder scenario implies one (crops, CSV, the report file), and scratch is deleted at release.
- closed agreed in round 2
- raised by: Coworker (Agent4)
Cheapest that fits §7: list + download from `scratch/<session>/` through
the app (already able to read it), read-only, capped; or accept
chat-text-only egress and say so in the plan. Seconding H3 because I would
act on the answer either way.
- Metic (Agent1): agree (round 2) — There must be an egress path: list + download from scratch/<session>/ through the app (which already reads it as the privileged uid), read-only, size-capped, session-scoped — the cheapest option that fits §7's fetch-not-push model and answers the card-binder scenario (crops, CSV, the report). Chat text alone would strand the report file; 'save this script' to lib/ remains the durable-artifact path. This matches my round-1 H3 answer; I second H3 because I would act on the answer either way.
- Senior (Agent2): agree (round 2) — Agree, and I would build it rather than wait for the human: read-only list+download served by the app from `scratch/<session>/`, capped per file, edge-gated like everything else, deleted at release with the session. The alternative — chat-text-only — fails the brief's own example, and the alternative of 'save it to lib/' puts user artifacts into a git-backed durable store they never asked for. If the human says chat-text-only, deleting one read-only endpoint is the whole revert.
- Pro (Agent3): agree (round 2) — Add the path: read-only list + download from the session's merged workdir, plus the two rules that make it safe — every open is dirfd-relative with `O_NOFOLLOW` refusing symlinks and non-regular files (see my new item), and the UI must make clear that release deletes the artifacts, with stable sessions as the 'leave it until I come back' mechanism. Chat-text-only is an acceptable fallback only if the plan says so out loud, which §1's card-binder report does not.
- Coworker (Agent4): agree (round 2) — Agree — and Agent3's HTTP stage already lists artifact download, so this is converging; fix the scope so it cannot grow: read-only listing + download of `scratch/<session>/`, path resolved under the session root with no `..` and no symlink follow, size-capped, no execution, and the files still die at release (`stable` pins the slot, not the artifacts). Chat text stays the primary egress so the card scenario is satisfiable without it.
- history: agreed in round 2

### T28 `[unseen]` The config stage gets an offline 'instructions and credentials actually arrive' gate built on a local OpenAI-compatible mock router, and it must include a negative gate for the read-only-agent-dir layout
- raised by: Senior (Agent2)
Four assertions, all runnable in CI here with zero inference: (1) the mock
receives `Authorization: Bearer <env value>`, so the key travelled
env→models.json→request and nothing credential-shaped is on disk or argv;
(2) the transcript's leading system message contains our SYSTEM.md marker
and the declared `<skills>` block, so the synced config is really loaded;
(3) sentinel AGENTS.md in cwd and in an ancestor directory are absent from
that system message; (4) NEGATIVE gate: pointing PI_CODING_AGENT_DIR at the
read-only sync mount must fail the run (prompt `success:false`, no events)
so nobody 'simplifies' the layout back into a broken one that still passes
`get_state`. My three-arm harness already implements all four in ~120
lines.

### T29 `[unseen]` The state volume must be a real bind-mounted filesystem, not container storage: overlay refuses upperdir/workdir on overlayfs
- raised by: Senior (Agent2)
Consequences for the compose layout (H5): the scratch/upper/work tree has
to come from a bind mount or named volume backed by the guest's ext4, and
boot must assert it (`stat -f`/`findmnt -T` on the scratch path must not
report overlay) before any session mounts. Fixing this late is a
data-layout change, and every session transcript records its cwd, so it is
a freeze-now decision.

### T30 `[unseen]` 'Save this script' into `lib/` is authorized by a human action in the UI and is never callable by the agent or triggered by agent output
- raised by: Senior (Agent2)
Admits: (a) the UI calls the endpoint with a path the human picked from a
scratch listing, app-side validation confines it to `scratch/<session>/`
(resolved realpath, no symlink escape), size-capped, name-collided names
get a suffix, no git push from inside the VM — this is the answer to the
persistence question Q7 raises; (b) an agent-callable save makes `lib/`,
the one durable reviewable artifact, writable by injected content on every
future session; (c) agent-suggests-and-human-confirms is acceptable only if
the confirm is a distinct human action, which collapses to (a).

### T31 `[unseen]` Every app-side read of an agent-owned tree is a symlink/traversal hazard, because the app runs as root and the tree is written by the slot uid: transcript reads, lazy tool-result bodies, scratch artifact listing/download, and any save-to-lib copy must open dirfd-relative with O_NOFOLLOW, refuse symlinks and non-regular files, cap size, and never accept a resolved path from the client.
- raised by: Pro (Agent3)
The agent controls every byte of its overlay, so it can plant `report.csv
-> /var/lib/servitor/inbox/<other>/…`, `link -> /home/slotN/…`, or a fifo
that hangs a reader. The app's own privilege is what makes this dangerous,
not a bug in the agent's sandbox. Same rule applies to `save-script`
(T27/H3): validate the source inside the session root the same way before
copying it to the durable artifact.

### T32 `[unseen]` `get_state.model.id == "unknown"` is the single no-usable-model signal and appears both when models.json is absent and when a declared model's credential fails to resolve; the spawn path must fail closed on it (config error, session not runnable) instead of waiting for a prompt to fail silently.
- raised by: Pro (Agent3)
This is the cheapest guard against the class of failure T25 describes, and
it belongs in the same place as the 'model scope' assertion in the
config-layout gate. It also corrects the narrower reading in N16(4)
(placeholder only when nothing is configured).

### T33 `[unseen]` The app's privilege level must be decided in the plan, not left to §8's 'root or a narrowly privileged uid': the read paths (transcript of a dead session, lazy tool bodies, scratch downloads, the owner sweep, mount/umount, chown handoff) either run as root or need an explicit ACL scheme on every per-session tree.
- raised by: Pro (Agent3)
Recommend root, with the confinement rule above as the compensation and the
invariant 'the agent is never root' unchanged; if the owner prefers a
narrow uid, then ACLs (setfacl on inbox/scratch/sessions roots at allocate
time, removed at release) become a required step of both allocate and
release, and the sweep must account for app-owned files inside the session
tree. Picking this late would rewrite allocate/release twice.

### T34 `[unseen]` The per-session agent dir is necessarily writable by the slot uid, so `models.json` inside it is writable by generated code too: pin it read-only with a single-file bind mount, and assert at every spawn/respawn that the resolved provider id, `baseUrl` and declared tool set equal the intended values, failing closed otherwise.
- raised by: Coworker (Agent4)
Admits: (a) read-only bind of the one file over the merged agent dir
(verified: write EROFS, unlink EBUSY — even as root, so certainly not
doable by a slot); (b) detection-only per-spawn assertion via
`get_available_models` (works even if the bind is dropped); (c) do nothing
— unacceptable, because respawn happens after every 1 h idle kill, so a
shadowed config lands on the *next* run rather than this one. Note the same
hole exists under the seeded-copy design in T15, so this is independent of
that choice.

### T35 `[unseen]` `servitor doctor` must certify config resolvability with a no-prompt spawn: assert `get_available_models` lists the intended provider with the intended baseUrl, as a deploy gate before serving and after every sync.
- raised by: Coworker (Agent4)
One short-lived spawn (`--mode rpc`, no prompt, no model call, ~1.5 s) that
catches the whole failure class this council found: read-only or
unmountable agent dir, dropped `settings.json`/`models.json`,
un-interpolated baseUrl, missing provider, missing key. Admits: (a)
doctor-only; (b) doctor plus the same assertion at every respawn
(preferred, pairs with the item above); (c) rely on startup exit codes —
refuted, since `--no-approve` makes the worst case silent.

### T36 `[unseen]` Synced config reaches a session only at spawn, so the panel and the transcript must record which config version a live process booted from; a sync is invisible until the next respawn, and must be an atomic versioned-directory swap rather than an in-place update.
- raised by: Coworker (Agent4)
Admits: (a) stamp the resolved config version into the session row and show
it in the panel next to 'skills loaded'; (b) additionally surface an
`APPEND_SYSTEM`-level note in the transcript when a session is respawned
under a newer config than it last ran. Admits rejecting: hot-reloading
config into a live session (pi does not support it), and 'overlay means
live sessions track the sync' (false).

## Folded ids — name the survivor, not the folded one (2)

- T24 → T15
- T21 → T19

## Human input

- H1 (withdrawn) Metic (Agent1): Does v1 need fork/branch navigation ('edit that message') in the UI, or is deferring it to post-v1 acceptable? The brief lists the session tree among pi's values, but the delivery spec never requires the UI to expose it; I have deferred it and noted the RPC commands (fork/get_fork_messages/switch_session) make it additive later.
  - retracted by Metic (Agent1) in round 2
  - answered instead of asking: Senior (Agent2): In v1, but scoped: get_tree + get_fork_messages + fork from a user message, all present in 1.5.2 (docs/rpc-commands.md). The brief §5 names branching/edit-that-message as a headline reason to use Pi, so deferring it is a scope decision the code can already support cheaply.
  - answered instead of asking: Pro (Agent3): Ship it in v1 as the last UI slice. §5 lists 'session tree with branching/fork/navigate (the "edit that message" UX, for free)' among the reasons Pi is used at all, and the RPC surface makes it additive rather than structural: `get_tree` for the read-only view, `get_fork_messages` + `fork` for edit-and-branch, `switch_session` for navigation. Build it after meters/abort/upload so it cannot delay the phone-critical path, but do not cut it.
  - assumed in its place: whatever the council did with it — never settled
- H2 (never asked) Senior (Agent2), Coworker (Agent4): Who runs the host-side sync (repo → VM) and how: a documented operator script shipped in this repo, or wired into some existing host CI? I have assumed a script in this repo that the operator runs on the host; if the host has an existing pipeline, the sync stage plugs into it instead.
  - raised, but this run had no human to answer it
- H3 (never asked) Senior (Agent2), Pro (Agent3), Coworker (Agent4), Metic (Agent1): How do artifacts leave the VM? The brief defines uploads in and text out, but the pokemon-card scenario ends with 'a report with the values of each card' plus generated files, and there is no download path in §7/§8 (see H3).
  - asked as well by Senior (Agent2): How does a user get generated files out of the VM — chat text only, or an artifact download endpoint?
  - raised, but this run had no human to answer it
- H4 (never asked) Senior (Agent2), Pro (Agent3): Is this repo the single implementation repo, or does the service code belong in a separate agent repo with this repo holding only the compose/traefik/.env contract (§4 splits them)? See H1.
  - asked as well by Senior (Agent2): Single implementation repo (service + UI + agent-config + compose contract in /dockerx), or split the service into a separate agent repo as §4's table implies?
  - asked as well by Pro (Agent3): Repo topology: this repo holds only the interface contract (§4) while the app/UI/agent-config live in a second 'agent repo' with a one-way sync, or one repo contains both with the contract paths kept exactly as named?
  - answered instead of asking: Metic (Agent1): Single implementation repo: /dockerx. The workspace holds exactly one repo (N10: no commits, no remote), the council is planning one implementation in it, and §4's 'this repo' — where AGENT-BRIEF.md and the contract paths (compose/, traefik dynamic, .env) live — is this repo. If the owner later maintains a separate agent repo, only the sync pipeline's target changes; no stage changes.
  - raised, but this run had no human to answer it
- H5 (never asked) Senior (Agent2), Pro (Agent3), Coworker (Agent4): Does the service run as a compose container inside the VM (needs CAP_SYS_ADMIN for overlay/slot machinery) or as a systemd service on the VM with compose kept as the route contract only? See H2.
  - asked as well by Senior (Agent2): Run the Servitor app as a compose container inside the VM, or as a systemd service directly on the VM?
  - asked as well by Pro (Agent3): Runtime packaging inside the VM: a systemd service running as root on the VM OS (which is what overlayfs mounts, chown and setuid-to-slot need), versus a container described by compose/docker-compose.yml that must then be given CAP_SYS_ADMIN/SETUID/SETGID/CHOWN and its own view of /var/lib/servitor.
  - asked as well by Coworker (Agent4): Which runtime actually executes the service in the guest — systemd units generated from `compose/docker-compose.yml`, or `docker`/`podman compose` run inside the guest (which forces a privileged container for overlayfs and per-session uids)?
  - answered instead of asking: Metic (Agent1): Compose container inside the VM, privileged (CAP_SYS_ADMIN for overlay mounts, setuid/kill-by-uid, owner sweep). The brief itself places the service there: §4 says the interface contract 'lives in compose/docker-compose.yml (service)', and §8's 'toolkit baked into the image' means a container image. A systemd-service reading would demote the compose file to route-contract-only, contradicting the '(service)' annotation; the isolation mechanics are identical either way, so this only affects packaging.
  - raised, but this run had no human to answer it
- H6 (never asked) Senior (Agent2), Pro (Agent3): Which web-search backend and key does pi-web-access use in the VM (zero-config Exa endpoint vs a keyed provider), and is its package version pinned at image build?
  - asked as well by Pro (Agent3): Does the owner have a search provider key for the VM, or should the config default to the extension's keyless provider chain?
  - answered instead of asking: Metic (Agent1): Keyed Exa, pinned 0.47.0. The owner's live environment already runs keyed Exa (web-search.json with an exaApiKey in the owner's agent dir — the key is not printed here), so keyless would be a deviation from the working setup. The key is a machine cred in .env, rendered per spawn into the slot's homedir-based path (the extension's config lookup is homedir-based, not PI_CODING_AGENT_DIR — verified in dist code), agent-readable by design (accepted residual, same class as the env key, per N9). The extension tree is vendored at a baked path and loaded by explicit -e so nothing npm-installs at session start (N22).
  - raised, but this run had no human to answer it
- H7 (withdrawn) Senior (Agent2): What is the expected concurrent-session ceiling for one VM beyond 16 live sessions, i.e. how many may run simultaneously without the 30 GB ceiling becoming the limiter?
  - retracted by Senior (Agent2) in round 1
  - assumed in its place: whatever the council did with it — never settled
- H8 (withdrawn) Senior (Agent2): Single implementation repo (service + UI + agent-config + compose contract in /dockerx), or split the service into a separate agent repo as §4's table implies?
  - folded into H4: the same ask in other words
  - assumed in its place: whatever the council did with it — never settled
- H9 (withdrawn) Senior (Agent2): Run the Servitor app as a compose container inside the VM, or as a systemd service directly on the VM?
  - folded into H5: the same ask in other words
  - assumed in its place: whatever the council did with it — never settled
- H10 (withdrawn) Senior (Agent2): How does a user get generated files out of the VM — chat text only, or an artifact download endpoint?
  - folded into H3: the same ask in other words
  - assumed in its place: whatever the council did with it — never settled
- H11 (withdrawn) Pro (Agent3): Repo topology: this repo holds only the interface contract (§4) while the app/UI/agent-config live in a second 'agent repo' with a one-way sync, or one repo contains both with the contract paths kept exactly as named?
  - folded into H4: the same ask in other words
  - assumed in its place: whatever the council did with it — never settled
- H12 (withdrawn) Pro (Agent3): Runtime packaging inside the VM: a systemd service running as root on the VM OS (which is what overlayfs mounts, chown and setuid-to-slot need), versus a container described by compose/docker-compose.yml that must then be given CAP_SYS_ADMIN/SETUID/SETGID/CHOWN and its own view of /var/lib/servitor.
  - folded into H5: the same ask in other words
  - assumed in its place: whatever the council did with it — never settled
- H13 (withdrawn) Pro (Agent3): Does the owner have a search provider key for the VM, or should the config default to the extension's keyless provider chain?
  - folded into H6: the same ask in other words
  - assumed in its place: whatever the council did with it — never settled
- H14 (never asked) Pro (Agent3), Senior (Agent2): Confirm the router's image-capable model id, context window and whether it accepts parallel tool calls, before the vision path is built out.
  - asked as well by Senior (Agent2): Does the router's image-capable model actually accept the vision path at the sizes we will send (uploaded card-photo batches), and is parallel tool calling enabled for it?
  - asked as well by Pro (Agent3): Confirm the router's image-capable model id and whether it accepts parallel tool calls, before the vision + card-report path is built.
  - answered instead of asking: Metic (Agent1): Fully confirmed from the workspace (this sandbox is the owner's live pi environment). Model: Qwen3.8-27B at https://27b.pietrasik.top/v1 (openai-completions), contextWindow 250000, maxTokens 200000, input ["text","image"] per the owner's models.json. Parallel tool calls: verified round 1 (single assistant message with 2 tool_calls, finish_reason tool_calls). Vision at card sizes: verified this round — sent two 300x420 synthetic card images (name + 5-digit number each) in ONE message; the model returned both names and both numbers correctly in 2.3 s. The vision + card-report path can be built against this model; a final check with real photos is already in the stage-7 deployment gate.
  - raised, but this run had no human to answer it
- H15 (withdrawn) Pro (Agent3): Whether uploads must survive session release in any case (the brief says inbox is deleted at release, so 'keep this file' would have to go through lib/ or an explicit download).
  - retracted by Senior (Agent2) in round 2
  - answered instead of asking: Metic (Agent1): No — uploads do not survive release in any case. §8 is explicit: inbox lifetime is 'until the session is released' and release deletes inbox/<session>/ after the owner sweep. Keeping a file past release is the user's explicit action: download it (the T27/H3 read-only artifact endpoint) or save it to lib/ (the write-through action). No hidden persistence.
  - assumed in its place: whatever the council did with it — never settled
- H16 (withdrawn) Pro (Agent3): Does /dockerx hold everything (app + UI + agent-config + compose/traefik contract), or only the interface contract with the app in a separate agent repo?
  - folded into H4: the same ask in other words
  - assumed in its place: whatever the council did with it — never settled
- H17 (withdrawn) Pro (Agent3): Does the Servitor app run as a systemd service on the VM OS, or as a container inside the VM?
  - folded into H5: the same ask in other words
  - assumed in its place: whatever the council did with it — never settled
- H18 (withdrawn) Coworker (Agent4): Which runtime actually executes the service in the guest — systemd units generated from `compose/docker-compose.yml`, or `docker`/`podman compose` run inside the guest (which forces a privileged container for overlayfs and per-session uids)?
  - folded into H5: the same ask in other words
  - assumed in its place: whatever the council did with it — never settled
- H19 (withdrawn) Coworker (Agent4): What performs the one-way sync of prompts/skills/tool defs/`lib/` into the guest, and how 'save this script' reaches git without a push credential inside the VM.
  - asked as well by Coworker (Agent4): How do prompts/skills/tool defs get into the guest read-only config, and how does a kept script reach git?
  - moot with T11: the council settled that item, so nothing was assumed in its place
- H20 (withdrawn) Coworker (Agent4): Is a per-session `web-search-cache` (extension-owned, under the agent dir, bounded at 128 entries/128 MiB) acceptable inside `scratch/`, so it dies with the session, and do we ship pi-web-access via an explicit `-e` path plus `--no-extensions`?
  - retracted by Coworker (Agent4) in round 1
  - answered instead of asking: Senior (Agent2): Explicit `-e /opt/pi-ext/<ext>` plus `--no-extensions` is verified to load an extension offline (get_commands listed the extension command under PI_OFFLINE=1). Anything the extension caches under the agent dir lands in the per-session overlay upper in scratch, so it dies with the session without us configuring a cache path.
  - answered instead of asking: Pro (Agent3): The workspace settles this. pi-web-access stores its fetched-content cache in a `web-search-cache` under the Pi config directory, shared between concurrent Pi processes, and offers `PI_WEB_ACCESS_CACHE_ROOT` to relocate it — so per-session cache inside scratch is the right default and dies with the session. Loading by explicit path is settled too: `-ne -e <path>` disables discovered/configured/built-in extensions while still loading the named one (docs/cli.md), and pinning an exact version avoids the npm re-check pi performs at every start for configured packages (docs/packages.md:31).
  - assumed in its place: whatever the council did with it — never settled
- H21 (withdrawn) Coworker (Agent4): Confirm `gc_idle_days` (14) against the ~200 GB fixed disk: is 14 days of 16 live sessions' scratch acceptable, or should disk pressure be the primary trigger with the timer as a backstop?
  - retracted by Coworker (Agent4) in round 1
  - answered instead of asking: Senior (Agent2): AGENT-BRIEF.md:172 fixes the default at 14 days and :178 makes disk pressure a separate trigger, so implement both with disk pressure as the effective limiter and junk size reported to the panel.
  - answered instead of asking: Pro (Agent3): Both triggers ship and both call the same release(); `gc_idle_days` is a config value defaulting to 14 per §8, and disk pressure is the trigger that will actually fire, so the design requirement is visibility, not a better default: junk size per session and total must be on the meters endpoint and in the panel before the timer value matters. Making the timer the backstop and pressure the primary is the safe reading and needs no human input.
  - assumed in its place: whatever the council did with it — never settled
- H22 (withdrawn) Coworker (Agent4): In the guest, is the service run by systemd units generated from the compose contract, or by docker/podman compose executed inside the guest?
  - folded into H5: the same ask in other words
  - assumed in its place: whatever the council did with it — never settled
- H23 (withdrawn) Coworker (Agent4): How do prompts/skills/tool defs get into the guest read-only config, and how does a kept script reach git?
  - folded into H19: the same ask in other words
  - assumed in its place: whatever the council did with it — never settled
- H24 (withdrawn) Coworker (Agent4): What should happen to in-flight runs when the app is upgraded or restarted?
  - retracted by Coworker (Agent4) in round 1
  - answered instead of asking: Senior (Agent2): Drain (stop accepting prompts, wait a bounded grace for agent_settled), then abort with a recorded cause and mark the session interrupted; session files and scratch survive so work continues on next pickup. docs/rpc.md: stdin EOF is an orderly shutdown that waits for pending work.
  - answered instead of asking: Pro (Agent3): In-flight runs cannot survive an app restart: RPC is JSONL over stdio (docs/rpc.md), so when the control plane dies the channel dies with it and nothing can re-attach to the child's pipes. Decide it as a drain instead of a durability feature: stop the GC timer, refuse new prompts, wait for `agent_settled` (or abort on operator command), restart, and let sessions resume from files on pickup. Use a per-session systemd scope (--uid=slotN) so a kill is complete over descendants and release can verify the scope drained before sweeping.
  - assumed in its place: whatever the council did with it — never settled
- H25 (withdrawn) Senior (Agent2): Does the router's image-capable model actually accept the vision path at the sizes we will send (uploaded card-photo batches), and is parallel tool calling enabled for it?
  - folded into H14: the same ask in other words
  - assumed in its place: whatever the council did with it — never settled
- H26 (withdrawn) Senior (Agent2): Should the app ship a way to get generated files out of the VM (the report and any artifacts), or is chat text plus explicit save-to-lib the only egress in v1?
  - folded into H3: the same ask in other words
  - assumed in its place: whatever the council did with it — never settled
- H27 (withdrawn) Pro (Agent3): Confirm the router's image-capable model id and whether it accepts parallel tool calls, before the vision + card-report path is built.
  - folded into H14: the same ask in other words
  - assumed in its place: whatever the council did with it — never settled
- H28 (withdrawn) Coworker (Agent4): How does the one-way sync into the VM run, and how does a kept script reach git without a push credential the agent can read?
  - folded into H19: the same ask in other words
  - assumed in its place: whatever the council did with it — never settled

## Notes for the human (34)

Notes about an item print with it — 10 filed.

- N1 (retracted) Metic (Agent1): Every §10 verified fact was re-checked against the installed pi 1.5.2 (all docs cited, plus package.json license/version) and none is wrong; nothing flagged. A live smoke test also passed: pi --mode rpc --no-session answers a correlated get_state and shuts down cleanly on stdin EOF. One operational nuance: the first-ever run in a fresh environment can delay startup (first-run initialization), so the harness's spawn timeout should absorb it.
  - shown to you in round 0
  - **retracted in round 2**, after you had been shown it: The 'first-run initialization delay' claim is unsupported: a fresh-HOME pi --mode rpc answered get_state in 0.1 s in my test this round. The 60 s hang I originally observed was my own test blocking on a readline that would never return (buffered output lost to the timeout), not pi being slow. The spawn-timeout-should-absorb-slow-startup advice is fine as generic robustness, but the stated mechanism was wrong.
- N2 (standing) Metic (Agent1): One precision for peers: PI_CODING_AGENT_SESSION_DIR is overridden by the --session-dir CLI flag (docs/environment-variables.md). The brief's 'split via the two env vars' is correct, but the harness must use exactly one mechanism for the session dir and keep it identical across spawn and respawn — passing both is a silent footgun.
  - shown to you in round 0
- N3 (standing) Metic (Agent1): The project repo is an empty git tree (no commits); this plan is the build order. The sandbox's own pi happens to be configured against an OpenAI-compatible 'omnissiah' router with api openai-completions, which is consistent with the brief's machine-creds model and confirms the models.json + env-interpolation mechanism is the intended one.
  - shown to you in round 0
- N4 (retracted) Senior (Agent2): Verified in this sandbox against installed pi 1.5.2, all with no model calls where noted: a read-only PI_CODING_AGENT_DIR does not stop Pi from starting, but Pi logs 'Invalid settings file … EROFS … settings.json.lock' and silently ignores settings.json (SYSTEM.md, prompts and skills still load) — so the synced config must be an overlay lower with a per-session writable upper, not a direct read-only PI_CODING_AGENT_DIR.
  - shown to you in round 0
  - **retracted in round 1**, after you had been shown it: Said a read-only PI_CODING_AGENT_DIR 'does not stop Pi from starting'. Reproduced today that this is true only when --session-dir is passed: without it pi crashes on `mkdir <agent-dir>/sessions/--<cwd>--` with ENOENT and exits 1 before answering any command. Corrected statement is in my notes.
- N5 (standing) Senior (Agent2): Verified: Pi writes `auth.json` and `models-store.json` into the agent dir at every startup, and loads skills/prompts from a read-only lower dir (`skill:demo-skill` appeared in `get_commands`), so skills need a `description` frontmatter key to show up.
  - shown to you in round 0
- N6 (standing) Senior (Agent2): Verified: resume works with `--session <path>` (`get_messages` returned system/user/assistant, `get_entries` returned the entry tree and leafId) and a session with zero messages is never persisted (empty session dir after shutdown), so the app's own DB must own session existence and must record `get_state.sessionFile`.
  - shown to you in round 0
- N7 (standing) Senior (Agent2): Verified: opening a stored session whose recorded working directory no longer exists exits 1 with 'Stored session working directory does not exist' and emits no response — the app must mount before respawn and must treat that as a fatal spawn error. overlayfs mounts (lower RO + upper/work on the same fs) succeed on this kernel 7.3.3-1-cachyos.
  - shown to you in round 0
- N8 (standing) Senior (Agent2): Verified event order for one real prompt (single call against the owner's router, provider omnissiah-27b): agent_start, turn_start, message_start, message_end (user echo), message_start with thinking_start/10×thinking_delta/text_delta/thinking_end/text_end, message_end, turn_end, agent_end, agent_settled — matching docs/json.md; thinking was streamed even with reasoning configured off, so the thinking filter has to run on every run, not only for reasoning models.
  - shown to you in round 0
- N9 (standing) Senior (Agent2): pi-web-access (npm, MIT, v0.47.0 published 2026-11-05) is the brief's intended search route; it stores provider keys in `<agent-dir>/web-search.json`, which the agent uid can read — same accepted residual risk as OMNISSIAH_API_KEY in env, but worth stating in the security checklist rather than pretending it is protected.
  - shown to you in round 0
- N10 (standing) Pro (Agent3): /dockerx currently contains only AGENT-BRIEF.md: `git log` reports no commits and `git remote -v` is empty, so every stage is greenfield and the plan cannot reuse existing paths.
  - shown to you in round 0
- N11 (standing) Pro (Agent3): pi's agent directory cannot be the read-only synced config: dist/core/auth-storage.js creates and lock-files `<agent-dir>/auth.json` even when only reading credentials, so the per-session agent dir must be a writable copy (or resources passed by --system-prompt/--skill/--extension from the read-only mount).
  - shown to you in round 1
- N12 (standing) Pro (Agent3): The API key can stay env-only with a on-disk models.json: docs/models-configuration.md documents `apiKey` as 'literal key, "$NAME" environment interpolation, or "!command"', so the config file holds only `$OMNISSIAH_API_KEY`; do not use `--api-key`, which is visible in `ps` to every slot.
  - shown to you in round 0
- N13 (standing) Pro (Agent3): pi's built-in bash tool has no default timeout (dist/core/tools/bash.js: 'optional, no default timeout'), so the brief's unbounded runs need no workaround; model-facing output is truncated to a line/byte budget with the full output in a temp file, which is another reason the release sweep by owner must cover /tmp.
  - shown to you in round 0
- N14 (standing) Pro (Agent3): In RPC mode extension dialogs (select/confirm/input/editor) block the child until the client answers and `ctx.hasUI` is true (docs/rpc-extension-ui.md); any extension shipped must have its dialogs answered by the server or disabled, or a run can hang indefinitely with no client attached.
  - shown to you in round 0
- N16 (standing) Coworker (Agent4): Four §10-adjacent facts need correcting against pi 1.5.2, each reproduced here: (1) `PI_CODING_AGENT_DIR` cannot be the §8 read-only config mount — pi mkdirs `<agent-dir>/sessions/--<cwd>--` at startup and dies on a read-only dir, and writes `auth.json`/`models-store.json` there in an ordinary run; load synced config via explicit `--system-prompt/--append-system-prompt/--skill/--prompt-template/-e` paths instead (all verified working). (2) `models.json` is read only from the agent dir and its `baseUrl` is NOT env-interpolated (`$VAR` stays literal → `Invalid URL`), while `apiKey: "$OMNISSIAH_API_KEY"` does resolve; so the app must render `models.json` per spawn. (3) `--session-id` resolves sessions per cwd and silently creates an empty session when the cwd differs — respawn must use `--session <path>` with `sessionFile` captured from `get_state`. (4) `get_state` reports `model.id == "unknown"` instead of omitting `model` when no model is configured.
  - shown to you in round 0
- N17 (retracted) Coworker (Agent4): Isolation mechanics cannot be validated in this sandbox: `setuid()`/`Popen(user=)` return EPERM and `mount -t overlay` is refused, so the uid-drop, overlay, and owner-sweep gates must run on a guest or privileged runner and Stage 3/8 cannot be signed off from CI alone.
  - shown to you in round 0
  - **retracted in round 2**, after you had been shown it: Wrong about overlayfs: `mount -t overlay` with lower/upper/work on one filesystem succeeds in this sandbox (verified rc 0, lower visible through the merge, new writes landing in upper), so only the setuid half of the note is true. Agent3's N25 supersedes it.
- N18 (standing) Coworker (Agent4): The obvious Python translation of the brief's Node-`readline` warning is worse: text-mode universal-newline `readline()` splits records on an embedded CRLF *and* rewrites it, corrupting the JSON string. Read bytes, split on `\n` only, and keep `str.splitlines()` out of the protocol path.
  - shown to you in round 0
- N19 (retracted) Metic (Agent1): I take Agent4's direction: contract + skeleton first, then the RPC driver with its protocol-contract tests, then registry, then guest isolation, then delivery. My phase 0 order (isolation before RPC) is revised: N17 shows the isolation gates cannot be validated in this sandbox (setuid EPERM, overlay refused), so starting there stalls verifiable progress, while the RPC contract is fully testable now and N16/N18 show its details are the expensive knowledge. The control-plane-first order (Agent3) would build the session lifecycle on driver semantics not yet proven by tests.
  - shown to you in round 1
  - **retracted in round 2**, after you had been shown it: Its claim that 'the isolation gates cannot be validated in this sandbox' is overbroad: N25/N23 show overlay mounts, EROFS assertions, and the sweep walk DO run here (mount -t overlay succeeds on kernel 7.3.3-1-cachyos; unshare -Urm works as a CI proxy). Only the uid-drop half is guest-only (N17 stands). My stage order (RPC first) is unaffected, but the stated reason is corrected by T23's gate split.
- N20 (standing) Senior (Agent2): My N4 was incomplete and I retract it: a read-only PI_CODING_AGENT_DIR does NOT merely warn — without an explicit `--session-dir`, pi crashes at startup (`ENOENT: mkdir '/mnt/ro/sessions/--mnt-work--'`, exit 1, no response). With `--session-dir` it starts, and then still silently drops settings.json (EROFS on settings.json.lock) while SYSTEM.md, prompts and skills load normally from the read-only tree. Both readings in the docket describe the same invariant: the agent dir must be writable, hence the per-session overlay upper.
  - shown to you in round 1
- N21 (standing) Senior (Agent2): Verified: `models.json` does not interpolate `baseUrl` (`$PROBE_BASE_URL` came back verbatim from `get_available_models`) while `apiKey: "$NAME"` resolves. Put the router URL in the synced config as a literal; the .env-var-for-baseUrl idea in more than one plan silently produces `Invalid URL`.
  - shown to you in round 1
- N22 (standing) Senior (Agent2): Verified: an extension at a baked local path loads with `pi --no-extensions -e <path>` under PI_OFFLINE=1 with no registry contact, which is the deterministic way to ship pi-web-access without per-session npm installs. PI_OFFLINE=1 is proven for Pi's own startup noise only — prove search still works with it set before shipping it.
  - shown to you in round 1
- N23 (standing) Senior (Agent2): CI nuance for the isolation gates: `setpriv --reuid` to an unmapped uid returns EINVAL and `unshare --map-users` rejects a multi-uid map on this util-linux, so uid-drop and sweep-by-owner can only be gated on the guest (N17 stands). `mount -t overlay` inside `unshare -Urm` does work (rc=0 on kernel 7.3.3-1-cachyos), so CI can still smoke the overlay/whiteout/upper-path logic as a proxy — the guest run remains the authoritative gate and a skip must block merge.
  - shown to you in round 1
- N24 (standing) Pro (Agent3): N4 as written is wrong on the point that matters: with a truly read-only PI_CODING_AGENT_DIR (tmpfs remounted ro), `pi --mode rpc` never answers a command — it dies with an uncaught `EROFS: read-only file system, mkdir '<agent-dir>/trust.json.lock'`, exit 1. The safe rule for every stage is the same either way but the wording changes: the agent dir must be writable and per-session, and the synced config reaches pi only through --system-prompt/--append-system-prompt/--skill/--prompt-template/-e.
  - shown to you in round 1
- N25 (standing) Pro (Agent3): N17 is right about setuid and wrong about overlayfs in this sandbox: `mount -t overlay` with lower/upper/work on one fs succeeded here (lower visible through the merge, new files landing in upper), so mount/EROFS/sweep-walk gates are runnable in CI here, and only the uid-drop half genuinely needs a guest or privileged runner.
  - shown to you in round 1
- N26 (standing) Pro (Agent3): `baseUrl` in models.json is not env-interpolated (get_state returned the literal "$PROBE_BASE_URL") while `apiKey: "$VAR"` resolves (auth check reports ready and the resolved key equals the env value) — so the router address must be literal in the on-disk config and the env-interpolation trick is only for the key. That keeps the key off disk without any per-spawn rendering.
  - shown to you in round 1
- N32 (standing) Metic (Agent1): Verification log for this round, all in-sandbox: (1) fresh-HOME pi --mode rpc answers get_state in 0.1 s — no first-run delay exists, my N1 was a misdiagnosis of my own test bug (retracted); (2) -nc/--no-context-files and -a/--approve/-na/--no-approve confirmed at docs/cli.md:216/239/241/302; (3) live vision probe to the owner's router: two synthetic card images in one message, both read back correctly (name + 5-digit number) in 2.3 s — the card-report use case is buildable against Qwen3.8-27B.
  - shown to you in round 2
- N36 (standing) Senior (Agent2): Sandbox capability boundary, measured for the T23 split: overlay mount, read-only-bind EROFS, mount-order/teardown, and the whole pi config/credential/context-file gate set run in CI here (root, `CapEff=000001ffffffffff`); nothing that needs a second uid does — `setuid`, `setgid`, `chown` to an unmapped uid all return EINVAL because `/proc/self/uid_map` is `0 1000 1`. So `vm-smoke` = setuid spawn, chown handoff, cross-uid EPERM, real sweep-by-owner; everything else has a CI home. This also means N17's two errnos and its overlay half are wrong (EINVAL, not EPERM; overlay is not refused), which matters because it would otherwise have pushed the entire isolation suite off CI and out of every merge gate.
  - shown to you in round 2
- N37 (standing) Senior (Agent2): Standing assumptions I am writing into the plan because no human answered them, each with the guess it stands for: one repo holds service+UI+agent-config+contract (H4); the service runs as a compose container in the guest with CAP_SYS_ADMIN/SETUID/SETGID/CHOWN and a bind-mounted ext4 state volume (H5) — which the new nested-overlay item now constrains; the search extension ships pinned with its keyless chain and the key override documented (H6); the router's vision model id/window/parallel-tool-calling is read at runtime via `get_available_models` and never hardcoded (H14); the sync is an operator-run script from this repo (H2/H19); artifact download exists as a read-only capped scratch endpoint until told otherwise (H3/T27).
  - shown to you in round 2
- N39 (standing) Pro (Agent3): The read-only-agent-dir discrepancy is not worth more cycles, but the record should be straight: I re-ran it with `mount -o remount,ro,bind` on an agent dir already containing `trust.json`, `auth.json` and `models-store.json`, with the session dir redirected, and `pi --mode rpc` still exited 1 at startup with no response (`EROFS … mkdir '<agent-dir>/trust.json.lock'`). So N20/N27's 'with the redirect it starts and only degrades' did not reproduce for me. It changes no design choice — both crash and degrade fail the negative gate in the config-layout stage — so the stage wording should just say 'a read-only agent dir must fail the gate' without claiming which of the two you get.
  - shown to you in round 2
- N40 (standing) Pro (Agent3): Root is not a defence, it is the hazard: reading through a file symlink and a directory symlink into a `d---------` directory both succeeded for the app uid, while `O_NOFOLLOW` refused. Every app-side read of an agent-owned tree (transcript of a dead session, lazy tool body, artifact list/download, save-to-lib source) has to be confined per open, and that is a stage-7 gate assertion, not a code-review note.
  - shown to you in round 2
- N41 (standing) Coworker (Agent4): N17: retracting the overlay half — it was wrong, and Agent3's N25 is right. Restated: in this sandbox `mount -t overlay` (lower + upper/work on one filesystem) and read-only bind mounts both succeed, so overlay, EROFS, teardown-order and sweep-walk gates are runnable in CI here; only `setuid`/`setresuid` (EPERM for both `setpriv` and `Popen(user=)`) needs a guest, and that is where uid-drop and sweep-by-owner must be certified.
  - shown to you in round 2
- N42 (standing) Coworker (Agent4): The docket's crash-vs-degrade disagreement is settled and the switch is a flag: read-only agent dir + a `.pi/` directory in cwd with **default** project trust dies before answering anything (`EROFS … mkdir '<agent-dir>/trust.json.lock'`, exit 1); the same setup with `--no-approve` starts, warns on stderr about `settings.json.lock`, and every prompt fails with zero events. So both Agent2's N20 and Agent3's N24 are correct, and the dangerous variant is the quiet one. Stage wording should say: the agent dir must be writable, and gates must assert a router-side request rather than process liveness.
  - shown to you in round 2
- N44 (standing) Coworker (Agent4): One-line answer for the human if the report needs it: the router's model in this environment is `Qwen3.8-27B`, `input: [text,image]`, `contextWindow: 250000`, `supportsParallelToolCalls: true`, so the card-photo path needs no GPU in the guest (pi's `read` tool attaches and auto-resizes images and warns when a model lacks vision). Assert the id and window at deploy time rather than hardcoding them, because this is the sandbox's config, not necessarily production's.
  - shown to you in round 2
- N45 (standing) Coworker (Agent4): I retire my round-1 label `merged: rpc-spine-with-parallel-isolation-spike`. T23 removed its main premise (the mount/sweep half is CI-testable here, so isolation does not need guest lead time), so my direction is Agent3's `protocol-first-permanent-contract`; my remaining contributions are filed as items (agent-dir config integrity, the doctor resolvability gate, config-version stamping, artifact egress scope) rather than as a rival plan.
  - shown to you in round 2

## Planner Q&A

- Q1 Senior (Agent2) → Coworker (Agent4): In your guest smoke gate, is 'an uploaded AGENTS.md is not loaded' proved by content-addressed naming alone, or do you also spawn with `--no-context-files`? Context files load regardless of project trust and from the cwd, so once the agent copies an upload into its workdir the file is reachable by name — naming only protects the inbox.
  - answer: Both, and now with ancestor evidence too. Content-addressed naming only protects the *inbox* filename; discovery is a separate mechanism, so the spawn carries `--no-context-files` and the vm-smoke gate plants sentinels in three places — the uploads dir, the workdir, and one ancestor of the workdir — asserting none reaches the leading system message. The cwd case and the ancestor case are both reproduced here (`AGENTS.md` reached `messages[0].role=system`; `-nc` removed it in both), and the same defence also covers an upload the agent copies into its own workdir, which is exactly the case you were worried about.
- Q2 Senior (Agent2) → Metic (Agent1): Your API stage reads the transcript 'via get_messages' and says opening a session respawns a dead subprocess. Does the *list* view also wake all dead sessions, or does it read the registry only? A list that respawns 15 sessions to render is the failure mode I want designed out before stage 6.
  - answer: The list view reads the registry only — O(1), zero pi processes, always. No respawn-on-view at all: a live process exists only for user actions (prompt/steer/follow_up/abort of an active run), which is the T18 position I stanced this round (probe for reads, one process per session file, never against a live one). So a phone poll of 15 dead sessions wakes nothing, and the §6 subprocess-count meter counts actual active conversations, not viewers.
- Q3 Senior (Agent2) → Pro (Agent3): Your stage 7 gate asserts 'no npm or model-catalog request at process start (PI_OFFLINE=1 or exact version pins)'. Which mechanism do you commit to — PI_OFFLINE=1, or exact pins with the extension loaded by explicit -e path? PI_OFFLINE may also disable whatever the search extension borrows from Pi's own network stack, so the two are not interchangeable.
  - answer: Exact pins + extension loaded by explicit `-e` path, and that is the committed mechanism. I am dropping PI_OFFLINE from that gate: it is a blunt instrument that may disable the very network path the search extension needs (your own N22 caveat), and the pin already prevents the per-start npm re-check (docs/packages.md:31 — an exact pin is not re-checked; a configured-but-unpinned source is). If PI_OFFLINE is used at all it is separate hardening with its own gate proving search still works. The gate asserts a request actually reaches the mock router, so a config-dead session cannot pass by returning `disposition: started`.
- Q4 Pro (Agent3) → Senior (Agent2): N4 says a read-only PI_CODING_AGENT_DIR lets Pi start (only 'Invalid settings file … settings.json.lock' logged). My repro on the same 1.5.2 with a genuinely read-only tmpfs agent dir died with an uncaught `EROFS … mkdir '<agent-dir>/trust.json.lock'`, exit 1, before any response was written. What exactly did your read-only setup look like (overlay lower+upper? existing trust.json? non-root uid?) — because the plan's stage-2/3 wording depends on whether this is a crash or a silent-degrade?
  - answer: My setup was a real read-only mount (`mount --bind` then `mount -o remount,bind,ro`, not chmod), agent dir empty of trust.json, running as the container's uid 0, always with `--session-dir` redirected — and my run never sent a `prompt`, which is why I saw 'starts and warns' rather than your crash. Two refinements to merge: (1) with the session dir redirected, 1.5.2 in my configuration does not touch `trust.json.lock` at all — stderr held only `Invalid settings file … settings.json.lock` — so your trust.json crash is triggered by something my tree lacked (a project-local `.pi`/trust-gated resource in cwd is the likely difference), and both crashes share one cause: any lock/`mkdir` in the agent dir. (2) The important half is what my later run found: after the warning, `prompt` fails `Credential store read failed for probe: EROFS … open '<agent-dir>/auth.json'` with zero events while `get_state` still returns success. So the wording is neither 'crash' nor 'silent degrade' — it is 'healthy-looking, permanently unable to run'. Writable per-session agent dir is the rule either way, so stage 2/3 wording should say: agent dir writable, session dir redirected, and a negative gate asserting the read-only layout fails.
- Q5 Coworker (Agent4) → Senior (Agent2): Your N4 says a read-only PI_CODING_AGENT_DIR makes Pi ignore settings.json. Did `models.json` survive in your run — did any inference request actually reach the router? Under `mount -o ro,bind` I get the `settings.json.lock` EROFS warning and then `prompt` → `success:false "No API Key found for the selected model"` with zero events, so your Stage-1 'config volume' as agent dir would never reach the router. Did you test it with the session dir redirected, and did you see a request?
  - answer: You are right and my Stage-1 config-volume-as-agent-dir framing was wrong; I retract it. Re-run with your layout plus a local mock router: `models.json` is *read* (the model is listed and selected), but `prompt` → `{"success":false,"error":"Credential store read failed for probe: EROFS … open '<agent-dir>/auth.json'"}` with no `agent_start` and no `agent_settled`, and **no request ever reached the mock** — so a gate that asserts only 'prompt response received' would pass while inference is dead, exactly as you said. The overlay layout passes the same harness: `disposition:started` → `agent_settled` and the mock logged `Authorization: Bearer mock-secret-key`, with `auth.json`/`models-store.json` landing in the scratch upper. That is why I have filed the negative gate as a permanent assertion. On my Q1 to you: forget the naming question — `-nc` is load-bearing, proven by a sentinel AGENTS.md in cwd *and* in an ancestor directory both reaching the system message without it.
- Q6 Coworker (Agent4) → Metic (Agent1): Your Stage 4 spawns with `env PI_CODING_AGENT_DIR=config volume` and your config volume is built read-only per §8. Does your Stage-4 gate prove a request reaches the stub router (mine proves it does not)? If your gate only asserts 'disposition started' or a prompt RPC response, it passes while inference is dead — I would add a router-side assertion to your gate.
  - answer: You're right and I concede it: my stage gate as written (RPC-level success + transcript) could pass while inference is dead, and T25's no-events failure mode makes router-side assertion mandatory. The revised gate asserts the mock router RECEIVED the completion request with Bearer = the env key value and the correct model, and the transcript contains the assistant reply — plus I'm adopting your negative gate verbatim: PI_CODING_AGENT_DIR pointed at a read-only mount must FAIL (no request reaches the router), so nobody rediscovers it in production. Under the now-settled T15 overlay the failure mode also cannot structurally recur (the agent dir is the writable overlay; the lower's models.json has the literal URL + $VAR key, exactly the owner's live shape).
- Q7 Coworker (Agent4) → Pro (Agent3): Your `POST /sessions/{id}/save-script` copies a scratch file into `lib/` as the app uid. Who chooses the file and who signs off — the human in the UI, or the agent asking? §2's threat model is prompt injection, so an agent-reachable write to the one durable artifact is the persistence path; and what bounds the copy to `lib/` (name collision, path shape, size) given the app is the one doing the writing?
  - answer: The human chooses, always: 'save this script' is a UI action the user takes on a named file in the session's artifact list — there is no agent-reachable path to `lib/` (no tool, no RPC surface, no convention where the agent writing to some magic directory triggers a copy), because you are right that an agent-reachable write to the one durable artifact is the persistence channel. Bounds: the source must resolve inside `scratch/<session>/`'s merged tree through dirfd-relative opens with O_NOFOLLOW and regular-file+size checks (my new item — as root the app would otherwise follow an agent-planted symlink anywhere, which I demonstrated); the destination name is normalized, collision-appended with a short hash, never taken verbatim from the agent's text; the copy records provenance (session id, source path, sha256, size, timestamp) in a sidecar the human reviews; and `lib/` stays read-only to the agent, with git reconciliation happening host-side so no push credential lives in the VM. The residual risk — a human tricked into clicking — is the accepted human-as-reviewer risk, not an automated one.
- Q8 Metic (Agent1) → Senior (Agent2): With the overlay agent dir settled (T15), two details to close: (1) policy when the agent overwrites settings.json or SYSTEM.md in the upper — I propose visibility-only (agent-upper file list in panel + doctor, release logs it), no auto-restore, since auto-restore would race a live process; do you accept that? (2) Confirm the lower's settings.json is where defaultTools +codemode lives (so codemode works under the overlay with no flag acrobatics), given -ne would kill the built-in and we are not passing it.
- Q9 Senior (Agent2) → Metic (Agent1): Still unanswered and blocking my sign-off on the registry stage: does the session *list* view wake dead sessions to read their transcripts, or does it render from the registry alone? If it wakes them, 15 pickup-spawns fire per page load and every one has to mount first.
- Q10 Pro (Agent3) → Coworker (Agent4): T26 asserts the flag set in CI — do we also assert `--no-extensions` together with the explicit `-e` path in that same constant, so an extension the agent writes into its own tree can never load, and that `-e` path is inside the read-only overlay lower rather than a writable scratch location?
- Q11 Pro (Agent3) → Senior (Agent2): Your T15 overlay for the agent dir and your probe rule (T18) meet at ownership: if a probe runs as the slot uid, its writes land in the agent-dir upper like any run — do you agree the probe must reuse the exact run launch path (uid, mounts, flags) with only a shorter lifetime, so no probe ever runs as the app uid?
- Q12 Coworker (Agent4) → Senior (Agent2): T15's rationale says the overlay keeps the agent dir 'always current with the sync'. I measured that it does not: adding `skills/beta` and widening `defaultTools` in the settings file mid-session changed neither `get_commands` nor the declared tools of the live process. Do you accept the corrected rationale (no copy step, writes die with the session) and will your `servitor/iso` stage take two additions: an atomic versioned-dir swap for the sync so no live lowerdir is mutated, and a read-only bind of `models.json` inside the merged agent dir?
- Q13 Coworker (Agent4) → Pro (Agent3): Q7 is still unanswered and it is the persistence path: who names the file for `POST /sessions/{id}/save-script` — the human in the UI or the agent in the transcript — and what bounds the write to `lib/` (name, path shape, size, collision)? §2's threat model is prompt injection, so an agent-reachable write to the one durable artifact is how injected instructions become permanent.
- Q14 Coworker (Agent4) → Metic (Agent1): Q6, sharpened: will your Stage-4 gate assert on the **router side** that a request arrived, plus `get_available_models` listing the provider? A gate that only checks 'disposition started' or an RPC response passes while inference is dead — I reproduced exactly that: `prompt` answers `success:false 'No API key found'` with zero events and the process stays alive, and with default project trust the same misconfiguration instead exits 1 on `trust.json.lock`.

## Alternatives on the table

- Metic (Agent1): Build Servitor as one privileged compose service in the VM (Python app + React static UI + per-session pi RPC subprocesses setuid'd to 16 static slot uids, overlayfs workdirs, owner-swept release), with the edge as the only gate — in 8 stages where each gate is runnable without the real router until final deployment.
  - Repo scaffold exists: compose/docker-compose.yml defining the single servitor service (env from .env, exactly one published port, volumes: config ro, state rw, toolkit ro, /home bind), compose/traefik/dynamic/servitor.yml (router + service URL), .env.example (OMNISSIAH_API_KEY, router URL, port, bind address, gc_idle_days, coalesce default), firewall script (nftables: admit the API port from the edge subnet only), and deploy/ docs for VM provisioning and the one-way sync pipeline.
  - A servitor image containing: the Python app runtime with python-magic; pi pinned at 1.5.2 resolvable from every user's PATH (absolute path used by the harness); slot users slot1..slot16 with fixed uids and unique 0700 homes under /home/slotN; and a toolkit layer at /opt/toolkit (python, uv, Pillow, numpy, ffmpeg, ImageMagick). A config-volume builder produces: SYSTEM.md, skills/, settings.json (defaultTools with +codemode), models.json declaring the OpenAI-compatible router (api openai-completions, baseUrl, key by env interpolation of ${OMNISSIAH_API_KEY}), and extensions/ with pinned pi-web-access. The toolkit layer is materialized as the read-only overlay lowerdir (docker export to a host path) at deploy time.
  - The isolation core works without any LLM: state volume layout (inbox/<s>/, scratch/<s>/{upper,work}/, sessions/<s>/, homes under /home/slotN, lib/); a 16-slot pool (allocate/free, stable pinning, slot-pressure policy); per-session overlay mount (lower=toolkit ro, upper/work=scratch/<s>, mounted at a stable per-session workdir path, with inbox/<s> bind-mounted into the workdir); setuid spawn helper that asserts the child's uid; the single release() code path (orderly kill → uid-wide kill of strays → unmount → find / -user slotN sweep and delete → delete inbox/scratch/sessions → free slot); and a persistent session registry (session id, slot, stable flag, timestamps, run state) with boot reconciliation.
  - The RPC harness drives a real pi subprocess per session: spawn with cwd=overlay workdir, env PI_CODING_AGENT_DIR=config volume, PI_CODING_AGENT_SESSION_DIR=sessions/<s>, OMNISSIAH_API_KEY env-only; first spawn uses --session-id <uuid>, respawn uses --session <id> with identical cwd and session dir; LF-only binary reader (strip trailing CR); id correlation; event reader attached before any prompt; prompt with disposition handling (handled → do not wait; started|queued → wait for agent_settled, never agent_end); steer/follow_up during an active run; explicit abort; 1 h idle kill that fires only when settled and is orderly (close stdin → SIGTERM grace → SIGKILL); respawn on pickup; stderr logged and never parsed; extension UI records logged and auto-answered (decline/default); registry updated on run start/settle and tool start/end for the §6 meters.
  - The full HTTP/SSE API: session create/list/get/close (close ⇒ release), stable toggle, and wait-queue state; meters (run active, elapsed, current tool, subprocess count, scratch usage, last activity); transcript via get_messages (opening a session is a pickup and respawns a dead subprocess); uploads (multipart, streamed to a temp file, sha256 content-addressed name, MIME by magic, client filename discarded, chowned to the slot uid, atomic rename into inbox/); prompt endpoint (plain when idle, steer|follow_up while a run is active); explicit POST abort; SSE events with per-subscriber bounded queues that drop and coalesce on overflow and never block; ~1 s configurable coalesced flushes; server-side show_thinking none|final|all per subscriber (none emits a compact 'thinking… N s' line; final emits only the authoritative thinking_end); tool progress collapsed to status lines with result bodies omitted; lazy tool-result fetch by tool-call id from the session JSONL; health endpoint.
  - A React SPA (Vite build, served as static files by the API — still one port): session list with meters, stable pins, slot usage, and wait state; chat view rendering authoritative messages, thinking per the client's mode, tool status lines, and lazy tool-result expansion; composer with send (steer by default while a run is active, explicit follow-up), abort button, file upload, and show_thinking + coalesce settings; close-with-confirm that aborts first when a run is active; stable toggle.
  - Deployed on the real VM: provisioned per §9 (24 vCPU, no quota, low scheduling weight; virtio-balloon + free-page-reporting with a 30 GB ceiling; guest systemd slice MemoryHigh=25G/MemoryMax=30G; zram; ~200 GB fixed disk; no GPU; firewall admitting the API port from the edge subnet only); the sync pipeline runs (repo → VM: app/config rebuild, lib/ push, toolkit lowerdir); compose is up; the edge route is live (portcullis file provider picks up the dynamic file — one host-side change, documented in the repo); the GC timer (idle > gc_idle_days, non-stable ⇒ release) and disk-pressure handler (near-full state fs ⇒ release oldest non-stable until headroom) run through the single release() path; lib/ is never GC'd.
  - Failure modes are handled and proven: router outage mid-run surfaces pi's retry/compaction events in the UI instead of hanging, and a prompt-time router failure shows as a run error, not a harness crash; the app-crash/restart drill passes (registry reconciliation, sessions resume on pickup, no double slot allocation); a slot that cannot be swept is quarantined, visible in the panel, retryable, and never reused; the pi version-bump procedure (pin change ⇒ re-run the stage 4 gate) is documented; an ops runbook covers provisioning, sync, release, GC, key rotation (env change + restart; the key is in no backup), and lib/ backup via git.
  - rejected: In-process Pi SDK (createAgentSession) instead of per-session RPC subprocesses: the brief mandates a killable, uid-assignable subprocess per conversation; an SDK session would run inside the app process under the app's uid, breaking the read-only-mount and uid-gating invariants.
  - rejected: Per-session VMs or per-session containers: explicitly rejected by the brief ('not per-session VMs'); the isolation ladder is per-session directory + slot uid + overlayfs.
  - rejected: A Node.js host for the app/harness: the brief's own text names the app server as Python; RPC mode is language-agnostic by design, so one runtime (Python) for harness + API keeps the ops surface minimal, with TypeScript confined to the UI.
  - rejected: WebSockets instead of SSE: the channel is one-way push with reconnect; EventSource's auto-reconnect plus the fetch-then-tail reattach design fits exactly, and there is no bidirectional need on the event channel.
  - rejected: An in-memory replay ring buffer for reconnects: contradicts 'reattach is a fetch, not a replay'; the session JSONL is the documented authoritative store and deltas are disposable by contract.
  - rejected: systemd DynamicUser= for slots: rejected in the brief — recycled uids break 'look at it tomorrow' (junk must persist until release) and a uid change triggers a recursive re-chown of the state tree.
  - rejected: Per-session $HOME via environment: rejected in the brief — expanduser/getpwuid fall back to the passwd entry, so a shared uid with per-session $HOME leaks on first forget and concurrent sessions collide in caches.
  - rejected: Reimplementing pi's loop (extracting the agent loop into the app): §5 says the value is the long tail (session tree, compaction, overflow recovery, retries, provider shims, RPC framing) — extraction is pure engineering cost with none of that value.
  - rejected: End-user authentication inside the service: an explicit non-goal; the edge (Traefik + Authelia 2FA forward-auth) is the only gate and reachability is the control.
  - rejected: Egress filtering inside the VM: deliberately absent per the brief; the VM boundary is the mitigation for the accepted exfiltration residual risk.
  - revision `Agent1 original` (round 2): 8 stages
- Senior (Agent2): Prove isolation and the RPC bridge as throwaway spikes first, then hang the API, delivery layer, UI, and GC off two abstractions (SessionManager, RpcBridge) that those spikes already exercise.
  - Repo skeleton + interface contract exist and run: `docker compose up` in the VM serves /healthz on the ingress address, reachable only through the edge route with Authelia; the image bakes pinned node+pi, the Python runtime, the toolkit (Pillow/numpy/ffmpeg/ImageMagick/uv), 16 slot uids with passwd homes on the state volume, and a pre-populated pi package dir; config paths/ports/flags have one source of truth.
  - `servitor/iso` exists as tested primitives: per-session scratch layout (inbox/scratch/lib-sessions/agent-upper), overlay mount/umount for workdir and for the agent dir, chown-to-slot handoff, spawn-as-slot-uid, sweep-by-owner, release. Proven by an integration test that runs inside the real container.
  - `servitor/pi.RpcBridge` exists: spawns a pi subprocess with the pinned flag set, writes JSONL commands, reads stdout byte-wise splitting only on LF, correlates by id, models disposition and `agent_settled`, answers extension-UI dialogs, captures stderr to a per-session log, and has an idle-kill + shutdown ladder.
  - `SessionManager` owns sessions in a SQLite state DB: slot pool, mounts, spawn/resume, per-session Pi flags and env, and a transcript probe for sessions with no live process. A session survives a killed subprocess and a killed app.
  - HTTP API serves sessions, uploads, meters, and control: POST create/list, POST prompt/steer/follow_up/abort, POST uploads, GET state, GET tool result by call id. Uploads are content-addressed, magic-MIME typed, and handed to the agent as paths only.
  - Delivery is independent of the run: a per-session broker fans out to per-subscriber bounded queues that drop and coalesce under overflow; SSE reattach is snapshot (state → transcript → live tail); thinking verbosity is filtered server-side with a configurable flush cadence; tool progress collapses to one line and result bodies are omitted.
  - React UI is usable from a phone through the edge: chat with thinking modes, upload, meters panel with abort, steer vs follow-up vs queue, session list with stable flag, and session tree/fork/'edit that message'.
  - Release is the only cleanup path, with all four triggers wired (UI close, GC timer, slot pressure, disk pressure), stable sessions pinned, and junk size visible.
  - Ops hardening and docs: systemd slice (`CPUWeight` low, `MemoryHigh=25G`/`MemoryMax=30G`), virtio-balloon + zram notes, fixed 200 GB disk, nftables rule admitting the API port from the edge subnet only, VM image build script, runbook, backup notes for `lib/` + stable sessions, and a soak test.
  - rejected: Pi SDK in-process (Node) instead of `pi --mode rpc`: it runs the harness as the app's uid, which voids the §8 privilege invariant and gives no drop-privilege or uid-per-session story; also forces a Node app server.
  - rejected: One container or micro-VM per session: 16 dockerd-managed containers (or VMs) per box plus docker.sock inside the VM is heavier, slower to hand over, and directly contrary to §8's 'explicitly not per-session VMs'.
  - rejected: systemd `DynamicUser=` per session: rejected by §8 — recycled uids break both the 'leave the junk until release' contract and the sweep, and cost a recursive re-chown.
  - rejected: bwrap/nsjail per session instead of uid + overlayfs: needs user namespaces inside the container, does not give the owner-based sweep or uid-gated signals §8 relies on, and adds a second isolation mechanism to defend.
  - rejected: WebSocket instead of SSE: 'reattach is a fetch' is exactly SSE's semantics; a socket invites the mistake of treating connection death as cancel.
  - rejected: Server-side event replay (ring buffer or Last-Event-ID resume): unnecessary once the transcript fetch plus authoritative end-blocks exist, and it would reintroduce per-token streaming cost on phones.
  - rejected: Permitting web access / MCP / uploads as Pi-side extensions that mutate the agent dir at run time: keeps sessions from being reproducible and forces registry calls at session start — bake and pin instead.
  - rejected: A capability or spend limiter (iterations, wall clock, tokens, per-run upload counts) anywhere in the harness: §6 forbids it; meters and human abort only.
  - rejected: Storing transcripts or uploads in SQLite/S3: transcripts are Pi's JSONL contract and must stay files for fork/tree/resume semantics.
  - revision `risk-first-isolation-and-rpc-contract-before-api` (round 1): 10 stages
- Pro (Agent3): Build a small privileged Python control plane (FastAPI) that owns session slots, storage areas and SSE fan-out, and treats `pi --mode rpc` as an untrusted, uid-isolated worker it can spawn, steer, abort and reap.
  - This repo states the interface contract and the deploy topology, and nothing else: compose/docker-compose.yml declares the service (image, port, env var NAMES only), compose/traefik/dynamic/ has the edge router for the VM address with Authelia forward-auth middleware, .env.example lists every secret name, README.md records the boundary decisions, and a `make check` target parses everything.
  - A buildable VM artifact exists (Containerfile or an arch-chroot/cloud-init provisioning script) with node+pi pinned, python3.12+/uv, Pillow, numpy, ffmpeg, ImageMagick, yt-dlp, ripgrep, libmagic; 16 static slot users slot01..slot16 with fixed uids and no passwords; the app uid, distinct from every slot uid; /var/lib/servitor/{inbox,scratch,lib,config}; systemd unit + GC timer; an nftables table admitting the API port from the edge subnet only; and `servitor-doctor`, a script that asserts the whole invariant set.
  - `servitor/rpc/` exists as a standalone, HTTP-free Python module: spawn/cwd/uid/env-controlled `pi --mode rpc` child, an LF-only JSONL framer, id-correlated request/response futures, an always-on event dispatcher (subscribed before any prompt), typed wrappers for prompt/steer/follow_up/abort/clear_queue/get_state/get_messages/get_entries/get_tree/get_session_stats/set_model/set_session_name, an extension-UI responder, and a `fake_pi` Python script speaking the protocol for tests.
  - `SessionManager` owns the whole session lifecycle: slot allocation from the 16-uid pool as a state machine (allocated → cleaning → free), per-session dirs (inbox, scratch with overlay upper/work, a per-session writable agent dir, a per-session session dir), overlay mount at <session>/root plus read-only binds for uploads and lib, seeding of the writable agent dir from the read-only synced config, spawn with `--session-id <app-minted-id>` (first run) or `--session <transcript path>` (resume) plus `--session-dir`, idle-kill after 1h of true idle, one `release()` used by every trigger, and GC triggers (idle > gc_idle_days, slot pressure, disk pressure) with `stable` honoured.
  - The FastAPI control plane serves sessions, uploads, meters and events on the single VM port: POST /sessions, GET /sessions, GET /sessions/{id}, POST /sessions/{id}/messages|steer|follow-up|abort|release|stable|save-script|uploads, GET /sessions/{id}/transcript|files/..., GET /sessions/{id}/events (SSE), GET /meters, GET /healthz. A producer task per session drains pi events and publishes per-subscriber bounded queues that drop and coalesce and never block; reattach is state → transcript → live tail with an entry-id cursor; wire verbosity honours show_thinking none|final|all with ~1s coalesced flushes, one collapsed tool status line, and no tool result bodies.
  - The React UI is built to static assets served by the same origin/port: session list with stable/close, chat with thinking per the show_thinking selector, meters (run active, elapsed, current tool, subprocess count, scratch usage) with an always-visible abort, steer and follow-up inputs that work mid-run, upload widget with per-file status, transcript fetch on reconnect, and the fork/navigate branch affordance for 'edit that message'.
  - The synced agent config exists and is loaded one-way read-only into the VM: SYSTEM.md that explains the inbox/uploads/lib/scratch layout and the 'beyond context' workflow, skills and prompts, a pinned pi-web-access extension with a web-search.json chosen for this router, settings.json (defaultTools including codemode, compaction on), and models.json declaring the router endpoint with `apiKey: "$OMNISSIAH_API_KEY"`, image-capable models, and raised image resize limits. A 'save this script' endpoint copies a scratch file into lib/ as the app uid; lib/ stays read-only to the agent.
  - Operations are in place: GC timer + disk-pressure pass wired to the same release(), boot-time remount of live sessions before serving, junk size and slot occupancy on /meters and a panel page, per-session stderr logs inside scratch, journald units, a pin/upgrade procedure, a backup story for lib/ (git) and the VM disk, and a runbook covering 'slot pool full', 'session broken', 'disk pressure' and 'pi upgraded'.
  - rejected: SDK in-process (Node) instead of `pi --mode rpc`: one process per session is the isolation and uid boundary; an in-process runtime would put the agent inside the privileged app.
  - rejected: systemd `DynamicUser=` or a shared uid with per-session $HOME: breaks the leave-the-junk-until-release contract and makes 'everything this session touched' unanswerable (§8).
  - rejected: Per-session VMs or a container per session: explicitly out (§8), and adds a boot per message to a system whose unit of work is a long run.
  - rejected: Per-token SSE or client-side thinking filtering: fails the phone requirement; deltas are disposable and the filter is server-side.
  - rejected: WebSocket or replay-from-log reconnect: SSE plus authoritative fetch plus `get_entries(since)` needs no replay machinery and survives a locked phone.
  - rejected: Redis/queue broker for fan-out: one asyncio process with bounded per-subscriber queues is the whole requirement for one VM and 16 sessions.
  - rejected: Node/TypeScript control plane using pi's exported RpcClient: technically closer to the protocol, but the brief fixes the app as Python and the protocol is thin enough to own; kept as a fallback if framing bugs prove stubborn.
  - rejected: An egress proxy or allow-list inside the VM, and any tool path allow-list: excluded by §3/§6 in favour of keys and meters.
  - rejected: Exposing thinking level or per-run token budgets in the UI: §7 puts model thinking level out of scope.
  - revision `protocol-first-permanent-contract` (round 1): 9 stages
  - revision `rpc-spine-with-parallel-isolation-spike` (round 2): 9 stages
- Coworker (Agent4): Build the RPC driver and its protocol-contract tests first, then the registry, then guest-level isolation, then the delivery layer; treat pi's RPC/session-format as a versioned external contract with a conformance suite that must pass on every upgrade.
  - This repo holds the §4 interface contract plus an app skeleton: `compose/docker-compose.yml` (service: image, port, env-var NAMES, mount points), `compose/traefik/dynamic/servitor.yml` (one router, one service at `http://<vm>:<port>`, forward-auth middleware, SSE-tuned timeouts), `.env.example` + `.env` (gitignored) carrying `SERVITOR_BIND`, `SERVITOR_PORT`, `SERVITOR_ROUTER_BASE_URL`, `OMNISSIAH_API_KEY`, `SERVITOR_EDGE_SUBNET`, `SERVITOR_ROOT`, `SERVITOR_GC_IDLE_DAYS`, `SERVITOR_PI_VERSION`. Nothing in the app hardcodes an address, a port, or a path. Pin `@earendil-works/pi-coding-agent` to one version and install it `--ignore-scripts` in the image.
  - A `pi` subprocess can be spawned, driven, steered, aborted, idle-killed and respawned with transcript continuity, and the JSONL framing is proven byte-faithful. A throwaway OpenAI-compatible mock router stands in for the real router so the whole driver is testable without inference.
  - One SQLite registry is the only source of session truth (session id, display name, slot uid, workdir/overlay/transcript paths, `sessionFile`, stable flag, state machine `allocated|live|draining|released`, last-active, junk-size cache). Pi's session file is treated as opaque payload; the app never guesses its location. `servitor doctor` prints the fully resolved config, launch line, pi version and pool state.
  - On the guest: per-session uids from a fixed pool created at image build with real passwd homes; per-session overlay workdir (lower = read-only toolkit base, upper/work in `scratch/`); a writable per-slot agent dir seeded at each spawn from the read-only synced config; uploads received by the privileged app and made visible to the slot uid before the agent can see them; release does abort → kill-by-uid → unmount → sweep-by-owner → delete → free.
  - The delivery layer exists as its own module with no HTTP dependency: one pump per live subprocess drains stdout forever, keeps authoritative state (run active, elapsed, current tool, last usage, pending queue), appends to a bounded live tail, and fans out to per-subscriber bounded queues that drop delta-class records and coalesce instead of ever blocking.
  - HTTP surface is complete and thin: sessions CRUD, `POST /sessions/{id}/prompts|steer|follow_up|abort`, upload, authoritative state, transcript, lazy tool-result by `toolCallId`, meters, health. Panel meters come from the pump, not from clients. GC timer, slot pressure and disk pressure all call the same release path; `stable` is honoured by all three.
  - React UI served same-origin from the same process and port: chat with thinking-mode selector, steer/abort controls, upload, session list with stable pin, fork/navigate tree, meters panel, and a reconnect path that fetches rather than replays.
  - Guest is reachable only through the edge: app bound to the ingress address, firewall admits the API port from the edge subnet only, Traefik route with forward-auth in place, standalone fallback documented, GC timer and slice limits installed, runbook + upgrade/drain procedure written.
  - rejected: SDK in-process instead of `pi --mode rpc`: cannot drop per-session privileges (one uid for all sessions), so §8's signal gating, `$HOME`-per-uid, and sweep-by-owner all collapse.
  - rejected: Per-session VMs or per-session containers: rejected by §8 and by cost; a shared container for all sessions with one uid reproduces the SDK problem.
  - rejected: `DynamicUser=` for slots: uid recycling destroys 'look at the junk tomorrow' and forces recursive re-chowns (the brief's own rejection, and the reason the registry needs a `draining` state).
  - rejected: Mounting `PI_CODING_AGENT_DIR` read-only as the config-sync mechanism: verified to crash pi at startup and contradicted by pi's own runtime writes.
  - rejected: Keeping the router address only in `.env` and interpolating it in `models.json`: verified impossible (baseUrl is literal); rendering `models.json` at spawn is the alternative that still avoids a second source of truth.
  - rejected: Replaying the event stream on reconnect (Last-Event-ID or a ring buffer): contradicts §7, adds an unbounded buffer per session, and would have to be lossless for records the contract says are disposable.
  - rejected: Per-token SSE with client-side thinking filtering: pays the framing bytes the brief sets out to avoid, and a client-side filter still sends thinking.
  - rejected: Handing the guest a git push credential so 'save this script' is automatic: verified that generated code sees the app's env, so that credential is agent-readable and it guards the one durable artifact.
  - rejected: Exposing pi's RPC `bash` command through the API as a 'quick shell': unauthenticated-by-design socket plus shell = remote code execution as the slot uid.
  - revision `merged: rpc-spine-with-parallel-isolation-spike` (round 1): 8 stages

## Round-by-round verdicts

### Round 1
round 1: 3/4 signed off · 11 unseen
- Metic (Agent1): 14 stances · backing declined · no signoff
- Senior (Agent2): 14 stances · backing merged: risk-first-isolation-and-rpc-contract-before-api · signed off
- Pro (Agent3): 14 stances · backing merged: protocol-first-permanent-contract · signed off
- Coworker (Agent4): 14 stances · backing merged: rpc-spine-with-parallel-isolation-spike · signed off

### Round 2
round 2: 4/4 signed off · 9 unseen
- Metic (Agent1): 11 stances · backing Agent4 · signed off
- Senior (Agent2): 11 stances · backing Agent4 · signed off
- Pro (Agent3): 11 stances · backing Agent4 · signed off
- Coworker (Agent4): 11 stances · backing Agent3 · signed off

## Artifacts

- `plan.md` — the settled surface
- `report.md` — this record: the direction's verdict, each point's debate, the human input, the notes, the Q&A and the rounds
- `agents/<file_base>.stderr.txt` — per-member stderr, untrimmed (the verbatim transcripts are written at `full` only)
- nothing else: verbosity `quiet` writes no `run.json`, `human_input.json` or `events.jsonl`

Run directory: `/home/crazybot/.cache/council/runs/20261114T081903-dockerx`
