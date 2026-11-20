"""Unit tests for PiSession.prompt_and_wait_settled (app/rpc/session.py).

The waiter lifecycle is pure asyncio bookkeeping, so no subprocess is
spawned: the session is constructed but never started, and ``request``
is shadowed. The doctor's live-run gate relies on this contract:

- the settle waiter is installed BEFORE the prompt is sent — a fast run
  can settle between the prompt's reply and a post-hoc waiter;
- the (disposition, aborted) pair comes back intact so the caller can
  honor "handled" precisely (consumed, no run started: nothing was
  certified, and no settle was ever coming);
- the waiter is discarded on "handled", on prompt rejection, and on
  settle timeout — a raw asyncio.wait_for around settle_waiter() would
  leave the cancelled future in _settled_waiters.
"""
from __future__ import annotations

import asyncio

import pytest

from app.iso.spawn import SlotIdentity
from app.rpc.launch import Launch
from app.rpc.session import CommandResult, PiSession, PromptRejected


def _session() -> PiSession:
    # constructed but never started: no spawn, no root, no protocol I/O
    launch = Launch(argv=("/usr/sbin/node", "pi.js", "--mode", "rpc"), env=())
    slot = SlotIdentity(name="dummy", uid=1000, gid=1000, home="/tmp")
    return PiSession(launch, slot, cwd="/tmp")


def _patch_request(s: PiSession, behavior) -> None:
    """Shadow request() with behavior(cmd) — no subprocess in between.

    An instance attribute shadows the bound method, so `behavior` is a
    plain function taking (cmd, timeout=None)."""
    async def fake_request(cmd, timeout=None):
        return behavior(cmd)

    s.request = fake_request  # type: ignore[method-assign]


def test_waiter_installed_before_prompt_and_settles():
    """The waiter exists before the prompt is sent (the race the
    pre-prompt install exists for), and the (disposition, aborted) pair
    comes back intact."""
    s = _session()
    saw_waiter_before_prompt = False

    def behavior(cmd):
        nonlocal saw_waiter_before_prompt
        saw_waiter_before_prompt = len(s._settled_waiters) == 1
        # fast run: settles BEFORE the wait even begins
        s._on_agent_settled({"type": "agent_settled", "aborted": False})
        return CommandResult(
            command="prompt", success=True, data={"disposition": "started"})

    _patch_request(s, behavior)
    disposition, aborted = asyncio.run(
        s.prompt_and_wait_settled("hi", timeout=5))
    assert saw_waiter_before_prompt, \
        "settle waiter must be installed before the prompt is sent"
    assert (disposition, aborted) == ("started", False)
    assert s._settled_waiters == []


def test_handled_returns_without_waiting_and_discards_waiter():
    """"handled" = consumed, no run started: nothing was certified and no
    settle was ever coming — return immediately, discard the waiter, and
    leave run_active untouched (the run never started)."""
    s = _session()

    def behavior(cmd):
        return CommandResult(
            command="prompt", success=True, data={"disposition": "handled"})

    _patch_request(s, behavior)
    # the outer wait_for is a guard: a regression to "wait for a settle
    # that never comes" must fail in 5 s, not burn the 30 s cap
    disposition, aborted = asyncio.run(asyncio.wait_for(
        s.prompt_and_wait_settled("hi", timeout=30), timeout=5))
    assert (disposition, aborted) == ("handled", False)
    assert s._settled_waiters == []
    assert not s.run_active


def test_rejection_discards_waiter():
    s = _session()

    def behavior(cmd):
        raise PromptRejected("prompt", "bad prompt")

    _patch_request(s, behavior)
    with pytest.raises(PromptRejected):
        asyncio.run(s.prompt_and_wait_settled("hi", timeout=5))
    assert s._settled_waiters == []


def test_timeout_discards_waiter_and_session_still_settles():
    """The cancelled waiter must be GONE after a settle timeout (the old
    raw wait_for leaked it), and a later settle must be unaffected."""
    s = _session()

    def behavior(cmd):
        return CommandResult(
            command="prompt", success=True, data={"disposition": "started"})

    _patch_request(s, behavior)
    with pytest.raises(asyncio.TimeoutError):
        asyncio.run(s.prompt_and_wait_settled("hi", timeout=0.05))
    assert s._settled_waiters == [], \
        "cancelled waiter must be removed, not left in _settled_waiters"
    # a later settle is clean: no stale waiter, no error
    s._on_agent_settled({"type": "agent_settled", "aborted": True})
    assert s.last_settled_aborted is True
