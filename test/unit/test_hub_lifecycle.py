"""SseHub entries (and probe-cache entries) are freed at terminal
transitions.

The per-session SSE ring is capped at sse_ring_bytes (default 50 MB) —
a bound meant to be per-LIVE-session. The contract:

* ``hub()`` refuses to CREATE for release-phase rows (DRAINING/CLEANING/
  FREE), unknown rows, and ``"*"`` (it still returns an existing hub —
  lookup first, so in-flight releases keep delivering on their hub);
* ``release()`` and ``_abort_start()`` drop the hub + probe-cache
  entries; idle-kill deliberately keeps them (respawn re-attaches to
  the same hub — Last-Event-ID resume);
* the DRAINING refusal closes the quarantine gap: a failed sweep leaves
  the row DRAINING forever, and a reconnecting client must not mint a
  fresh sse_ring_bytes hub that no drop site ever reaches.

Companion contracts: ``_respawn`` refuses terminal rows up front (a
stuck DRAINING row keeps a truthy session_file string past the sweep and
passes _get_row — the refusal surfaces as 409 via SessionNotResumable,
not an unmapped RuntimeError → 500), and probes of terminal rows never
cache (release() would never drop the entry — nothing else evicts
_probe_cache).

Root-free: real registry (sqlite in tmp_path) + real Lifecycle; the
process-shaped bits are faked, per the test_drain pattern.
"""
from __future__ import annotations

import asyncio
import sqlite3
import time
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import app.lifecycle as lcmod
from app.api.app import EDGE_KEY_HEADER, build_app
from app.iso.layout import state_paths
from app.lifecycle import (
    FREE, ALLOCATED, LIVE, DRAINING, CLEANING,
    Lifecycle, LifecycleConfig, Live, PiInstall, SessionNotResumable,
)
from app.registry import Registry

SLOT = "slot1"


# ---------------------------------------------------------------------- #
# fixtures / helpers                                                      #
# ---------------------------------------------------------------------- #


class FakePool:
    """Permissive pool: any slot name resolves. The keyset carries the
    names the fixture OWNS — lifecycle._release's pool-membership guard
    (the off-pool refusal) reads it, as gc_tick's two release passes do
    in production."""

    def __init__(self, names=("slot1",)):
        self.slots = {n: SimpleNamespace(name=n) for n in names}

    def get(self, name):
        return SimpleNamespace(name=name)


class FakeDriver:
    """Process-shaped fake: state/shutdown/close, no real child."""

    def __init__(self, *, exited=False, state=None):
        self.exited = exited
        self.exit_code = 0 if exited else None
        self._state = state or {
            "isStreaming": False, "isCompacting": False,
            "pendingMessageCount": 0,
        }
        self.shutdown_calls = 0
        self.close_calls = 0

    async def get_state(self):
        return dict(self._state)

    async def shutdown(self, **kw):
        self.shutdown_calls += 1
        self.exited = True
        return 0

    async def close(self):
        self.close_calls += 1


def _run(coro):
    """Run a coroutine on a fresh loop (the unit suite has no session loop)."""
    res = asyncio.new_event_loop()
    try:
        return res.run_until_complete(coro)
    finally:
        res.close()


def _make_lc(tmp_path, monkeypatch, idle_kill_sec=3600.0) -> Lifecycle:
    """A full Lifecycle (real __init__, real registry) with the heavy
    release path neutralized: release_session runs the REAL hooks
    (mark_draining / mark_released — the registry transitions and the
    release_start/released feed events) but skips kill/unmount/sweep."""
    reg = Registry(str(tmp_path / "registry.db"))
    # TestClient runs the app in a worker thread; production touches the
    # registry from the loop thread only, so same-thread sqlite is fine
    # there — the test's route-level checks need the cross-thread read.
    # The schema is already created (journal_mode=WAL persists in the
    # file), so the original same-thread connection is closed and
    # swapped, not leaked
    reg.db.close()
    reg.db = sqlite3.connect(reg.path, check_same_thread=False)
    reg.db.row_factory = sqlite3.Row

    class Outcome:
        released = True
        quarantined = False
        errors = []
        config_pruned = []

    async def fake_release_session(*, session_id, slot, state, pool, hooks,
                                   driver=None, mount_ops=None, reason=""):
        hooks.mark_draining(session_id, reason)
        hooks.mark_released(session_id, quarantined=False, detail=f"reason={reason}")
        return Outcome()

    monkeypatch.setattr(lcmod, "release_session", fake_release_session)
    cfg = LifecycleConfig(
        state=state_paths(str(tmp_path)),
        pool=FakePool((SLOT,)),
        registry=reg,
        pi=PiInstall(node_bin="/usr/bin/node", cli_js="/x/cli.js", ext_path=None),
        api_key="k",
        idle_kill_sec=idle_kill_sec,
    )
    return Lifecycle(cfg)


