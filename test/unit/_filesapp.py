"""Shared fake app_state for the file routes' unit tests (not a test module).

Imported as ``unit._filesapp``: ``test/`` has no ``__init__.py``, so pytest
puts ``test/`` on sys.path and the package name is the directory name.

Layout comes from ``state_paths()`` and ``Delivery.files()`` — never from a
hand-built path here. A test that spells the outbox ``scratch/<sid>/workdir/
outbox`` keeps passing after the areas move and then quietly compares the
listing against a directory the app never reads.

The delivery routes need four collaborators; a stub with *more* behaviour
than the real one (a fake watcher, a fake meter) would hide the very
interactions under test, so the real ``FileWatch``/``UsageMeter``/``Delivery``
are used over a tmp state volume, and only the registry/lifecycle are stubs.
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from fastapi.testclient import TestClient

from app.api.app import EDGE_KEY_HEADER, build_app
from app.api.sse import SseHub
from app.delivery.service import Delivery
from app.delivery.usage import UsageMeter
from app.delivery.watch import FileWatch
from app.iso.layout import state_paths

SID = "s1"
SIDS = ("s1", "s2")  # two sessions: cross-session attach is a main use case


def slot_identity(uid, home="/nonexistent-home"):
    """A SlotIdentity-shaped stub for the gate (real uids are discovered from
    passwd, so tests inject the identity they want to exercise)."""
    return SimpleNamespace(name="slot9", uid=uid, gid=uid, home=home)


def make_client(
    tmp_path,
    *,
    sid: str = SID,
    sids: tuple[str, ...] = SIDS,
    slot: int | None = None,
    edge_key: str = "k",
    slot_ident=None,
    monkeypatch=None,
    mint_hubs: bool = True,
):
    """(client, app_state, state_root). ``slot`` is the row's slot uid — set
    it to exercise the read gate through the routes; None means unmounted
    (the gate is open, which is what the dev/no-slot case is). Pass
    ``slot_ident`` + the ``monkeypatch`` fixture to pin the identity the API
    resolves from passwd (a test tree cannot chown files as an unprivileged
    user, so the identity comes to the test instead). ``mint_hubs=False``
    starts with no session streams (no tab open): with the stub's minting
    ``hub``, that lets a test prove the routes asked for no stream nobody
    asked for."""
    root = Path(tmp_path) / "state"
    state = state_paths(str(root))
    wanted = dict.fromkeys((sid, *sids))
    for s in wanted:
        for d in (
            state.inbox_for(s),
            state.outbox_for(s),
            state.sessions_for(s),
            state.scratch_for(s),
        ):
            Path(d).mkdir(parents=True, exist_ok=True)
    rows = {s: SimpleNamespace(id=s, display_name=s, slot=slot, state="live") for s in wanted}
    hubs = {s: SseHub(s) for s in wanted if mint_hubs}  # real rings

    def hub(s):
        # the real getter's contract: mint on first use for a known row,
        # None otherwise — a stub that mints nothing would let a route
        # regression (asking for a stream nobody asked for) pass silently
        h = hubs.get(s)
        if h is None and s in wanted:
            h = hubs[s] = SseHub(s)
        return h

    app_state = SimpleNamespace(
        registry=SimpleNamespace(
            get=lambda s: rows.get(s),
            rows=lambda: list(rows.values()),
        ),
        lc=SimpleNamespace(
            live={},
            cfg=SimpleNamespace(gc_idle_days=14.0),
            events=SimpleNamespace(add=lambda *a, **k: None),
            files=FileWatch(),
            usage=UsageMeter(str(root)),
            hub=hub,
            hub_if_present=lambda s: hubs.get(s),
            shutting_down=False,
        ),
        delivery=Delivery(root),
        state=state,
        boot_report=0,
    )
    if monkeypatch is not None:
        monkeypatch.setattr("app.api.app.slot_identity", lambda _name: slot_ident)
    client = TestClient(
        build_app(
            app_state,
            SimpleNamespace(browser_origin=None, edge_key=edge_key, heartbeat_secs=15),
        ),
        headers={EDGE_KEY_HEADER: edge_key} if edge_key else {},
    )
    return client, app_state, root


def upload(client, name: str = "a.txt", body: bytes = b"hello", sid: str = SID):
    return client.post(
        f"/api/v1/sessions/{sid}/files/upload?name={name}",
        content=body,
        headers={"content-type": "application/octet-stream"},
    )
