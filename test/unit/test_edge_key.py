"""Edge credential gate — root-free unit tests.

The EdgeKeyMiddleware is pure-ASGI and wraps the WHOLE app (route-level
dependencies never reach the /assets mount, so the gate has to sit there
to cover everything). Every request must carry ``X-Servitor-Edge-Key``
with the value the edge (Traefik's ``servitor-edge`` middleware) injects
after the auth chain; the app constant-time-verifies it and answers one
uniform 403 for missing vs wrong. The gate is active only when the key
is set: an unset/empty key means no gate at all — dev/standalone runs
authenticate nobody (the bind is the control); production is always
keyed (baked into the image, key slice, doctor probes).

The app runs against a fake app state (no lifespan — TestClient is used
without a context), per the test_fork_gate pattern.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

from fastapi.testclient import TestClient

import app.api.app as app_mod
from app.api.app import EDGE_KEY_HEADER, EdgeKeyMiddleware, build_app

KEY = "unit-test-edge-key"
H = {EDGE_KEY_HEADER: KEY}
INDEX = "<html>index</html>\n"


def _client(tmp_path, monkeypatch, *, edge_key: str | None = KEY):
    dist = tmp_path / "dist"
    dist.mkdir(parents=True, exist_ok=True)
    (dist / "index.html").write_text(INDEX)
    monkeypatch.setattr(app_mod, "_ui_dist", lambda: dist)
    app_state = SimpleNamespace(
        registry=SimpleNamespace(
            get=lambda sid: None,
            list_sessions=lambda include_released: [],
        ),
        lc=SimpleNamespace(live={}),
        boot_report=0,
    )
    return TestClient(
        build_app(app_state, SimpleNamespace(browser_origin=None, edge_key=edge_key))
    )


# ---- the gate itself ------------------------------------------------------


def test_keyless_request_rejected(tmp_path, monkeypatch):
    c = _client(tmp_path, monkeypatch)
    r = c.get("/healthz")
    assert r.status_code == 403
    assert r.json() == {"detail": "forbidden"}


def test_wrong_key_rejected_like_missing(tmp_path, monkeypatch):
    """No oracle: a wrong key gets the SAME 403 body as a missing one."""
    c = _client(tmp_path, monkeypatch)
    r = c.get("/healthz", headers={EDGE_KEY_HEADER: "not-the-key"})
    assert r.status_code == 403
    assert r.json() == c.get("/healthz").json()


def test_keyed_request_admitted(tmp_path, monkeypatch):
    c = _client(tmp_path, monkeypatch)
    r = c.get("/healthz", headers=H)
    assert r.status_code == 200
    assert r.json()["ok"] is True


def test_unset_key_disables_the_gate(tmp_path, monkeypatch):
    """Dev/standalone mode: no key configured = no gate — the app
    authenticates nobody, the bind is the control (the gate is active
    only when the key is set). An empty key is the same as unset."""
    c = _client(tmp_path, monkeypatch, edge_key=None)
    assert c.get("/healthz").status_code == 200
    assert c.get("/api/v1/sessions").status_code == 200
    assert c.get("/").text == INDEX
    c2 = _client(tmp_path, monkeypatch, edge_key="")
    assert c2.get("/healthz").status_code == 200
    assert c2.get("/healthz", headers=H).status_code == 200


# ---- the gate covers EVERY route, not just the API ------------------------


def test_api_routes_gated(tmp_path, monkeypatch):
    c = _client(tmp_path, monkeypatch)
    for path in (
        "/api/v1/sessions",
        "/api/v1/sessions/sid-x/events",
        "/api/v1/sessions/sid-x/files",
        "/panel/sessions/sid-x/action",
    ):
        assert c.get(path).status_code == 403, path
        assert c.post(path).status_code == 403, path
    # and the same paths answer with the credential
    assert c.get("/api/v1/sessions", headers=H).status_code == 200


def test_spa_fallback_and_assets_gated(tmp_path, monkeypatch):
    """The static UI rides the same port: without the credential even the
    shell is invisible to an in-subnet caller (the middleware wraps the
    mount + fallback, which route dependencies cannot reach)."""
    c = _client(tmp_path, monkeypatch)
    assert c.get("/").status_code == 403
    assert c.get("/assets/x.js").status_code == 403
    assert c.get("/", headers=H).text == INDEX
    # unknown UI path falls back to the SPA index (with the credential)
    assert c.get("/s/whatever", headers=H).text == INDEX


# ---- the gate covers websocket upgrades too (defensive: SSE is the
#      transport, the app has no WS routes — but "EVERY request" must not
#      depend on that staying true) ------------------------------------------


def _drive_ws(key: str | None, header: str | None):
    """Drive one websocket upgrade through EdgeKeyMiddleware against a
    recording app. Returns the event list: "app" (the inner app was
    called), "connect" (the app received the connect message — it expects
    to be FIRST), "websocket.accept" (the app accepted), "websocket.close"
    (the middleware refused the upgrade without ever calling the app)."""
    events: list = []

    async def fake_app(scope, receive, send):
        events.append("app")
        msg = await receive()
        assert msg["type"] == "websocket.connect", "the app must see connect first"
        events.append("connect")
        await send({"type": "websocket.accept"})  # recorded by the send wrapper

    headers = [(b"host", b"t")]
    if header is not None:
        headers.append(
            (EDGE_KEY_HEADER.lower().encode("latin-1"), header.encode("latin-1"))
        )
    scope = {"type": "websocket", "path": "/ws", "headers": headers}
    mw = EdgeKeyMiddleware(fake_app, (key or "").encode("latin-1"))

    async def main():
        async def receive():
            return {"type": "websocket.connect"}

        async def send(message):
            events.append(message["type"])

        await mw(scope, receive, send)

    asyncio.run(main())
    return events


def test_ws_upgrade_keyless_rejected_before_the_app():
    events = _drive_ws(KEY, header=None)
    assert "app" not in events  # the app is never called
    assert events == ["websocket.close"]  # the server turns this into a 403


def test_ws_upgrade_wrong_key_rejected_like_missing():
    events = _drive_ws(KEY, header="not-the-key")
    assert events == ["websocket.close"]


def test_ws_upgrade_keyed_admitted_and_connect_delivered_first():
    events = _drive_ws(KEY, header=KEY)
    assert events == ["app", "connect", "websocket.accept"]


def test_ws_gate_off_when_key_unset():
    """Dev/standalone: no key configured = no gate, same as http — the
    upgrade reaches the app with its untouched receive."""
    events = _drive_ws(None, header=None)
    assert events == ["app", "connect", "websocket.accept"]


def test_lifespan_passes_through_when_keyed():
    """A gate that swallowed lifespan would silently kill startup/shutdown.
    """
    events: list = []

    async def fake_app(scope, receive, send):
        events.append(scope["type"])
        msg = await receive()
        await send({"type": "lifespan.startup.complete"})

    mw = EdgeKeyMiddleware(fake_app, KEY.encode("latin-1"))

    async def main():
        async def receive():
            return {"type": "lifespan.startup"}

        async def send(message):
            events.append(message["type"])

        await mw({"type": "lifespan"}, receive, send)

    asyncio.run(main())
    assert events == ["lifespan", "lifespan.startup.complete"]
