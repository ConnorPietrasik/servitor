"""Update-pending gate (root-free unit tests).

- A doctor-only flag is cleared ONLY by a PASSED cert: a failed doctor
  keeps the flag (visible — panel update_waiting, doctor update.waiting
  warn — and retried by the SAME instance on its next tick: a
  doctor-only flag stages no restart, so there is no post-restart
  instance; doctor runs are sequential, each bounded by the doctor's
  600 s subprocess timeout). "File gone" is the fact sync's post-clear edge
  probe trusts: it must imply an instance's cert passed against a live
  port. The 2026-11-19 false-negative sync ran on the old contract,
  where the stop-draining OLD instance re-ran the doctor against its
  own just-closed port and the unconditional clear let it eat the NEW
  instance's cert.
- A pre-restart doctor FAIL must not hold the staged restart
  (provisioning: the restart IS the fix).
- A degenerate all-false flag is cleared (a lingering file would wedge
  sync's APPLIED gate and the doctor's warn forever).
- A shutting-down instance does not act on the flag at all.
- The HTTP stop path (lifespan-installed SIGTERM handler):
  shutting_down FIRST, then close every SSE stream, only then
  uvicorn's should_exit — the streams end before uvicorn's 'waiting
  for connections' phase, so a connected browser's SSE stream can no
  longer ride the stop out to the unit's TimeoutStopSec SIGKILL.

Lifecycle is built with __new__ (no __init__ side effects); the
doctor/registry seams are real (sqlite + the real flag file with its
locking) and the doctor run itself is shadowed, per the
test_session_waiters / test_fork_gate pattern.
"""
from __future__ import annotations

import signal as signal_mod
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

from app.iso.layout import state_paths
from app.lifecycle import Lifecycle
from app.registry import Registry
from app.update_pending import UpdatePending, load, save

SID = "s1"


def _lc(tmp_path, flag_path) -> Lifecycle:
    """Lifecycle with __init__ skipped: real registry (sqlite) + flag
    file; the doctor run and the run-active check are shadowed per
    test."""
    lc = Lifecycle.__new__(Lifecycle)
    lc.cfg = SimpleNamespace(
        registry=Registry(str(tmp_path / "registry.db")),
        state=state_paths(str(tmp_path)),
        update_pending_path=str(flag_path),
        restart_cmd=("true",),
    )
    lc.shutting_down = False
    lc.events = SimpleNamespace(add=lambda *a, **k: None)
    lc.any_run_active = lambda: []
    return lc


def _flag(tmp_path) -> "object":
    p = tmp_path / "update-pending.json"
    save(p, UpdatePending(reason="test", ts=time.time()))
    return p


# --- doctor-only flag: clear on pass, keep on fail --------------------- #


async def test_doctor_only_flag_cleared_on_pass(tmp_path):
    flag = _flag(tmp_path)
    save(flag, UpdatePending(reason="test", restart=False, doctor=True,
                             ts=time.time()))
    lc = _lc(tmp_path, flag)
    lc._run_doctor = AsyncMock(return_value={"ok": True, "rc": 0,
                                             "output": "GREEN"})
    out = await lc.update_tick()
    assert out["doctor_applied"] is True
    assert load(flag) is None, "a passed cert must clear the flag file"


async def test_doctor_only_flag_kept_on_failure(tmp_path):
    flag = _flag(tmp_path)
    save(flag, UpdatePending(reason="test", restart=False, doctor=True,
                             ts=time.time()))
    lc = _lc(tmp_path, flag)
    lc._run_doctor = AsyncMock(return_value={"ok": False, "rc": 1,
                                             "output": "FAIL ui.index"})
    out = await lc.update_tick()
    assert out["doctor_failed"] is True
    f = load(flag)
    assert f is not None and f.doctor is True and f.restart is False, (
        "a failed cert must NOT consume the flag: the next tick — the "
        "post-restart instance's — retries it"
    )


# --- restart bit: the pre-restart cert never holds it ------------------ #