def _add_row(lc: Lifecycle, session_id: str, state=ALLOCATED, session_file=None):
    lc.cfg.registry.upsert_session(session_id, slot=SLOT, state=state)
    if session_file is not None:
        lc.cfg.registry.set_session_file(session_id, session_file)


# ---------------------------------------------------------------------- #
# hub() creation policy                                                   #
# ---------------------------------------------------------------------- #


def test_hub_created_and_reused_for_live_session(tmp_path, monkeypatch):
    lc = _make_lc(tmp_path, monkeypatch)
    _add_row(lc, "s1", state=LIVE)
    h1 = lc.hub("s1")
    assert h1 is not None
    assert lc.hub("s1") is h1  # same instance (respawn re-attach)


def test_hub_refused_for_release_phase_unknown_and_wildcard(tmp_path, monkeypatch):
    lc = _make_lc(tmp_path, monkeypatch)
    _add_row(lc, "s-free", state=FREE)
    _add_row(lc, "s-draining", state=DRAINING)  # incl. the stuck quarantine row
    _add_row(lc, "s-cleaning", state=CLEANING)
    assert lc.hub("s-free") is None
    assert lc.hub("s-draining") is None
    assert lc.hub("s-cleaning") is None
    assert lc.hub("s-unknown") is None
    assert lc.hub("*") is None
    assert lc._hubs == {}  # nothing was created

    # feed events must not smuggle hubs back in either: the wildcard
    # broadcast, a FREE session, and a quarantined (DRAINING) session
    # all skip the (nonexistent) hub
    lc.events.add("*", "update_applying", "reason=x")
    lc.events.add("s-free", "orphan_kind", "detail")
    lc.events.add("s-draining", "slot_quarantined", "sweep failed")
    assert lc._hubs == {}


def test_existing_hub_returned_even_for_free_row(tmp_path, monkeypatch):
    """hub() is a LOOKUP first: an existing hub is returned whatever the
    row says (release() drops it — it does not rely on lookup refusing)."""
    lc = _make_lc(tmp_path, monkeypatch)
    _add_row(lc, "s1", state=LIVE)
    h = lc.hub("s1")
    # the allowed terminal chain: LIVE -> DRAINING -> CLEANING -> FREE;
    # the hub follows the session through all of it
    lc.cfg.registry.set_state("s1", DRAINING, cause="test")
    assert lc.hub("s1") is h
    lc.cfg.registry.set_state("s1", CLEANING, cause="test")
    assert lc.hub("s1") is h
    lc.cfg.registry.set_state("s1", FREE, cause="test")
    assert lc.hub("s1") is h


# ---------------------------------------------------------------------- #
# release(): the terminal drop                                            #
# ---------------------------------------------------------------------- #


def test_release_drops_hub_and_probe_cache(tmp_path, monkeypatch):
    lc = _make_lc(tmp_path, monkeypatch)
    sid = "s1"
    _add_row(lc, sid, state=LIVE)
    hub = lc.hub(sid)
    hub.publish("message", {"text": "x" * 100_000})
    lc._probe_cache[(sid, "messages")] = (time.time(), {"messages": [1, 2, 3]})
    lc._probe_cache[(sid, "stats")] = (time.time(), {"stats": {}})

    driver = FakeDriver()
    live = Live(session_id=sid, sp=SimpleNamespace(), mount_ops=[], driver=driver,
                config_version="v1")
    lc.live[sid] = live

    outcome = _run(lc.release(sid, reason="user close"))

    assert outcome.released is True
    # live entry and process state are gone
    assert sid not in lc.live
    assert driver.close_calls == 1
    # the hub is dropped, closed, and unreachable
    assert sid not in lc._hubs
    assert hub.closed is True
    assert lc.hub(sid) is None  # FREE row: never re-created
    # the probe cache entries went with it
    assert lc._probe_cache == {}
    # the release events still landed in the ring before the drop
    names = [e["name"] for e in hub.ring_snapshot()]
    assert names[-2:] == ["release_start", "released"]


