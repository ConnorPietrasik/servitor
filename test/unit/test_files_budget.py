"""Unit: the phone-budget layer — 304s, byte meters, and the global stream.

A phone on a metered plan pays for change and nothing else:

- ``GET /files`` is ETag'd: an unchanged listing costs ~200 bytes instead of
  the whole body, 64-char sha strings per row included;
- byte meters (``GET /api/v1/usage``) are TTL-cached — a report that walks a
  multi-GB tree is not a per-request thing — and a delete forces a re-walk,
  because the human just moved the number;
- the app-wide stream lets the sessions list follow reality without polling;
- a file's MIME is read once per version: the listing's sniff is the
  download's value (an upload's is the one recorded while its bytes streamed
  in) — "never re-sniff a version a cache already knows".

Pinned: the 304 and its invalidation, deduped vs referenced bytes, the meter
cache and its forced invalidation, the feed/sink fan-out to the global hub
(allow-listed by GLOBAL_EVENTS: off-contract kinds stay session-local, and
per-token chatter is explicitly NOT mirrored), and the once-per-version sniff
on the download paths.
"""
from __future__ import annotations

import json
import os
import time
from collections import OrderedDict
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from unit._filesapp import make_client, slot_identity, upload
from app.api.sse import SseHub
from app.delivery.store import SessionFiles
from app.delivery.usage import UsageMeter, area_usage
from app.lifecycle import GLOBAL_EVENTS, EventFeed, Lifecycle

FOREIGN_UID = 0xFFFFFFFF  # no file in a test tree can be owned by this


def _client(tmp_path):
    c, _state, root = make_client(tmp_path)
    return c, root


def _upload(c, name="a.txt", body=b"hello"):
    assert upload(c, name, body).status_code == 201


def _settle_delivery(timeout: float = 10.0) -> None:
    """Wait for the store's background pass (hash + sniff) to drain.

    A listing triggers it asynchronously; a test that then downloads must
    measure the cache, not a race between the pass and the download's own
    inline sniff of a not-yet-known version."""
    import app.delivery.store as store_mod

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with store_mod._sha_lock:
            if not store_mod._SHA_PENDING:
                return
        time.sleep(0.01)
    pytest.fail("the background pass did not finish in time")


# ---------------------------------------------------------------------- #
# ETag / 304
# ---------------------------------------------------------------------- #


def test_files_listing_carries_an_etag(tmp_path):
    c, _ = _client(tmp_path)
    _upload(c)
    r = c.get("/api/v1/sessions/s1/files")
    assert r.status_code == 200 and r.headers.get("etag")
    assert r.headers["cache-control"] == "no-cache"  # revalidate, never stale


def test_an_unchanged_listing_costs_a_304_and_no_body(tmp_path):
    c, _ = _client(tmp_path)
    _upload(c)
    etag = c.get("/api/v1/sessions/s1/files").headers["etag"]
    r = c.get("/api/v1/sessions/s1/files", headers={"if-none-match": etag})
    assert r.status_code == 304
    assert r.content == b""
    assert r.headers["etag"] == etag


def test_a_new_upload_moves_the_etag(tmp_path):
    c, _ = _client(tmp_path)
    _upload(c, "a.txt")
    etag = c.get("/api/v1/sessions/s1/files").headers["etag"]
    _upload(c, "b.txt", b"more")
    r = c.get("/api/v1/sessions/s1/files", headers={"if-none-match": etag})
    assert r.status_code == 200
    assert r.headers["etag"] != etag
    assert sorted(u["name"] for u in r.json()["uploads"]) == ["a.txt", "b.txt"]


def test_an_artifact_appearing_moves_the_etag(tmp_path):
    c, root = _client(tmp_path)
    etag = c.get("/api/v1/sessions/s1/files").headers["etag"]
    (root / "outbox" / "s1" / "report.md").write_bytes(b"# hi")
    r = c.get("/api/v1/sessions/s1/files", headers={"if-none-match": etag})
    assert r.status_code == 200
    assert [e["path"] for e in r.json()["outbox"]] == ["report.md"]


