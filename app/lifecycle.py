"""Session lifecycle — the stage-2 coordinator (PLAN §5, Stage 2).

Owns, in one process:
- boot reconciliation (proc-scan for live slot-uid processes ⇒ kill
  crash-orphans via the ladder, mark the sessions `interrupted` —
  BEFORE any spawn/respawn);
- start / prompt (with respawn-on-pickup) / release through the
  registry (the only source of session truth);
- probe: reads of sessions with no live process — short-lived RPC
  spawn as the slot uid with the exact run launch, one process per
  session file, never the first to touch an agent dir;
- idle-kill (1 h default, settled-only, ladder, never unmounts);
- abort-then-drain on app shutdown (grace < TimeoutStopSec);
- GC + disk pressure in-process through release();
- per-spawn extension recording (loaded set vs image baseline ⇒
  new-extension warning — D3: warn, never block).

No wall-clock/iteration/token caps anywhere (no capability caps — the
only controls are human steer/abort and these lifecycle rules).
"""
from __future__ import annotations

import asyncio
import dataclasses
import json
import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from app.iso import mounts as M
from app.iso.configswap import active_dir as config_active_dir
from app.iso.configswap import require_active_dir
from app.iso.layout import SessionMountPaths, StatePaths
from app.iso.slots import NoFreeSlot, SlotPool
from app.iso.spawn import slot_identity, spawn_setuid
from app.iso.sweep import kill_strays_by_uid
from app.iso.release import release_session
from app.api.sse import SseHub
from app.api.transcript import (
    active_branch,
    build_forest,
    entry_to_message,
    forkable_messages,
    read_entries,
)
from app.registry import (
    ALLOCATED,
    CLEANING,
    DRAINING,
    FREE,
    LIVE,
    Registry,
    SessionRow,
)
from app.rpc.launch import build_launch
from app.rpc.session import PiSession, SpawnFailure

log = logging.getLogger("servitor.lifecycle")

#: probe result cache TTL (seconds) — "results briefly cached"
PROBE_CACHE_TTL = 30.0

#: disk pressure: release oldest non-stable until usage is below this
DISK_PRESSURE_FRACTION = 0.90


@dataclass
class PiInstall:
    node_bin: str
    cli_js: str
    ext_path: str | None = None


@dataclass
class Live:
    """In-memory state for a session with (or that had) a driver."""

    session_id: str
    sp: SessionMountPaths
    mount_ops: list[M.MountOp]
    driver: Optional[PiSession]
    config_version: str
    last_settled: Optional[float] = None
    subscribers: int = 0
    config_lower: Optional[str] = None  # the version this session booted from
    api_key: Optional[str] = None  # per-session key (browser-held; never persisted)


@dataclass
class LifecycleConfig:
    state: StatePaths
    pool: SlotPool
    registry: Registry
    pi: PiInstall
    api_key: str
    exa_key: str | None = None
    toolkit_dir: str = "/opt/toolkit"
    baseline_extensions: tuple[str, ...] = ()  # image/config baseline names
    idle_kill_sec: float = 3600.0
    shutdown_grace_sec: float = 90.0  # strictly < the unit's TimeoutStopSec
    gc_idle_days: float = 14.0
    gc_interval_sec: float = 300.0
    delivery: "Delivery | None" = None  # stage-3 delivery service (blobs/staging)
    sse_ring_bytes: int | None = None  # per-session SSE cap (None = 50 MB default)


class LifecycleHooks:
    """The ReleaseHooks seam (app/iso/release.py) backed by the registry."""

    def __init__(self, reg: Registry, pool: SlotPool, events: "EventFeed"):
        self.reg = reg
        self.pool = pool
        self.events = events

    def mark_draining(self, session_id: str, reason: str) -> None:
        self.reg.set_state(session_id, DRAINING, cause=f"release: {reason}")
        self.events.add(session_id, "release_start", reason)

    def mark_released(self, session_id: str, *, quarantined: bool, detail: str) -> None:
        row = self.reg.get(session_id)
        if row is None:
            return
        if quarantined:
            # the slot is quarantined in the pool; the row stays DRAINING —
            # visible in the panel, retryable, never reused
            self.events.add(session_id, "slot_quarantined", detail)
            return
        self.reg.set_state(session_id, CLEANING, cause=detail)
        self.reg.set_state(session_id, FREE, cause="sweep verified empty")
        self.events.add(session_id, "released", detail)


