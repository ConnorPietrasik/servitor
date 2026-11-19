"""Fork gate (root-free unit tests).

- A CANCELED fork (``session_before_fork`` extension returned
  ``{cancel: true}``; pi answers ``success`` with
  ``data: {text, cancelled: true}``) is reported as NOT forked: the
  HTTP endpoint answers ``forked: false, cancelled: true``, and the
  lifecycle skips the sessionFile sync — the process never left the
  original file, so nothing can be orphaned.
- The fork's abandoned file is retired ONLY once a write confirms the
  fork: ``prompt`` with an accepted disposition (anything but
  "handled") syncs + sweeps; "handled" (consumed, nothing appended)
  confirms nothing. ``retire_orphaned_files`` deletes exactly the
  ``*.jsonl`` files the registry no longer points at — the active file
  and non-transcript files are spared.
- An ABANDONED fork (never confirmed by a write) is reclaimed by the
  grace-bounded sweep (``gc_orphaned_files``, run from the GC tick)
  once the row's ``updated_at`` — bumped by the fork transition, and
  only ever lengthened by later row events — passes
  ORPHAN_IDLE_GRACE_SEC: fresh rows are kept, non-LIVE rows are
  skipped, and a live process's file is re-synced before the sweep.

No subprocess is spawned: Lifecycle is built with ``__new__`` (no
__init__ side effects) and the driver/lifecycle seams are shadowed as
instance attributes, per the test_session_waiters pattern; the HTTP
endpoint runs against ``build_app`` with a fake app state (no
lifespan — TestClient is used without a context).
"""
from __future__ import annotations

import sqlite3
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

from app import lifecycle as lifecycle_mod
from app.iso.layout import state_paths
from app.lifecycle import Lifecycle
from app.registry import ALLOCATED, FREE, LIVE, Registry

SID = "s1"


# --- fixtures (fakes) -------------------------------------------------- #


def _registry(tmp_path, session_file=None) -> Registry:
    reg = Registry(str(tmp_path / "registry.db"))
    reg.upsert_session(SID, display_name="t", slot="slot1", state=ALLOCATED)
    if session_file is not None:
        reg.set_session_file(SID, str(session_file))
    return reg


def _lc(tmp_path, reg) -> Lifecycle:
    """A Lifecycle with __init__ side effects skipped: registry + state
    are real (sqlite + path math), everything process-shaped is faked
    per test."""
    lc = Lifecycle.__new__(Lifecycle)
    lc.cfg = SimpleNamespace(registry=reg, state=state_paths(str(tmp_path)))
    lc.live = {}
    return lc


# --- canceled fork: no sync at the lifecycle --------------------------- #


async def test_fork_cancelled_skips_sync(tmp_path):
    lc = _lc(tmp_path, _registry(tmp_path))
    lc.ensure_alive = AsyncMock()
    driver = SimpleNamespace(
        fork=AsyncMock(return_value={"text": "hi", "cancelled": True}),
    )
    lc._live_driver = lambda _sid: driver
    lc._sync_session_file = AsyncMock()
    out = await lc.fork(SID, "entry-1")
    assert out == {"text": "hi", "cancelled": True}
    lc._sync_session_file.assert_not_awaited()


async def test_fork_ok_still_syncs(tmp_path):
    lc = _lc(tmp_path, _registry(tmp_path))
    lc.ensure_alive = AsyncMock()
    driver = SimpleNamespace(
        fork=AsyncMock(return_value={"text": "hi", "cancelled": False}),
    )
    lc._live_driver = lambda _sid: driver
    lc._sync_session_file = AsyncMock()
    out = await lc.fork(SID, "entry-1")
    assert out == {"text": "hi", "cancelled": False}
    lc._sync_session_file.assert_awaited_once_with(SID, driver)


# --- accepted write confirms the fork (retire gate) -------------------- #


async def test_prompt_accepted_confirms_fork(tmp_path):
    lc = _lc(tmp_path, _registry(tmp_path))
    lc.shutting_down = False
    driver = SimpleNamespace(
        exited=False, prompt=AsyncMock(return_value="started"),
    )
    lc.live = {SID: SimpleNamespace(driver=driver)}
    lc.retire_after_write = AsyncMock()
    d = await lc.prompt(SID, "hello")
    assert d == "started"
    lc.retire_after_write.assert_awaited_once_with(SID, driver)


