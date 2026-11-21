"""Session lifecycle: start/prompt/release, probes, idle-kill, shutdown
drain, GC and disk pressure. The registry is the only session truth.
No wall-clock/iteration/token caps."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import stat
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from app.iso import mounts as M
from app.env import optional_env
from app.iso.configswap import active_dir as config_active_dir
from app.iso.configswap import require_active_dir
from app.iso.layout import SessionMountPaths, StatePaths
from app.iso.slots import NoFreeSlot, SlotOutsidePool, SlotPool
from app.iso.spawn import slot_identity
from app.iso.sweep import kill_strays_by_uid
from app.iso.release import release_session
from app.api.sse import SseHub
from app.api.transcript import (
    active_branch,
    entry_to_message,
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

PROBE_CACHE_TTL = 30.0
PROBES = ("messages", "stats")

# orphan transcript GC grace: a fork is confirmed by the next write, so
# an abandoned fork's file lingers this long before the sweep takes it
ORPHAN_IDLE_GRACE_SEC = 86_400.0

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
    config_lower: Optional[str] = None  # the version this session booted from


class SessionNotResumable(RuntimeError):
    """The row is in a terminal state (release phases DRAINING/CLEANING,
    FREE, or a stuck quarantine): no route re-opens it, so respawn/
    pickup is refused before any side effect (stray kill, mounts,
    process). The API maps it to 409."""

    def __init__(self, session_id: str, state: str) -> None:
        self.session_id = session_id
        self.state = state
        super().__init__(
            f"{session_id}: state {state} is terminal — no route re-opens it"
        )


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
    delivery: "Delivery | None" = None  # delivery service (staging)
    sse_ring_bytes: int | None = None  # per-session SSE cap (None = 50 MB default)
    update_pending_path: str | None = None  # the update-pending flag file (sync/update write)
    update_tick_sec: float = 5.0
    restart_cmd: tuple[str, ...] | None = None  # default: systemctl restart servitor
    doctor_cmd: tuple[str, ...] | None = None  # default: own venv python -m app.doctor


class LifecycleHooks:
    """ReleaseHooks (app/iso/release.py) backed by the registry."""

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
            # slot quarantined in the pool; row stays DRAINING, never reused
            self.events.add(session_id, "slot_quarantined", detail)
            return
        self.reg.set_state(session_id, CLEANING, cause=detail)
        self.reg.set_state(session_id, FREE, cause="sweep verified empty")
        self.events.add(session_id, "released", detail)


class EventFeed:
    """Bounded in-memory event feed; app events are also published to the
    session's SseHub, the single per-session event source."""

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
        self._release_tasks: dict[str, asyncio.Task] = {}
        self.shutting_down = False
        self._gc_task: Optional[asyncio.Task] = None
        self._update_task: Optional[asyncio.Task] = None

    def hub(self, session_id: str) -> "SseHub | None":
        """The session's SSE hub (created on first use; respawns re-attach).

        Creation is refused for every non-ALLOCATED/LIVE row (release
        phases DRAINING/CLEANING, FREE, unknown): re-openable by no
        route, so a hub made for it would live for the process's life
        holding up to sse_ring_bytes. The DRAINING refusal matters for
        the quarantine case — a failed sweep leaves the row DRAINING
        forever, and a reconnecting client would otherwise mint a fresh
        50 MB hub that no drop site ever reaches. A session with no hub
        has no subscribers, so refusing loses nothing: release events of
        a never-spawned session land in the feed (the panel), and the
        client 404s on connect and stops. release() and _abort_start()
        are the drop sites; idle-kill never drops — respawn re-attaches
        to the same hub (Last-Event-ID resume). Lookup is first, so an
        existing hub is returned whatever the row says (in-flight
        releases keep delivering on their hub). ``"*"`` (broadcast app
        events) gets no hub either: no client can subscribe to an id that
        is not a registry row."""
        h = self._hubs.get(session_id)
        if h is not None:
            return h
        if session_id == "*":
            return None
        row = self.cfg.registry.get(session_id)
        if row is None or row.state not in (ALLOCATED, LIVE):
            return None
        h = SseHub(session_id, cap_bytes=self.cfg.sse_ring_bytes)
        self._hubs[session_id] = h
        return h

    def close_streams(self) -> None:
        """Close every session hub. Called at SIGTERM and (idempotently)
        from shutdown(): a streaming response never ends on its own and
        uvicorn does not cancel one, so it would hold the unit stop open
        to TimeoutStopSec. Reconnecting clients get 503 until restart."""
        for h in list(self._hubs.values()):
            try:
                h.close()
            except Exception:
                log.warning("hub close failed for %s", h.session_id,
                            exc_info=True)

    # ------------------------------------------------------------------ #
    # boot reconciliation                                                 #
    # ------------------------------------------------------------------ #

    def boot_reconcile(self) -> list[dict]:
        """Kill crash-orphans / mark interrupted sessions before any
        spawn/respawn. Crash signatures: (1) a live slot-uid process —
        killed via the ladder; (2) no process but the transcript's last
        run never settled. Transcripts and slots survive either way;
        the expectation is a respawn on pickup, not a release. A third
        signature is the RELEASE itself: a DRAINING/CLEANING row means
        a release was interrupted by the crash — no respawn ever comes
        for it, so its slot is quarantined, never handed out
        (invariant 8)."""
        out: list[dict] = []
        for slot in self.cfg.pool.slots.values():
            row = self.cfg.registry.session_for_slot(slot.name)
            if row is None or row.state == FREE:
                continue
            # a draining/cleaning row: its session is being released and
            # nothing will ever respawn it — a slot-uid process is an
            # orphan either way (the run-level 'interrupted' flag belongs
            # to respawnable rows only; the quarantine records this one)
            quarantining = row.state in (DRAINING, CLEANING)
            pids = self._pids_of_uid(slot.uid)
            killed = kill_strays_by_uid(slot.uid) if pids else []
            interrupted = False
            detail = ""
            if not quarantining:
                if killed:
                    interrupted = True
                    detail = f"orphan killed by boot reconciliation: {killed}"
                elif row.session_file and transcript_unsettled(row.session_file):
                    interrupted = True
                    detail = "crash signature: last run never settled (process died mid-run)"
            if interrupted:
                self.cfg.registry.mark_interrupted(row.id)
                self.events.add(row.id, "interrupted", detail)
            # rebuild the pool from the registry: a draining/cleaning row
            # means a release was interrupted by the crash — quarantine the
            # slot, never hand it out
            self._rebuild_pool_slot(slot, row)
            out.append({"slot": slot.name, "uid": slot.uid, "killed": killed, "session": row.id})
            if quarantining:
                log.warning(
                    "boot reconcile: slot %s session %s: release interrupted at boot (state %s) — slot quarantined%s",
                    slot.name, row.id, row.state,
                    f"; orphan killed: {killed}" if killed else "")
            elif killed or interrupted:
                log.warning("boot reconcile: slot %s session %s: %s", slot.name, row.id, detail or "killed")
        # any surviving staging dir is an interrupted upload
        if self.cfg.delivery is not None:
            self.cfg.delivery.gc_boot()
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
            # a release was interrupted by the crash: quarantine the slot,
            # never hand it out (invariant 8). The pool is rebuilt from
            # scratch at boot — every slot still has session_id None, so
            # pool.quarantine(row.id) could never find it via
            # by_session(): the session is re-attached here directly, the
            # way the allocated branch above re-attaches its row. The
            # fields mirror SlotPool.quarantine(): state quarantined,
            # stable False, session_id kept so the panel shows whose junk
            # is stuck where (the sweep's deletion list is the audit
            # trail). The row stays DRAINING/CLEANING; the human re-runs
            # release (quarantined -> cleaning is the RUNBOOK retry path,
            # and mark_cleaning accepts quarantined slots by design).
            if slot.state != "quarantined":
                slot.state = "quarantined"
                slot.session_id = row.id
                slot.stable = False
                self.events.add(row.id, "slot_quarantined", "release interrupted at boot")

    @staticmethod
    def _pids_of_uid(uid: int) -> list[int]:
        from app.iso.sweep import _pids_of_uid

        return _pids_of_uid(uid)

    # ------------------------------------------------------------------ #
    # extension recording (warn, never block)                               #
    # ------------------------------------------------------------------ #

    def _extension_names(self, sp: SessionMountPaths) -> tuple[str, ...]:
        return extension_names(sp, self.cfg.pi.ext_path)

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
            # the LLM key (env-only, invariant 5) — child env, never argv
            omnissiah_api_key=self.cfg.api_key,
            exa_api_key=self.cfg.exa_key,
            web_cache_root=sp.web_cache,
        )

    def _slot_name_for(self, session_id: str) -> str:
        row = self.cfg.registry.get(session_id)
        if row is None:
            raise ValueError(f"no registry row for {session_id}")
        return row.slot

    def _make_driver(self, session_id: str, session_file: str | None, live: Live) -> PiSession:
        launch = self._launch(session_id, session_file, live.sp)
        slot_name = self.cfg.registry.get(session_id).slot
        ident = slot_identity(slot_name)
        driver = PiSession(launch, ident, cwd=live.sp.workdir)
        # the second argument is the run's aborted flag: an ABORTED
        # settle must not clear the interrupted flag (the drain's mark
        # is owed to the operator — see _on_settled)
        driver.on_settled = (
            lambda _s, aborted, **k: self._on_settled(session_id, aborted))
        # event stream is the SSE hub; delivery never feeds back into the
        # run (invariant 4 — the hub evicts + gaps, it never blocks)
        hub = self.hub(session_id)
        if hub is None:
            # a spawn invariant violation: the row is ALLOCATED/LIVE here
            # by construction (start_session upserts it ALLOCATED,
            # _respawn refuses terminal rows) — refuse loudly rather than
            # spawn a process whose events go nowhere
            raise RuntimeError(
                f"{session_id}: no event hub at spawn (row missing or "
                f"not allocated/live)")
        driver.on_event = hub.on_event
        # extension dialogs park in the driver (the child blocks until a
        # human answers or aborts): surface them — feed + SSE stream
        driver.on_dialog = lambda _d, rec: self._on_dialog(session_id, rec)
        # pi-side resolution (timeout/abort) is swept at settle
        driver.on_dialog_resolved = lambda _d, rids: self._on_dialog_resolved(
            session_id, rids
        )
        return driver

    def _on_dialog(self, session_id: str, rec: dict) -> None:
        """An extension dialog just parked (select/confirm/input/editor).
        The run waits for the human — a designed, visible state: it lands
        in the event feed and the session's SSE stream (the UI re-fetches
        the full dialog via GET .../dialogs and answers via POST).
        Dialogs die with the process; there is nothing to clean up here."""
        method = str(rec.get("method", ""))
        rid = str(rec.get("id", ""))
        title = str(rec.get("title", ""))
        detail = f"{method} ({rid}): {title}" if title else f"{method} ({rid})"
        self.events.add(session_id, "extension_dialog", detail)
        log.info("session %s: extension dialog pending — %s", session_id, detail)

    def _on_dialog_resolved(self, session_id: str, rids: list[str]) -> None:
        """pi resolved parked dialogs on its own (its timeout, or an
        abort): the run's settle swept them. The UI re-fetches the
        pending set on the feed event — the dialogs are gone, and no
        answer is on the way. Nothing else to clean up: a dialog dies
        with its process."""
        for rid in rids:
            self.events.add(session_id, "extension_dialog_resolved", rid)
        log.info(
            "session %s: %d extension dialog(s) resolved by pi at settle: %s",
            session_id, len(rids), rids,
        )

    def _on_settled(self, session_id: str, aborted: bool = False) -> None:
        live = self.live.get(session_id)
        if live is not None:
            live.last_settled = time.time()
        if not aborted:
            row = self.cfg.registry.get(session_id)
            if row is not None and row.interrupted:
                # the flag means "the last run never COMPLETED" — a clean
                # settle refutes it, so clear it (the tests:
                # test/unit/test_settle_clears_interrupted.py). An ABORTED
                # settle must not clear it: the shutdown drain aborts a
                # live run and marks the session interrupted (the mark is
                # owed to the operator — the Sessions chip is the only
                # meter for a parked session, invariant 2), and the
                # child's agent_settled {aborted: true} then lands here —
                # clearing on it would race the drain's mark and lose
                # the flag. Without a clear at all it sticks for the
                # session's whole life: mark_interrupted is only ever
                # called with the default True (boot reconcile, shutdown
                # drain) — it would read 'interrupted' forever, past
                # respawns and clean runs.
                self.cfg.registry.mark_interrupted(session_id, False)
        self.cfg.registry.touch(session_id)

    # ------------------------------------------------------------------ #
    # start / prompt / respawn                                            #
    # ------------------------------------------------------------------ #

    async def start_session(
        self, display_name: str | None = None
    ) -> dict:
        """Allocate slot + mounts + first spawn. Under slot pressure,
        release the oldest non-stable non-running; if none is releasable
        the start is REFUSED (NoFreeSlot → 503) — there is no queue: a
        parked row has no end state (nothing could wake it, and with no
        slot it could not be released either)."""
        if self.shutting_down:
            raise RuntimeError("app is shutting down")
        session_id = _new_session_id()
        # the row exists before side effects
        slot = None
        try:
            slot = self.cfg.pool.allocate(session_id)
        except NoFreeSlot:
            if await self._release_oldest_for_pressure():
                slot = self.cfg.pool.allocate(session_id)
            else:
                # nothing releasable: refuse cleanly (the caller retries
                # later) — never park a row that can neither be woken
                # nor released
                raise NoFreeSlot(
                    "all slots are live sessions (running or awaiting "
                    "release) and none is releasable — try again later"
                )
        assert slot is not None
        row = self.cfg.registry.upsert_session(
            session_id,
            display_name=display_name,
            slot=slot.name,
            state=ALLOCATED,
        )

        # Everything from here on is under the abort guard: a failure at
        # ANY step (config missing, dir creation, mount — the tamper gate
        # included — spawn) must release the slot and delete the row, never
        # leak them. A leaked ALLOCATED row pins its slot for the life of
        # the process (boot reconciliation only runs at boot).
        sp = SessionMountPaths.for_session(session_id, str(self.cfg.state.scratch))
        live = Live(
            session_id=session_id, sp=sp, mount_ops=[], driver=None,
            config_version="", config_lower="",
        )
        self.live[session_id] = live
        try:
            lower = require_active_dir(str(self.cfg.state.config))
            version = _config_version_of(lower)
            live.config_version = version
            live.config_lower = lower
            await asyncio.to_thread(M.ensure_session_dirs, sp, self.cfg.state)
            live.mount_ops = await asyncio.to_thread(
                M.mount_session, sp, lower, self.cfg.state, slot.uid, slot.gid, self.cfg.toolkit_dir
            )
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
            return {"session_id": session_id, "slot": slot.name,
                    "session_file": session_file, "config_version": version}
        except Exception:
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
        self._drop_session_state(session_id)
        self.cfg.pool.release_slot_if_owned(session_id, slot)
        # no transcript was produced: not a session, the row goes
        self.cfg.registry.delete(session_id)

    async def prompt(self, session_id: str, text: str) -> str:
        """Steer/prompt with respawn-on-pickup. A transcript-less row is
        `idle, nothing to resume` — a disposition, not an error. An
        accepted prompt is a write on the active transcript: it confirms
        any pending fork, so the registry is re-synced and orphaned
        session files are retired ("handled" confirms nothing)."""
        if self.shutting_down:
            raise RuntimeError("app is shutting down; prompts refused")
        row = self.cfg.registry.get(session_id)
        if row is None:
            raise KeyError(session_id)
        live = self.live.get(session_id)
        if live is not None and live.driver is not None and not live.driver.exited:
            disposition = await live.driver.prompt(text)
        else:
            # respawn only when the registry says a transcript exists
            if not row.session_file:
                return "idle_no_transcript"
            await self._respawn(session_id)
            live = self.live[session_id]
            disposition = await live.driver.prompt(text)  # type: ignore[union-attr]
        if disposition != "handled":
            await self.retire_after_write(session_id, live.driver)  # type: ignore[union-attr]
        return disposition

    async def _respawn(self, session_id: str) -> None:
        row = self.cfg.registry.get(session_id)
        if row is None:
            raise KeyError(session_id)
        if row.state not in (ALLOCATED, LIVE):
            # terminal row (release phase, stuck quarantine): no route
            # re-opens it, and hub() refuses to mint its event hub —
            # refuse BEFORE any side effect (stray kill, mounts,
            # process); the API maps this to 409
            raise SessionNotResumable(session_id, row.state)
        if not row.session_file:
            raise ValueError(f"{session_id}: no transcript to respawn from")
        live = self.live.get(session_id)
        slot_name = row.slot
        ident = slot_identity(slot_name)

        # invariant 7: re-check the slot uid for a live process and kill
        # it first (a crash could have happened since boot)
        stray = self._pids_of_uid(ident.uid)
        if stray:
            kill_strays_by_uid(ident.uid)

        if live is None:
            sp = SessionMountPaths.for_session(session_id, str(self.cfg.state.scratch))
            # mounts survive the process: re-mount only if needed
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
            # fatal spawn error: release, never retry
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
        as the slot uid, one process per session file (concurrent probes
        coalesce), no model call, results briefly cached."""
        if which not in PROBES:
            raise ValueError(f"unknown probe {which!r}")
        row = self.cfg.registry.get(session_id)
        if row is None:
            raise KeyError(session_id)
        if row.state == FREE or not row.session_file or not os.path.exists(row.session_file):
            # the probe is never the first process to touch an agent dir
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
            # a terminal row (release phase, stuck quarantine) is never
            # dropped again by release(): caching for it would keep the
            # result to the process's life — nothing else evicts
            # _probe_cache
            if row.state in (ALLOCATED, LIVE):
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
        """1 h (default), settled-only, ladder, never unmounts. A run in
        progress is never eligible; a session with no process has nothing
        to kill (mounts survive; the next pickup respawns)."""
        if self.shutting_down:
            return []
        now = time.time()
        killed: list[str] = []
        for session_id, live in list(self.live.items()):
            d = live.driver
            if d is None or d.exited or d.exit_code is not None:
                continue
            hub = self._hubs.get(session_id)
            if hub is not None and hub.consumer_count > 0:
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
        """Abort-then-drain. Per-session order stays load-bearing: the
        abort travels over stdin BEFORE that child's stdin closes, and a
        session with a run in flight is marked `interrupted` — but the
        waits are shared: notify all (phase 1), close all stdins
        (phase 2), wait on everything under ONE budget (phase 3), then
        the ladder escalates for the survivors. Worst case stays under
        TimeoutStopSec regardless of N. Files and mounts survive."""
        self.shutting_down = True
        self.close_streams()  # idempotent if the stop path already did it at SIGTERM
        if self._gc_task is not None:
            self._gc_task.cancel()
            self._gc_task = None
        grace = self.cfg.shutdown_grace_sec
        term_grace = min(10.0, grace)
        probe_cap = min(10.0, grace)
        t0 = time.monotonic()

        # every live driver (a dead process is nothing to drain)
        drivers = [
            (session_id, live.driver)
            for session_id, live in list(self.live.items())
            if live.driver is not None and not live.driver.exited
        ]

        # phase 1 — notify: probe for a run in flight, abort over stdin
        # for every child with work, BEFORE its stdin closes. Gathered:
        # the phase costs the slowest child, never the sum.
        async def _notify(session_id: str, d) -> None:
            active = d.run_active
            try:
                st = await asyncio.wait_for(d.get_state(), timeout=probe_cap)
                active = active or bool(
                    st.get("isStreaming") or st.get("isCompacting")
                    or st.get("pendingMessageCount", 0) > 0
                )
            except Exception:
                pass  # probe failed: the local flag is all we have
            if active and not d.exited:
                try:
                    await asyncio.wait_for(d.abort(), timeout=probe_cap)
                except Exception as e:
                    log.warning("drain: abort to %s did not settle in "
                                "time: %s", session_id, e)
                self.cfg.registry.mark_interrupted(session_id)
                self.events.add(session_id, "interrupted", cause)

        for r in await asyncio.gather(
            *(_notify(session_id, d) for session_id, d in drivers),
            return_exceptions=True,
        ):
            if isinstance(r, Exception):
                log.warning("drain: notify step failed: %r", r)

        # phase 2 — close: stdin EOF on every child (rung 1); per-child
        # close is bounded inside close_stdin
        for r in await asyncio.gather(
            *(d.close_stdin() for _session_id, d in drivers),
            return_exceptions=True,
        ):
            if isinstance(r, Exception):
                log.warning("drain: stdin close failed: %r", r)

        # phase 3 — wait: ONE shared EOF budget for ALL children (clock
        # started at phase 1), then the ladder escalates: SIGTERM (rung 2),
        # SIGKILL (rung 3 — the rung that guarantees exit)
        def _survivors() -> list[tuple[str, Any]]:
            return [(sid, d) for sid, d in drivers if not d.exited]

        ended_by: dict[str, str] = {}  # session_id -> the rung that ended it
        alive = _survivors()
        remaining = max(0.0, t0 + grace - time.monotonic())
        if alive and remaining > 0:
            try:
                await asyncio.wait_for(
                    asyncio.gather(*(d.wait_exited() for _sid, d in alive)),
                    timeout=remaining,
                )
            except asyncio.TimeoutError:
                pass
        alive = _survivors()
        if alive:
            for _sid, d in alive:
                d.terminate()
            try:
                await asyncio.wait_for(
                    asyncio.gather(*(d.wait_exited() for _sid, d in alive)),
                    timeout=term_grace,
                )
            except asyncio.TimeoutError:
                pass
            for sid, d in alive:
                if d.exited:
                    ended_by[sid] = "SIGTERM"
        alive = _survivors()
        if alive:
            for _sid, d in alive:
                d.kill()
            # SIGKILL exits at once; bound the wait for the exit task
            try:
                await asyncio.wait_for(
                    asyncio.gather(*(d.wait_exited() for _sid, d in alive)),
                    timeout=5.0,
                )
            except asyncio.TimeoutError:
                pass
            for sid, d in alive:
                if d.exited:
                    ended_by[sid] = "SIGKILL"

        # bookkeeping: the drained event (tagged with the rung that ended
        # a stubborn child), reap the driver's tasks, hand the session
        # back to the respawn-on-pickup path (mounts and files survive)
        for session_id, live in list(self.live.items()):
            d = live.driver
            if d is None:
                continue
            rung = ended_by.get(session_id)
            suffix = f" ({rung})" if rung else ""
            self.events.add(session_id, "drained", f"exit={d.exit_code}{suffix}")
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
        if self._gc_task is None:
            self._gc_task = asyncio.get_running_loop().create_task(self._gc_loop())

    # ------------------------------------------------------------------ #
    # update-pending tick                                                    #
    # ------------------------------------------------------------------ #

    def any_run_active(self) -> list[str]:
        """Sessions with a run in flight."""
        return [
            sid
            for sid, live in self.live.items()
            if live.driver is not None
            and not live.driver.exited
            and live.driver.run_active
        ]

    def start_update_tick(self) -> None:
        """In-process tick: applies the pending restart + doctor at the
        first idle moment; defers (and flags the sessions) while a run
        is active."""
        if self._update_task is None:
            self._update_task = asyncio.get_running_loop().create_task(
                self._update_loop()
            )

    async def _update_loop(self) -> None:
        while True:
            try:
                await self.update_tick()
            except Exception:
                log.exception("update tick failed")
            await asyncio.sleep(self.cfg.update_tick_sec)

    async def update_tick(self) -> dict | None:
        """One pass over the flag. Returns the outcome (or None when the
        flag is absent — the normal steady state)."""
        if self.shutting_down:
            return None
        from app.update_pending import consume_restart, load

        path = self.cfg.update_pending_path
        if not path:
            return None
        flag = load(path)
        if flag is None:
            return None
        active = self.any_run_active()

        if not flag.doctor and not flag.restart:
            # degenerate (all-false) flag: a lingering FILE would wedge
            # sync's APPLIED gate and the doctor's update.waiting warn
            from app.update_pending import clear as _clear_flag
            _clear_flag(path)
            return {"no_op": True, "reason": flag.reason}

        if flag.doctor and not active:
            # a doctor run (the in-guest cert): applied inline for a
            # doctor-only flag, pre-restart cert when restart is staged
            result = await self._run_doctor()
            if not flag.restart:
                # Clear the FILE only on a PASSED cert: the file's
                # existence means "staged target pending" (sync's APPLIED
                # gate and update.waiting key on it), and "file gone" is
                # what sync's post-clear edge probe trusts. Kept on
                # failure: stays visible, this instance retries next tick.
                if result.get("ok") is True:
                    from app.update_pending import clear as _clear_flag
                    _clear_flag(path)
                    return {"doctor_applied": True, "rc": result.get("rc")}
                log.error(
                    "update doctor (cert) failed — flag kept for retry:\n%s",
                    (result.get("output") or "")[-2000:],
                )
                return {"doctor_failed": True, "rc": result.get("rc")}
            if result.get("ok") is False:
                # A pre-restart doctor FAIL must not hold the staged
                # restart (provisioning: the image ships keyless env
                # defaults, so the pre-restart doctor can never pass
                # until the restart injects the keys). The post-restart
                # tick re-runs the doctor on the new state.
                log.error(
                    "update doctor (pre-restart cert) failed — proceeding "
                    "with the staged restart anyway:\n%s",
                    (result.get("output") or "")[-2000:],
                )

        if flag.restart:
            if active:
                for sid in active:
                    self.cfg.registry.set_update_waiting(sid, True)
                log.info(
                    "update deferred (run active: %s) — flag survives, tick retries",
                    active,
                )
                return {"deferred": True, "active": active}
            # clear the per-session flags, then consume the restart bit
            # BEFORE restarting (crash-safe)
            for row in self.cfg.registry.list_sessions(include_released=False):
                if row.update_waiting:
                    self.cfg.registry.set_update_waiting(row.id, False)
            self.events.add("*", "update_applying", f"reason={flag.reason}")
            consume_restart(path)
            cmd = self.cfg.restart_cmd or ("systemctl", "restart", "servitor")
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
            out, _ = await proc.communicate()
            self.events.add("*", "update_restarted", f"cmd={' '.join(cmd)} rc={proc.returncode}")
            return {
                "restarted": True,
                "rc": proc.returncode,
                "output": out.decode(errors="replace")[-2000:],
            }

        return {"no_op": True, "reason": flag.reason}

    async def _run_doctor(self) -> dict:
        """Run the guest doctor in a subprocess (own venv on the guest,
        test override elsewhere). Bounded: 600 s subprocess timeout.
        Inherits the service environment (the 0600 env file)."""
        import sys

        cmd = self.cfg.doctor_cmd
        if cmd is None:
            active = config_active_dir(self.cfg.state.config)
            parts: list[str] = [
                sys.executable,
                "-m",
                "app.doctor",
                "--state",
                self.cfg.state.root,
                "--pi-node",
                self.cfg.pi.node_bin,
                "--pi-cli",
                self.cfg.pi.cli_js,
                "--toolkit",
                self.cfg.toolkit_dir,
            ]
            if active:
                parts += ["--config-src", active]
            if self.cfg.pi.ext_path:
                parts += ["--pi-ext", self.cfg.pi.ext_path]
            cmd = tuple(parts)
        log.info("update doctor run: %s", " ".join(str(c) for c in cmd))
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                cwd=optional_env("SERVITOR_DOCTOR_CWD") or ".",
            )
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=600)
        except (asyncio.TimeoutError, OSError) as e:
            return {"ok": False, "error": str(e)}
        text = out.decode(errors="replace")
        ok = proc.returncode == 0
        log.info("update doctor rc=%s tail=%s", proc.returncode, text[-800:])
        return {"ok": ok, "rc": proc.returncode, "output": text[-4000:]}

    async def _gc_loop(self) -> None:
        while True:
            await asyncio.sleep(self.cfg.gc_interval_sec)
            try:
                await self.gc_tick()
            except Exception:
                log.exception("gc tick failed")

    async def gc_tick(self) -> list[str]:
        """Release sessions idle > gc_idle_days (not stable, not
        running); sweep abandoned fork transcripts past the orphan
        grace; disk pressure releases the oldest non-stable until
        headroom. Slot pressure is handled at allocation."""
        if self.shutting_down:
            return []
        released: list[str] = []
        # pool-ownership guard shared by both release passes below: a row
        # on a slot outside --slots (shrunk/renamed under it) is never
        # releasable from here — Lifecycle.release refuses it with
        # SlotOutsidePool, which would raise out of gc_tick and skip the
        # disk-pressure section on every tick (the volume's automatic
        # protection, lost until the row is hand-fixed)
        owned = set(self.cfg.pool.slots)
        # orphan sweep first: the reclaimed space may be the headroom
        await self.gc_orphaned_files()
        # idle GC
        cutoff = time.time() - self.cfg.gc_idle_days * 86400
        for row in self.cfg.registry.list_sessions():
            if row.state != LIVE or row.stable:
                continue
            if row.slot not in owned:
                continue
            if self._is_running(row.id):
                continue
            if row.last_activity > cutoff:
                continue
            res = await self.release(row.id, reason="idle GC")
            if res.released:
                released.append(row.id)
        # disk pressure (same pool-ownership guard as slot pressure)
        if await asyncio.to_thread(self._disk_near_full):
            for row in sorted(
                (r for r in self.cfg.registry.list_sessions()
                 if r.state == LIVE and r.slot in owned and not r.stable and not self._is_running(r.id)),
                key=lambda r: r.last_activity,
            ):
                if not await asyncio.to_thread(self._disk_near_full):
                    break
                res = await self.release(row.id, reason="disk pressure")
                if res.released:
                    released.append(row.id)
        return released

    def _is_running(self, session_id: str) -> bool:
        live = self.live.get(session_id)
        if live is None or live.driver is None or live.driver.exited:
            return False
        # a live process counts as running unless we know it is settled
        return True

    async def ensure_alive(self, session_id: str) -> dict:
        """Panel action `start`: make sure the session's process is up.
        Live ⇒ no-op; dead with a transcript ⇒ respawn; no transcript ⇒
        idle (nothing to start). Never starts a run."""
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
    # transcript fetch (reattach on load — fetch, never replay)
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
        """Authoritative active-branch messages (UI reattach base):
        pi RPC get_entries with a live process, the session file on
        disk otherwise. Returns {leaf_id, cursor, messages}."""
        row = self.cfg.registry.get(session_id)
        if row is None:
            raise KeyError(session_id)
        driver = self._live_driver(session_id)
        # cursor FIRST: it is the hub position before the (awaiting)
        # entries read; a concurrent event in between duplicates an entry
        # the snapshot already contains (the client dedupes by id), while
        # the reverse order would silently skip the tail. A session with
        # no hub (never spawned, or already released) has no ring: 0.
        hub = self.hub(session_id)
        cursor = hub.latest_id if hub is not None else 0
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
        hub = self.hub(session_id)
        return {
            "leaf_id": branch[-1]["id"] if branch else None,
            "cursor": hub.latest_id if hub is not None else 0,
            "messages": self._branch_messages(branch),
        }

    async def retire_after_write(self, session_id: str, driver) -> None:
        """After an accepted write on the live process: re-sync the
        registry to the process's file and retire the orphaned session
        files (the accepted write is what confirms a fork — until it
        lands the abandoned file stays put)."""
        await self._live_sync_file(session_id, driver)
        self.retire_orphaned_files(session_id)

    def retire_orphaned_files(self, session_id: str) -> int:
        """Delete the session files the registry no longer points at
        (orphaned by a fork). Call only after a write extended the
        active file: an unconfirmed orphan is still recoverable and is
        reclaimed by the grace-bounded sweep anyway. Best-effort: a
        failure leaves the file — deletion is a space optimization,
        never a correctness gate. Returns the number deleted."""
        row = self.cfg.registry.get(session_id)
        if row is None or not row.session_file:
            return 0
        sdir = self.cfg.state.sessions_for(session_id)
        active = os.path.basename(row.session_file)
        try:
            names = os.listdir(sdir)
        except OSError:
            return 0
        swept = 0
        for name in names:
            if name == active or not name.endswith(".jsonl"):
                continue
            try:
                os.unlink(os.path.join(sdir, name))
                swept += 1
            except OSError:
                log.warning("could not delete orphaned session file %s",
                            os.path.join(sdir, name), exc_info=True)
        return swept

    async def gc_orphaned_files(self) -> int:
        """Sweep orphaned transcripts in sessions past the grace
        (ORPHAN_IDLE_GRACE_SEC): forks never confirmed by a write.
        Safe with a live process: re-sync first so the sweep never
        touches the file being written. Returns the number deleted."""
        cutoff = time.time() - ORPHAN_IDLE_GRACE_SEC
        swept = 0
        for row in self.cfg.registry.list_sessions():
            if row.state != LIVE or row.updated_at > cutoff:
                continue
            driver = self._live_driver(row.id)
            if driver is not None:
                # a live process may sit on a file the registry has not
                # flipped to yet (lazy fork file): sync first
                await self._live_sync_file(row.id, driver)
            swept += self.retire_orphaned_files(row.id)
        if swept:
            log.info("orphan sweep: deleted %d abandoned session file(s)",
                     swept)
        return swept

    async def _sync_session_file(self, session_id: str, driver) -> None:
        """Follow the process's sessionFile after a fork (pi creates the
        new file lazily, on the next write). The abandoned file is NOT
        deleted here — retirement waits for the next accepted write, so
        an unconfirmed fork stays recoverable. The registry must never
        point at a missing file (the probe/respawn/disk paths trust it)."""
        try:
            st = await driver.get_state()
            new_file = st.get("sessionFile")
            if new_file and os.path.exists(new_file):
                row = self.cfg.registry.get(session_id)
                if row is not None and row.session_file != new_file:
                    self.cfg.registry.set_session_file(session_id, new_file)
        except Exception:
            log.warning("could not sync sessionFile after fork", exc_info=True)

    async def _live_sync_file(self, session_id: str, driver):
        """Fetch-time sessionFile sync: pick up the fork's new file on
        any live fetch (and after any accepted write)."""
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

    async def fork(self, session_id: str, entry_id: str) -> dict[str, Any]:
        """Fork from a previous user message ("edit that message").
        Dead process ⇒ respawn first, then the RPC. An extension-
        canceled fork passes ``cancelled`` through: nothing is synced
        (nothing can be orphaned)."""
        row = self.cfg.registry.get(session_id)
        if row is None:
            raise KeyError(session_id)
        await self.ensure_alive(session_id)
        driver = self._live_driver(session_id)
        if driver is None:
            raise RuntimeError("session has no transcript to fork from")
        out = await driver.fork(entry_id)
        if not out.get("cancelled"):
            await self._sync_session_file(session_id, driver)
        return out

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
        session. All running ⇒ False (the new session's start is
        refused). Rows on slots outside this app's pool are never
        releaseable here."""
        owned = set(self.cfg.pool.slots)
        candidates = sorted(
            (
                r
                for r in self.cfg.registry.list_sessions()
                if r.state in (ALLOCATED, LIVE) and r.slot in owned
                and not r.stable and not self._is_running(r.id)
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

    def _drop_session_state(self, session_id: str) -> None:
        """Drop the session's in-memory state at a TERMINAL transition
        (release, aborted start). The hub's ring can hold up to
        sse_ring_bytes (default 50 MB) and _probe_cache holds probe
        results — without this, _hubs grows one entry per session for
        the life of the process: the 50 MB cap is per-LIVE-session, not
        per-session-since-boot. The hub is closed, not just popped, so
        in-flight streams end now (the client re-fetches, gets 404, and
        stops retrying) and close_streams' SIGTERM contract still holds.
        Never call this from idle-kill: the row survives, pickup
        respawns, and clients re-attach to the same hub."""
        hub = self._hubs.pop(session_id, None)
        if hub is not None:
            hub.close()
        for which in PROBES:
            self._probe_cache.pop((session_id, which), None)

    async def release(self, session_id: str, *, reason: str = "user close") -> Any:
        """Concurrent-release idempotency in front of the single cleanup
        path: a second release for a session whose release is already in
        flight (a panel double-tap, a user close racing gc_tick or the
        slot/disk pressure release — all on one loop) does NOT re-enter
        release_session; it shares the in-flight task and gets the same
        outcome. Pre-fix the re-entry ran release_session's first step,
        pool.mark_cleaning, on a slot already 'cleaning' (release_session
        marks it before suspending at its first await) and raised
        SlotQuarantined, which propagated through Lifecycle.release and
        the /release and /panel action routes (neither catches it) to the
        500 handler: a spurious error banner on a normal double-tap. The
        shared task is shielded: one caller disconnecting — or a GC tick
        being cancelled — never cancels the cleanup the other callers
        are waiting for."""
        task = self._release_tasks.get(session_id)
        if task is not None and not task.done():
            return await asyncio.shield(task)
        task = asyncio.get_running_loop().create_task(
            self._release(session_id, reason=reason)
        )
        self._release_tasks[session_id] = task
        task.add_done_callback(lambda t: self._release_task_done(session_id, t))
        return await asyncio.shield(task)

    def _release_task_done(self, session_id: str, task: "asyncio.Task") -> None:
        """Drop a finished in-flight release entry. The identity check
        matters: a FRESH release may have registered a newer task before
        this callback ran (the old task finished between its last await
        and this loop iteration). A failed task whose callers are all
        gone would otherwise surface only as 'exception was never
        retrieved' at GC — log it here instead; the slot is left
        'cleaning' and the row DRAINING, which boot reconciliation and
        the quarantine retry own."""
        if self._release_tasks.get(session_id) is task:
            del self._release_tasks[session_id]
        if not task.cancelled():
            exc = task.exception()
            if exc is not None:
                log.error("release %s failed with no waiting caller: %r",
                          session_id, exc)

    async def _release(self, session_id: str, *, reason: str = "user close") -> Any:
        row = self.cfg.registry.get(session_id)
        if row is None:
            raise KeyError(session_id)
        if row.slot not in self.cfg.pool.slots:
            # --slots was shrunk/renamed under an existing row: pool.get
            # would KeyError out of EVERY release path (idle GC tick,
            # POST /release, panel action — 500s, gc_tick aborted forever).
            # Refuse with the typed error the API maps to 4xx instead; the
            # row is only fixable by restoring the slot name or editing the
            # registry (doctor: slots.off_pool_rows flags it).
            raise SlotOutsidePool(session_id, row.slot)
        live = self.live.get(session_id)
        slot = self.cfg.pool.get(row.slot)
        # a session with a live run is never released by pressure/GC —
        # the caller guarantees; a user close releases after abort-then-
        # drain (the driver is shut down by release_session's ladder)
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
        # after release_session's events (release_start, released) have
        # landed in the ring: dropping the hub is the terminal move
        self._drop_session_state(session_id)
        return outcome


# ---------------------------------------------------------------------- #
# helpers                                                                 #
# ---------------------------------------------------------------------- #


def extension_names(sp: SessionMountPaths, ext_path: str | None) -> tuple[str, ...]:
    """Loaded extension set as the app sees it before spawn: the
    explicit -e paths plus the agent dir's extensions dir (one level
    deep, pi's discovery rule). Shared by the registry recorder and the
    doctor's standalone probe (no registry row)."""
    names: list[str] = []
    if ext_path:
        names.append(Path(ext_path).name)
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


def _new_session_id() -> str:
    import uuid

    return str(uuid.uuid4())


def transcript_unsettled(path: str) -> bool:
    """The transcript's last run never completed (crash signature): an
    empty assistant message, a dangling tool call, or a tool result with
    no following assistant message (pi's stdin-EOF shutdown aborts the
    in-flight run). An empty transcript is also unsettled."""
    import json as _json

    last_message: dict | None = None
    try:
        # the session dir is slot-owned: a planted link must not make the
        # root app read a root-readable target at boot. A tampered
        # transcript is judged UNSETTLED: the session gets marked
        # interrupted and lands in the operator's view.
        if stat.S_ISLNK(os.lstat(path).st_mode):
            return True
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = _json.loads(line)
                except _json.JSONDecodeError:
                    continue  # torn tail line: conservatively unsettled
                if rec.get("type") == "message":
                    last_message = rec
    except OSError:
        return False  # no readable transcript: nothing to judge
    if last_message is None:
        return True
    msg = last_message.get("message", {})
    if msg.get("role") != "assistant":
        return True  # dangling user prompt / tool result
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
    """get_messages / get_session_stats — no model call."""
    if which == "messages":
        return await driver.get_messages()
    if which == "stats":
        return await driver.get_session_stats()
    raise ValueError(f"unknown probe {which}")
