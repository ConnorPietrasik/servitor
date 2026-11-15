"""Conformance: config/credential gate (PLAN §8 stage-1; runs offline).

(1) The key travelled env → models.json (`$OMNISSIAH_API_KEY`) → request:
    the mock receives `Authorization: Bearer <env value>`, and nothing
    credential-shaped is on argv or on disk in the session's trees.
(2) The mock-recorded leading system message contains the SYSTEM.md
    marker ([servitor-config v1]) — the synced config reached the model.
(3) Sentinel AGENTS.md planted in the workdir, the inbox, and one ancestor
    level up is ABSENT from that system message (--no-context-files,
    PLAN §4; content-addressed upload naming does not stop an agent that
    copies an upload into its workdir — the launch flag is the control).
(4) NEGATIVE gate: a read-only PI_CODING_AGENT_DIR layout must FAIL (
    startup crash or prompt success:false) and NO request may reach the
    router — so nobody "simplifies" the layout back into a broken one
    that still passes get_state (report.md N42).
"""
from __future__ import annotations

import asyncio
import os
import subprocess

import pytest

from app.rpc.session import ModelUnavailable, PromptRejected, SpawnFailure

def _headers(r) -> dict[str, str]:
    """Case-insensitive header view (the mock records wire headers
    lowercase, per HTTP/1.1)."""
    return {k.lower(): v for k, v in r["headers"].items()}


MARKER = "[servitor-config v1]"
SENTINEL = "SENTINEL-AGENTS-MUST-NEVER-APPEAR"


def _leading_system_text(reqs) -> str:
    for r in reqs:
        msgs = r["body"].get("messages", [])
        if msgs and msgs[0].get("role") == "system":
            c = msgs[0].get("content")
            if isinstance(c, str):
                return c
            if isinstance(c, list):
                return "".join(b.get("text", "") for b in c if b.get("type") == "text")
    return ""


async def test_key_rides_env_and_bearer_only(harness):
    live = await harness.start_session()
    await harness.prompt_and_settle(live, "credential gate probe")
    reqs = harness.router.requests()
    assert reqs
    assert _headers(reqs[0]).get("authorization") == f"Bearer {harness.config_key}"
    # nothing credential-shaped on argv
    pid = live.driver.pid
    with open(f"/proc/{pid}/cmdline", "rb") as f:
        assert harness.config_key.encode() not in f.read()
    # ...or anywhere on disk in the session's trees (agent dir + upper,
    # sessions, workdir upper, scratch)
    roots = [live.sp.agent_dir, live.sp.agent_upper, live.sp.upper,
             os.path.join(harness.state.sessions, live.session_id)]
    for root in roots:
        for dirpath, dirnames, filenames in os.walk(root):
            for name in filenames:
                p = os.path.join(dirpath, name)
                try:
                    with open(p, "rb") as f:
                        assert harness.config_key.encode() not in f.read(), p
                except OSError:
                    continue
    await harness.release(live)


async def test_system_message_carries_the_config_marker(harness):
    live = await harness.start_session()
    await harness.prompt_and_settle(live, "config gate probe")
    sys_text = _leading_system_text(harness.router.requests())
    assert MARKER in sys_text, "SYSTEM.md marker missing from the leading system message"
    await harness.release(live)


async def test_planted_agents_md_never_reaches_the_model(harness, tmp_path):
    """The sentinel test (stage-1 gate): AGENTS.md planted in the workdir,
    the inbox, and one ancestor level up — all absent from the system
    message."""
    live = await harness.start_session()
    # workdir (the cwd), one ancestor up, and the inbox (bind-mounted in)
    for p in (
        os.path.join(live.sp.workdir, "AGENTS.md"),
        os.path.join(live.sp.scratch, "AGENTS.md"),  # the parent of the workdir
        os.path.join(harness.state.inbox_for(live.session_id), "AGENTS.md"),
    ):
        with open(p, "w") as f:
            f.write(f"# injected\n{SENTINEL}\n")
    os.chown(os.path.join(live.sp.workdir, "AGENTS.md"), live.slot.uid, live.slot.gid)
    os.chown(os.path.join(live.sp.scratch, "AGENTS.md"), live.slot.uid, live.slot.gid)
    await harness.prompt_and_settle(live, "sentinel probe")
    sys_text = _leading_system_text(harness.router.requests())
    assert SENTINEL not in sys_text
    # not in ANY request of the session (not just the first)
    for r in harness.router.requests():
        assert SENTINEL not in str(r["body"].get("messages", []))
    await harness.release(live)


async def test_negative_gate_read_only_agent_dir_fails_closed(harness):
    """A read-only agent-dir layout must fail (crash or success:false)
    and no request may reach the router.

    The ro is applied AT MOUNT TIME (overlayfs refuses a later
    `remount,ro`): this reproduces the simplified/broken layout the gate
    exists to catch (report.md N42) — a layout where get_state looks
    healthy but the session can never run.
    """
    live = await harness.start_session(expect_startup=False, agent_dir_ro=True)
    driver = live.driver
    try:
        crashed = False
        try:
            await driver.assert_model_usable()
        except (ModelUnavailable, SpawnFailure):
            crashed = True
        if not crashed:
            # the startup survived (pi may not need the agent dir before
            # the first model call): the prompt must still fail terminal
            with pytest.raises((PromptRejected, SpawnFailure, asyncio.TimeoutError)):
                await asyncio.wait_for(driver.prompt("negative gate probe"), 30)
        # the gate's invariant: NOTHING reached the router
        assert harness.router.requests() == [], "a request reached the router — the gate is wrong"
    finally:
        await driver.shutdown(eof_grace=2, term_grace=2)
        await driver.close()
        await harness.cleanup_failed(live)