def test_the_listing_fetchs_own_sweep_publishes_its_delta(tmp_path):
    """A GET /files is itself a sweep driver: its delta must land on the
    session's hub like a timer sweep's, while the hub has a watcher. The
    fetch advances the shared snapshot — a delta it discarded would be a
    change the session's other watcher (a second open tab) never learns
    about, because the timer now diffs against the snapshot the fetch just
    wrote."""
    c, app_state, _root = make_client(tmp_path)
    assert c.get("/api/v1/sessions/s1/files").status_code == 200  # seed the baseline
    app_state.lc.hub("s1").subscribe(0)  # the second open tab: a live watcher
    (Path(app_state.state.outbox_for("s1")) / "late.bin").write_bytes(b"x")
    assert c.get("/api/v1/sessions/s1/files").status_code == 200
    hub = app_state.lc.hub("s1")
    deltas = [json.loads(e.data) for e in hub._ring if e.name == "files_changed"]
    assert deltas, "the route swept and discarded its own delta"
    assert [a["path"] for a in deltas[-1]["added"]] == ["late.bin"]


def test_a_listing_fetch_with_no_watcher_parks_no_delta(tmp_path):
    """The timer's contract is the fetch's: a delta nobody can receive is
    pure waste. With no subscriber on the hub the fetch still advances the
    snapshot (the next sweep diffs against it), but it parks no ring entry —
    a later subscriber starts from a full listing, never a replay, so the
    delta would be bytes a metered phone pays once and uses never."""
    c, app_state, _root = make_client(tmp_path)
    assert c.get("/api/v1/sessions/s1/files").status_code == 200  # seed
    (Path(app_state.state.outbox_for("s1")) / "late.bin").write_bytes(b"x")
    assert c.get("/api/v1/sessions/s1/files").status_code == 200  # a delta exists
    hub = app_state.lc.hub("s1")  # mints the hub; nothing is subscribed to it
    assert [e for e in hub._ring if e.name == "files_changed"] == []


def test_a_listing_fetch_mints_no_hub_when_nobody_is_watching(tmp_path):
    """The fetch's delta lands only on a hub that already exists: a
    session with no stream gets none from a listing fetch — the fresh
    hub would only park a ring (up to the cap) of deltas a later
    subscriber never needs (it starts from a full listing, not from a
    replay). The stub's MINTING ``hub`` is what makes the pin mean: if
    the route asked for a stream, the no-hub assert would fail."""
    c, app_state, _root = make_client(tmp_path, mint_hubs=False)
    out = Path(app_state.state.outbox_for("s1"))
    out.mkdir(parents=True, exist_ok=True)
    assert c.get("/api/v1/sessions/s1/files").status_code == 200  # seed
    (out / "late.bin").write_bytes(b"x")
    assert c.get("/api/v1/sessions/s1/files").status_code == 200  # a delta exists
    assert app_state.lc.hub_if_present("s1") is None  # the route did not mint


# ---------------------------------------------------------------------- #
# per-version sniff: a value the cache already knows is not re-read
# ---------------------------------------------------------------------- #


def _sniff_counter(monkeypatch):
    """Count the real sniffs — every binding in the delivery layer (the
    store's, as listings/downloads reach it through ``mime_for``; the
    service's, as uploads reach it at stream-in; the API's, as the
    manifest-less fallback) — and hand them a distinctive MIME so a
    fall-through to the real sniff cannot fake a hit. The module-level
    cache is reset per test, so a neighbouring test's version cannot be
    what satisfies this one."""
    import app.api.app as api_mod
    import app.delivery.service as service_mod
    import app.delivery.store as store_mod

    calls = []

    def counting(path, head_bytes=8192):
        calls.append(path)
        return "application/x-counted-sniff"

    for mod in (store_mod, service_mod, api_mod):
        monkeypatch.setattr(mod, "sniff_mime", counting)
    monkeypatch.setattr(store_mod, "_MIME_CACHE", OrderedDict())
    return calls


def test_an_outbox_download_reuses_the_listings_sniff(tmp_path, monkeypatch):
    calls = _sniff_counter(monkeypatch)
    c, root = _client(tmp_path)
    (root / "outbox" / "s1" / "report.md").write_bytes(b"# hi")
    c.get("/api/v1/sessions/s1/files")
    _settle_delivery()  # the listing's sniff rides its background pass
    assert len(calls) == 1, "the listing sniffs the version once"
    for _ in range(2):
        r = c.get("/api/v1/sessions/s1/files/outbox/report.md")
        assert r.status_code == 200
        assert r.headers["content-type"] == "application/x-counted-sniff"
    assert len(calls) == 1, "a download of a known version costs no re-read"
    # a new version of the same path is a new value: it may be sniffed once
    (root / "outbox" / "s1" / "report.md").write_bytes(b"# hi, v2")
    c.get("/api/v1/sessions/s1/files/outbox/report.md")
    assert len(calls) == 2


