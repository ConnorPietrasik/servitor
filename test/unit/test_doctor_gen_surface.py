"""check_gen_surface (the guest doctor's live probe of the machine gen
surface) — root-free unit tests.

The probe is WARN, never FAIL, and must never RAISE: a mis-set
SERVITOR_GEN_BASE_URL or a dead panel-api is a report line, not a
deploy-gate traceback — the doctor is the gate, and an uncaught error in
a check skips every later check and misreads the whole run as a FAIL.
A loopback http.server stands in for the machine door's list route
(the first route every gen CLI call takes, the one the probe hits) and
enforces the Bearer key (401 = key drift, the machine door key IS the
LLM router key).
"""
from __future__ import annotations

import threading
import types
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from app.doctor import PASS, Report, SKIP, WARN, check_gen_surface

KEY = "unit-test-machine-key"


class _Door:
    """Machine-door stand-in: the list route with the bearer gate (401
    on missing/wrong key) — the verdict shape of every /machine/gen
    route. ``status``/``body`` model a healthy or erroring surface."""

    def __init__(self, key: str, status: int = 200,
                 body: bytes = b'{"jobs": [], "next": null}') -> None:
        self.key = key
        self.status = status
        self.body = body
        self.seen_paths: list[str] = []

    def make_server(self) -> ThreadingHTTPServer:
        door = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                door.seen_paths.append(self.path)
                if self.headers.get("Authorization") != f"Bearer {door.key}":
                    self.send_response(401)
                    self.end_headers()
                    self.wfile.write(b'{"detail": "bad or absent key"}')
                    return
                self.send_response(door.status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(door.body)))
                self.end_headers()
                self.wfile.write(door.body)

            def log_message(self, *args):
                pass  # no request noise in the test output

        return ThreadingHTTPServer(("127.0.0.1", 0), Handler)


def _run(base_url: str, monkeypatch, *, llm_key: str = KEY) -> Report:
    """One probe of ``base_url`` (no server of its own: the closed-port
    URL is a legitimate target). The LLM key rides env the way the
    guest env file gives it (delenv, not merely not-set: the repo .env
    sourced by `make test` may carry one)."""
    if llm_key is None:
        monkeypatch.delenv("OMNISSIAH_API_KEY", raising=False)
    else:
        monkeypatch.setenv("OMNISSIAH_API_KEY", llm_key)
    rep = Report()
    check_gen_surface(
        rep,
        types.SimpleNamespace(gen_base_url=base_url, api_key_env="OMNISSIAH_API_KEY"),
    )
    assert len(rep.results) == 1
    return rep


def _check(monkeypatch, *, llm_key: str = KEY, door_key: str = KEY,
           status: int = 200) -> tuple[Report, _Door]:
    """One probe of a LIVE loopback door (the URL is built from it)."""
    door = _Door(door_key, status)
    server = door.make_server()
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        rep = _run(
            f"http://127.0.0.1:{server.server_address[1]}/machine/gen",
            monkeypatch, llm_key=llm_key,
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
    return rep, door


def test_unset_is_skip(monkeypatch):
    rep = _run("", monkeypatch)
    assert rep.results[0].status == SKIP


def test_mis_set_url_warns_without_crashing(monkeypatch):
    """A scheme-less value must be a report line, not a traceback: the
    regression this file exists to keep dead (an uncaught ValueError
    here skips every later doctor check and misreads the run as a FAIL)."""
    rep = _run("panel.local/machine/gen", monkeypatch)
    assert rep.results[0].status == WARN
    assert "not an absolute http(s) URL" in rep.results[0].detail


def test_no_llm_key_warns(monkeypatch):
    rep = _run("http://127.0.0.1:1/machine/gen", monkeypatch, llm_key=None)
    assert rep.results[0].status == WARN
    assert "no LLM key" in rep.results[0].detail


def test_healthy_surface_passes_on_the_list_route(monkeypatch):
    """The probe hits {base}/jobs — the same first route every CLI call
    takes — and presents the bearer key."""
    rep, door = _check(monkeypatch)
    assert rep.results[0].status == PASS
    assert door.seen_paths == ["/machine/gen/jobs"]


def test_key_drift_warns(monkeypatch):
    """The doctor's env carries a DIFFERENT key than the door enforces:
    401 is the drift signature — WARN (a panel-key issue must not gate a
    guest deploy; the gen CLI fails closed per call), with the reason."""
    rep, door = _check(monkeypatch, llm_key="not-the-key")
    assert rep.results[0].status == WARN
    assert "rejected the key (401)" in rep.results[0].detail
    assert door.seen_paths == ["/machine/gen/jobs"]


def test_down_surface_warns(monkeypatch):
    rep = _run("http://127.0.0.1:1/machine/gen", monkeypatch)
    assert rep.results[0].status == WARN
    assert "probe failed" in rep.results[0].detail


def test_erroring_surface_warns(monkeypatch):
    rep, _ = _check(monkeypatch, status=500)
    assert rep.results[0].status == WARN
    assert "answered 500" in rep.results[0].detail
