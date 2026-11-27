"""SSE resume precedence — Last-Event-ID beats ?after= on a reconnect.

The per-session stream is opened as ``EventSource(...?after=N)``; the
browser reconnects a dropped connection to the SAME url (``after`` is a
constant of it) carrying the client's progressed ``Last-Event-ID``. The
header is the resume position and must win: with ``after`` winning, every
radio blip mid-chat replays everything the client already consumed — the
UI dedups completed messages by fingerprint but not tool rows or a message
still in flight, so the replay is visible corruption, not just waste.
``after`` still governs the FIRST connect (a fresh stream object sends no
header), and ``head`` still yields no replay for the app-wide stream's
open (a resumed head stream — header present — still resumes).

Root-free: the precedence is pinned on ``_start_pos`` directly, and on the
real ``/events`` route (the wiring that reads the header) against a real
SseHub.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

import app.api.app as app_mod
from app.api.app import _start_pos, build_app
from app.api.sse import SseHub

INDEX = "<html>index</html>\n"


def _state(hub: SseHub) -> SimpleNamespace:
    """The fake app state the route needs: a live row and the hub."""
    return SimpleNamespace(
        registry=SimpleNamespace(
            get=lambda sid: SimpleNamespace(state="live"),
            list_sessions=lambda include_released: [],
        ),
        lc=SimpleNamespace(shutting_down=False, hub=lambda sid: hub),
        boot_report=0,
    )


def _cfg():
    return SimpleNamespace(browser_origin=None, edge_key=None, heartbeat_secs=15)


def _client(tmp_path, monkeypatch, hub: SseHub) -> TestClient:
    dist = tmp_path / "dist"
    dist.mkdir(parents=True, exist_ok=True)
    (dist / "index.html").write_text(INDEX)
    monkeypatch.setattr(app_mod, "_ui_dist", lambda: dist)
    return TestClient(build_app(_state(hub), _cfg()))


def _first_frame(response) -> tuple[str, int | None]:
    """The first event the stream delivers (past the ':servitor'
    comment), as (name, id-or-None) — a single pass, since the stream can
    only be iterated once. A gap frame carries no id — name only."""
    name = eid = None
    for line in response.iter_lines():
        if name is None:
            # the first frame is a normal frame (id, then event) or a gap
            # frame (event only); the ':servitor' and ':hb' comments
            # carry neither
            if line.startswith("id:"):
                eid = int(line.split(":", 1)[1].strip())
            elif line.startswith("event:"):
                name = line.split(":", 1)[1].strip()
        elif line == "":  # the first frame is complete at its blank line
            return name, eid
    raise AssertionError("the stream ended before its first frame")


# ---- the precedence itself ------------------------------------------------


def test_start_pos_precedence_last_event_id_beats_after():
    hub = SseHub("s1")
    hub.publish("t", {})  # latest_id = 1
    # the reconnect contract: the header wins over the url's after
    assert _start_pos(hub, 0, "4", False) == 4
    # first connect (no header): after applies
    assert _start_pos(hub, 0, None, False) == 0
    # a resumed head stream still resumes exactly where it stopped
    assert _start_pos(hub, None, "0", True) == 0
    # the app-wide stream's open: newest, no replay
    assert _start_pos(hub, None, None, True) == 1
    # neither: from the beginning
    assert _start_pos(hub, None, None, False) == 0


def test_start_pos_bad_last_event_id_is_400():
    hub = SseHub("s1")
    with pytest.raises(HTTPException) as e:
        _start_pos(hub, 0, "not-a-number", False)
    assert e.value.status_code == 400


# ---- the route wiring (the header actually reaches the precedence) --------


def test_reconnect_resumes_at_last_event_id_not_after(tmp_path, monkeypatch):
    hub = SseHub("s1")
    for i in range(5):
        hub.publish("t", {"i": i})  # ids 1..5
    c = _client(tmp_path, monkeypatch, hub)

    # first connect: after=0 (no header yet) replays from id 1
    with c.stream("GET", "/api/v1/sessions/s1/events?after=0") as r:
        assert r.status_code == 200
        assert _first_frame(r) == ("t", 1)

    # the SAME url, a dropped connection's reconnect: the client has
    # received through id 4 — it resumes at 5, not a re-replay of 1..4
    with c.stream(
        "GET",
        "/api/v1/sessions/s1/events?after=0",
        headers={"last-event-id": "4"},
    ) as r:
        assert r.status_code == 200
        assert _first_frame(r) == ("t", 5)


def test_reconnect_past_the_hubs_lifetime_is_a_gap_not_a_wait(
    tmp_path, monkeypatch
):
    # app-restart shape: a FRESH hub (latest_id 0, empty ring) behind a
    # reconnect carrying the dead hub's last id. The position is not
    # resume-able — the first frame is a gap (the UI re-fetches
    # /messages and re-attaches), never a silent heartbeat-only wait for
    # the new hub to re-issue those ids and then deliver fresh events
    # as continuous over a hole.
    hub = SseHub("s1")  # no publishes yet
    c = _client(tmp_path, monkeypatch, hub)
    with c.stream(
        "GET",
        "/api/v1/sessions/s1/events?after=0",
        headers={"last-event-id": "999"},
    ) as r:
        assert r.status_code == 200
        assert _first_frame(r) == ("gap", None)

    # a fresh client (no header, after=0) on a fresh hub is not a gap —
    # it replays whatever the hub has issued and then follows live
    # (publish BEFORE the stream opens: publish is loop-thread work and
    # the test thread must not race the app loop's waiters)
    hub2 = SseHub("s2")
    hub2.publish("t", {"i": 0})
    c2 = _client(tmp_path, monkeypatch, hub2)
    with c2.stream("GET", "/api/v1/sessions/s2/events?after=0") as r:
        assert r.status_code == 200
        assert _first_frame(r) == ("t", 1)