def test_an_upload_download_never_re_sniffs_the_bytes(tmp_path, monkeypatch):
    """The MIME is computed exactly once — while the bytes stream in — and
    recorded in the manifest; the inbox file is immutable to the session, so
    that is still its MIME. Listing and downloading must read that record,
    not the bytes: no sniff anywhere in the delivery layer afterwards."""
    calls = _sniff_counter(monkeypatch)
    c, _ = _client(tmp_path)
    _upload(c, "a.txt", b"hello")
    assert len(calls) == 1, "the one sniff happens while the bytes stream in"
    c.get("/api/v1/sessions/s1/files")
    r = c.get("/api/v1/sessions/s1/files/uploads/a.txt")
    assert r.status_code == 200
    assert len(calls) == 1, "listing and download read the manifest's record"


def test_a_kept_download_reuses_the_shelf_listings_sniff(tmp_path, monkeypatch):
    calls = _sniff_counter(monkeypatch)
    c, root = _client(tmp_path)
    (root / "outbox" / "s1" / "art.md").write_bytes(b"keep me")
    r = c.post("/api/v1/sessions/s1/files/keep", json={"path": "outbox/art.md"})
    assert r.status_code == 201, r.text
    kept = r.json()["path"]
    c.get("/api/v1/files")  # the shelf listing sniffs the kept version once
    n = len(calls)
    assert n >= 1
    r = c.get(f"/api/v1/files/keep/{kept}")
    assert r.status_code == 200
    assert len(calls) == n, "a download of a listed kept file costs no re-read"


def test_a_keep_primes_the_shelfs_mime_cache(tmp_path, monkeypatch):
    """keep() must cache the MIME under the key the SHELF uses — the key of
    the link it made, not of the file it read: the shelf lists the link's
    inode (its own size+mtime), and a key from the source's version would
    cost the listing a re-sniff on every keep."""
    calls = _sniff_counter(monkeypatch)
    c, root = _client(tmp_path)
    (root / "outbox" / "s1" / "art.md").write_bytes(b"keep me")
    r = c.post("/api/v1/sessions/s1/files/keep", json={"path": "outbox/art.md"})
    assert r.status_code == 201, r.text
    assert r.json()["mime"] == "application/x-counted-sniff"
    c.get("/api/v1/files")
    assert len(calls) == 1, "the keep's one sniff is the shelf's; no re-sniff"


def test_the_upload_listing_reads_the_manifest_once(tmp_path, monkeypatch):
    """The read gate asks each root-owned entry for its recorded sha, and a
    listing of N uploads must cost one manifest read, not N: the UI revalidates
    this route on every change, and N manifest re-reads scale quadratically
    on a session that holds many uploads."""
    c, _state, _root = make_client(
        tmp_path, slot=FOREIGN_UID, slot_ident=slot_identity(FOREIGN_UID), monkeypatch=monkeypatch
    )
    for i in range(20):
        assert upload(c, f"u{i:02d}.bin", b"x" * 64).status_code == 201
    calls = []
    orig = SessionFiles.read_manifest

    def counting(self):
        calls.append(1)
        return orig(self)

    monkeypatch.setattr(SessionFiles, "read_manifest", counting)
    body = c.get("/api/v1/sessions/s1/files").json()
    assert len(body["uploads"]) == 20  # the gate still admits the real blobs
    assert len(calls) == 1, "one manifest read per listing, whatever N is"


# ---------------------------------------------------------------------- #
# byte meters
# ---------------------------------------------------------------------- #


def test_area_usage_buckets_per_session(tmp_path):
    root = tmp_path / "state"
    (root / "inbox" / "s1").mkdir(parents=True)
    (root / "inbox" / "s2").mkdir(parents=True)
    (root / "scratch" / "s1" / "workdir").mkdir(parents=True)
    (root / "staging" / "s1").mkdir(parents=True)
    (root / "inbox" / "s1" / "a.bin").write_bytes(b"x" * 100)
    (root / "inbox" / "s2" / "b.bin").write_bytes(b"y" * 50)
    (root / "scratch" / "s1" / "workdir" / "big.bin").write_bytes(b"z" * 1000)
    # an in-flight staged upload: the UI's "held" number and the eviction
    # order both count it, so the per-session bucket must carry it too
    (root / "staging" / "s1" / "u1").mkdir()
    (root / "staging" / "s1" / "u1" / "part").write_bytes(b"s" * 512)

    r = area_usage(str(root))

    assert r["areas"]["inbox"]["bytes"] == 150
    assert r["areas"]["scratch"]["bytes"] == 1000
    assert r["areas"]["staging"]["bytes"] == 512
    assert r["by_session"]["s1"]["inbox"] == 100
    assert r["by_session"]["s1"]["scratch"] == 1000
    assert r["by_session"]["s1"]["staging"] == 512
    assert r["by_session"]["s2"]["inbox"] == 50
    assert r["by_session"]["s1"]["files"] == 3
    assert r["fs"]["total"] > 0