async def test_pre_restart_doctor_failure_does_not_hold_restart(tmp_path):
    flag = _flag(tmp_path)
    save(flag, UpdatePending(reason="test", restart=True, doctor=True,
                             ts=time.time()))
    lc = _lc(tmp_path, flag)
    lc._run_doctor = AsyncMock(return_value={"ok": False, "rc": 1,
                                             "output": "FAIL env"})
    out = await lc.update_tick()
    assert out.get("restarted") is True and out["rc"] == 0
    # consume_restart semantics: the doctor-only remainder stays for the
    # new instance
    f = load(flag)
    assert f is not None and f.doctor is True and f.restart is False


async def test_restart_with_passed_pre_doctor_still_restarts(tmp_path):
    flag = _flag(tmp_path)
    save(flag, UpdatePending(reason="test", restart=True, doctor=True,
                             ts=time.time()))
    lc = _lc(tmp_path, flag)
    lc._run_doctor = AsyncMock(return_value={"ok": True, "rc": 0,
                                             "output": "GREEN"})
    out = await lc.update_tick()
    assert out.get("restarted") is True and out["rc"] == 0
    f = load(flag)
    assert f is not None and f.doctor is True and f.restart is False


# --- degenerate / absent / dying states --------------------------------- #


async def test_all_false_flag_is_cleared(tmp_path):
    flag = _flag(tmp_path)
    save(flag, UpdatePending(reason="test", restart=False, doctor=False,
                             ts=time.time()))
    lc = _lc(tmp_path, flag)
    lc._run_doctor = AsyncMock()
    out = await lc.update_tick()
    assert out["no_op"] is True
    lc._run_doctor.assert_not_awaited()
    assert load(flag) is None, "an all-false flag must not wedge the gates"


async def test_absent_flag_is_noop(tmp_path):
    lc = _lc(tmp_path, tmp_path / "update-pending.json")
    lc._run_doctor = AsyncMock()
    assert await lc.update_tick() is None
    lc._run_doctor.assert_not_awaited()


async def test_shutting_down_instance_does_not_act_on_the_flag(tmp_path):
    # the 2026-11-19 mode: the stop-draining OLD instance's tick must be
    # inert — it must neither re-run the doctor nor touch the flag that
    # belongs to the NEW instance
    flag = _flag(tmp_path)
    save(flag, UpdatePending(reason="test", restart=False, doctor=True,
                             ts=time.time()))
    lc = _lc(tmp_path, flag)
    lc.shutting_down = True
    lc._run_doctor = AsyncMock()
    assert await lc.update_tick() is None
    assert load(flag) is not None
    lc._run_doctor.assert_not_awaited()


# --- the HTTP stop path (lifespan-installed SIGTERM handler) ------------ #


def test_http_stop_handler_wiring():
    from app.api.app import _install_http_stop_handler

    closed = []
    lc = SimpleNamespace(shutting_down=False,
                         close_streams=lambda: closed.append(1))
    server = SimpleNamespace(should_exit=False)
    app_state = SimpleNamespace(lc=lc, http_server=server)
    prev = signal_mod.getsignal(signal_mod.SIGTERM)
    try:
        _install_http_stop_handler(app_state)
        handler = signal_mod.getsignal(signal_mod.SIGTERM)
        assert callable(handler)
        handler(signal_mod.SIGTERM, None)
        assert lc.shutting_down is True, \
            "the tick/GC/prompt paths must stop acting first"
        assert closed == [1], \
            "every stream must end before uvicorn starts waiting"
        assert server.should_exit is True
    finally:
        signal_mod.signal(signal_mod.SIGTERM, prev)


def test_http_stop_handler_without_server_is_noop():
    # stdio front door: no uvicorn server, nothing to override
    from app.api.app import _install_http_stop_handler

    app_state = SimpleNamespace(lc=SimpleNamespace(), http_server=None)
    prev = signal_mod.getsignal(signal_mod.SIGTERM)
    _install_http_stop_handler(app_state)
    assert signal_mod.getsignal(signal_mod.SIGTERM) is prev
