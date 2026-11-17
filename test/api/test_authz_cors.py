"""Authz, CORS, and API-key hygiene gates.

- A session can only reach its own mount: unknown/released sessions are
  404 (existence never leaked); file paths cannot escape the session
  dirs; one session's files are not reachable through another's.
- CORS: the configured browser origin is allowed; any other origin is
  not (no ACAO header — the browser enforces).
- API key: the browser holds it (sessionStorage); the API may carry it
  in session creation (env-only for the spawn) — it must NEVER appear
  in any response body, header, or SSE frame.
"""
from __future__ import annotations

import json

SECRET = "SUPERSECRET-KEY-DO-NOT-LEAK-12345"


def test_unknown_and_released_sessions_are_404(api):
    for path in (
        "/api/v1/sessions/00000000-0000-4000-8000-000000000000",
        "/api/v1/sessions/does-not-exist/files",
        "/api/v1/sessions/does-not-exist/events",
        "/api/v1/sessions/does-not-exist/probe",
    ):
        r = api.get(path)
        assert r.status_code == 404, f"{path} -> {r.status_code}"

    res = api.create_session("authz-released")
    sid = res["session_id"]
    api.release(sid)
    # released: gone (404), not a dangling 200
    r = api.get(f"/api/v1/sessions/{sid}")
    assert r.status_code == 404
    r = api.get(f"/api/v1/sessions/{sid}/files")
    assert r.status_code == 404
    r = api.post(f"/api/v1/sessions/{sid}/prompt", json_body={"text": "x"})
    assert r.status_code == 404


def test_sessions_cannot_reach_each_others_files(api):
    """Two live sessions: A's file listing and downloads see only A's
    outbox/inbox; B's files are not reachable via A (and vice versa)."""
    ra = api.create_session("authz-a")
    rb = api.create_session("authz-b")
    try:
        # A uploads a file; it must not appear in B's listing
        r = api.post(
            f"/api/v1/sessions/{ra['session_id']}/files/upload?name=secret-of-a",
            content=b"belongs to A",
        )
        assert r.status_code == 201

        la = api.get(f"/api/v1/sessions/{ra['session_id']}/files").json()
        lb = api.get(f"/api/v1/sessions/{rb['session_id']}/files").json()
        assert any(u["name"] == "secret-of-a" for u in la["uploads"])
        assert not any(u["name"] == "secret-of-a" for u in lb["uploads"]), (
            "B sees A's upload"
        )
        # B cannot download A's file by name (its own inbox doesn't have it)
        r = api.get(f"/api/v1/sessions/{rb['session_id']}/files/uploads/secret-of-a")
        assert r.status_code == 404
        # and A's outbox cannot be reached through B's path
        r = api.get(f"/api/v1/sessions/{rb['session_id']}/files/outbox/anything")
        assert r.status_code == 404
    finally:
        api.release(ra["session_id"])
        api.release(rb["session_id"])


def test_cors_configured_origin_only(api, api_env):
    origin = api_env["origin"]
    other = api_env["other_origin"]

    # preflight from the configured origin: allowed
    r = api.post(
        "/api/v1/sessions",
        headers={
            "Origin": origin,
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "content-type",
        },
        json_body={},
        timeout=10,
    )
    # the preflight itself is handled by the middleware (200 + headers)
    assert r.status_code in (200, 204), r.text
    assert r.headers.get("access-control-allow-origin") == origin

    # a request from the configured origin gets the header
    r = api.get("/healthz", headers={"Origin": origin})
    assert r.headers.get("access-control-allow-origin") == origin

    # another origin: no ACAO header (the browser blocks it)
    r = api.get("/healthz", headers={"Origin": other})
    assert r.headers.get("access-control-allow-origin") is None

    # no origin at all (curl, the panel, tests): no CORS headers, works
    r = api.get("/healthz")
    assert r.status_code == 200
    assert r.headers.get("access-control-allow-origin") is None


def test_api_key_never_leaks(api, api_env):
    """Create a session with the key in the body (the browser holds it);
    run a run; sweep every response body, every header, and the full
    SSE stream — the key must appear nowhere."""
    res = api.create_session("key-hygiene", api_key=SECRET)
    sid = res["session_id"]
    seen: list[str] = []

    def sweep(label: str, body: bytes, headers=None):
        seen.append(f"{label}: {body.decode('utf-8', 'replace')}")
        if headers:
            seen.append(f"{label}-headers: {json.dumps(dict(headers))}")

    sweep("create", json.dumps(res).encode())
    d = api.prompt(sid, "hello")
    sweep("prompt", json.dumps(d).encode())
    # SSE: read the whole stream to the settle
    frames, _ = api.sse_read(sid, after=0, until_settled=True, timeout=120)
    for (_i, n, data) in frames:
        seen.append(f"sse:{n}: {data}")
    row = api.get(f"/api/v1/sessions/{sid}")
    sweep("row", row.content, row.headers)
    files = api.get(f"/api/v1/sessions/{sid}/files")
    sweep("files", files.content, files.headers)
    rel = api.release(sid)
    sweep("release", json.dumps(rel).encode())

    blob = "\n".join(seen)
    assert SECRET not in blob, "the API key leaked into the API surface"
    assert "test-super-secret-key" not in blob, "the env key leaked too"
