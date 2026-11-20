"""SSE backpressure gate.

A deliberately slow consumer: the unconsumed bytes pass the cap and the
RUN PAUSES (the driver stops draining pi's stdout — the pipe fills and
pi blocks); the consumer catches up, the run RESUMES; the run settles
with its complete final answer (the transcript is never truncated); the
ring stays bounded (never more than the cap plus a streaming burst for
a live consumer); the hub's meter records the pause episodes.

Cap scaling: this test overrides ``sse_cap`` (1 MB) for its app
instance. The pause/resume/bounded mechanism is cap-independent — it
is the same code path at 1 MB as at 50 MB. The production default is
pinned by a unit test (test/unit/test_sse_hub.py: the default cap is
50 MB), and 50 MB is the right default because pi's per-delta
message_update events bound a single context window's SSE volume well
below it.

Event anatomy (measured, pi 1.5.2): each model content delta is its
own small message_update (~250 B). A 32 KB preamble streamed as
2048 pieces is ~2000 events ≈ 0.6 MB; 20 tool rounds ≈ 42 000 events
≈ 12 MB total — comfortably past the 1 MB cap, drainable in ~1 min.
"""
from __future__ import annotations

import json
import queue
import threading
import time

import httpx
import pytest

from conftest import Api  # noqa: F401 (Api client)


@pytest.fixture()
def sse_cap() -> int:
    return 1024 * 1024  # 1 MB — the mechanism is cap-independent