class EventFeed:
    """The event feed (registry/panel/doctor consume it; stage 3 serves it).

    Bounded in memory; the doctor reads `new_extensions` from the registry
    directly, so dropping old feed entries is safe. App-level events are
    also published to the session's SseHub (stage 3) so the SSE stream is
    the single per-session event source."""

    def __init__(self, maxlen: int = 1000, hub_getter=None):
        self._items: list[dict] = []
        self._maxlen = maxlen
        self._hub_getter = hub_getter

    def add(self, session_id: str | None, kind: str, detail: str = "") -> None:
        item = {
            "ts": time.time(), "session_id": session_id, "kind": kind, "detail": detail
        }
        self._items.append(item)
        if len(self._items) > self._maxlen:
            self._items = self._items[-self._maxlen :]
        if self._hub_getter is not None and session_id:
            try:
                hub = self._hub_getter(session_id)
                if hub is not None:
                    hub.publish(kind, {"detail": detail, "ts": item["ts"]})
            except Exception:
                log.exception("hub publish failed for %s/%s", session_id, kind)

    def items(self, since: float = 0.0) -> list[dict]:
        return [i for i in self._items if i["ts"] >= since]


class Lifecycle:
    def __init__(self, cfg: LifecycleConfig):
        self.cfg = cfg
        self.live: dict[str, Live] = {}
        self._hubs: dict[str, "SseHub"] = {}
        self.events = EventFeed(hub_getter=self.hub)
        self.hooks = LifecycleHooks(cfg.registry, cfg.pool, self.events)
        self._probe_locks: dict[str, asyncio.Future] = {}
        self._probe_cache: dict[tuple[str, str], tuple[float, Any]] = {}
        self.shutting_down = False
        self._gc_task: Optional[asyncio.Task] = None

    def hub(self, session_id: str) -> "SseHub":
        """The session's SSE hub (created on first use; the hub outlives
        any single driver — respawns re-attach to the same hub)."""
        h = self._hubs.get(session_id)
        if h is None:
            h = SseHub(session_id, cap_bytes=self.cfg.sse_ring_bytes)
            self._hubs[session_id] = h
        return h

    # ------------------------------------------------------------------ #
    # boot reconciliation                                                 #
    # ------------------------------------------------------------------ #

    def boot_reconcile(self) -> list[dict]:
        """Kill crash-orphans / mark interrupted sessions BEFORE any
        spawn/respawn (PLAN §5).

        Two crash signatures (both ⇒ interrupted):
        1. a live slot-uid process still exists — a crash-orphan the
           drain never ran for: kill it via the ladder (SIGTERM →
           SIGKILL, never SIGKILL first);
        2. no process, but the transcript's last run never completed
           (the process died mid-run: pi's stdin-EOF shutdown aborts an
           in-flight run and records the abort — an empty assistant
           message or a dangling tool call, never a final answer).

        The session's transcript and slot survive either way: the
        expectation is a respawn on pickup, not a release."""
        out: list[dict] = []
        for slot in self.cfg.pool.slots.values():
            row = self.cfg.registry.session_for_slot(slot.name)
            if row is None or row.state not in (ALLOCATED, LIVE):
                continue
            pids = self._pids_of_uid(slot.uid)
            killed = kill_strays_by_uid(slot.uid) if pids else []
            interrupted = False
            detail = ""
            if killed:
                interrupted = True
                detail = f"orphan killed by boot reconciliation: {killed}"
            elif row.session_file and transcript_unsettled(row.session_file):
                interrupted = True
                detail = "crash signature: last run never settled (process died mid-run)"
            if interrupted:
                self.cfg.registry.mark_interrupted(row.id)
                self.events.add(row.id, "interrupted", detail)
            # rebuild the in-memory pool from the registry (the only truth):
            # a live-or-allocating row owns its slot across restarts; a
            # draining/cleaning row means a release was interrupted by the
            # crash — the slot must NOT be handed out (quarantine: visible,
            # retryable, never reused until a re-sweep verifies clean)
            self._rebuild_pool_slot(slot, row)
            out.append({"slot": slot.name, "uid": slot.uid, "killed": killed, "session": row.id})
            if killed or interrupted:
                log.warning("boot reconcile: slot %s session %s: %s", slot.name, row.id, detail or "killed")
        # delivery GC: any surviving staging dir is an interrupted upload
        # (a completed upload removes its staging dir); no half-blobs can
        # survive a crash (PLAN §3.4 crash drill)
        if self.cfg.delivery is not None:
            self.cfg.delivery.gc_boot()
            for row in self.cfg.registry.list_sessions():
                if row.state in (ALLOCATED, LIVE):
                    try:
                        slot = slot_identity(row.slot) if row.slot != "waiting" else None
                        self.cfg.delivery.files(self.cfg.state, row.id, slot).gc_inbox_temps()
                    except Exception:
                        pass
        return out

    def _rebuild_pool_slot(self, slot, row) -> None:
        if row is None or row.state == FREE:
            return
        if row.state in (ALLOCATED, LIVE):
            if slot.state != "allocated":
                slot.state = "allocated"
                slot.session_id = row.id
                slot.stable = row.stable
        elif row.state in (DRAINING, CLEANING):
            try:
                self.cfg.pool.quarantine(row.id, "release interrupted by app crash at boot")
                self.events.add(row.id, "slot_quarantined", "release interrupted at boot")
            except Exception:
                log.exception("boot reconcile: could not quarantine %s", slot.name)

    @staticmethod
    def _pids_of_uid(uid: int) -> list[int]:
        from app.iso.sweep import _pids_of_uid

        return _pids_of_uid(uid)

    # ------------------------------------------------------------------ #
    # extension recording (D3: warn, never block)                         #
    # ------------------------------------------------------------------ #

    def _extension_names(self, sp: SessionMountPaths) -> tuple[str, ...]:
        """The loaded extension set as the app can see it before spawn:
        the explicit -e paths plus the agent dir's auto-discovered
        extensions dir (one level deep — pi's discovery rule). A planted
        extension in the agent upper shows up here."""
        names: list[str] = []
        if self.cfg.pi.ext_path:
            names.append(Path(self.cfg.pi.ext_path).name)
        ext_dir = Path(sp.agent_dir) / "extensions"
        if ext_dir.is_dir():
            for entry in sorted(ext_dir.iterdir()):
                if entry.is_file() and entry.suffix in (".ts", ".js"):
                    names.append(f"agent:{entry.name}")
                elif entry.is_dir() and (entry / "index.ts").exists():
                    names.append(f"agent:{entry.name}/")
                elif entry.is_dir() and (entry / "package.json").exists():
                    names.append(f"agent:{entry.name}/")
        return tuple(names)

    def _record_extensions(self, session_id: str, sp: SessionMountPaths) -> None:
        loaded = self._extension_names(sp)
        new = tuple(n for n in loaded if n not in set(self.cfg.baseline_extensions))
        self.cfg.registry.set_extensions(session_id, loaded, new)
        for n in new:
            self.events.add(session_id, "new_extension", n)
            log.warning("session %s: extension %s loaded that is not in the image baseline", session_id, n)

    # ------------------------------------------------------------------ #
    # launch / spawn                                                      #
    # ------------------------------------------------------------------ #

    def _launch(self, session_id: str, session_file: str | None, sp: SessionMountPaths):
        live = self.live.get(session_id)
        return build_launch(
            node_bin=self.cfg.pi.node_bin,
            cli_js=str(self.cfg.pi.cli_js),
            session_dir=f"{self.cfg.state.sessions}/{session_id}",
            session_id=None if session_file else session_id,
            session_file=session_file,
            extension_paths=(str(self.cfg.pi.ext_path),) if self.cfg.pi.ext_path else (),
            home=f"/home/{self._slot_name_for(session_id)}",
            user=self._slot_name_for(session_id),
            agent_dir=sp.agent_dir,
            # per-session key (browser-held, in-memory only) beats the app
            # env key; env-only, never persisted, never returned
            omnissiah_api_key=(live.api_key if live and live.api_key else self.cfg.api_key),
            exa_api_key=self.cfg.exa_key,
            web_cache_root=sp.web_cache,
        )

    def _slot_name_for(self, session_id: str) -> str:
        row = self.cfg.registry.get(session_id)
        if row is None:
            raise ValueError(f"no registry row for {session_id}")
        if row.slot == "waiting":
            raise RuntimeError(f"{session_id}: no slot allocated")
        return row.slot

    def _make_driver(self, session_id: str, session_file: str | None, live: Live) -> PiSession:
        launch = self._launch(session_id, session_file, live.sp)
        slot_name = self.cfg.registry.get(session_id).slot
        if slot_name == "waiting":
            raise RuntimeError(f"{session_id}: no slot allocated")
        ident = slot_identity(slot_name)
        driver = PiSession(launch, ident, cwd=live.sp.workdir)
        driver.on_settled = lambda *a, **k: self._on_settled(session_id)
        # stage 3: the session's event stream is the SSE hub (pi events +
        # app events; backpressure pauses THIS driver's event stream)
        hub = self.hub(session_id)
        driver.on_event = hub.on_event
        hub.attach(driver)
        return driver

    def _on_settled(self, session_id: str) -> None:
        live = self.live.get(session_id)
        if live is not None:
            live.last_settled = time.time()
        self.cfg.registry.touch(session_id)

    # ------------------------------------------------------------------ #
    # start / prompt / respawn                                            #
    # ------------------------------------------------------------------ #

    async def start_session(
        self, display_name: str | None = None, api_key: str | None = None
    ) -> dict:
        """Allocate slot + mounts + first spawn. Slot pressure: release the
        oldest non-stable non-running; if none, the session WAITS (the
        panel says so) — it is not an error."""
        if self.shutting_down:
            raise RuntimeError("app is shutting down")
        session_id = _new_session_id()
        # the row exists before side effects (a crash mid-start is visible)
        slot = None
        try:
            slot = self.cfg.pool.allocate(session_id)
        except NoFreeSlot:
            freed = await self._release_oldest_for_pressure()
            if freed:
                slot = self.cfg.pool.allocate(session_id)
            else:
                row = self.cfg.registry.upsert_session(
                    session_id, display_name=display_name, slot="waiting",
                    state=ALLOCATED,
                )
                self.cfg.registry.set_waiting(session_id, True)
                self.events.add(session_id, "waiting", "all slots live; queued")
                return {"session_id": session_id, "waiting": True}
        assert slot is not None
        row = self.cfg.registry.upsert_session(
            session_id,
            display_name=display_name,
            slot=slot.name,
            state=ALLOCATED,
        )

        sp = SessionMountPaths.for_session(session_id, str(self.cfg.state.scratch))
        lower = require_active_dir(str(self.cfg.state.config))
        version = _config_version_of(lower)

        # mounts BEFORE any spawn (PLAN §5)
        await asyncio.to_thread(M.ensure_session_dirs, sp, self.cfg.state)
        ops = await asyncio.to_thread(
            M.mount_session, sp, lower, self.cfg.state, slot.uid, slot.gid, self.cfg.toolkit_dir
        )
        live = Live(
            session_id=session_id, sp=sp, mount_ops=ops, driver=None,
            config_version=version, config_lower=lower, api_key=api_key,
        )
        self.live[session_id] = live
        try:
            self._record_extensions(session_id, sp)
            driver = self._make_driver(session_id, None, live)
            await driver.start()
            st = await driver.get_state()
            session_file = st.get("sessionFile")
            self.cfg.registry.set_session_file(session_id, session_file)
            live.driver = driver
            self.cfg.registry.set_state(session_id, LIVE, cause="spawn ok")
            self.cfg.registry.touch(session_id)
            self.events.add(session_id, "started", f"slot={slot.name} config={version}")
            return {"session_id": session_id, "waiting": False, "slot": slot.name,
                    "session_file": session_file, "config_version": version}
        except Exception:
            # start failed: no half-live row — clean up what we made
            await self._abort_start(session_id, live, slot)
            raise

    async def _abort_start(self, session_id: str, live: Live, slot) -> None:
        if live.driver is not None:
            try:
                await live.driver.shutdown(eof_grace=1, term_grace=1)
                await live.driver.close()
            except Exception:
                pass
        try:
            await M.aunmount_all_safe(live.mount_ops)
        except Exception:
            pass
        self.live.pop(session_id, None)
        self.cfg.pool.release_slot_if_owned(session_id, slot)
        # a start that never produced a transcript is not a session: the
        # row goes (session existence is the app's fact)
        self.cfg.registry.delete(session_id)

    async def prompt(self, session_id: str, text: str) -> str:
        """Steer/prompt with respawn-on-pickup. A transcript-less row is
        `idle, nothing to resume` — a disposition, not an error."""
        if self.shutting_down:
            raise RuntimeError("app is shutting down; prompts refused")
        row = self.cfg.registry.get(session_id)
        if row is None:
            raise KeyError(session_id)
        live = self.live.get(session_id)
        if live is not None and live.driver is not None and not live.driver.exited:
            return await live.driver.prompt(text)
        # no live process: respawn only when the registry says a transcript
        # exists (the transcript-exists guard)
        if not row.session_file:
            return "idle_no_transcript"
        await self._respawn(session_id)
        live = self.live[session_id]
        return await live.driver.prompt(text)  # type: ignore[union-attr]

    async def _respawn(self, session_id: str) -> None:
        row = self.cfg.registry.get(session_id)
        if row is None:
            raise KeyError(session_id)
        if not row.session_file:
            raise ValueError(f"{session_id}: no transcript to respawn from")
        live = self.live.get(session_id)
        slot_name = row.slot
        ident = slot_identity(slot_name)

        # invariant 7 at the spawn boundary: re-check the slot uid for a
        # live process and kill it first (boot reconcile is not enough —
        # a crash could have happened since boot)
        stray = self._pids_of_uid(ident.uid)
        if stray:
            kill_strays_by_uid(ident.uid)

        if live is None:
            sp = SessionMountPaths.for_session(session_id, str(self.cfg.state.scratch))
            # mounts survive the process: reuse if mounted, else re-mount
            lower = ""
            ops: list[M.MountOp] = []
            if sp.workdir not in M.mounted_targets():
                lower = (row.config_version and _lower_for_version(str(self.cfg.state.config), row.config_version)) \
                    or require_active_dir(str(self.cfg.state.config))
                await asyncio.to_thread(M.ensure_session_dirs, sp, self.cfg.state)
                ops = await asyncio.to_thread(
                    M.mount_session, sp, lower, self.cfg.state, ident.uid, ident.gid, self.cfg.toolkit_dir
                )
            live = Live(session_id=session_id, sp=sp, mount_ops=ops, driver=None,
                        config_version=row.config_version or "", config_lower=lower)
            self.live[session_id] = live

        if live.driver is not None:
            try:
                await live.driver.close()
            except Exception:
                pass
        self._record_extensions(session_id, live.sp)
        driver = self._make_driver(session_id, row.session_file, live)
        try:
            await driver.start()
            st = await driver.get_state()
        except SpawnFailure:
            # fatal spawn error (missing cwd, bad config): release, never retry
            log.error("respawn of %s failed fatally: releasing", session_id)
            self.events.add(session_id, "spawn_fatal", "respawn failed; releasing")
            await self.release(session_id, reason="fatal spawn error")
            raise
        # identity assert: the process must be on OUR session
        if st.get("sessionId") != session_id:
            await driver.shutdown(eof_grace=1, term_grace=1)
            await driver.close()
            raise RuntimeError(
                f"identity assert failed: spawned on {st.get('sessionId')}, expected {session_id}"
            )
        live.driver = driver
        self.cfg.registry.touch(session_id)
        self.events.add(session_id, "respawned", "transcript intact")

    # ------------------------------------------------------------------ #
    # probe                                                               #
    # ------------------------------------------------------------------ #

    async def probe(self, session_id: str, which: str = "messages") -> Any:
        """Reads of a session with no live process: short-lived RPC spawn
        as the slot uid with the exact run launch (same mounts, same
        flags), one process per session file (concurrent probes
        coalesce), never the first to touch an agent dir (the registry
        says a transcript exists), stdin EOF, no model call. Results are
        briefly cached."""
        row = self.cfg.registry.get(session_id)
        if row is None:
            raise KeyError(session_id)
        if row.state == FREE or not row.session_file or not os.path.exists(row.session_file):
            # the probe is NEVER the first process to touch a session's
            # agent dir (assert: registry says a transcript exists)
            raise ValueError(f"{session_id}: probe refused — no transcript (first-touch rule)")
        live = self.live.get(session_id)
        if live is not None and live.driver is not None and not live.driver.exited:
            return await self._probe_via(live.driver, which)
        # coalesce: one probe process per session file (per command)
        key = (session_id, which)
        cached = self._probe_cache.get(key)
        if cached and time.time() - cached[0] < PROBE_CACHE_TTL:
            return cached[1]
        fut = self._probe_locks.get(key)
        if fut is not None:
            return await fut
        fut = asyncio.get_running_loop().create_future()
        self._probe_locks[key] = fut
        try:
            result = await self._run_probe(session_id, which, row)
            self._probe_cache[key] = (time.time(), result)
            fut.set_result(result)
            return result
        except Exception as e:
            fut.set_exception(e)
            raise
        finally:
            self._probe_locks.pop(key, None)

    async def _probe_via(self, driver: PiSession, which: str) -> Any:
        return await _probe_command(driver, which)

    async def _run_probe(self, session_id: str, which: str, row: SessionRow) -> Any:
        live = self.live.get(session_id)
        sp = live.sp if live else SessionMountPaths.for_session(session_id, str(self.cfg.state.scratch))
        ident = slot_identity(row.slot)
        launch = self._launch(session_id, row.session_file, sp)
        driver = PiSession(launch, ident, cwd=sp.workdir)
        try:
            await asyncio.wait_for(driver.start(), 20)
            try:
                return await asyncio.wait_for(_probe_command(driver, which), 20)
            finally:
                # stdin EOF: ~2 s, no model call
                await driver.shutdown(eof_grace=2, term_grace=2)
                await driver.close()
        except asyncio.TimeoutError:
            try:
                await driver.shutdown(eof_grace=1, term_grace=1)
                await driver.close()
            except Exception:
                pass
            raise

    # ------------------------------------------------------------------ #
    # idle-kill                                                           #
    # ------------------------------------------------------------------ #

    async def idle_kill_tick(self) -> list[str]:
        """1 h (default), settled-only, ladder, NEVER unmounts. A run in
        progress — however long — is never eligible: the kill fires only
        after agent_settled. A session with no process is nothing to kill
        (mounts survive; the next pickup respawns)."""
        if self.shutting_down:
            return []
        now = time.time()
        killed: list[str] = []
        for session_id, live in list(self.live.items()):
            d = live.driver
            if d is None or d.exited or d.exit_code is not None:
                continue
            if live.subscribers > 0:
                continue  # a human is watching: never idle-kill
            try:
                st = await d.get_state()
            except Exception:
                continue  # a dead/odd process is handled by release, not here
            if st.get("isStreaming") or st.get("isCompacting"):
                continue
            if st.get("pendingMessageCount", 0) > 0:
                continue
            if live.last_settled is None:
                continue  # never settled: not "truly idle"
            if now - live.last_settled < self.cfg.idle_kill_sec:
                continue
            code = await d.shutdown()  # the ladder: EOF → TERM → KILL
            self.cfg.registry.touch(session_id)
            self.events.add(session_id, "idle_killed", f"exit={code}; mounts kept")
            killed.append(session_id)
        return killed

    # ------------------------------------------------------------------ #
    # shutdown: abort-then-drain                                          #
    # ------------------------------------------------------------------ #

    async def shutdown(self, cause: str = "app stop") -> None:
        """Abort-then-drain (PLAN §5). Order is load-bearing:
        1. stop GC/pressure triggers, refuse new prompts;
        2. send RPC `abort` with recorded cause to every run-active child
           WHILE ITS STDIN IS STILL OPEN (commands travel only over stdin);
        3. mark the session `interrupted`;
        4. only then close stdin (orderly shutdown);
        5. bounded grace (strictly < TimeoutStopSec); survivors finished by
           the ladder (SIGTERM → SIGKILL; SIGKILL never first).
        Session files and mounts survive; next pickup respawns."""
        self.shutting_down = True
        if self._gc_task is not None:
            self._gc_task.cancel()
            self._gc_task = None
        grace = self.cfg.shutdown_grace_sec
        for session_id, live in list(self.live.items()):
            d = live.driver
            if d is None or d.exited:
                continue
            try:
                st = await d.get_state()
                run_active = bool(
                    st.get("isStreaming") or st.get("isCompacting")
                    or st.get("pendingMessageCount", 0) > 0
                )
            except Exception:
                run_active = False
            if run_active:
                try:
                    await d.abort()
                except Exception as e:
                    log.warning("abort to %s failed: %s", session_id, e)
                self.cfg.registry.mark_interrupted(session_id)
                self.events.add(session_id, "interrupted", cause)
            # only now: close stdin (the ladder with a bounded grace)
            try:
                code = await d.shutdown(eof_grace=grace, term_grace=min(10.0, grace))
                self.events.add(session_id, "drained", f"exit={code}")
            except Exception as e:
                log.warning("drain of %s failed: %s", session_id, e)
            try:
                await d.close()
            except Exception:
                pass
            live.driver = None
        self.cfg.registry.close()

    # ------------------------------------------------------------------ #
    # GC + disk pressure (in-process, through release())                  #
    # ------------------------------------------------------------------ #

    def start_gc(self) -> None:
        self._gc_task = asyncio.get_running_loop().create_task(self._gc_loop())

    async def _gc_loop(self) -> None:
        while True:
            await asyncio.sleep(self.cfg.gc_interval_sec)
            try:
                await self.gc_tick()
            except Exception:
                log.exception("gc tick failed")

    async def gc_tick(self) -> list[str]:
        """Releases sessions idle > gc_idle_days (not stable, not
        running); disk pressure (scratch fs near full) releases the
        oldest non-stable until headroom. Slot pressure is handled at
        allocation (start_session)."""
        if self.shutting_down:
            return []
        released: list[str] = []
        # idle GC
        cutoff = time.time() - self.cfg.gc_idle_days * 86400
        for row in self.cfg.registry.list_sessions():
            if row.state != LIVE or row.stable:
                continue
            if self._is_running(row.id):
                continue
            if row.last_activity > cutoff:
                continue
            res = await self.release(row.id, reason="idle GC")
            if res.released:
                released.append(row.id)
        # disk pressure
        if await asyncio.to_thread(self._disk_near_full):
            for row in sorted(
                (r for r in self.cfg.registry.list_sessions()
                 if r.state == LIVE and not r.stable and not self._is_running(r.id)),
                key=lambda r: r.last_activity,
            ):
                if not await asyncio.to_thread(self._disk_near_full):
                    break
                res = await self.release(row.id, reason="disk pressure")
                if res.released:
                    released.append(row.id)
        # delivery GC: orphan blobs no manifest references (bounded)
        if self.cfg.delivery is not None:
            manifests = []
            for row in self.cfg.registry.list_sessions():
                try:
                    manifests.append(
                        self.cfg.delivery.files(self.cfg.state, row.id, None).read_manifest()
                    )
                except Exception:
                    pass
            self.cfg.delivery.gc_blobs(manifests)
        return released

    def _is_running(self, session_id: str) -> bool:
        live = self.live.get(session_id)
        if live is None or live.driver is None or live.driver.exited:
            return False
        # conservatively: a live process counts as running unless we know
        # it is settled (a fresh app cannot see in-flight state it did not
        # start — the proc is the fact)
        return True

    async def ensure_alive(self, session_id: str) -> dict:
        """Panel action `start`: make sure the session's process is up.

        Live process ⇒ no-op. Dead process with a transcript ⇒ respawn
        (the stage-2 pickup path). No transcript ⇒ the session is simply
        idle (nothing to start — not an error). Never starts a run: the
        panel cannot inject a prompt it does not have."""
        row = self.cfg.registry.get(session_id)
        if row is None:
            raise KeyError(session_id)
        live = self.live.get(session_id)
        if live is not None and live.driver is not None and not live.driver.exited:
            return {"alive": True, "respawned": False}
        if not row.session_file:
            return {"alive": False, "respawned": False, "idle_no_transcript": True}
        await self._respawn(session_id)
        return {"alive": True, "respawned": True}

    # ------------------------------------------------------------------ #
    # transcript fetch (stage 4: reattach on load — fetch, never replay)
    # ------------------------------------------------------------------ #

    def _live_driver(self, session_id: str):
        live = self.live.get(session_id)
        if live is not None and live.driver is not None and not live.driver.exited:
            return live.driver
        return None

    def _transcript_entries(self, session_id: str) -> list[dict[str, Any]]:
        row = self.cfg.registry.get(session_id)
        if row is None or not row.session_file:
            return []
        return read_entries(row.session_file)

    @staticmethod
    def _branch_messages(branch: list[dict[str, Any]]) -> list[dict[str, Any]]:
        out = []
        for e in branch:
            m = entry_to_message(e)
            if m is not None:
                out.append(m)
        return out

    async def fetch_messages(self, session_id: str) -> dict[str, Any]:
        """Authoritative active-branch messages (UI reattach base).

        Live process: pi RPC get_entries (pi's leaf is authoritative).
        Dead process (idle-killed, crashed, app restarted): the session
        file on disk — the same truth. Either way: {leaf_id, messages}."""
        row = self.cfg.registry.get(session_id)
        if row is None:
            raise KeyError(session_id)
        driver = self._live_driver(session_id)
        # cursor FIRST: it is the hub position before the (awaiting)
        # entries read; a concurrent event in between duplicates an
        # entry the snapshot already contains — the client dedupes by
        # entry id. The reverse order would silently skip the tail.
        cursor = self.hub(session_id).latest_id
        if driver is not None:
            try:
                await self._live_sync_file(session_id, driver)
                data = await driver.get_entries()
                entries = list(data.get("entries", []))
                leaf_id = data.get("leafId")
            except Exception:
                driver = None  # died mid-fetch: disk fallback
        if driver is not None:
            by_id = {e["id"]: e for e in entries}
            chain: list[dict[str, Any]] = []
            seen: set[str] = set()
            cur: dict[str, Any] | None = by_id.get(leaf_id) if leaf_id else None
            while cur is not None and cur["id"] not in seen:
                seen.add(cur["id"])
                chain.append(cur)
                pid = cur.get("parentId")
                cur = by_id.get(pid) if pid else None
            return {
                "leaf_id": leaf_id,
                "cursor": cursor,
                "messages": self._branch_messages(list(reversed(chain))),
            }
        branch = active_branch(self._transcript_entries(session_id))
        return {
            "leaf_id": branch[-1]["id"] if branch else None,
            "cursor": self.hub(session_id).latest_id,
            "messages": self._branch_messages(branch),
        }

    async def fetch_tree(self, session_id: str) -> dict[str, Any]:
        """The full branch picture (read-only): every session FILE in the
        session dir as one forest. pi's fork creates a new file and the
        process switches to it; the originals stay — the session's tree
        is the union of all of them. Uniform across live/dead (the files
        are written as events happen)."""
        row = self.cfg.registry.get(session_id)
        if row is None:
            raise KeyError(session_id)
        roots, leaf_id, files = build_forest(
            self.cfg.state.sessions_for(session_id)
        )
        return {"roots": roots, "leaf_id": leaf_id, "files": files}

    async def _sync_session_file(self, session_id: str, driver) -> None:
        """After fork/switch the process's sessionFile changes (fork
        creates a NEW file — but pi creates it lazily, on the next write,
        so it may not exist yet). The registry follows it when it exists;
        a fetch-time sync picks it up otherwise. The registry must never
        point at a missing file (the probe/respawn/disk paths trust it)."""
        try:
            st = await driver.get_state()
            new_file = st.get("sessionFile")
            if new_file and os.path.exists(new_file):
                row = self.cfg.registry.get(session_id)
                if row is not None and row.session_file != new_file:
                    self.cfg.registry.set_session_file(session_id, new_file)
        except Exception:
            log.warning("could not sync sessionFile after fork/switch", exc_info=True)

    async def _live_sync_file(self, session_id: str, driver):
        """Fetch-time sessionFile sync: the fork's new file appears on the
        first write after the fork; pick it up on any live fetch."""
        try:
            st = await driver.get_state()
            new_file = st.get("sessionFile")
            if new_file and os.path.exists(new_file):
                row = self.cfg.registry.get(session_id)
                if row is not None and row.session_file != new_file:
                    self.cfg.registry.set_session_file(session_id, new_file)
                    return new_file
        except Exception:
            pass
        row = self.cfg.registry.get(session_id)
        return row.session_file if row else None

    async def fetch_fork_messages(self, session_id: str) -> list[dict[str, Any]]:
        """Fork points: user messages on the active branch
        ({entryId, text}) — the \"edit that message\" list."""
        row = self.cfg.registry.get(session_id)
        if row is None:
            raise KeyError(session_id)
        driver = self._live_driver(session_id)
        if driver is not None:
            try:
                return list(await driver.get_fork_messages())
            except Exception:
                pass  # disk fallback
        return forkable_messages(active_branch(self._transcript_entries(session_id)))

    async def fork(self, session_id: str, entry_id: str) -> dict[str, Any]:
        """Fork from a previous user message ("edit that message").
        Dead process ⇒ respawn first (ensure_alive), then the RPC.
        The active branch moves to the new fork; the UI re-fetches."""
        row = self.cfg.registry.get(session_id)
        if row is None:
            raise KeyError(session_id)
        await self.ensure_alive(session_id)
        driver = self._live_driver(session_id)
        if driver is None:
            raise RuntimeError("session has no transcript to fork from")
        out = await driver.fork(entry_id)
        await self._sync_session_file(session_id, driver)
        return out

    async def switch_session(self, session_id: str, session_path: str) -> bool:
        """Navigate the live process to another session file (advanced
        branch navigation; the fork slice's escape hatch)."""
        row = self.cfg.registry.get(session_id)
        if row is None:
            raise KeyError(session_id)
        await self.ensure_alive(session_id)
        driver = self._live_driver(session_id)
        if driver is None:
            raise RuntimeError("no live process to switch")
        ok = await driver.switch_session(session_path)
        if ok:
            await self._sync_session_file(session_id, driver)
        return ok

    def _disk_near_full(self) -> bool:
        try:
            st = os.statvfs(self.cfg.state.scratch)
        except OSError:
            return False
        total = st.f_blocks * st.f_frsize
        if total == 0:
            return False
        used = (st.f_blocks - st.f_bfree) * st.f_frsize
        return (used / total) >= DISK_PRESSURE_FRACTION

    async def _release_oldest_for_pressure(self) -> bool:
        """Slot pressure: release the oldest non-stable non-running
        session and take its slot. All 16 running ⇒ False (the new
        session waits)."""
        candidates = sorted(
            (
                r
                for r in self.cfg.registry.list_sessions()
                if r.state in (ALLOCATED, LIVE) and not r.stable and not self._is_running(r.id)
            ),
            key=lambda r: r.last_activity,
        )
        if not candidates:
            return False
        res = await self.release(candidates[0].id, reason="slot pressure")
        return res.released

    # ------------------------------------------------------------------ #
    # release                                                             #
    # ------------------------------------------------------------------ #

    async def release(self, session_id: str, *, reason: str = "user close") -> Any:
        row = self.cfg.registry.get(session_id)
        if row is None:
            raise KeyError(session_id)
        live = self.live.get(session_id)
        slot = self.cfg.pool.get(row.slot)
        # a session with a live run is NEVER released by pressure/GC —
        # the caller guarantees (skip and report); a user close releases
        # after the abort-then-drain semantics above (the driver is shut
        # down by release_session's ladder step)
        outcome = await release_session(
            session_id=session_id,
            slot=slot,
            state=self.cfg.state,
            pool=self.cfg.pool,
            hooks=self.hooks,
            driver=live.driver if live else None,
            mount_ops=live.mount_ops if live else None,
            reason=reason,
        )
        if live:
            if live.driver is not None:
                try:
                    await live.driver.close()
                except Exception:
                    pass
            self.live.pop(session_id, None)
        # delivery GC for the released session's inbox (no .incoming-* may
        # outlive the session)
        if self.cfg.delivery is not None and live is not None:
            try:
                self.cfg.delivery.files(self.cfg.state, session_id, None).gc_inbox_temps()
            except Exception:
                pass
        return outcome