def test_a_hardlink_is_billed_once_in_the_totals_but_shown_in_both_sessions(tmp_path):
    """Two views, each honest: the volume totals dedup the inode (that is how
    much disk it costs), and each session is shown the bytes it references
    (so it never reads as holding nothing). ``shared_bytes`` is the gap."""
    root = tmp_path / "state"
    (root / "inbox" / "s1").mkdir(parents=True)
    (root / "inbox" / "s2").mkdir(parents=True)
    src = root / "inbox" / "s1" / "shared.bin"
    src.write_bytes(b"x" * 4096)
    os.link(src, root / "inbox" / "s2" / "shared.bin")

    r = area_usage(str(root))

    assert r["areas"]["inbox"]["bytes"] == 4096  # billed once
    assert r["areas"]["inbox"]["files"] == 2  # referenced twice
    assert r["by_session"]["s1"]["inbox"] == 4096
    assert r["by_session"]["s2"]["inbox"] == 4096
    assert r["shared_bytes"] == 4096


def test_a_session_area_is_walked_once_per_report(tmp_path, monkeypatch):
    """The report-wide dedup and the session's referenced bytes are two
    views of the SAME walk — a meter that walked each per-session area
    twice would bill the state volume double the metadata cost for one
    number, on every TTL expiry the panel or the sessions list forces.
    Pinned on the walk count, per area."""
    import app.delivery.usage as usage_mod

    root = tmp_path / "state"
    (root / "inbox" / "s1").mkdir(parents=True)
    (root / "inbox" / "s2").mkdir(parents=True)
    (root / "inbox" / "s1" / "a.bin").write_bytes(b"x" * 100)
    (root / "scratch" / "s1" / "workdir").mkdir(parents=True)
    (root / "scratch" / "s1" / "workdir" / "big.bin").write_bytes(b"z" * 1000)

    counts: dict[str, int] = {}
    real_walk = os.walk

    def counting_walk(path, *a, **k):
        counts[os.path.abspath(path)] = counts.get(os.path.abspath(path), 0) + 1
        return real_walk(path, *a, **k)

    monkeypatch.setattr(usage_mod.os, "walk", counting_walk)
    r = area_usage(str(root))

    assert r["by_session"]["s1"]["inbox"] == 100
    assert r["by_session"]["s1"]["scratch"] == 1000
    for area in ("inbox", "scratch"):
        base = os.path.abspath(str(root / area))
        under = {p: n for p, n in counts.items() if p.startswith(base)}
        # the session bucket is walked exactly once (os.walk descends into
        # its subdirs on the way — a second view of the same tree would be
        # a second walk of the bucket)
        assert all(n == 1 for n in under.values()), f"{area} walked more than once: {under}"
    assert os.path.abspath(str(root / "inbox" / "s1")) in counts
    assert os.path.abspath(str(root / "scratch" / "s1")) in counts


def test_usage_meter_caches_and_a_delete_forces_a_rewalk(tmp_path):
    root = tmp_path / "state"
    (root / "inbox" / "s1").mkdir(parents=True)
    m = UsageMeter(str(root), ttl_sec=3600)
    (root / "inbox" / "s1" / "a.bin").write_bytes(b"x" * 10)
    first = m.report()
    (root / "inbox" / "s1" / "b.bin").write_bytes(b"x" * 10)
    assert m.report() is first  # cached: no re-walk per request
    m.invalidate()
    assert m.report()["areas"]["inbox"]["bytes"] == 20


def test_a_delivered_upload_is_billed_to_the_store_not_twice(tmp_path):
    """The bytes live in the store once; the inbox holds a link. So the area
    numbers say what removing that area would actually free: the inbox frees
    nothing, the store frees the bytes — and the session still sees the bytes
    it references, so "what do I hold" never reads 0."""
    c, _ = _client(tmp_path)
    _upload(c)
    body = c.get("/api/v1/usage").json()
    assert body["areas"]["blobs"]["bytes"] == 5
    assert body["areas"]["inbox"] == {"bytes": 0, "files": 1}
    assert body["areas"]["inbox"]["files"] == 1  # referenced by one session
    assert body["by_session"]["s1"]["inbox"] == 5
    assert body["shared_bytes"] == 5