async def test_prompt_handled_confirms_nothing(tmp_path):
    lc = _lc(tmp_path, _registry(tmp_path))
    lc.shutting_down = False
    driver = SimpleNamespace(
        exited=False, prompt=AsyncMock(return_value="handled"),
    )
    lc.live = {SID: SimpleNamespace(driver=driver)}
    lc.retire_after_write = AsyncMock()
    d = await lc.prompt(SID, "/a command")
    assert d == "handled"
    lc.retire_after_write.assert_not_awaited()


# --- retire_orphaned_files: sweep semantics ---------------------------- #


def test_retire_orphaned_files_keeps_only_the_active(tmp_path):
    sdir = tmp_path / "sessions" / SID
    sdir.mkdir(parents=True)
    active = sdir / "2026-01-01T00-00-00-000_aaaa.jsonl"
    orphan = sdir / "2026-01-02T00-00-00-000_bbbb.jsonl"
    outbox = sdir / "note.txt"  # a non-transcript file: never swept
    active.write_text('{"type": "session"}\n')
    orphan.write_text('{"type": "session"}\n')
    outbox.write_text("keep me")
    lc = _lc(tmp_path, _registry(tmp_path, session_file=active))
    lc.retire_orphaned_files(SID)
    assert active.exists(), "the registry's file is never swept"
    assert not orphan.exists(), "the orphaned transcript is deleted"
    assert outbox.exists(), "non-transcript files are spared"


def test_retire_orphaned_files_no_transcript_is_noop(tmp_path):
    lc = _lc(tmp_path, _registry(tmp_path))  # row without a session file
    lc.retire_orphaned_files(SID)  # nothing to do, no error
    lc.retire_orphaned_files("ghost")  # unknown session: nothing to do


def test_retire_orphaned_files_missing_dir_is_noop(tmp_path):
    # row points at a file whose dir is gone (released out from under
    # it): the sweep must not raise
    sdir = tmp_path / "sessions" / SID  # not created
    lc = _lc(tmp_path, _registry(tmp_path, session_file=sdir / "x.jsonl"))
    assert lc.retire_orphaned_files(SID) == 0


# --- grace sweep: abandoned (never-confirmed) forks -------------------- #


def _backdate(reg: Registry, sid: str, seconds: float) -> None:
    """Age a row's updated_at (WAL: a second connection reads the same
    committed data the Registry sees)."""
    db = sqlite3.connect(reg.path)
    try:
        db.execute("UPDATE sessions SET updated_at=? WHERE id=?",
                   (time.time() - seconds, sid))
        db.commit()
    finally:
        db.close()


def _sdir_with(tmp_path, active_name: str, extra: tuple[str, ...] = ()):
    sdir = tmp_path / "sessions" / SID
    sdir.mkdir(parents=True)
    (sdir / active_name).write_text('{"type": "session"}\n')
    for name in extra:
        (sdir / name).write_text('{"type": "session"}\n')
    return sdir


async def test_gc_orphans_sweeps_a_stale_abandoned_fork(tmp_path):
    sdir = _sdir_with(tmp_path, "a.jsonl", extra=("orphan.jsonl",))
    reg = _registry(tmp_path, session_file=sdir / "a.jsonl")
    reg.set_state(SID, LIVE, cause="test")
    _backdate(reg, SID, 90_000)  # 25 h > the 24 h grace
    lc = _lc(tmp_path, reg)
    swept = await lc.gc_orphaned_files()
    assert swept == 1
    assert (sdir / "a.jsonl").exists()
    assert not (sdir / "orphan.jsonl").exists()


async def test_gc_orphans_keeps_a_fresh_fork(tmp_path):
    sdir = _sdir_with(tmp_path, "a.jsonl", extra=("orphan.jsonl",))
    reg = _registry(tmp_path, session_file=sdir / "a.jsonl")
    reg.set_state(SID, LIVE, cause="test")
    # updated_at is fresh (set by the state flip): the recovery window
    # is still open — nothing may be deleted
    lc = _lc(tmp_path, reg)
    swept = await lc.gc_orphaned_files()
    assert swept == 0
    assert (sdir / "a.jsonl").exists()
    assert (sdir / "orphan.jsonl").exists()


