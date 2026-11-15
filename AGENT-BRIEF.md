# Servitor — design brief (pre-implementation)

Owner intent: a general home assistant with coding abilities — *not* a pure coding agent — that
can be handed files, write scripts to work on them, run as long as it needs to, and be steered
or aborted by a human at any point.

---

## 0. Name: Servitor

---

## 1. Problem statement

A custom browser chat UI for an LLM + more. The Pi agent should live in a VM with full standard tools, as well as a way to search the internet (either pi-web-access or something custom, leaning pi-web-access for simplicity). It should include some way to upload files separate from sending them directly to the agent, allowing for uploads beyond max context (for example, uploading photos of a binder of pokemon cards and then having the agent use scripts + web research to write a report with the values of each card).

- Long, unbounded, human-steerable runs. **No capability caps.**

---

## 2. Target system

- **CachyOS** (Arch) system with a Threadripper Pro 9965WX (24 cores 48 threads) + 256GB DDR5
- **ext4** filesystem

## 3. The harness will be in a VM

The dominant threat is **prompt injection**: search results,
fetched pages, and uploaded files steer an agent that already holds credentials and a network
position. 

- **No GPU in the VM.** Inference is HTTPS to the host's existing LLM router (OpenAI-compatible;
  the service gets its address and `OMNISSIAH_API_KEY` as machine creds — §4). The key is passed
  to each `pi` subprocess **env-only** — never on disk, never in a transcript. The agent process
  therefore holds the key, and exfiltration to the open internet is the accepted residual risk:
  the VM boundary is the mitigation, so there is deliberately no egress filtering inside the VM.