def test_release_without_live_entry_drops_hub(tmp_path, monkeypatch):
    lc = _make_lc(tmp_path, monkeypatch)
    sid = "s2"
    _add_row(lc, sid, state=LIVE)
    lc.hub(sid).publish("t", {"i": 0})
    outcome = _run(lc.release(sid, reason="idle GC"))
    assert outcome.released is True
    assert sid not in lc._hubs and lc.hub(sid) is None


def test_churn_does_not_grow_hubs(tmp_path, monkeypatch):
    """The _hubs dict returns to empty after each release (50 MB cap x
    churn was the unbounded part)."""
    lc = _make_lc(tmp_path, monkeypatch)
    for i in range(25):
        sid = f"s-{i}"
        _add_row(lc, sid, state=LIVE)
        lc.hub(sid).publish("t", {"i": i})
        _run(lc.release(sid, reason="slot pressure"))
    assert len(lc._hubs) == 0, f"churn leaked {len(lc._hubs)} hubs"


def test_quarantined_release_never_recreates_hub(tmp_path, monkeypatch):
    """The quarantine gap: a failed sweep leaves the row DRAINING
    forever (visible in the panel, retryable via a re-release) — never
    FREE. release() still drops the hub, and hub() must not mint a
    replacement for the stuck row: otherwise a client that reconnects
    pins a fresh sse_ring_bytes ring that no drop site ever reaches.
    The events route 404s instead and the client's EventSource stops.
    """
    lc = _make_lc(tmp_path, monkeypatch)
    sid = "s-quar"
    _add_row(lc, sid, state=LIVE)
    hub = lc.hub(sid)
    hub.publish("message", {"text": "x" * 1000})

    class Outcome:
        released = False
        quarantined = True
        errors = ["sweep not verified clean"]
        config_pruned = []

    # the quarantine path of release_session: the REAL hooks in the real
    # order — mark_released(quarantined=True) keeps the row DRAINING
    async def fake_release_session(*, session_id, slot, state, pool, hooks,
                                   driver=None, mount_ops=None, reason=""):
        hooks.mark_draining(session_id, reason)
        hooks.mark_released(session_id, quarantined=True,
                            detail="; ".join(Outcome.errors))
        return Outcome()

    monkeypatch.setattr(lcmod, "release_session", fake_release_session)
    outcome = _run(lc.release(sid, reason="user close"))

    assert outcome.quarantined is True
    assert lc.cfg.registry.get(sid).state == DRAINING  # stuck, never FREE
    # the hub is dropped and closed, as on a clean release — and the
    # release events landed in the ring before the drop
    assert sid not in lc._hubs and hub.closed is True
    assert [e["name"] for e in hub.ring_snapshot()][-2:] == [
        "release_start", "slot_quarantined"]
    # the gap: a reconnect mints no replacement hub for the stuck row
    assert lc.hub(sid) is None
    assert lc._hubs == {}

    # end-to-end: the events route 404s (a DRAINING row passes _get_row)
    # without minting a hub either
    app = build_app(
        SimpleNamespace(registry=lc.cfg.registry, lc=lc, boot_report=0),
        SimpleNamespace(browser_origin=None, heartbeat_secs=15, edge_key="unit-test-edge-key"),
    )
    c = TestClient(app, headers={EDGE_KEY_HEADER: "unit-test-edge-key"})
    r = c.get(f"/api/v1/sessions/{sid}/events")
    assert r.status_code == 404, r.text
    assert lc._hubs == {}


# ---------------------------------------------------------------------- #
# aborted start: the other terminal transition                            #
# ---------------------------------------------------------------------- #


def test_abort_start_drops_hub(tmp_path, monkeypatch):
    lc = _make_lc(tmp_path, monkeypatch)
    sid = "s-abort"
    _add_row(lc, sid, state=ALLOCATED)
    hub = lc.hub(sid)  # created by the (failing) spawn path
    live = Live(session_id=sid, sp=SimpleNamespace(), mount_ops=[], driver=None,
                config_version="v1")
    lc.live[sid] = live
    _run(lc._abort_start(sid, live, SimpleNamespace(name=SLOT)))
    assert sid not in lc.live
    assert sid not in lc._hubs
    assert hub.closed is True
    assert lc.cfg.registry.get(sid) is None  # the row goes too
    assert lc.hub(sid) is None


# ---------------------------------------------------------------------- #
# idle-kill: deliberately NOT a drop site                                 #
# ---------------------------------------------------------------------- #


