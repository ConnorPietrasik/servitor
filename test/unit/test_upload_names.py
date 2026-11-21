"""Unit: F16 — the manifest must record the name the file LANDED under.

``deliver()`` renames through ``_safe_name`` (leading dots stripped: an
``.env`` upload lands on disk as ``env``). Registering the RAW name in
the manifest made the entry unmatchable — ``list_files`` filters on
``name in os.listdir(inbox)`` — so dotfile uploads (plausible for a
coding tool: .env, .gitignore) returned 201 but were omitted from
GET /files forever, even though downloadable.

Pinned here:
- ``deliver`` returns the landed name and the manifest stores it;
- the upload endpoint reports the landed name in the 201 body;
- GET /files lists dotfile uploads (and the file is still served under
  BOTH the raw and the landed name);
- legacy manifests written before the fix (raw dot-name entries) are
  still listed — the state volume survives app updates;
- a re-upload of the same landed name dedupes to one entry (a ``.env``
  then ``env`` upload overwrite the same on-disk file);
- a tampered manifest entry is dropped, not raised (the manifest lives
  in a slot-owned dir): each entry is matched through ``_safe_name``,
  so a forged path entry resolves to its last component and an
  all-dots name falls back to a raw match;
- a download whose requested name ``_safe_name`` rejects (all dots)
  is a 404, not a 500.
"""
from __future__ import annotations

import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.api.app import build_app
from app.delivery.service import Delivery
from app.delivery.store import DeliveredFile, FileNotDelivered, SessionFiles


def _stage(tmp_path, data: bytes):
    """A verified staged part, ready for deliver()."""
    part = tmp_path / f"part-{time.time_ns()}"
    part.write_bytes(data)
    return part


def _sf(session_dir, inbox, outbox) -> SessionFiles:
    return SessionFiles(
        session_dir=session_dir, inbox=inbox, outbox=outbox, slot=None
    )


def _entry(name: str, sha: str, ts: float) -> DeliveredFile:
    return DeliveredFile(name=name, sha256=sha, size=4, mime="text/plain", ts=ts)


# ---------------------------------------------------------------------- #
# store: deliver returns the landed name; manifest matches disk
# ---------------------------------------------------------------------- #


def test_deliver_returns_safe_name(tmp_path):
    sf = _sf(tmp_path / "s", tmp_path / "inbox", tmp_path / "o")
    landed = sf.deliver(_stage(tmp_path, b"SECRET=1\n"), ".env")
    assert landed == "env"
    assert (sf.inbox / "env").read_bytes() == b"SECRET=1\n"
    assert not (sf.inbox / ".env").exists()


def test_manifest_records_landed_name(tmp_path):
    sf = _sf(tmp_path / "s", tmp_path / "inbox", tmp_path / "o")
    landed = sf.deliver(_stage(tmp_path, b"a\n"), ".env")
    sf.register_upload(_entry(landed, "ab" * 32, time.time()))
    man = sf.read_manifest()["uploads"]
    # the entry matches what os.listdir(inbox) can actually produce:
    assert man[0]["name"] in {p.name for p in sf.inbox.iterdir()}
    assert man[0]["name"] == "env"


def test_dotfile_then_plain_dedupes_to_one_entry(tmp_path):
    """`.env` then `env` land on the SAME on-disk file — one entry,
    the second upload's record (old-sha duplicates were a latent bug of
    the same raw-name mismatch)."""
    sf = _sf(tmp_path / "s", tmp_path / "inbox", tmp_path / "o")
    sf.deliver(_stage(tmp_path, b"old\n"), ".env")
    sf.register_upload(_entry("env", "aa" * 32, 1.0))
    sf.deliver(_stage(tmp_path, b"new\n"), "env")
    sf.register_upload(_entry("env", "bb" * 32, 2.0))
    man = sf.read_manifest()["uploads"]
    assert [u["sha256"] for u in man] == ["bb" * 32]
    assert len(list(sf.inbox.iterdir())) == 1


# ---------------------------------------------------------------------- #
# API: 201 + GET /files + download for a dotfile upload
# ---------------------------------------------------------------------- #


def _client(tmp_path):
    root = tmp_path / "state"
    root.mkdir(exist_ok=True)
    row = SimpleNamespace(
        id="s1", display_name="s1", slot=None, state="allocated"
    )
    app_state = SimpleNamespace(
        registry=SimpleNamespace(get=lambda sid: row),
        lc=SimpleNamespace(live={}, events=SimpleNamespace(add=lambda *a, **k: None)),
        delivery=Delivery(root),
        state=SimpleNamespace(
            sessions_for=lambda sid: str(root / "sessions" / sid),
            inbox_for=lambda sid: str(root / "inbox" / sid),
            scratch_for=lambda sid: str(root / "scratch" / sid),
        ),
        boot_report=0,
    )
    return TestClient(build_app(app_state, SimpleNamespace(browser_origin=None))), app_state


