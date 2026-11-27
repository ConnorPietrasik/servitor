"""Conformance: the failure modes the system must fail CLOSED on.

- **no-event failure / no-API-key repro:** an unresolvable credential
  must NOT leave a healthy-looking dead session. Either the spawn path
  fails closed (model.id == "unknown") or the prompt returns
  success:false with ZERO events and the idling process. The driver
  fails the run, never hangs, never retries (a dead credential is a
  config fault; retrying burns the slot). No request ever reaches the
  router.
- **Missing recorded workdir:** respawn with the stored cwd gone — pi
  exits 1 with NO response. The driver classifies it a fatal spawn
  error (route to release; never a retry loop).
"""
from __future__ import annotations

import asyncio
import os

import pytest

from app.rpc.session import ModelUnavailable, PromptRejected


async def test_t25_unset_key_is_terminal_not_a_hang(harness):
    """No OMNISSIAH_API_KEY in the spawn env: the system fails closed —
    spawn-level (unknown model) or prompt-level (success:false, zero
    events) — and NEVER presents a healthy idling session."""
    live = await harness.start_session(expect_startup=False, key="")
    driver = live.driver
    try:
        try:
            await driver.assert_model_usable()
        except ModelUnavailable as exc:
            # spawn path failed closed: model.id == "unknown"
            assert "unknown" in str(exc) or "no usable model" in str(exc)
            assert harness.router.requests() == []
            return  # gate satisfied at the spawn boundary
        # the model looked usable: the prompt must still fail terminal
        with pytest.raises(PromptRejected):
            await asyncio.wait_for(driver.prompt("hello?"), 30)
        # zero events: no agent_start, no turn_start, no agent_settled
        types = [e.get("type") for e in live.events]
        assert "agent_start" not in types
        assert "agent_settled" not in types
        # the process idles, still answering get_state (the trap): the
        # driver's fact (prompt rejected) is what ends the run
        st = await driver.get_state()
        assert st.get("isStreaming") in (False, None)
        assert harness.router.requests() == []  # nothing reached the router
    finally:
        await driver.shutdown(eof_grace=2, term_grace=2)
        await driver.close()
        await harness.cleanup_failed(live)  # harness seam


async def test_no_api_key_repro_regression(harness):
    """The named regression: the no-API-key repro, re-run as a
    distinct test so it can never be deleted with its sibling."""
    live = await harness.start_session(expect_startup=False, key="")
    driver = live.driver
    try:
        try:
            await driver.assert_model_usable()
        except ModelUnavailable:
            return
        with pytest.raises(PromptRejected):
            await asyncio.wait_for(driver.prompt("regression prompt"), 30)
        assert [e.get("type") for e in live.events] == [] or all(
            t not in ("agent_start", "agent_settled")
            for t in (e.get("type") for e in live.events)
        )
    finally:
        await driver.shutdown(eof_grace=2, term_grace=2)
        await driver.close()
        await harness.cleanup_failed(live)  # harness seam


async def test_missing_cwd_respawn_is_fatal_spawn_not_retry(harness):
    """A missing recorded workdir makes pi exit 1 with NO response. The
    driver must classify it a fatal spawn error — no response correlation
    ever happens, and there is no retry path in the driver (the caller
    routes to release)."""
    live = await harness.start_session()
    sid = live.session_id
    st = await live.driver.get_state()
    session_file = st["sessionFile"]
    assert session_file and "sessions" in session_file

    # the session is idling; idle-kill it (the respawn scenario starts
    # from a dead process), then unmount the workdir — NESTED inbox bind
    # first (a nested mount makes the parent EBUSY), then the overlay —
    # simulating the stored cwd being gone
    await live.driver.shutdown()
    await live.driver.close()

    workdir_op = [op for op in live.mount_ops if op.target == live.sp.workdir][0]
    inbox_op = [op for op in live.mount_ops if op.target == f"{live.sp.workdir}/inbox"][0]
    inbox_op.unmount()
    workdir_op.unmount()
    # unmounting leaves the mountpoint dir: remove it so the recorded cwd
    # is genuinely GONE (an empty surviving dir would let pi start)
    os.rmdir(live.sp.workdir)

    # respawn directly (driver level): the harness would remount, which is
    # exactly what the FATAL error exists to prevent
    from app.rpc.session import PiSession, SpawnFailure

    launch = harness.launch_args(session_id=sid, session_file=session_file)
    from app.iso.spawn import slot_identity

    driver2 = PiSession(launch, slot_identity(harness.slot_name), cwd=live.sp.workdir)
    spawn_err = None
    try:
        try:
            await driver2.start()
            # if spawn survived (it shouldn't), get_state must fail:
            # no response arrives, the process exit is the fact
            with pytest.raises(SpawnFailure):
                await asyncio.wait_for(driver2.get_state(), 20)
        except SpawnFailure as e:
            spawn_err = e
        # the fatal spawn error is 1 (pi) or 127 (chdir at exec); the
        # pre-exec path carries it on the exception, the post-exec path
        # on the process
        code = spawn_err.exit_code if spawn_err is not None else driver2.exit_code
        assert code in (1, 127), (
            f"expected exit 1 (pi) or 127 (chdir at exec): got {code}"
        )
        assert driver2.launched_argv is None or driver2.launched_argv == tuple(launch.argv)
    finally:
        await driver2.shutdown(eof_grace=1, term_grace=1)
        await driver2.close()
        # restore the mounts (workdir first — the inbox bind is nested in it)
        os.makedirs(live.sp.workdir, exist_ok=True)
        workdir_op.mount()
        inbox_op.mount()
        await harness.cleanup_failed(live)
