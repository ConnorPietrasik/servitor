"""Conformance: framing + disposition matrix + settle-wait.

Framing: U+2028/2029 legal inside JSON strings are NOT record
boundaries — live round-trip through the real pi subprocess. The unit
suite (test/unit/test_framer.py) covers the framer directly; this proves
the full pipeline keeps them intact end to end.

Disposition: `started` (idle prompt) / `queued` (steer during a run) /
`handled` (extension command consumed — NO run starts, do NOT wait for
agent_settled).

Settle: the run ends at `agent_settled`, not `agent_end` (a tool-using run
emits agent_end for the low-level run while session-level work — here the
steered continuation — can still follow; the driver resolves only at
settled).
"""
from __future__ import annotations

import asyncio

import pytest


def _last_assistant_text(live) -> str:
    msgs = [e["message"] for e in live.events if e.get("type") == "message_end"]
    assistant = [m for m in msgs if m.get("role") == "assistant"]
    if not assistant:
        return ""
    content = assistant[-1].get("content")
    if isinstance(content, list):
        return "".join(b.get("text", "") for b in content if b.get("type") == "text")
    return str(content or "")


async def test_prompt_disposition_started_settles(harness):
    live = await harness.start_session()
    disp = await harness.prompt_and_settle(live, "hello")
    assert disp == "started"
    types = [e.get("type") for e in live.events]
    assert types[0] == "agent_start"
    assert types[-1] == "agent_settled"
    assert not live.driver.exited  # settled != dead: the session keeps idling
    await harness.release(live)


async def test_u2028_u2029_roundtrip_through_real_pi(harness):
    """A prompt containing U+2028 and U+2029 must reach the router intact
    (the framer would have split the protocol on them otherwise)."""
    import json as _json

    live = await harness.start_session()
    text = "alpha\u2028beta\u2029gamma"
    await harness.prompt_and_settle(live, text)
    reqs = harness.router.requests()
    assert reqs, "the mock router saw no request"
    # json round-trip (not str()): str() repr-escapes the control chars and
    # would mask a corruption — a re-parse must contain the raw sequence
    joined = _json.dumps([r["body"] for r in reqs], ensure_ascii=False)
    assert "alpha\u2028beta\u2029gamma" in joined
    await harness.release(live)


async def test_prompt_during_run_queues_via_steer(harness):
    """`steer` during a live run returns disposition `queued`, and the
    steering message is delivered to the NEXT LLM call (assert router-
    side, not by event shape)."""
    live = await harness.start_session()
    # make the first LLM call take a beat so the run is live while we steer
    harness.router.set_script({"gap_seconds": 2})
    fut = live.driver.settle_waiter()  # install BEFORE the prompt (race-free)
    disp = await live.driver.prompt("start a run")
    assert disp == "started"
    await asyncio.sleep(0.4)  # run is live (in the gap)
    steer_disp = await live.driver.steer("STEER-MARKER: do this instead")
    assert steer_disp == "queued"
    await asyncio.wait_for(fut, 60)
    harness.router.reset()
    # next run: the queued steer was delivered as a user message
    await harness.prompt_and_settle(live, "next prompt")
    joined = str(harness.router.requests()[0]["body"]["messages"])
    assert "STEER-MARKER" in joined
    await harness.release(live)


async def test_handled_disposition_starts_no_run(harness, fixture_ext):
    """An extension command is CONSUMED by prompt: disposition `handled`,
    no agent_start, no run — the driver must not wait for agent_settled."""
    live = await harness.start_session(extra_exts=(str(fixture_ext),))
    disp = await live.driver.prompt("/svtest")
    assert disp == "handled"
    await asyncio.sleep(0.3)  # a run, if started, would have emitted agent_start
    types = [e.get("type") for e in live.events]
    assert "agent_start" not in types
    assert "agent_settled" not in types  # nothing to wait for; none came
    assert harness.router.requests() == []  # no model call at all
    await harness.release(live)


async def test_agent_end_is_not_settle(harness):
    """A tool-using run: agent_end closes the low-level run; the driver
    resolves only on agent_settled (the plan's settle-wait correctness)."""
    live = await harness.start_session()
    disp = await harness.prompt_and_settle(live, "use the mock tool")
    assert disp == "started"
    types = [e.get("type") for e in live.events]
    assert "agent_end" in types  # the low-level run boundary was emitted
    assert types.index("agent_settled") > types.index("agent_end")
    # the run actually used the tool (router-side evidence)
    tool_events = [e for e in live.events if e.get("type") == "tool_execution_start"]
    assert tool_events and tool_events[0]["toolName"] == "bash"
    await harness.release(live)


async def test_wait_settled_never_hangs_on_dead_process(harness):
    """A dead process never settles: the waiter raises SpawnFailure (no
    cap, no silent hang — the run's liveness is the driver's fact)."""
    from app.rpc.session import SpawnFailure

    live = await harness.start_session()
    fut = live.driver.settle_waiter()
    # kill the child outright (not the ladder — the unclean path)
    live.driver._proc.kill()  # noqa: SLF001 — test forcing the unclean exit
    with pytest.raises(SpawnFailure):
        await asyncio.wait_for(fut, 15)
    # release must still complete: the single cleanup path handles it
    out = await harness.release(live, reason="test: dead child")
    assert out.released or out.quarantined
