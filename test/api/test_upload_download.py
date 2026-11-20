"""Upload/download end-to-end gates.

- 30 MB binary upload through the real pipeline: stored in full,
  byte-identical (sha256), MIME by magic (not extension), no truncation.
- Download an agent-created file of known contents: byte-identical.
- Real artifact flow: the agent produces an artifact during a run (a
  scripted bash tool call writes outbox/report.txt); the artifact list
  reflects only what the agent produced; download matches.
- No caps: a 100 MB upload is served (no 413); a 10 MB single SSE event
  is not truncated (see test/unit/test_sse_hub.py).
"""
from __future__ import annotations

import hashlib
import os
import secrets
import time


PNG_MAGIC = (
    b"\x89PNG\r\n\x1a\n"
    b"\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06"
    b"\x00\x00\x00\x1f\x15\xc4\x89"
)


def _sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def test_upload_30mb_byte_identical_mime_by_magic(api, api_env):
    """30 MB binary: stored in full, byte-identical on download, MIME
    from magic bytes (the content starts with a PNG signature but the
    name has no extension — the mime must be image/png, not octet-stream
    or anything extension-derived)."""
    pad = secrets.token_bytes(30 * 1024 * 1024 - len(PNG_MAGIC))
    payload = PNG_MAGIC + pad
    digest = _sha(payload)

    r = api.post(
        f"/api/v1/sessions/__nope__/files/upload?name=binder", content=payload
    )
    assert r.status_code == 404, "upload to an unknown session must be 404"

    res = api.create_session("upload-test")
    sid = res["session_id"]
    try:
        r = api.post(
            f"/api/v1/sessions/{sid}/files/upload?name=binder", content=payload
        )
        assert r.status_code == 201, r.text
        body = r.json()
        assert body["sha256"] == digest, "server-side sha256 must match the upload"
        assert body["size"] == len(payload)
        assert body["mime"] == "image/png", (
            f"MIME must come from magic bytes (PNG signature), got {body['mime']}"
        )

        # download back: byte-identical
        r = api.get(f"/api/v1/sessions/{sid}/files/uploads/binder")
        assert r.status_code == 200
        got = r.content
        assert len(got) == len(payload), "truncation"
        assert _sha(got) == digest, "download is not byte-identical"
        assert r.headers["content-type"].startswith("image/png")

        # the files listing shows the upload (and its magic mime)
        r = api.get(f"/api/v1/sessions/{sid}/files")
        assert r.status_code == 200
        up = r.json()["uploads"]
        assert any(u["name"] == "binder" and u["sha256"] == digest for u in up)
    finally:
        api.release(sid)


def test_agent_artifact_flow_download_byte_identical(api, api_env):
    """The agent produces an artifact during a run (scripted bash tool
    call writes outbox/report.txt); the artifact list shows only agent
    output; download is byte-identical."""
    artifact = f"artifact-bytes: {secrets.token_hex(16)}\n".encode()
    router = api_env["router"]
    router.set_script(
        {
            "gap_seconds": 0.2,
            "tools": [
                {
                    "name": "bash",
                    "arguments": {
                        "command": f"mkdir -p outbox && printf '%s' '{artifact.decode()}' > outbox/report.txt"
                    },
                }
            ],
        }
    )
    res = api.create_session("artifact-test")
    sid = res["session_id"]
    try:
        d = api.prompt(sid, "build the report")
        assert d["disposition"] == "started"
        st = api.settle(sid)
        assert st["aborted"] is False

        r = api.get(f"/api/v1/sessions/{sid}/files")
        assert r.status_code == 200
        outbox = r.json()["outbox"]
        paths = [f["path"] for f in outbox]
        assert "report.txt" in paths, f"agent artifact missing from listing: {paths}"
        # only what the agent produced: no config files, no inbox files
        assert all(p.startswith("report") for p in paths), paths

        r = api.get(f"/api/v1/sessions/{sid}/files/outbox/report.txt")
        assert r.status_code == 200
        assert r.content == artifact, "artifact download is not byte-identical"
    finally:
        api.release(sid)


def test_upload_100mb_no_413(api, api_env):
    """No caps: a 100 MB upload is served exactly like a 100 B one."""
    res = api.create_session("big-upload")
    sid = res["session_id"]
    try:
        chunk = secrets.token_bytes(1024 * 1024)
        size = 100 * 1024 * 1024
        h = hashlib.sha256()

        def gen():
            sent = 0
            while sent < size:
                n = min(len(chunk), size - sent)
                h.update(chunk[:n])
                sent += n
                yield chunk[:n]

        t0 = time.time()
        r = api.post(
            f"/api/v1/sessions/{sid}/files/upload?name=big.bin",
            content=gen(),
            timeout=600,
        )
        dt = time.time() - t0
        assert r.status_code == 201, f"100 MB upload must not 413 (got {r.status_code})"
        body = r.json()
        assert body["sha256"] == h.hexdigest()
        assert body["size"] == size
        # the stored blob is complete (re-download a byte of it via listing)
        r = api.get(f"/api/v1/sessions/{sid}/files")
        assert any(u["name"] == "big.bin" and u["size"] == size for u in r.json()["uploads"])
        assert dt < 300, f"upload took {dt:.0f}s (should stream, not buffer)"
    finally:
        api.release(sid)


def test_path_traversal_is_404(api):
    """File paths cannot escape the session dirs.

    Traversal paths are sent RAW (http.client putrequest — no client-side
    URL normalization; a normalizing client turns ``outbox/..`` into the
    listing URL before it ever reaches the server, which is fine, but the
    server must reject the raw form too)."""
    import http.client
    import socket

    res = api.create_session("traversal")
    sid = res["session_id"]
    from urllib.parse import urlparse
    p = urlparse(api.base)
    host, port = p.hostname, p.port
    try:
        for evil in (
            "outbox/../../registry.db",
            "uploads/..%2f..%2fregistry.db",
            "outbox/..",
            "outbox/....//",
        ):
            c = http.client.HTTPConnection(host, port, timeout=10)
            c.putrequest("GET", f"/api/v1/sessions/{sid}/files/{evil}")
            c.endheaders()
            r = c.getresponse()
            r.read()
            c.close()
            assert r.status in (404, 400), f"{evil} -> {r.status}"
        r = api.get(f"/api/v1/sessions/{sid}/files/not-a-known-prefix/x")
        assert r.status_code == 404
    finally:
        api.release(sid)