def _app_sf(app_state, sid: str = "s1") -> SessionFiles:
    st = app_state.state
    return _sf(
        st.sessions_for(sid), st.inbox_for(sid), f"{st.scratch_for(sid)}/workdir/outbox"
    )


def test_upload_dotfile_is_listed_and_downloadable(tmp_path):
    c, app_state = _client(tmp_path)

    r = c.post(
        "/api/v1/sessions/s1/files/upload?name=.env",
        content=b"SECRET=1\n",
        headers={"content-type": "application/octet-stream"},
    )
    assert r.status_code == 201
    # the 201 reports the landed name — what the file actually is on disk:
    assert r.json()["name"] == "env"

    # on disk under the safe name:
    inbox = Path(app_state.state.inbox_for("s1"))
    assert (inbox / "env").is_file()
    assert not (inbox / ".env").exists()

    # GET /files lists it (pre-fix: 201 + manifest entry, listing dropped it):
    r = c.get("/api/v1/sessions/s1/files")
    assert r.status_code == 200
    assert [u["name"] for u in r.json()["uploads"]] == ["env"]

    # downloadable under BOTH the raw and the landed name:
    r = c.get("/api/v1/sessions/s1/files/uploads/.env")
    assert r.status_code == 200
    assert r.content == b"SECRET=1\n"
    assert c.get("/api/v1/sessions/s1/files/uploads/env").status_code == 200


def test_plain_upload_still_listed(tmp_path):
    c, _ = _client(tmp_path)
    r = c.post(
        "/api/v1/sessions/s1/files/upload?name=photo.jpg",
        content=b"\xff\xd8\xff\xe0jpg",
        headers={"content-type": "application/octet-stream"},
    )
    assert r.status_code == 201
    assert r.json()["name"] == "photo.jpg"
    r = c.get("/api/v1/sessions/s1/files")
    assert [u["name"] for u in r.json()["uploads"]] == ["photo.jpg"]


def test_upload_path_rejects_unsafe_name_as_not_delivered(tmp_path):
    """A name ``_safe_name`` rejects (all dots) is "no such file" at
    the store level: FileNotDelivered, not a raw DeliveryError the
    endpoint would have to special-case."""
    sf = _sf(tmp_path / "s", tmp_path / "inbox", tmp_path / "o")
    with pytest.raises(FileNotDelivered):
        sf.upload_path("...")


def test_download_unsafe_name_is_404_not_500(tmp_path):
    """Pre-fix, a download whose name _safe_name rejects (all dots)
    escaped as an unhandled DeliveryError — a client-triggerable 500."""
    c, _ = _client(tmp_path)
    r = c.get("/api/v1/sessions/s1/files/uploads/...")
    assert r.status_code == 404


def test_legacy_raw_manifest_entry_still_listed(tmp_path):
    """Manifests written before the fix keep the raw dot-name; the
    state volume survives app updates, so the listing must still
    resolve them instead of hiding the upload forever."""
    c, app_state = _client(tmp_path)
    sf = _app_sf(app_state)
    (sf.inbox / "env").write_bytes(b"legacy\n")
    sf.register_upload(_entry(".env", "cd" * 32, time.time()))
    r = c.get("/api/v1/sessions/s1/files")
    assert r.status_code == 200
    assert [u["name"] for u in r.json()["uploads"]] == [".env"]


def test_tampered_manifest_entries_are_dropped_not_raised(tmp_path):
    """The manifest is slot-owned, so entries may be forged. Each is
    matched through ``_safe_name``: ``../outside`` and ``a/b/c``
    resolve to their LAST component (``outside``, ``c`` — neither on
    disk here), and the all-dots name ``...`` is not a valid name at
    all (the fallback raw match finds nothing). All are dropped; none
    raises into a 500."""
    c, app_state = _client(tmp_path)
    sf = _app_sf(app_state)
    (sf.inbox / "real.txt").write_bytes(b"real\n")
    for bad in ("../outside", "...", "a/b/c"):
        sf.register_upload(_entry(bad, "ef" * 32, time.time()))
    sf.register_upload(_entry("real.txt", "11" * 32, time.time()))
    r = c.get("/api/v1/sessions/s1/files")
    assert r.status_code == 200
    assert [u["name"] for u in r.json()["uploads"]] == ["real.txt"]
