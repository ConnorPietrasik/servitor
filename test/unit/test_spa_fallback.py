"""SPA fallback static serving (root-free unit tests).

The ``/{path:path}`` fallback is the only hand-rolled file-serving
route (API/panel/healthz routes are registered earlier and win). The
path arrives percent-decoded (``%2f`` == ``/``), so a name that is
lexically inside ``ui/dist`` can still walk out via ``..`` — and the
app runs as root, so an uncontained path is a root file-read
(/etc/servitor/servitor.env holds the only secrets, invariant 5).
The fallback serves only files that RESOLVE inside the dist (an
escaping symlink fails the check too); anything else falls back to
the SPA index — an indistinguishable 200, never a leak of the
escaped file's existence.

The dist is a scratch dir under tmp_path (ui/dist is a gitignored
build artifact, absent in fresh checkouts) — ``_ui_dist`` is
monkeypatched to point at it. The app runs against a fake app state
(no lifespan — TestClient is used without a context), per the
test_fork_gate pattern.
"""
from __future__ import annotations

from types import SimpleNamespace

from fastapi.testclient import TestClient

import app.api.app as app_mod
from app.api.app import EDGE_KEY_HEADER, build_app

INDEX = "<html>index</html>\n"
SECRET = "OMNISSIAH_API_KEY=super-secret-router-bearer\n"


def _client(tmp_path, monkeypatch):
    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "subdir").mkdir()
    (dist / "index.html").write_text(INDEX)
    (dist / "known.txt").write_text("known asset\n")
    # outside the dist — what a root traversal would reach in the guest:
    secret = tmp_path / "servitor.env"
    secret.write_text(SECRET)
    # an escaping symlink planted inside the dist:
    (dist / "link-out").symlink_to(secret)

    monkeypatch.setattr(app_mod, "_ui_dist", lambda: dist)
    app_state = SimpleNamespace(
        registry=SimpleNamespace(get=lambda sid: None),
        lc=SimpleNamespace(live={}),
        boot_report=0,
    )
    return TestClient(
        build_app(app_state, SimpleNamespace(browser_origin=None, edge_key="unit-test-edge-key")),
        headers={EDGE_KEY_HEADER: "unit-test-edge-key"},
    )


def test_index_and_known_files_served(tmp_path, monkeypatch):
    c = _client(tmp_path, monkeypatch)
    r = c.get("/")
    assert r.status_code == 200 and r.text == INDEX
    r = c.get("/index.html")
    assert r.status_code == 200 and r.text == INDEX
    r = c.get("/known.txt")
    assert r.status_code == 200 and r.text == "known asset\n"
    # a client-side route still falls back to the index
    r = c.get("/s/deadbeef")
    assert r.status_code == 200 and r.text == INDEX


def test_percent_encoded_traversal_never_leaks(tmp_path, monkeypatch):
    c = _client(tmp_path, monkeypatch)
    # %2f arrives decoded: the param IS "../..." — containment must be
    # checked on the RESOLVED path, not the lexical one. (httpx
    # normalizes literal unencoded dot segments client-side; the
    # percent-encoded form is the wire vector.)
    for target in (
        "/..%2fservitor.env",
        "/..%2f..%2f..%2fservitor.env",
        "/..%2f..%2f..%2fetc%2fpasswd",
    ):
        r = c.get(target)
        assert "super-secret" not in r.text, f"secret leaked via {target}"
        assert "/etc/passwd" not in r.text, f"passwd leaked via {target}"
        assert r.text == INDEX, (
            f"{target}: escaped path must fall back to the SPA index, "
            f"got {r.status_code} {r.text[:60]!r}"
        )


def test_escaping_symlink_never_leaks(tmp_path, monkeypatch):
    c = _client(tmp_path, monkeypatch)
    r = c.get("/link-out")
    assert "super-secret" not in r.text
    assert r.text == INDEX


def test_directory_path_falls_back_to_index(tmp_path, monkeypatch):
    c = _client(tmp_path, monkeypatch)
    r = c.get("/subdir")
    assert r.status_code == 200 and r.text == INDEX


def test_api_ish_prefix_still_404s(tmp_path, monkeypatch):
    c = _client(tmp_path, monkeypatch)
    r = c.get("/api/v1/nope")
    assert r.status_code == 404
