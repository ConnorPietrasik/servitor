"""Stage-3 crash drill: kill -9 the app mid-upload.

The staged blob must be EITHER fully present and registered OR absent —
no half-blobs, no partial inbox files, no unregistered staging that
survives the restart. After the restart, a fresh upload of the same
content completes byte-identical (the delivery pipeline is undamaged).
"""
from __future__ import annotations

import hashlib
import json
import secrets
import threading
import time
from pathlib import Path

import httpx

from conftest import relaunch_app  # noqa: F401  (importable in tests)


def test_kill9_mid_upload_no_half_blobs(api_env):
    env = api_env
    base = env["base"]
    proc = env["proc"]
    state: Path = env["state_root"]

    res = httpx.post(base + "/api/v1/sessions", json={"display_name": "crash"}, timeout=30)
    assert res.status_code == 200
    sid = res.json()["session_id"]

    payload = secrets.token_bytes(16 * 1024 * 1024)
    digest = hashlib.sha256(payload).hexdigest()

    # start a SLOW upload in a thread, then kill -9 the app mid-stream.
    # The content is throttled on the client side (~50 ms per 64 KB
    # chunk ≈ 250 KB/s): 16 MB takes ~60 s, so the kill at 2 s lands
    # deterministically mid-stream. (Unthrottled, loopback delivers
    # 16 MB in well under a second and the upload completes before the
    # kill — the drill would not be testing a crash mid-upload.)
    state_box: dict = {"code": None, "err": None}

    def slow_chunks():
        for i in range(0, len(payload), 65536):
            time.sleep(0.05)
            yield payload[i : i + 65536]

    def uploader():
        try:
            r = httpx.post(
                f"{base}/api/v1/sessions/{sid}/files/upload?name=big.bin",
                content=slow_chunks(),
                timeout=600,
            )
            state_box["code"] = r.status_code
        except Exception as e:  # the connection dies with the app
            state_box["err"] = f"{type(e).__name__}: {e}"

    t = threading.Thread(target=uploader)
    t.start()
    time.sleep(2.0)  # mid-stream: ~2 MB of the 16 MB delivered
    proc.kill()
    proc.wait()
    t.join(timeout=30)
    # the upload could not have completed (the app is dead)
    assert state_box["code"] != 201

    # --- the crash left no half-visible state -------------------------
    inbox = state / "inbox" / sid
    if inbox.is_dir():
        partials = [p.name for p in inbox.iterdir() if p.name.startswith(".incoming-")]
        assert not partials, f"partial inbox files: {partials}"
    manifest = state / "sessions" / sid / "files.json"
    if manifest.is_file():
        man = json.loads(manifest.read_text())
        for u in man.get("uploads", []):
            assert (inbox / u["name"]).is_file(), f"manifest entry without file: {u}"

    # --- restart: boot reconciliation + delivery GC --------------------
    proc2, base2 = relaunch_app(env)
    try:
        # boot GC swept the interrupted upload's staging dir
        staging2 = state / "staging"
        assert not (staging2.is_dir() and any(staging2.iterdir())), (
            "interrupted upload staging survived boot GC"
        )

        # the session still exists (the registry is truth), not released
        r = httpx.get(f"{base2}/api/v1/sessions/{sid}", timeout=10)
        assert r.status_code == 200, "the session must survive the crash"

        # the files listing shows NO upload (the crashed one never
        # registered)
        r = httpx.get(f"{base2}/api/v1/sessions/{sid}/files", timeout=10)
        assert r.status_code == 200
        assert r.json()["uploads"] == [], "a half-upload is visible after the crash"

        # a fresh upload of the same content completes byte-identical
        r = httpx.post(
            f"{base2}/api/v1/sessions/{sid}/files/upload?name=big.bin",
            content=payload,
            timeout=120,
        )
        assert r.status_code == 201, r.text
        body = r.json()
        assert body["sha256"] == digest
        assert body["size"] == len(payload)
        r = httpx.get(f"{base2}/api/v1/sessions/{sid}/files/uploads/big.bin", timeout=120)
        assert hashlib.sha256(r.content).hexdigest() == digest
    finally:
        if proc2.poll() is None:
            proc2.kill()
        proc2.wait()
