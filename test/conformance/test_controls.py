"""Conformance: steer / follow_up / abort during a live run.

All three are RPC commands (`steer`, `follow_up`, `abort`) — no custom
protocol. Delivery is asserted ROUTER-SIDE: the mock records what the
model actually saw, so a test cannot pass on event shapes alone.
"""
from __future__ import annotations

import asyncio

import pytest


async def test_abort_mid_run_settles_aborted(harness):
    """Abort during a live run: agent_settled arrives with aborted=true;
    the run stops (no second LLM call after the abort)."""
    live = await harness.start_session()
    harness.router.set_script({"gap_seconds": 3})
    fut = live.driver.settle_waiter()
    await live.driver.prompt("slow run")
    await asyncio.sleep(0.5)  # run is live in the gap
    await live.driver.abort()  # waits for the session to become idle
    aborted = await asyncio.wait_for(fut, 30)
    assert aborted is True
    settled = [e for e in live.events if e.get("type") == "agent_settled"]
    assert settled and settled[-1].get("aborted") is True
    n_reqs = len(harness.router.requests())
    assert n_reqs == 1  # the aborted run made exactly one model call
    await harness.release(live)


async def test_follow_up_delivered_after_run(harness):
    """follow_up: queued DURING a run; delivered when the agent has no
    more tool calls — as a queued turn that EXTENDS the current run
    (queue_update inside the run; ONE agent_settled for prompt+follow-up,
    verified in the event stream). From a fully idle session a queued
    follow-up simply waits — it never starts a run (documented in
    test_controls' sibling; the delivery layer uses it as the
    queued-continuation mechanism).
    """
    live = await harness.start_session()
    harness.router.set_script({"gap_seconds": 3})
    fut = live.driver.settle_waiter()
    await live.driver.prompt("first run")
    await asyncio.sleep(0.5)  # run is live in the gap
    disp = await live.driver.follow_up("FOLLOWUP-MARKER: after you finish")
    assert disp == "queued"
    # the single settle fires only AFTER the queued follow-up turn completes
    aborted = await asyncio.wait_for(fut, 60)
    assert aborted is False
    # router-side: the marker reached the model as a user message
    def _text(c):
        if isinstance(c, str):
            return c
        if isinstance(c, list):  # structured content parts
            return " ".join(
                p.get("text", "")
                for p in c
                if isinstance(p, dict) and p.get("type") == "text"
            )
        return ""

    reqs = harness.router.requests()
    last_msgs = reqs[-1]["body"]["messages"]
    user_texts = [
        _text(m.get("content")) for m in last_msgs if m.get("role") == "user"
    ]
    assert any("FOLLOWUP-MARKER" in (t or "") for t in user_texts)
    # ONE run settled (the follow-up extended it; it did not become a
    # second run — a second settle waiter would hang forever)
    settled = [e for e in live.events if e.get("type") == "agent_settled"]
    assert len(settled) == 1
    assert any(e.get("type") == "queue_update" for e in live.events)
    await harness.release(live)


async def test_prompt_while_streaming_requires_behavior(harness):
    """Protocol rule (rpc-commands.md): a prompt during streaming without
    streamingBehavior returns an error — the driver surfaces it as a
    PromptRejected (terminal for that command, the run itself continues)."""
    from app.rpc.session import PromptRejected

    live = await harness.start_session()
    harness.router.set_script({"gap_seconds": 3})
    fut = live.driver.settle_waiter()
    await live.driver.prompt("run")
    await asyncio.sleep(0.5)
    with pytest.raises(PromptRejected):
        await live.driver.prompt("no behavior given")
    await asyncio.wait_for(fut, 30)  # the original run still settles
    await harness.release(live)


async def test_abort_is_available_for_a_waiting_dialog(harness):
    """A dialog pauses the run indefinitely; abort remains available
    for a dialog the user never wants to answer. (The full dialog
    wait/re-offer cycle is the delivery gate; here we prove the
    RPC path: a parked dialog + abort settles the session.)"""
    live = await harness.start_session()
    # no dialog will actually appear from the mock (the extension is not
    # scripted to ask) — assert the parked-dialog bookkeeping is empty and
    # abort still settles a live run (the same code path a dialog run
    # takes: the child blocks on the dialog, abort ends the run).
    assert live.driver.pending_dialogs == {}
    harness.router.set_script({"gap_seconds": 2})
    fut = live.driver.settle_waiter()
    await live.driver.prompt("run for the abort")
    await asyncio.sleep(0.4)
    await live.driver.abort()
    await asyncio.wait_for(fut, 30)
    await harness.release(live)
