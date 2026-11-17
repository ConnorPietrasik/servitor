"""Concurrent-sessions gate — events do not cross over.

Three sessions on three slots run at the same time. Each session's SSE
stream contains only its own session's events (its own prompt text, its
own tool output); files stay per-session.
"""
from __future__ import annotations

import json
import time

import httpx


def test_three_concurrent_sessions_streams_stay_clean(api, api_env):
    router = api_env["router"]
    # the mock echoes the last user message as the final answer — a
    # per-session marker without shared script state (the mock is one
    # server for all sessions; a set_script per session would race)
    router.set_script({"echo_final": True})
    sids = []
    markers = {}
    for i in range(3):
        res = api.create_session(f"conc-{i}")
        sid = res["session_id"]
        sids.append(sid)
        markers[sid] = f"MARKER-{i}-"

    # start all three runs at once
    for i, sid in enumerate(sids):
        d = api.prompt(sid, f"{markers[sid]} work {i}")
        assert d["disposition"] == "started"

    # read each stream to settle; collect everything it saw
    seen = {}
    for i, sid in enumerate(sids):
        frames, _ = api.sse_read(sid, after=0, until_settled=True, timeout=180)
        seen[sid] = frames
        names = [n for (_x, n, _d) in frames]
        assert "agent_settled" in names, f"session {i} never settled"

    # no cross-talk: session A's stream never contains B's marker
    for i, sid in enumerate(sids):
        blob = json.dumps([d for (_x, _n, d) in seen[sid]])
        assert markers[sid] in blob, f"session {i} missed its own marker"
        for j, other in enumerate(sids):
            if i != j:
                assert markers[other] not in blob, (
                    f"session {i}'s stream leaked session {j}'s marker"
                )
        # and the stream's app events reference only this session
        for (_x, n, d) in seen[sid]:
            rec = json.loads(d)
            for key in ("session_id",):
                if key in rec:
                    assert rec[key] == sid, f"foreign session id in stream: {rec[key]}"

    # files stay per-session
    r = api.post(f"/api/v1/sessions/{sids[0]}/files/upload?name=f0", content=b"file-0")
    assert r.status_code == 201
    for j in (1, 2):
        r = api.get(f"/api/v1/sessions/{sids[j]}/files")
        assert not any(u["name"] == "f0" for u in r.json()["uploads"])
    for sid in sids:
        api.release(sid)
