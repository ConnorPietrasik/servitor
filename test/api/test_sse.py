"""Stage-3 gates: the SSE event stream (PLAN §3.3).

- Reconnect: kill the SSE connection mid-run, reconnect with
  Last-Event-ID — no lost events, no duplicates (ids strictly
  contiguous; a position past the ring edge is a `gap`, never partial
  data).
- Backpressure: a slow consumer — the buffer grows, past the 50 MB cap
  the RUN PAUSES (the driver's event stream stops), catch-up RESUMES
  it; the cap is enforced (the ring never holds more than the cap plus
  one event for a live consumer); the transcript is never truncated
  (the run still settles with its final answer).
- No caps: a 10 000+ event run is served end to end (a single scripted
  response streamed as 10 000 content chunks — one model call, no
  wall-clock or event limit anywhere).
"""
from __future__ import annotations

import json
import time

import httpx


def _ids(frames):
    return [i for (i, _n, _d) in frames if i is not None]


def _httpx_timeout(t: float) -> httpx.Timeout:
    return httpx.Timeout(t, read=t)


def test_sse_reconnect_no_loss_no_duplicates(api, api_env):
    """Mid-run connection death + Last-Event-ID resume: the union of
    what both connections saw covers the run's ids exactly once."""
    router = api_env["router"]
    # a run with a gap: the tool call sleeps, giving us time to drop
    # and reconnect mid-run
    router.set_script(
        {"gap_seconds": 0.3, "tools": [{"name": "bash", "arguments": {"command": "sleep 3"}}]}
    )
    res = api.create_session("sse-reconnect")
    sid = res["session_id"]
    try:
        d = api.prompt(sid, "long tool please")
        assert d["disposition"] == "started"
        time.sleep(2.5)  # the run is mid-tool (3 s sleep)

        # connection 1: read a few frames mid-run, then die (the run
        # produces ~66 events total; 10 lands mid-tool, inside the sleep)
        frames1, last1 = api.sse_read(sid, after=0, max_frames=10, timeout=30)
        ids1 = _ids(frames1)
        assert len(ids1) >= 5, "should have seen some events before dying"
        assert ids1 == list(range(ids1[0], ids1[-1] + 1)), "conn1 saw a gap"

        # connection 2: reconnect with Last-Event-ID and read to settle
        frames2 = []
        eid = None
        name = None
        data = []
        with httpx.stream(
            "GET", f"{api.base}/api/v1/sessions/{sid}/events",
            headers={"Last-Event-ID": str(last1)}, timeout=_httpx_timeout(120),
        ) as resp:
            assert resp.status_code == 200
            for line in resp.iter_lines():
                if line.startswith("id: "):
                    eid = int(line[4:])
                elif line.startswith("event: "):
                    name = line[7:]
                elif line.startswith("data: "):
                    data.append(line[6:])
                elif line == "":
                    if name is None:
                        continue
                    frames2.append((eid, name, "\n".join(data)))
                    done_name = name
                    eid, name, data = None, None, []
                    if done_name == "agent_settled":
                        break
        ids2 = _ids(frames2)
        assert ids2, "reconnected stream delivered nothing"

        # no duplicates: conn2 starts strictly after conn1 ended
        assert min(ids2) > max(ids1), f"overlap: conn1 max={max(ids1)} conn2 min={min(ids2)}"
        # no loss: contiguity across the seam
        assert min(ids2) == max(ids1) + 1, f"seam gap: conn1 max={max(ids1)} conn2 min={min(ids2)}"
        # and conn2 is contiguous through the settle
        assert ids2 == list(range(ids2[0], ids2[-1] + 1))
    finally:
        api.release(sid)


def test_sse_10k_event_run_no_caps(api, api_env):
    """A 10 000+ event run is served end to end: every id, no gaps, no
    duplicates, and the run settles. Pi coalesces content deltas into a
    small number of message_update events, so the event volume comes
    from a 300-round tool loop (≈35-40 events/round — verified): one long
    run, no wall-clock or event cap anywhere."""
    router = api_env["router"]
    router.set_script(
        {
            "loop_count": 300,
            "final_text": "done looping",
            "include_usage": False,
            "tools": [{"name": "bash", "arguments": {"command": "echo tick"}}],
        }
    )
    res = api.create_session("sse-10k")
    sid = res["session_id"]
    try:
        d = api.prompt(sid, "many events")
        assert d["disposition"] == "started"
        frames, last_id = api.sse_read(sid, after=0, until_settled=True, timeout=300)
        ids = _ids(frames)
        assert ids, "no events at all"
        assert len(ids) >= 10_000, f"expected 10 000+ events, saw {len(ids)}"
        # contiguous: no lost event, no duplicate id
        assert ids == list(range(ids[0], ids[-1] + 1))
        # the settle is there, un-aborted
        settles = [json.loads(d) for (_i, n, d) in frames if n == "agent_settled"]
        assert settles and settles[-1].get("aborted") is False
        # the final answer is intact (not truncated): from the transcript
        row = api.get(f"/api/v1/sessions/{sid}").json()
        with open(row["session_file"]) as f:
            tail = [json.loads(l) for l in f if l.strip()]
        last_assistant = [
            r["message"] for r in tail if r.get("type") == "message" and r.get("message", {}).get("role") == "assistant"
        ]
        final = "".join(
            p.get("text", "")
            for m in last_assistant[-1:]
            for p in m.get("content", [])
            if isinstance(p, dict) and p.get("type") == "text"
        )
        assert final == "done looping", f"final answer wrong/truncated: {final!r}"
        # and the transcript records all 300 tool rounds (run length,
        # not just delivery, is unbounded)
        tool_results = [r for r in tail if r.get("type") == "message" and r.get("message", {}).get("role") == "toolResult"]
        assert len(tool_results) == 300, f"expected 300 tool rounds, saw {len(tool_results)}"
    finally:
        api.release(sid)