# ---------------------------------------------------------------------- #
# helpers                                                                 #
# ---------------------------------------------------------------------- #

def _new_session_id() -> str:
    import uuid

    return str(uuid.uuid4())


def transcript_unsettled(path: str) -> bool:
    """The transcript's last run never completed (crash signature).

    A completed run ends with an assistant message carrying text content
    (the final answer). A crashed run ends with the abort trace: an
    assistant message with empty content (pi's stdin-EOF shutdown aborts
    the in-flight stream), a dangling assistant tool call, or a tool
    result with no following assistant message. An empty/no-message
    transcript is also unsettled (a run was in flight, nothing settled)."""
    import json as _json

    last_message: dict | None = None
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = _json.loads(line)
                except _json.JSONDecodeError:
                    continue  # a torn tail line: conservatively unsettled
                if rec.get("type") == "message":
                    last_message = rec
    except OSError:
        return False  # no readable transcript: nothing to judge
    if last_message is None:
        return True
    msg = last_message.get("message", {})
    if msg.get("role") != "assistant":
        return True  # dangling user prompt / tool result: the run did not finish
    content = msg.get("content") or []
    if isinstance(content, str):
        return not content.strip()
    has_text = any(
        isinstance(p, dict) and p.get("type") == "text" and str(p.get("text", "")).strip()
        for p in content
    )
    return not has_text


def _config_version_of(lower_dir: str) -> str:
    return Path(lower_dir).name


def _lower_for_version(config_root: str, version: str) -> str | None:
    p = Path(config_root) / version
    return str(p) if p.is_dir() else None


async def _probe_command(driver: PiSession, which: str) -> Any:
    """get_messages / get_session_stats / get_tree — no model call."""
    if which == "messages":
        return await driver.get_messages()
    if which == "stats":
        return await driver.get_session_stats()
    if which == "tree":
        return await driver.get_tree()
    raise ValueError(f"unknown probe {which}")
