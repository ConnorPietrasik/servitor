"""check_ui (the guest doctor's self-probe of the app's UI) — root-free
unit tests.

The doctor runs in-guest with the guest env file sourced (the host's
deploy/doctor.sh does ``set -a; . /etc/servitor/servitor.env`` before
``python -m app.doctor``). In production the app key-gates every route
(EdgeKeyMiddleware in app/api/app.py), so the probe must
present SERVITOR_EDGE_KEY: a keyless probe against a healthy, gated app
403s and turns ``ui.index`` (and with it the whole guest certification)
red. A loopback http.server stands in for the app and replicates exactly
that gate: no key / wrong key → 403, the right key → 200 text/html.
"""
from __future__ import annotations

import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from app.doctor import FAIL, PASS, Report, check_ui

KEY = "unit-test-edge-key"
HEADER = "X-Servitor-Edge-Key"


class _Gate:
    """App stand-in: records the header it sees and, when ``key`` is set,
    answers 403 to every request that does not carry it — the
    EdgeKeyMiddleware's verdict shape (uniform 403, missing == wrong)."""

    def __init__(self, key: str | None) -> None:
        self.key = key
        self.seen: list[str | None] = []

    def make_server(self) -> ThreadingHTTPServer:
        gate = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                gate.seen.append(self.headers.get(HEADER))
                if gate.key is not None and self.headers.get(HEADER) != gate.key:
                    self.send_response(403)
                    self.end_headers()
                    self.wfile.write(b'{"detail": "forbidden"}')
                    return
                body = b"<html>index</html>\n"
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass  # no request noise in the test output

        return ThreadingHTTPServer(("127.0.0.1", 0), Handler)


def _check(server_key: str | None, env_key: str | None, monkeypatch) -> tuple[Report, _Gate]:
    """Run check_ui against a loopback app stand-in. ``server_key`` is
    the key the app enforces (None = dev/standalone, gate off);
    ``env_key`` is what the doctor's env carries (None = unset — the repo
    .env sourced by `make test` may set it, so the unset case must
    delenv, not merely not-set)."""
    gate = _Gate(server_key)
    server = gate.make_server()
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setenv("SERVITOR_BIND", "127.0.0.1")
    monkeypatch.setenv("SERVITOR_PORT", str(server.server_address[1]))
    if env_key is None:
        monkeypatch.delenv("SERVITOR_EDGE_KEY", raising=False)
    else:
        monkeypatch.setenv("SERVITOR_EDGE_KEY", env_key)
    try:
        rep = Report()
        check_ui(rep)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
    assert len(rep.results) == 1
    return rep, gate


def test_keyed_probe_presents_the_key(monkeypatch):
    """Production shape: the app enforces the key, the doctor's env has
    the same value — the probe must PASS and, above all, the app must
    have SEEN the header (a keyless probe 403s: the regression this
    file exists to keep dead)."""
    rep, gate = _check(KEY, KEY, monkeypatch)
    assert gate.seen == [KEY], "the probe did not present the edge key"
    assert rep.results[0].status == PASS


def test_keyless_dev_probe_unchanged(monkeypatch):
    """Dev/standalone: no key anywhere — gate off, a bare GET is right,
    and the probe sends no header."""
    rep, gate = _check(None, None, monkeypatch)
    assert gate.seen == [None]
    assert rep.results[0].status == PASS


def test_key_drift_fails_loud(monkeypatch):
    """The doctor's env carries a DIFFERENT value than the app enforces:
    the probe must FAIL (403), not pass silently — a drift is a red gate
    that deploy/sync.sh fixes by re-injecting the key slice."""
    rep, gate = _check(KEY, "not-the-key", monkeypatch)
    assert gate.seen == ["not-the-key"]
    assert rep.results[0].status == FAIL
