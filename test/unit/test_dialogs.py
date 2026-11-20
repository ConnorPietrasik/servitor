"""Dialog endpoints + wiring (root-free unit tests).

Extension dialogs (select/confirm/input/editor) park in the session's
process — the child blocks until the human answers (or aborts):
- GET .../dialogs lists the driver's pending set; 409 when the process
  is dead (a dialog cannot outlive its child — nothing to re-offer);
- POST .../dialogs/{id}/answer forwards value/confirmed/cancelled to
  the driver (the child unblocks, the run resumes) and publishes the
  answered event; an unknown id is 404 (already answered, or resolved
  by the pi-side timeout/abort and swept at the run's settle);
- pi may resolve a dialog on its own (its timeout, or an abort)
  WITHOUT re-notifying: the run's settle sweeps the park and the
  lifecycle publishes ``extension_dialog_resolved`` per straggler (the
  UI re-fetches — a stale card must not outlive the run that blocked
  on it);
- the driver's on_dialog sink publishes ``extension_dialog`` to the
  event feed + the session's SSE hub (the notification; the full
  dialog record is served by GET).

The HTTP endpoint runs against build_app with a fake app state (no
lifespan — TestClient is used without a context), per the
test_fork_gate pattern.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

from fastapi.testclient import TestClient

from app.api.app import build_app
from app.lifecycle import EventFeed, Lifecycle
from app.rpc.session import PiSession

SID = "s1"
DIALOG = {
    "type": "extension_ui_request",
    "id": "d-1",
    "method": "confirm",
    "title": "Clear session?",
    "message": "All messages will be lost.",
}


# --- driver on_dialog sink: visibility -------------------------------- #


def _lc_with_feed():
    lc = Lifecycle.__new__(Lifecycle)
    lc.cfg = SimpleNamespace(registry=None, state=None, sse_ring_bytes=None)
    lc.live = {}
    lc._hubs = {}
    lc.events = EventFeed(hub_getter=lc.hub)
    return lc


def test_on_dialog_publishes_to_feed_and_hub():
    lc = _lc_with_feed()
    lc._on_dialog(SID, DIALOG)
    items = lc.events.items()
    assert items and items[-1]["kind"] == "extension_dialog"
    assert "confirm" in items[-1]["detail"]
    assert "d-1" in items[-1]["detail"]
    assert "Clear session?" in items[-1]["detail"]
    snap = lc.hub(SID).ring_snapshot()
    assert snap and snap[-1]["name"] == "extension_dialog"


def test_on_dialog_without_title_still_reports_method_and_id():
    lc = _lc_with_feed()
    lc._on_dialog(SID, {"method": "input", "id": "d-2"})
    items = lc.events.items()
    assert items[-1]["kind"] == "extension_dialog"
    assert "input" in items[-1]["detail"] and "d-2" in items[-1]["detail"]


def test_on_dialog_resolved_publishes_to_feed_and_hub():
    lc = _lc_with_feed()
    lc._on_dialog_resolved(SID, ["d-1", "d-2"])
    items = lc.events.items()
    assert [i["kind"] for i in items[-2:]] == [
        "extension_dialog_resolved",
        "extension_dialog_resolved",
    ]
    assert items[-2]["detail"] == "d-1"
    assert items[-1]["detail"] == "d-2"
    snap = lc.hub(SID).ring_snapshot()
    assert snap[-1]["name"] == "extension_dialog_resolved"


# --- settle sweep: pi resolved the dialog on its own ------------------ #


def _bare_driver(pending, on_resolved=None, on_settled=None):
    d = PiSession.__new__(PiSession)
    d._pending_dialogs = dict(pending)
    d._last_settled_aborted = False
    d._run_active = True
    d._settled_waiters = []
    d._tasks = []
    d.on_settled = on_settled
    d.on_dialog_resolved = on_resolved
    return d


def test_settle_sweeps_dialogs_pi_resolved_on_its_own():
    """pi resolves a dialog via its timeout or an abort WITHOUT
    re-notifying the parent. A run cannot settle while a dialog still
    blocks the child, so anything still parked at settle is stale:
    swept from the park and announced via on_dialog_resolved (the UI
    re-fetches the pending set — the dialogs are gone)."""
    seen: list[list[str]] = []
    d = _bare_driver(
        {"d-1": DIALOG, "d-2": {**DIALOG, "id": "d-2"}},
        on_resolved=lambda _drv, rids: seen.append(rids),
    )
    d._on_agent_settled({"aborted": True})
    assert d.pending_dialogs == {}
    assert seen == [["d-1", "d-2"]]


def test_settle_without_stale_dialogs_announces_nothing():
    """A dialog the HUMAN answered was already popped from the park by
    answer_dialog: a settle right after must not re-announce it (the
    UI already got extension_dialog_answered)."""
    seen: list[list[str]] = []
    d = _bare_driver({}, on_resolved=lambda _drv, rids: seen.append(rids))
    d._on_agent_settled({"aborted": False})
    assert d.pending_dialogs == {}
    assert seen == []


# --- HTTP endpoints ----------------------------------------------------- #


def _client(pending, *, dead: bool = False):
    class _Row:
        state = "live"

    class _Registry:
        def get(self, sid):
            return _Row() if sid == SID else None

    driver = SimpleNamespace(
        exited=dead,
        pending_dialogs=pending,
        answer_dialog=AsyncMock(),
    )
    live = {} if dead else {SID: SimpleNamespace(driver=driver)}
    app_state = SimpleNamespace(
        registry=_Registry(),
        lc=SimpleNamespace(
            live=live, events=SimpleNamespace(add=lambda *a, **k: None)
        ),
        boot_report=0,
    )
    app = build_app(app_state, SimpleNamespace(browser_origin=None))
    return TestClient(app), driver


def test_list_dialogs_returns_the_pending_set():
    c, _ = _client({"d-1": DIALOG, "d-2": {**DIALOG, "id": "d-2"}})
    r = c.get(f"/api/v1/sessions/{SID}/dialogs")
    assert r.status_code == 200, r.text
    got = {d["id"] for d in r.json()["dialogs"]}
    assert got == {"d-1", "d-2"}


def test_list_dialogs_empty_when_nothing_pending():
    c, _ = _client({})
    r = c.get(f"/api/v1/sessions/{SID}/dialogs")
    assert r.status_code == 200
    assert r.json() == {"dialogs": []}


def test_list_dialogs_409_when_the_process_is_dead():
    c, _ = _client({"d-1": DIALOG}, dead=True)
    r = c.get(f"/api/v1/sessions/{SID}/dialogs")
    assert r.status_code == 409


def test_answer_confirmed_forwards_to_the_driver():
    c, driver = _client({"d-1": DIALOG})
    r = c.post(f"/api/v1/sessions/{SID}/dialogs/d-1/answer",
               json={"confirmed": True})
    assert r.status_code == 200, r.text
    assert r.json() == {"ok": True}
    driver.answer_dialog.assert_awaited_once_with(
        "d-1", value=None, confirmed=True, cancelled=False
    )


def test_answer_value_and_cancelled_forward_verbatim():
    c, driver = _client({"d-1": DIALOG})
    r = c.post(f"/api/v1/sessions/{SID}/dialogs/d-1/answer",
               json={"value": "Allow"})
    assert r.status_code == 200
    driver.answer_dialog.assert_awaited_once_with(
        "d-1", value="Allow", confirmed=None, cancelled=False
    )
    r = c.post(f"/api/v1/sessions/{SID}/dialogs/d-1/answer",
               json={"cancelled": True})
    assert r.status_code == 200
    driver.answer_dialog.assert_awaited_with(
        "d-1", value=None, confirmed=None, cancelled=True
    )


def test_answer_unknown_id_is_404():
    c, driver = _client({"d-1": DIALOG})
    driver.answer_dialog.side_effect = KeyError("no pending dialog")
    r = c.post(f"/api/v1/sessions/{SID}/dialogs/d-9/answer",
               json={"confirmed": True})
    assert r.status_code == 404