# ---------------------------------------------------------------------- #
# the app-wide stream
# ---------------------------------------------------------------------- #


def test_app_events_reach_the_global_stream_with_their_session():
    """The sessions list must know a session started, was released, or is
    waiting on a dialog — without polling the whole list."""
    gh = SseHub("*")
    feed = EventFeed(hub_getter=lambda sid: None, global_hub=gh)
    feed.add("s1", "started", "slot=slot1")
    feed.add(None, "update_applying", "reason=flag")
    body = [json.loads(e.data) for e in gh._ring]
    assert body[0] == {"session_id": "s1", "detail": "slot=slot1", "ts": body[0]["ts"]}
    assert body[1]["session_id"] is None


@pytest.mark.parametrize("kind", sorted(GLOBAL_EVENTS))
def test_every_listed_kind_reaches_the_global_stream_from_the_feed(kind):
    """The allow-list, from the inside: every kind the sessions list renders
    is mirrored from the feed, so the list never learns about reality late.
    (The driver-sink twin of this pin parametrizes the same tuple.)"""
    gh = SseHub("*")
    feed = EventFeed(hub_getter=lambda sid: None, global_hub=gh)
    feed.add("s1", kind, "detail")
    assert [e.name for e in gh._ring] == [kind]


def test_a_kind_off_the_global_contract_is_session_local():
    """The allow-list, from the outside: a kind the sessions list does not
    render must NOT ride the stream the phone holds open for the process's
    life. It stays on the session's own hub — which is where the open chat
    reads it.

    The kind is pinned on purpose: if the contract changes, this test fails
    and the decision (mirror it globally, or keep it session-local) is made
    in the diff — not silently by a filter picking a different candidate."""
    off = "extension_dialog_answered"  # the list renders no dialog detail
    assert off not in GLOBAL_EVENTS, (
        f"{off!r} is now on the global contract — update the pin"
    )
    sh, gh = SseHub("s1"), SseHub("*")
    feed = EventFeed(hub_getter=lambda sid: sh, global_hub=gh)
    feed.add("s1", off, "detail")
    assert [e.name for e in sh._ring] == [off]  # the session stream still carries it
    assert list(gh._ring) == []


def test_app_events_still_reach_their_own_session_hub():
    sh = SseHub("s1")
    feed = EventFeed(hub_getter=lambda sid: sh, global_hub=SseHub("*"))
    feed.add("s1", "released", "swept 3 paths")
    assert [e.name for e in sh._ring] == ["released"]


def test_global_hub_none_keeps_the_feed_working():
    """The stdio front door has no stream at all: the feed must not care."""
    feed = EventFeed(hub_getter=None, global_hub=None)
    feed.add("s1", "released", "x")
    assert feed.items()[0]["kind"] == "released"


def _sink_for(lc: Lifecycle, hub: SseHub, sid: str = "s1"):
    lc.global_hub = SseHub("*")
    return lc._record_sink(sid, hub)


def _lc_stub() -> Lifecycle:
    return Lifecycle.__new__(Lifecycle)


@pytest.mark.parametrize("kind", sorted(GLOBAL_EVENTS))
def test_run_state_is_mirrored_so_the_list_can_show_busy(kind):
    lc, hub = _lc_stub(), SseHub("s1")
    _sink_for(lc, hub)({"type": kind, "pad": "x" * 500})
    assert [e.name for e in lc.global_hub._ring] == [kind]
    # the mirror carries the session id (and a timestamp), not the payload
    # (a chat frame is the open chat's business; the list only needs "this
    # session changed state"). The feed's ingress additionally carries a
    # human-readable `detail` — it has one; a run-state record does not.
    payload = json.loads(lc.global_hub._ring[0].data)
    assert payload["session_id"] == "s1"
    assert "ts" in payload
    assert "pad" not in payload
    # and the session's own stream still got the whole record
    assert json.loads(hub._ring[0].data)["pad"]


def test_transcript_chatter_is_never_mirrored():
    """Per-token/per-message events are the open chat's business; mirroring
    them would make the sessions list stream the transcript."""
    lc, hub = _lc_stub(), SseHub("s1")
    sink = _sink_for(lc, hub)
    for name in ("message_update", "tool_execution_update", "turn_start"):
        sink({"type": name})
    assert list(lc.global_hub._ring) == []
    assert [e.name for e in hub._ring] == [
        "message_update",
        "tool_execution_update",
        "turn_start",
    ]