def test_pause_resume_bounded_no_truncation(api_env, sse_cap):
    cap = sse_cap
    api = Api(api_env["base"])
    router = api_env["router"]

    # 20 tool rounds; each round streams a 32 KB assistant preamble
    # before its tool call. Note the mock's ``chunk_count`` is a PIECE
    # COUNT (the mock splits the text into that many pieces): 2048
    # pieces of 16 chars → pi emits one small message_update per
    # piece. 20 rounds ≈ 42 000 events ≈ 12 MB ≫ the 1 MB cap.
    prefix = "p" * 32_768
    router.set_script(
        {
            "loop_count": 20,
            "prefix_text": prefix,
            "final_text": "done under backpressure",
            "chunk_count": 2048,
            "include_usage": False,
            "tools": [{"name": "bash", "arguments": {"command": "echo tick"}}],
        }
    )
    res = api.create_session("sse-backpressure")
    sid = res["session_id"]
    try:
        d = api.prompt(sid, "stream it all")
        assert d["disposition"] == "started"

        # the consumer is two parts:
        #   * a reader THREAD drains the socket as fast as it can — the
        #     kernel must never accumulate a deep receive backlog behind
        #     the slow parser (on some hosts a deep backlog under
        #     64KB-MSS loopback bursts drops packets and wedges the
        #     connection for good); and
        #   * the parser (this thread) is deliberately slow — it is the
        #     "slow consumer" that lags the run past the hub cap and
        #     triggers the backpressure pause. The pause is driven by the
        #     hub's position (ack'd ids), not by TCP socket state.
        #
        # Pacing: ~0.25 s of sleep per 200 parsed frames (~150 KB/s) vs
        # the mock's ~300 KB/s stream — the consumer stays behind, the
        # unconsumed bytes cross the 1 MB cap, the run pauses; the
        # consumer drains below the resume threshold and it resumes.
        frames = 0
        last_id = 0
        sub_id = None
        saw_paused = False
        max_unconsumed = 0
        max_buffered = 0
        settled = False
        last_progress = time.time()
        last_wire = time.time()
        last_diag = 0.0
        wire_bytes = 0
        deadline = time.time() + 600
        with httpx.stream(
            "GET", f"{api.base}/api/v1/sessions/{sid}/events?after=0",
            timeout=httpx.Timeout(600, read=600),
        ) as resp:
            assert resp.status_code == 200
            sub_id = int(resp.headers.get("X-Sub-Id", "0"))
            name = None
            eid = None
            data = []
            linebuf = ""

            def process_line(line: str):
                nonlocal name, eid, data, frames, last_id, settled
                nonlocal saw_paused, max_unconsumed, max_buffered
                nonlocal last_progress, last_diag
                if line.startswith("id: "):
                    eid = int(line[4:])
                elif line.startswith("event: "):
                    name = line[7:]
                elif line.startswith("data: "):
                    data.append(line[6:])
                elif line == "" and name is not None:
                    frames += 1
                    last_progress = time.time()
                    if eid is not None:
                        last_id = eid
                    if name == "agent_settled":
                        settled = True
                    name, eid, data = None, None, []
                    if frames % 200 == 0:
                        st = api.post(
                            f"/api/v1/sessions/{sid}/events/ack",
                            json_body={"sub_id": sub_id, "last_id": last_id},
                        ).json()
                        if st.get("paused"):
                            saw_paused = True
                        max_unconsumed = max(max_unconsumed, st.get("unconsumed_bytes", 0))
                        max_buffered = max(max_buffered, st.get("buffered_bytes", 0))
                    if frames % 200 == 0:
                        time.sleep(0.25)
                    # watchdog: no frame for 90 s = stuck (dump + fail fast)
                    now = time.time()
                    if now - last_progress > 90:
                        st = api.post(
                            f"/api/v1/sessions/{sid}/events/ack",
                            json_body={"sub_id": sub_id, "last_id": last_id},
                        ).json()
                        raise AssertionError(
                            f"SSE stream stalled: {frames} frames, last_id={last_id}, "
                            f"state={st}, hub latest_id={st.get('latest_id')}"
                        )
                    if now - last_diag > 30:
                        last_diag = now
                        print(f"  [bp diag] frames={frames} last_id={last_id} "
                              f"saw_paused={saw_paused} max_uncons={max_unconsumed} "
                              f"wire={wire_bytes}B linebuf={len(linebuf)}B", flush=True)

            # the reader thread: drains the socket at line rate (above).
            # NOTE: iter_raw() WITHOUT a chunk size — with a chunk size,
            # httpx re-chunks into FIXED-size pieces and the final partial
            # piece (holding the tail of the stream — possibly
            # agent_settled) is held back until more bytes arrive or the
            # stream closes; the stream never closes (heartbeats), so the
            # tail would never surface.
            raw_q: queue.Queue[bytes | None] = queue.Queue(maxsize=128)
            it = resp.iter_raw()
            reader_alive = [True]

            def _reader() -> None:
                try:
                    for chunk in it:
                        if not chunk:
                            break
                        raw_q.put(chunk)
                except Exception:
                    pass
                finally:
                    raw_q.put(None)  # EOF sentinel
                    reader_alive[0] = False

            rth = threading.Thread(target=_reader, daemon=True)
            rth.start()

            while time.time() < deadline and not settled:
                # wire-level watchdog: no BYTES at all for 90 s = the
                # connection is wedged (fail fast with state, don't burn
                # the test budget)
                if time.time() - last_wire > 90:
                    st = api.post(
                        f"/api/v1/sessions/{sid}/events/ack",
                        json_body={"sub_id": sub_id, "last_id": last_id},
                    ).json()
                    raise AssertionError(
                        f"SSE connection wedged (no bytes 90s): "
                        f"{frames} frames, last_id={last_id}, state={st}, "
                        f"reader_alive={reader_alive[0]} qsize={raw_q.qsize()} "
                        f"linebuf={linebuf[-200:]!r} wire={wire_bytes}B"
                    )
                try:
                    chunk = raw_q.get(timeout=5)
                except queue.Empty:
                    continue
                if chunk is None:
                    break  # stream closed (should not happen pre-settle)
                last_wire = time.time()
                wire_bytes += len(chunk)
                linebuf += chunk.decode("utf-8", "replace")
                while "\n" in linebuf:
                    line, linebuf = linebuf.split("\n", 1)
                    process_line(line)
                    if settled:
                        break
                    if settled:
                        break

        assert settled, "the run never settled — backpressure must delay, never kill"
        assert saw_paused, "the run was never paused for the slow consumer"
        # bounded: with a live consumer the hub never lets the
        # unconsumed bytes run away past the cap + one streaming burst
        burst = 4 * 1024 * 1024
        assert max_unconsumed <= cap + burst, (
            f"unconsumed ran away: {max_unconsumed} > cap+burst {cap + burst}"
        )
        assert max_buffered <= cap + burst, (
            f"ring memory ran away: {max_buffered} > cap+burst {cap + burst}"
        )

        # final position: caught up, unpaused
        st = api.post(
            f"/api/v1/sessions/{sid}/events/ack",
            json_body={"sub_id": sub_id, "last_id": last_id},
        ).json()
        assert st["paused"] is False, "still paused after the consumer caught up"
        assert st["unconsumed_bytes"] == 0

        # the pause was real (the meter counted episodes)
        assert st["pause_count"] >= 1

        # no truncation: the transcript (pi's own file) holds every
        # round's full 32 KB preamble and the complete final answer
        row = api.get(f"/api/v1/sessions/{sid}").json()
        with open(row["session_file"]) as f:
            tail = [json.loads(l) for l in f if l.strip()]
        assistants = [
            r["message"] for r in tail
            if r.get("type") == "message" and r.get("message", {}).get("role") == "assistant"
        ]
        tool_results = [
            r for r in tail
            if r.get("type") == "message" and r.get("message", {}).get("role") == "toolResult"
        ]
        assert len(tool_results) == 20, f"run truncated: {len(tool_results)} rounds of 20"
        preamble_total = sum(
            len(p.get("text", ""))
            for m in assistants
            for p in m.get("content", [])
            if isinstance(p, dict) and p.get("type") == "text"
        )
        assert preamble_total == 20 * len(prefix) + len("done under backpressure"), (
            f"content truncated: {preamble_total} != "
            f"{20 * len(prefix) + len('done under backpressure')}"
        )
        final = "".join(
            p.get("text", "")
            for m in assistants[-1:]
            for p in m.get("content", [])
            if isinstance(p, dict) and p.get("type") == "text"
        )
        assert final == "done under backpressure", f"final answer wrong: {final!r}"
    finally:
        api.release(sid)