def test_idle_kill_keeps_hub_for_respawn_reattach(tmp_path, monkeypatch):
    """idle-kill kills the process but the row survives: pickup respawns
    and clients resume via Last-Event-ID against the SAME hub — dropping
    it here would reset the resume window mid-session."""
    lc = _make_lc(tmp_path, monkeypatch)
    sid = "s-idle"
    _add_row(lc, sid, state=LIVE, session_file=str(tmp_path / "t.jsonl"))
    hub = lc.hub(sid)
    hub.publish("message", {"text": "history"})
    driver = FakeDriver()
    live = Live(session_id=sid, sp=SimpleNamespace(), mount_ops=[], driver=driver,
                config_version="v1")
    live.last_settled = time.time() - 2 * 3600  # settled past the threshold
    lc.live[sid] = live
    killed = _run(lc.idle_kill_tick())
    assert killed == [sid]
    assert driver.shutdown_calls == 1
    # hub and live entry survive the kill (respawn re-attaches)
    assert sid in lc._hubs and lc._hubs[sid] is hub
    assert hub.closed is False
    assert lc.hub(sid) is hub
    assert sid in lc.live


def test_idle_kill_never_kills_a_watched_session(tmp_path, monkeypatch):
    """The dead guard: Live.subscribers was never incremented, so the
    "a human is watching: never idle-kill" check could never fire. The
    real watcher count is the hub's live SSE consumers (one EventSource
    per open chat tab) — a watched-but-idle session survives the tick,
    and once the tab closes the next tick may kill."""
    lc = _make_lc(tmp_path, monkeypatch)
    sid = "s-watched"
    _add_row(lc, sid, state=LIVE, session_file=str(tmp_path / "t.jsonl"))
    hub = lc.hub(sid)
    driver = FakeDriver()
    live = Live(session_id=sid, sp=SimpleNamespace(), mount_ops=[], driver=driver,
                config_version="v1")
    live.last_settled = time.time() - 2 * 3600  # settled past the threshold
    lc.live[sid] = live

    sub, _, _ = hub.subscribe(after_id=0)  # the open chat tab
    assert _run(lc.idle_kill_tick()) == []
    assert driver.shutdown_calls == 0

    hub.unsubscribe(sub)  # the tab closed
    assert _run(lc.idle_kill_tick()) == [sid]
    assert driver.shutdown_calls == 1


# ---------------------------------------------------------------------- #
# fetch_messages: no hub creation on a released session                   #
# ---------------------------------------------------------------------- #


def test_fetch_messages_free_row_returns_zero_cursor_no_hub(tmp_path, monkeypatch):
    """GET /messages does not 404 on a FREE row (the route only catches
    KeyError): it returns cursor 0 (what the dead hub's ring would have
    replayed) and leaves no entry behind."""
    lc = _make_lc(tmp_path, monkeypatch)
    sid = "s-gone"
    _add_row(lc, sid, state=FREE)
    out = _run(lc.fetch_messages(sid))
    assert out["cursor"] == 0
    assert out["messages"] == []
    assert lc._hubs == {}


# ---------------------------------------------------------------------- #
# the /events endpoint: hub gone mid-request race                         #
# ---------------------------------------------------------------------- #


def test_events_endpoint_404_when_hub_gone():
    """The row check and hub() are not atomic: a release landing between
    them leaves the request with no hub. That is terminal (every further
    request 404s), so the endpoint 404s too — the client stops, it does
    not retry into a session that is gone."""
    class _Row:
        state = "live"

    class _Registry:
        def get(self, sid):
            return _Row()

    lc = SimpleNamespace(shutting_down=False, hub=lambda sid: None)
    app = build_app(
        SimpleNamespace(registry=_Registry(), lc=lc, boot_report=0),
        SimpleNamespace(browser_origin=None, heartbeat_secs=15, edge_key="unit-test-edge-key"),
    )
    c = TestClient(app, headers={EDGE_KEY_HEADER: "unit-test-edge-key"})
    r = c.get("/api/v1/sessions/s1/events")
    assert r.status_code == 404, r.text


# ---------------------------------------------------------------------- #
# terminal rows: respawn refused (409), probes never cached               #
# ---------------------------------------------------------------------- #


def _quarantine(lc: Lifecycle, monkeypatch, sid: str):
    """Drive the quarantine path of release(): the row stays DRAINING
    (never FREE) and keeps a truthy session_file string — release
    sweeps the file, it never clears the registry pointer."""
    class Outcome:
        released = False
        quarantined = True
        errors = ["sweep not verified clean"]
        config_pruned = []

    async def fake_release_session(*, session_id, slot, state, pool, hooks,
                                   driver=None, mount_ops=None, reason=""):
        hooks.mark_draining(session_id, reason)
        hooks.mark_released(session_id, quarantined=True,
                            detail="; ".join(Outcome.errors))
        return Outcome()

    monkeypatch.setattr(lcmod, "release_session", fake_release_session)
    return _run(lc.release(sid, reason="user close"))