- **The edge routes to the VM directly.** "The edge" is the host's single internet-facing
  entrance (portcullis: a Traefik reverse proxy with Authelia 2FA forward-auth) — the only
  path from the internet to anything internal. The service is just an address
  (`- url: "http://<vm-address>:<port>"`; in portcullis that's the Traefik file provider).
  No `docker.sock`, no proxy inside the VM.
- the VM should not be LAN-reachable beyond what the edge needs — its firewall admits the                                                                
  API port from the edge subnet only.

---

## 4. Component inventory

| component | home | owned by | notes |
|---|---|---|---|
| chat UI (browser) | VM | agent repo | served through the edge route |
| HTTP/SSE API for chat + uploads + health | VM | agent repo | **no end-user auth of its own** — the edge is the gate |
| agent harness (Pi subprocess per session) | VM | agent repo | `pi --mode rpc` |
| interface contract (routes, keys, ports, allowed gates) | **this repo** | this repo | lives in `compose/docker-compose.yml` (service) + `compose/traefik/dynamic/*.yml` (routes) + `.env` (secrets); keys = machine creds the service holds (e.g., the router's `OMNISSIAH_API_KEY`), not end-user tokens |
| prompts, skills, tool defs, saved scripts (`lib/`) | agent repo (git) | agent repo | synced one-way into the VM |

---

## 5. Stack

- Pi is MIT (`package.json: license`); extraction is purely an engineering-cost question. The
  value is the long tail, not the loop: session tree with branching/fork/navigate (the "edit
  that message" UX, for free), compaction, context-overflow recovery, retries, provider compat
  shims, RPC framing.
- **Interface: RPC mode** (`pi --mode rpc`), JSONL over stdio, one subprocess per conversation.
  The subprocess is **killed after 1 h idle** and respawned from the session file when the user
  picks the session up again. `docs/rpc.md` ships a minimal Python client.
  - Protocol: LF-only framing (do **not** use Node `readline` — it also splits on
    `U+2028`/`U+2029`, legal inside JSON strings); correlate by `id`; `prompt` response means
    *accepted*, not *finished* — `disposition` is `started` | `queued` | `handled`; wait for
    `agent_settled` unless `disposition` is `handled` (consumed, no run started); subscribe to
    events **before** prompting; stdout is protocol-only, diagnostics on stderr.
  - `--no-session` vs persistent `--session-dir`; `PI_CODING_AGENT_DIR` /
    `PI_CODING_AGENT_SESSION_DIR` split config and session data onto **different volumes**.

**React for frontend**. The API authenticates nobody — anything that can
reach the socket can drive the agent, so the edge is the only gate and
reachability is the control: bind the VM's ingress interface (the address
the edge route targets) and admit that one port from the edge subnet in the
VM firewall; the socket is not reachable off the VM. A deployment without an
external edge (standalone) puts it on loopback behind a local reverse proxy
instead — same guarantee, no code change. 

---

## 6. Capability policy = "no caps, only keys and meters"

- **Inside the VM: no caps.** Standard Pi tools, arbitrary scripts, no path
  allow-lists — reach is bounded only by the session's slot uid and the
  read-only mounts (synced config, `lib/`; §8), not by policy.
- **No wall-clock, iteration, or vision-call limits.** Long runs are the point; the control is
  a human (`steer()` / follow-up message) or `abort()` over RPC.
- **Meters, not limiters.** The UI shows: run active, elapsed, current tool, subprocess
  count, scratch usage, **abort** control.

---

## 7. Delivery is not the run

Owner requirements: a phone on a bad connection must never slow the agent; a dropped
connection must not cancel anything; thinking blocks optional **on the wire**. Desktop gets
near-live thinking tokens; a phone user starts a run, locks the phone, and comes back later
to see whether it finished.

- **The producer never awaits a client.** Each subscriber gets a bounded queue; on overflow it
  **drops and coalesces, never blocks.**
- **Deltas are disposable *by contract*** (`docs/json.md`): `text_end`, `thinking_end`,
  `toolcall_end` carry the completed content; `message_end.message` the whole message. A
  dropped delta changes nothing in the final transcript.
- **The run does not need a client.** Nothing attached ⇒ the log keeps growing; no stall, no
  cancel.
- **Reattach is a fetch, not a replay.** On (re)connect the client first gets authoritative
  state — run active/settled, elapsed, current tool (the §6 meters) — then the transcript, then
  re-subscribes to the live tail. No client is expected to have seen every delta; locking the
  phone mid-run costs nothing, and coming back just fetches.
- **Abort is an explicit `POST /sessions/{id}/abort`** — never a side effect of a socket dying
  (a backgrounded tab kills the connection; that must not kill the run).
- **Filter server-side** — a client that merely "doesn't render" thinking still pays for the
  bytes if they're sent.

"Thinking optional" means **not sending it over the wire** — the run is identical, only
delivery differs. Model thinking level is out of scope.

Wire verbosity composes:

- `show_thinking=none` — drop `thinking_start`/`thinking_delta`; keep a compact "thinking… 12 s" line.
- `show_thinking=final` — emit only the authoritative `thinking_end` block (thinking is
  persisted in `AssistantMessage.content` anyway, so the UI can fetch any block later).
- `show_thinking=all` — everything; desktop default.
- **Coalesce** thinking and text deltas into configurable (default ~1s) flushes — per-token SSE
  over LTE is mostly framing overhead; near-live on desktop, cheap on a phone plan.
- **Collapse** tool progress into one status line; **omit tool result bodies** — the UI fetches
  them lazily by tool-call id if desired.

Honest cost of `none`: thinking is how you notice a run going wrong — `final` is the
compromise (no stream, one authoritative block, still fetchable).

---

## 8. Storage areas and per-session isolation

| area | contents | lifetime |
|---|---|---|
| `inbox/<session>/` | what the user sent | until the session is released |
| `scratch/<session>/` | all generated junk; holds the overlay `upper`/`work` dirs | until the session is released |
| `lib/` | scripts the user chose to keep | git-backed in the agent repo; the only durable, reviewable artifact |

Isolation ladder: **per-session directory `0700` + a per-session UID from a small pool**, plus
overlayfs per session. Explicitly **not** per-session VMs.

**Privilege model.** The app is privileged; the agent never is.

- Each session's `pi` runs as a **slot uid** (fixed set created at image build); the app runs
  as root or a narrowly privileged uid. Invariant: **the agent's uid is never the app's uid** —
  that is what makes the read-only mounts real (root could remount them rw; the agent cannot
  be root).
- **Read-only where injection could persist itself:** synced config (`SYSTEM.md`, skills, tool
  defs) and `lib/` mounted read-only; "save this script" is a distinct write-through action (or
  a human commit).
- **Uploads:** content-addressed names, MIME by magic, client filename discarded; received by
  the privileged app, written into `inbox/<session>/` and chowned to the session's slot uid
  before the agent can see them.
- **Toolkit baked into the image** (Pillow, numpy, ffmpeg, ImageMagick, `uv`)
  so sessions don't mutate the base. The agent's workdir is the overlay mount (lowerdir =
  read-only toolkit base; upperdir/workdir = `scratch/<session>/{upper,work}`); per-session
  `pip install` lands in a venv under the workdir and dies with the session.
- **Release does all session cleanup.** Releasing a session: kill the subprocess (if alive),
  **sweep by owner** (`find / -user slotN` — its home, `/tmp`, everything dropped outside
  `scratch/`), delete `inbox/<session>/`, `scratch/<session>/` (overlay `upper`/`work`
  included), and the transcript, then free the slot. The sweep is a required step of release:
  a slot's uid is never handed to another session before it is clean.
- **GC is only the trigger.** A VM timer releases sessions idle > `gc_idle_days` (default 14)
  that are not marked stable; never `lib/`. All release triggers: user close (UI), GC timer,
  slot pressure, disk pressure.
- **Slot pressure.** 16 slots = 16 *live* sessions (running or awaiting release), not 16
  simultaneous runs. No free slot ⇒ release the oldest non-stable session that is not actively
  running and take its slot; if all 16 are running, the new session waits and the panel says so.
- **Disk pressure.** Near-full VM disk (§9) ⇒ release the oldest non-stable sessions until
  there is headroom.
- The user can mark a session **stable** (UI): never GC'd, revisitable whenever, and it pins
  its slot uid until closed (close ⇒ release). Because a slot is never reused before its sweep,
  a session's junk (workdir, transcript) stays intact from its last run until release, so
  "look at it tomorrow" works; a released session is simply gone. Stable sessions still count
  against the pool: 16 stable sessions block new ones until one is closed. Junk size reported
  to the panel — unbounded mess is only dangerous when invisible.

**Static slot uids — tidiness is the reason.** 16 slots — one per live session (running or
awaiting release); a stable session holds its slot until closed.

- **`$HOME` follows the uid, not an env var.** `expanduser("~")` falls back to the passwd
  entry and plenty of tools read `getpwuid`; a shared uid with per-session `$HOME` leaks the
  first time a script forgets to export it, and concurrent sessions collide in `~/.cache`,
  `~/.config`, uv/pip/npm caches, `__pycache__`.
- **Signals are uid-gated, not cgroup-gated.** Generated code does dumb global things
  (`pkill -f python`, `killall node`, `kill -9 -1`); a distinct uid keeps one session from
  killing another's tooling — or the app server, which is Python.
- **Sweep by owner.** `find / -user slotN` catches what a session dropped *outside*
  `scratch/` (`/tmp`, `/var/tmp`, `/`) and makes slot release a clean handover; with a shared
  uid, "everything this session touched" would be unanswerable.
- `DynamicUser=` rejected: recycled uids break "leave the junk so I can look at it tomorrow",
  and a uid change triggers a recursive re-chown of the state tree.

---

## 9. Resources

Owner requirement: **fast, and unbounded within its slice.**

- **vCPU:** as many as physical cores (24), no quota — a long run should own the idle
  machine. Use a low `CPUWeight` to avoid starving other processes. 
- **RAM:** ceiling 30 GB, not a reservation. `virtio-balloon` + free-page-reporting so idle
  memory returns to the host; slice `MemoryHigh=25G` / `MemoryMax=30G` so the guest throttles
  before it OOMs; zram absorbs the rest.
- **Disk:** fixed-size VM disk, ~200 GB, sized at VM creation — no quota mechanism; near-full
  disk is a GC trigger (§8)
- **No GPU.** Anything GPU-compute (OCR/ASR/embeddings/image gen) is a future update tool
  call. Model *vision* is not in that deferred set — a model reading uploaded card photos
  is a capability of whatever image-capable model the router serves, not GPU work in the
  VM.

---

## 10. Verified facts (council: cite, don't re-research; flag if wrong)

Pi (installed `@earendil-works/pi-coding-agent` 1.5.2, MIT; docs paths relative to the package):

- `docs/rpc.md` — JSONL over stdio; LF-only framing (Node `readline` also splits on
  `U+2028`/`U+2029`); correlate by `id`; `prompt` → `disposition` (`started` | `queued` |
  `handled`; `handled` ⇒ no run started, don't wait for `agent_settled`), otherwise wait for
  `agent_settled`; subscribe before prompting; stderr for diagnostics; orderly shutdown on
  stdin EOF.
- `docs/sdk.md` — `createAgentSession`, `SessionManager` (persistent or in-memory),
  `DefaultResourceLoader`, `tools`/`excludeTools`/`customTools`, inline extensions; SDK
  sessions do **not** auto-load `codemode`/`tool_search`/MCP.
- `docs/codemode.md` — QuickJS, no fs/net/timers; `tools.*`, `text()`, `image()`,
  `store()`/`load()` (≤256 KiB/value, ≤1 MiB total), `searchTools`, `models`; `timeout_ms`
  unset by default; over-cap output → temp file with path returned; `codemode.mode` `on`/`only`.
- `docs/cli.md` — `--tools` / `--exclude-tools` (plain names replace, `+name`/`-name` delta,
  no mixing), `--mode json|rpc`, `--session-dir`, `--no-session`.
- `docs/models.md` + `docs/custom-provider.md` — `models.json`, `api: "openai-completions"`,
  provider and per-model `baseUrl`, `compat`, `samplingParams`,
  `samplingParamsByThinkingLevel`; classifier/image model types.
- `docs/environment-variables.md` — `PI_CODING_AGENT_DIR`, `PI_CODING_AGENT_SESSION_DIR`.
- `docs/security.md` + `docs/containerization.md` — treat generated commands as untrusted;
  "entirely inside a container/VM/sandbox" is the strongest practical option; tool-only
  isolation does not constrain the harness process or other extension tools.
- Pi ships **without** sub-agents, plan mode, a built-in sandbox, and approval gates.
- `docs/session-format.md` — persisted JSONL is a documented contract (browse/reuse history).
- `docs/message-types.md` — `ThinkingContent` is part of `AssistantMessage.content`: thinking
  is persisted, so never streaming it loses nothing.
- `docs/json.md` — drop-safe contract: `thinking_start`/`thinking_delta`/`thinking_end`,
  `text_end`, `toolcall_end`, `message_end.message` are authoritative; deltas are display-only.
  Also `thinking_level_changed`.
- `docs/rpc-commands.md` — `abort`, `steer`, `follow_up`: stop/steer controls are RPC
  commands, no custom protocol needed.