async def test_gc_orphans_skips_non_live_rows(tmp_path):
    sdir = _sdir_with(tmp_path, "a.jsonl", extra=("orphan.jsonl",))
    reg = _registry(tmp_path, session_file=sdir / "a.jsonl")
    db = sqlite3.connect(reg.path)
    try:
        db.execute("UPDATE sessions SET state=? WHERE id=?", (FREE, SID))
        db.commit()
    finally:
        db.close()
    _backdate(reg, SID, 90_000)
    lc = _lc(tmp_path, reg)
    swept = await lc.gc_orphaned_files()
    assert swept == 0
    assert (sdir / "orphan.jsonl").exists()


async def test_gc_orphans_respects_the_grace_constant(tmp_path):
    sdir = _sdir_with(tmp_path, "a.jsonl", extra=("orphan.jsonl",))
    reg = _registry(tmp_path, session_file=sdir / "a.jsonl")
    reg.set_state(SID, LIVE, cause="test")
    _backdate(reg, SID, 90_000)
    lc = _lc(tmp_path, reg)
    # a grace longer than the row's age keeps the orphan
    lifecycle_mod.ORPHAN_IDLE_GRACE_SEC = 200_000
    try:
        assert await lc.gc_orphaned_files() == 0
        assert (sdir / "orphan.jsonl").exists()
        # ... and a shorter one sweeps it
        lifecycle_mod.ORPHAN_IDLE_GRACE_SEC = 1_000
        assert await lc.gc_orphaned_files() == 1
        assert not (sdir / "orphan.jsonl").exists()
    finally:
        lifecycle_mod.ORPHAN_IDLE_GRACE_SEC = 86_400.0


async def test_gc_orphans_syncs_a_live_driver_before_sweeping(tmp_path):
    # the registry still points at a.jsonl, but the live process sits
    # on b.jsonl (a lazy fork file that has just appeared): the sweep
    # must flip the registry to b FIRST and never delete the file the
    # process is writing
    sdir = _sdir_with(tmp_path, "a.jsonl", extra=("b.jsonl",))
    reg = _registry(tmp_path, session_file=sdir / "a.jsonl")
    reg.set_state(SID, LIVE, cause="test")
    _backdate(reg, SID, 90_000)
    lc = _lc(tmp_path, reg)
    driver = SimpleNamespace(
        exited=False,
        get_state=AsyncMock(
            return_value={"sessionFile": str(sdir / "b.jsonl")},
        ),
    )
    lc.live = {SID: SimpleNamespace(driver=driver)}
    swept = await lc.gc_orphaned_files()
    assert swept == 1
    assert not (sdir / "a.jsonl").exists(), "a is the true orphan now"
    assert (sdir / "b.jsonl").exists(), "the live process's file is spared"
    assert reg.get(SID).session_file == str(sdir / "b.jsonl")
    assert reg.get(SID).state == LIVE


# --- HTTP endpoint: canceled fork is reported as not forked ------------ #


def _client(fork_out):
    from fastapi.testclient import TestClient

    from app.api.app import build_app

    class _Row:
        state = "live"

    class _Registry:
        def get(self, sid):
            return _Row() if sid == SID else None

    app_state = SimpleNamespace(
        registry=_Registry(),
        lc=SimpleNamespace(live={}, fork=AsyncMock(return_value=fork_out)),
        boot_report=0,
    )
    app = build_app(app_state, SimpleNamespace(browser_origin=None))
    return TestClient(app)


def test_fork_endpoint_cancelled_reports_not_forked():
    c = _client({"text": "hi", "cancelled": True})
    r = c.post(f"/api/v1/sessions/{SID}/fork", json={"entry_id": "e1"})
    assert r.status_code == 200, r.text
    assert r.json() == {"forked": False, "cancelled": True, "text": "hi"}


def test_fork_endpoint_ok_reports_forked():
    c = _client({"text": "hi", "cancelled": False})
    r = c.post(f"/api/v1/sessions/{SID}/fork", json={"entry_id": "e1"})
    assert r.status_code == 200, r.text
    assert r.json() == {"forked": True, "cancelled": False, "text": "hi"}
