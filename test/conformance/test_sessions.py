"""Conformance: sessions — first spawn, transcript, respawn, identity.

- First spawn `--session-id <minted uuid>`; the transcript path comes
  from `get_state.sessionFile`, captured VERBATIM.
- Resume must use `--session <path>`; after every spawn/respawn the
  driver's caller asserts identity: `get_state.sessionId` == the minted
  id, `get_state.sessionFile` == the recorded path.
- Empty sessions (never prompted) are NEVER persisted — a row without a
  transcript is "idle, nothing to resume", not an error.
- One process per session file at a time: the respawn path replaces the
  (dead) process; the live child is never double-spawned.
"""
from __future__ import annotations

import os


async def test_first_spawn_mints_id_and_persists_transcript(harness):
    live = await harness.start_session()
    sid = live.session_id
    st = await live.driver.get_state()
    assert st["sessionId"] == sid  # identity assert, first spawn
    session_file = st["sessionFile"]
    assert session_file
    await harness.prompt_and_settle(live, "CODEWORD: foxtrot-kilo")
    # the transcript exists on disk, flat in sessions/<s>/ (explicit
    # session dir)
    assert os.path.isfile(session_file)
    await harness.release(live)


async def test_empty_session_never_persisted(harness):
    """A session that was never prompted has no transcript file (pi does
    not persist empty sessions). Respawn-of-nothing is a non-error state."""
    live = await harness.start_session()
    st = await live.driver.get_state()
    sf = st.get("sessionFile")
    # pi may name the would-be file before the first prompt, but it must
    # NOT exist on disk
    assert sf is None or not os.path.exists(sf), f"empty session persisted: {sf}"
    await live.driver.shutdown()
    await live.driver.close()
    await harness.cleanup_failed(live)


async def test_respawn_returns_the_transcript(harness):
    """Idle-kill (process gone, mounts survive) then respawn: the driver
    comes back on the SAME session — identity asserts pass, the
    transcript is intact, and the MODEL SEES THE HISTORY (router-side)."""
    live = await harness.start_session()
    sid = live.session_id
    st = await live.driver.get_state()
    recorded_file = st["sessionFile"]
    await harness.prompt_and_settle(live, "CODEWORD: foxtrot-kilo")

    # idle-kill + respawn in one step (respawn() kills the live process
    # itself — the driver must still be alive for the recorded state)
    live2 = await harness.respawn(live)
    assert live2.session_id == sid
    st2 = await live2.driver.get_state()
    # identity asserts after respawn (fail closed otherwise)
    assert st2["sessionId"] == sid
    assert st2["sessionFile"] == recorded_file
    # the transcript came back (driver-level read, never JSONL parsing)
    msgs = await live2.driver.get_messages()
    joined = str([m.get("content") for m in msgs if m.get("role") == "user"])
    assert "CODEWORD: foxtrot-kilo" in joined
    # and the model sees it: the next request's messages carry the history
    harness.router.reset()
    await harness.prompt_and_settle(live2, "what was the codeword?")
    body = harness.router.requests()[0]["body"]
    assert "foxtrot-kilo" in str(body["messages"])
    await harness.release(live2)


async def test_one_process_per_session_file(harness):
    """Invariant 7: while a live process owns the session file, the
    respawn path must not spawn a second one (the harness kills the old
    first; here we assert the proc-scan fact directly: exactly one
    slot-uid process for the session's workdir)."""
    live = await harness.start_session()
    pids = _pids_in_dir(live.sp.workdir)
    assert len(pids) == 1 and pids[0] == live.driver.pid
    await harness.release(live)


def _pids_in_dir(cwd: str) -> list[int]:
    out = []
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        try:
            # /proc/<pid>/cwd is a SYMLINK: readlink it (opening it would
            # follow the link and read the target file, not the path)
            if os.readlink(f"/proc/{entry}/cwd") == cwd:
                out.append(int(entry))
        except OSError:
            continue
    return out