def test_global_events_route_is_gated_during_the_stop():
    """503 while the stop path drains (the client's EventSource backs off and
    re-attaches after the restart) rather than opening a stream that the stop
    would never close."""
    from app.api.app import build_app

    lc = _lc_stub()
    lc.shutting_down = True
    lc.global_hub = SseHub("*")
    state = SimpleNamespace(
        registry=SimpleNamespace(get=lambda sid: None),
        lc=lc,
        delivery=None,
        state=None,
        boot_report=0,
    )
    tc = TestClient(build_app(state, SimpleNamespace(browser_origin=None, edge_key="", heartbeat_secs=15)))
    assert tc.get("/api/v1/events").status_code == 503

    lc.shutting_down = False
    with tc.stream("GET", "/api/v1/events") as r:
        # not shutting down: a real stream opens (first frame is the comment)
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("text/event-stream")


def test_global_events_kinds_view_is_filtered_and_validated():
    """?kinds= is a per-subscription view of the app-wide stream: the
    subscriber is delivered only the named kinds, so a screen that needs two
    kinds does not pay the wire bytes for the rest. An unknown kind is a
    400 — the view is a subset of the GLOBAL_EVENTS contract, never a
    rename — with one exception: ``gap`` is the stream's own resync frame
    (delivered under every view), and a view may name it; such a view
    receives no ring frame at all. The unfiltered stream (no param) still
    delivers everything, as the sessions list has always had it."""
    from app.api.app import build_app

    lc = _lc_stub()
    lc.shutting_down = False
    lc.global_hub = SseHub("*")
    lc.global_hub.publish("agent_start", {"session_id": "s1"})
    lc.global_hub.publish("keep_deleted", "s1/a.md")
    state = SimpleNamespace(
        registry=SimpleNamespace(get=lambda sid: None),
        lc=lc,
        delivery=None,
        state=None,
        boot_report=0,
    )
    tc = TestClient(
        build_app(state, SimpleNamespace(browser_origin=None, edge_key="", heartbeat_secs=15))
    )

    # an unknown kind is a client contract error: one finite 400, no stream
    r = tc.get("/api/v1/events", params={"kinds": "keep_deleted,nope"})
    assert r.status_code == 400

    # gap is the stream's own resync frame, not a ring kind: naming it is not
    # a 400 — the view simply receives no ring frame. A fast heartbeat is
    # what proves the stream is open with an empty replay.
    tc_gap = TestClient(
        build_app(
            SimpleNamespace(
                registry=SimpleNamespace(get=lambda sid: None),
                lc=lc,
                delivery=None,
                state=None,
                boot_report=0,
            ),
            SimpleNamespace(browser_origin=None, edge_key="", heartbeat_secs=1),
        )
    )
    with tc_gap.stream("GET", "/api/v1/events", params={"kinds": "gap"}) as r:
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("text/event-stream")
        it = r.iter_text()
        text = ""
        for _ in range(6):
            chunk = next(it, "")
            if not chunk:
                break
            text += chunk
            if ":hb" in text:  # alive, and the replay delivered no ring frame
                break
        assert "event: keep_deleted" not in text
        assert "event: agent_start" not in text

    with tc.stream("GET", "/api/v1/events", params={"kinds": "keep_deleted"}) as r:
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("text/event-stream")
        # the replay of the two published events reaches this view as ONE
        # frame: the agent_start next door is filtered, keep_deleted rides.
        # One iterator, kept across next() calls: httpx marks the stream
        # consumed at first touch, and a fresh iter_text() would refuse.
        it = r.iter_text()
        text = ""
        for _ in range(5):
            chunk = next(it, "")
            if not chunk:
                break
            text += chunk
            if "event: keep_deleted" in text:
                break
        assert "event: keep_deleted" in text
        assert "event: agent_start" not in text

    # no param: the full stream, as the sessions list has always had it
    with tc.stream("GET", "/api/v1/events") as r:
        assert r.status_code == 200
        it = r.iter_text()
        text = ""
        for _ in range(6):
            chunk = next(it, "")
            if not chunk:
                break
            text += chunk
            if "event: keep_deleted" in text and "event: agent_start" in text:
                break
        assert "event: agent_start" in text
        assert "event: keep_deleted" in text