def test_respawn_refused_for_terminal_row_before_side_effects(tmp_path, monkeypatch):
    """_respawn checks the row state first: a stuck DRAINING row is
    terminal — no stray kill, no mounts, no process, no hub mint."""
    lc = _make_lc(tmp_path, monkeypatch)
    sid = "s-q"
    _add_row(lc, sid, state=DRAINING, session_file=str(tmp_path / "t.jsonl"))
    with pytest.raises(SessionNotResumable):
        _run(lc._respawn(sid))
    assert lc._hubs == {} and sid not in lc.live


def test_prompt_quarantined_row_409_end_to_end(tmp_path, monkeypatch):
    """A stuck DRAINING row passes _get_row (only FREE 404s) and keeps a
    truthy session_file string, so a prompt reaches _respawn: the state
    check must raise SessionNotResumable → 409 (not an unmapped
    RuntimeError → 500), and no hub is minted for the stuck row. Panel
    start (ensure_alive) and fork share the same path/handler."""
    lc = _make_lc(tmp_path, monkeypatch)
    sid = "s-q409"
    _add_row(lc, sid, state=LIVE, session_file=str(tmp_path / "t.jsonl"))
    lc.hub(sid)
    _quarantine(lc, monkeypatch, sid)

    app = build_app(
        SimpleNamespace(registry=lc.cfg.registry, lc=lc, boot_report=0),
        SimpleNamespace(browser_origin=None, heartbeat_secs=15, edge_key="unit-test-edge-key"),
    )
    c = TestClient(app, headers={EDGE_KEY_HEADER: "unit-test-edge-key"})
    r = c.post(f"/api/v1/sessions/{sid}/prompt", json={"text": "hello"})
    assert r.status_code == 409, r.text
    assert "terminal" in r.json()["detail"]
    assert lc._hubs == {}  # the stuck row mints no replacement hub


def test_fork_quarantined_row_409_not_503(tmp_path, monkeypatch):
    """fork shares the respawn path but its route also catches
    RuntimeError (→ 503, retryable upstream failure) — a shadow that
    would turn the terminal-row refusal into a 503 the client retries
    into a session that is gone. The route must re-raise
    SessionNotResumable so the app-level handler maps it to 409 (stop,
    do not retry), same as prompt/panel start."""
    lc = _make_lc(tmp_path, monkeypatch)
    sid = "s-qfork"
    _add_row(lc, sid, state=LIVE, session_file=str(tmp_path / "t.jsonl"))
    lc.hub(sid)
    _quarantine(lc, monkeypatch, sid)

    app = build_app(
        SimpleNamespace(registry=lc.cfg.registry, lc=lc, boot_report=0),
        SimpleNamespace(browser_origin=None, heartbeat_secs=15, edge_key="unit-test-edge-key"),
    )
    c = TestClient(app, headers={EDGE_KEY_HEADER: "unit-test-edge-key"})
    r = c.post(f"/api/v1/sessions/{sid}/fork", json={"entry_id": "e1"})
    assert r.status_code == 409, r.text
    assert "terminal" in r.json()["detail"]
    assert lc._hubs == {}  # the stuck row mints no replacement hub


def test_probe_terminal_row_not_cached(tmp_path, monkeypatch):
    """A crashed release can leave a DRAINING row with its transcript on
    disk: probes of it run (first-touch is satisfied) but never cache —
    the row is never dropped again by release(), so a cached entry would
    live to the process's life. LIVE rows cache as before."""
    lc = _make_lc(tmp_path, monkeypatch)
    transcript = tmp_path / "t.jsonl"
    transcript.write_text('{"id": "e1", "parentId": null, "type": "message"}\n')
    _add_row(lc, "s-q", state=DRAINING, session_file=str(transcript))
    _add_row(lc, "s-live", state=LIVE, session_file=str(transcript))

    async def fake_run_probe(session_id, which, row):
        return {"messages": [session_id]}

    monkeypatch.setattr(lc, "_run_probe", fake_run_probe)
    assert _run(lc.probe("s-q", "messages")) == {"messages": ["s-q"]}
    assert _run(lc.probe("s-live", "messages")) == {"messages": ["s-live"]}
    assert ("s-q", "messages") not in lc._probe_cache
    assert ("s-live", "messages") in lc._probe_cache
