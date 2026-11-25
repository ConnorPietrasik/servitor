"""Unit: the manifest must record the path the file LANDED under.

``deliver()`` lands through ``_safe_relpath`` (leading dots stripped per
component: an ``.env`` upload lands on disk as ``env``; a folder upload
lands in its folder). The manifest must store the landed path —
``list_files`` filters on the files actually on disk, so a raw dot-name
entry is unmatchable and the upload is omitted from GET /files forever,
even though downloadable.

Pinned here:
- ``deliver`` returns the landed path and the manifest stores it;
- the upload endpoint reports the landed path in the 201 body;
- GET /files lists dotfile uploads (and the file is still served under
  BOTH the raw and the landed name);
- legacy manifests with raw dot-name entries are still listed — the
  state volume survives app updates;
- a second file with the same landed name lands BESIDE the first (a
  ``.env`` then ``env`` upload → ``env`` + ``env.1``): a same-name collision
  is not a replacement — without the blob store's byte-identity proof, no
  delivery may silently destroy a delivered file;
- a tampered manifest entry is dropped, not raised (the manifest lives
  in a slot-owned dir): each entry is matched through ``_safe_relpath``,
  so a forged path entry resolves to its safe form and an
  all-dots name falls back to a raw match;
- a manifest that is structurally broken (not a dict, ``uploads`` not a
  list, an entry without a string name) is dropped entry-wise / repaired
  on the next write, never a 500 on the listing or the upload routes;
- a NUL byte in any client-supplied name or path is a 4xx (400 where a
  name is being chosen, 404 where a file is being addressed) — never an
  unhandled ValueError from the first path syscall;
- a download whose requested path ``_safe_relpath`` rejects (all dots)
  is a 404, not a 500.
"""
from __future__ import annotations

import time
from pathlib import Path

import pytest

from _filesapp import make_client
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


def test_dotfile_then_plain_land_beside_each_other(tmp_path):
    """`.env` lands as `env`; a later `env` upload is a DIFFERENT file the
    human sent — the phone paid for both, so the second lands beside the
    first (``env.1``), never on top of it. (With the blob store, only
    byte-identical re-landings may take the same name back — see
    test_transfer_browse for that half.)"""
    sf = _sf(tmp_path / "s", tmp_path / "inbox", tmp_path / "o")
    landed1 = sf.deliver(_stage(tmp_path, b"old\n"), ".env")
    assert landed1 == "env"
    sf.register_upload(_entry("env", "aa" * 32, 1.0))
    landed2 = sf.deliver(_stage(tmp_path, b"new\n"), "env")
    assert landed2 == "env.1", "a same-name collision is not a replacement"
    sf.register_upload(_entry("env.1", "bb" * 32, 2.0))
    man = sf.read_manifest()["uploads"]
    assert [u["name"] for u in man] == ["env", "env.1"]
    assert (sf.inbox / "env").read_bytes() == b"old\n"
    assert (sf.inbox / "env.1").read_bytes() == b"new\n"


# ---------------------------------------------------------------------- #
# API: 201 + GET /files + download for a dotfile upload
# ---------------------------------------------------------------------- #


def _app_sf(app_state, sid: str = "s1") -> SessionFiles:
    """The same view the API routes use (one layout truth: a hand-built path
    here would drift from Delivery.files the moment the areas move)."""
    return app_state.delivery.files(app_state.state, sid, None)


def test_upload_dotfile_is_listed_and_downloadable(tmp_path):
    c, app_state, _root = make_client(tmp_path)

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

    # GET /files lists it (the 201 reported the landed name):
    r = c.get("/api/v1/sessions/s1/files")
    assert r.status_code == 200
    assert [u["name"] for u in r.json()["uploads"]] == ["env"]

    # downloadable under BOTH the raw and the landed name:
    r = c.get("/api/v1/sessions/s1/files/uploads/.env")
    assert r.status_code == 200
    assert r.content == b"SECRET=1\n"
    assert c.get("/api/v1/sessions/s1/files/uploads/env").status_code == 200


def test_plain_upload_still_listed(tmp_path):
    c, _, _root = make_client(tmp_path)
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
    """A download whose name _safe_name rejects (all dots) is a 404,
    not an unhandled DeliveryError (a client-triggerable 500)."""
    c, _, _root = make_client(tmp_path)
    r = c.get("/api/v1/sessions/s1/files/uploads/...")
    assert r.status_code == 404


def test_legacy_raw_manifest_entry_still_listed(tmp_path):
    """Manifests written before the fix keep the raw dot-name; the
    state volume survives app updates, so the listing must still
    resolve them instead of hiding the upload forever."""
    c, app_state, _root = make_client(tmp_path)
    sf = _app_sf(app_state)
    (sf.inbox / "env").write_bytes(b"legacy\n")
    sf.register_upload(_entry(".env", "cd" * 32, time.time()))
    r = c.get("/api/v1/sessions/s1/files")
    assert r.status_code == 200
    assert [u["name"] for u in r.json()["uploads"]] == [".env"]


def test_relanding_replaces_the_legacy_raw_entry_not_doubling_it(tmp_path):
    """A pre-fix manifest holds the raw dot-name for the file that landed
    as ``env``. Re-landing ``env`` (the dedup of identical bytes keeps the
    name) must drop the legacy row through the name the listing matches on:
    two rows for one file would render twice in the UI and hand the human
    the same bytes under two names."""
    sf = _sf(tmp_path / "s", tmp_path / "inbox", tmp_path / "o")
    (sf.inbox / "env").write_bytes(b"legacy\n")
    sf.register_upload(_entry(".env", "cd" * 32, 1.0))
    sf.register_upload(_entry("env", "cd" * 32, 2.0))
    entries = sf.read_manifest()["uploads"]
    assert [u["name"] for u in entries] == ["env"]
    assert entries[0]["ts"] == 2.0  # the re-landing's row, not the legacy one


def test_tampered_manifest_entries_are_dropped_not_raised(tmp_path):
    """The manifest is slot-owned, so entries may be forged. Each is
    matched through ``_safe_relpath``: ``a/b/c`` resolves to the path
    ``a/b/c`` (not on disk here), ``../outside`` refuses its traversal
    component (the fallback raw match finds nothing), and the all-dots
    name ``...`` is not a valid path at all. None exists as a file, so
    all are dropped; none raises into a 500."""
    c, app_state, _root = make_client(tmp_path)
    sf = _app_sf(app_state)
    (sf.inbox / "real.txt").write_bytes(b"real\n")
    for bad in ("../outside", "...", "a/b/c"):
        sf.register_upload(_entry(bad, "ef" * 32, time.time()))
    sf.register_upload(_entry("real.txt", "11" * 32, time.time()))
    r = c.get("/api/v1/sessions/s1/files")
    assert r.status_code == 200
    assert [u["name"] for u in r.json()["uploads"]] == ["real.txt"]


# ---------------------------------------------------------------------- #
# tampered manifest: structural damage is dropped, not a 500
# ---------------------------------------------------------------------- #


# the shapes are written as raw text: every one of them is a manifest the
# slot could have forged over the app's head
import json as _json


def _broken_manifests() -> list[tuple[str, list[str]]]:
    """(the forged text, the uploads that must survive it)."""
    return [
        (_json.dumps([1, 2, 3]), []),  # the manifest is a list, not a dict
        (_json.dumps("uploads"), []),  # a bare string
        (_json.dumps(42), []),  # a bare number
        (_json.dumps({"uploads": "hacked"}), []),  # uploads is a string
        (_json.dumps({"uploads": "not-a-list"}), []),
        (_json.dumps({"uploads": ["not-a-dict", {"name": 5}, {"name": "ok.txt"}]}), ["ok.txt"]),
        (_json.dumps({"uploads": [{"name": "ok.txt"}]}), ["ok.txt"]),  # missing every other field
        (_json.dumps({"uploads": None}), []),
    ]


def test_a_structurally_broken_manifest_is_listed_not_a_500(tmp_path):
    """The slot can rewrite files.json at any time; a structurally broken
    file must not 500 the listing. Every shape the slot could write is
    coerced: a non-dict manifest (or a non-list uploads) reads as no
    uploads, an entry without a string name is dropped (the one that would
    raise in a reader), and a well-shaped entry among the garbage still
    lists — provided the file it names is on disk."""
    c, app_state, _root = make_client(tmp_path)
    sf = _app_sf(app_state)
    (sf.inbox / "ok.txt").write_bytes(b"ok\n")
    for text, expected in _broken_manifests():
        sf.manifest_path.write_text(text)
        r = c.get("/api/v1/sessions/s1/files")
        assert r.status_code == 200, f"{text} must list as 200, got {r.status_code} {r.text}"
        assert [u["name"] for u in r.json()["uploads"]] == expected, text


def test_a_broken_manifest_is_repaired_by_the_next_upload(tmp_path):
    """A listing tolerates the damage; the next real upload atomically
    replaces the file with a clean one — the store self-heals, and the
    upload route itself must not 500 on the way in (register_upload
    reads the same manifest it rewrites)."""
    c, app_state, _root = make_client(tmp_path)
    sf = _app_sf(app_state)
    sf.manifest_path.write_text('{"uploads": "hacked"}')
    r = c.post(
        "/api/v1/sessions/s1/files/upload?name=after-repair.txt",
        content=b"back\n",
        headers={"content-type": "application/octet-stream"},
    )
    assert r.status_code == 201, r.text
    r = c.get("/api/v1/sessions/s1/files")
    assert r.status_code == 200
    assert [u["name"] for u in r.json()["uploads"]] == ["after-repair.txt"]


# ---------------------------------------------------------------------- #
# NUL bytes: a 4xx where a name is chosen, a 404 where a file is addressed
# ---------------------------------------------------------------------- #


def test_a_nul_in_the_upload_name_is_a_400_before_any_byte_lands(tmp_path):
    """A NUL in the name would make the first path syscall raise an
    unhandled ValueError (a 500). It is refused at the door — one-shot and
    staged — before a part or a meta exists to be swept."""
    c, app_state, _root = make_client(tmp_path)
    r = c.post(
        "/api/v1/sessions/s1/files/upload?name=a%00b.txt",
        content=b"x",
        headers={"content-type": "application/octet-stream"},
    )
    assert r.status_code == 400, r.text
    r = c.post("/api/v1/sessions/s1/staged?name=a%00b.txt")
    assert r.status_code == 400, r.text
    # nothing on the volume for the TTL to find
    assert list(Path(app_state.state.staging_for("s1")).glob("*")) == []


def test_a_nul_in_an_addressed_path_is_a_404_not_a_500(tmp_path):
    """Every route that addresses a file by a client-supplied name or path
    must refuse a NUL with the route's normal not-found, not with an
    unhandled ValueError from os.lstat/Path.resolve."""
    c, _, _root = make_client(tmp_path)
    n = "%00"
    assert c.get(f"/api/v1/sessions/s1/files/uploads/x{n}txt").status_code == 404
    assert c.delete(f"/api/v1/sessions/s1/files/uploads/x{n}txt").status_code == 404
    assert c.get(f"/api/v1/sessions/s1/files/outbox/x{n}txt").status_code == 404
    assert c.delete(f"/api/v1/sessions/s1/files/outbox/x{n}txt").status_code == 404
    assert (
        c.get(f"/api/v1/sessions/s1/files/archive?path=outbox/x{n}").status_code == 404
    )


def test_a_nul_in_a_rename_target_is_a_404_not_a_500(tmp_path):
    c, app_state, _root = make_client(tmp_path)
    sf = _app_sf(app_state)
    (sf.inbox / "a.txt").write_bytes(b"a")
    sf.register_upload(_entry("a.txt", "22" * 32, time.time()))
    outbox = Path(app_state.state.outbox_for("s1"))
    outbox.mkdir(parents=True, exist_ok=True)
    (outbox / "art.md").write_bytes(b"art")
    n = "\x00"  # JSON bodies carry the byte raw (query/path forms use %00)
    assert (
        c.patch(
            "/api/v1/sessions/s1/files/uploads/a.txt",
            json={"to": f"b{n}txt"},
        ).status_code
        == 404
    )
    assert (
        c.patch(
            "/api/v1/sessions/s1/files/outbox/art.md",
            json={"to": f"v2/n{n}w.md"},
        ).status_code
        == 404
    )


def test_a_nul_in_a_keep_or_attach_as_name_is_a_404_not_a_500(tmp_path):
    c, app_state, _root = make_client(tmp_path)
    sf = _app_sf(app_state)
    (sf.inbox / "a.txt").write_bytes(b"a")
    sf.register_upload(_entry("a.txt", "33" * 32, time.time()))
    n = "\x00"  # JSON carries it raw; the query forms use %00 above
    r = c.post(
        "/api/v1/sessions/s1/files/keep",
        json={"path": "uploads/a.txt", "as_name": f"k{n}eep.txt"},
    )
    assert r.status_code == 404, r.text
    r = c.post(
        "/api/v1/sessions/s1/files/attach",
        json={"source": "s1", "path": "uploads/a.txt", "as_name": f"k{n}eep.txt"},
    )
    assert r.status_code == 404, r.text


def test_download_of_a_file_unlinked_under_the_gate_is_a_404_not_a_500(tmp_path, monkeypatch):
    """The gate's lstat and the route's per-version keying lstat are separate
    syscalls (the link-free walk's and realpath's in between), and the outbox
    is agent-writable: an artifact unlinked in the window answers the gate's
    404 shape, never a 500 off a vanished path."""
    import os

    c, app_state, _root = make_client(tmp_path)
    out = Path(app_state.state.outbox_for("s1"))
    out.mkdir(parents=True, exist_ok=True)
    p = out / "gone.md"
    p.write_bytes(b"bye")
    real_lstat = os.lstat
    seen = {"n": 0}

    def racy(path, *a, **k):
        if str(path) == str(p):
            seen["n"] += 1
            # the sequence per download: the walk's islink (1), realpath's
            # inside the resolve (2), the gate (3), the route (4). The route's
            # is the one that fails: the gate already passed.
            if seen["n"] == 4:
                raise FileNotFoundError(path)
        return real_lstat(path, *a, **k)

    monkeypatch.setattr(os, "lstat", racy)
    r = c.get("/api/v1/sessions/s1/files/outbox/gone.md")
    assert r.status_code == 404, r.text


# ---------------------------------------------------------------------- #
# fixture completeness: every route this file can hit, not just the first
# ---------------------------------------------------------------------- #


def test_a_second_listing_after_an_outbox_change_is_200_not_500(tmp_path):
    """The app_state the client stands on must be complete for EVERY route
    it can hit, not only the ones the first request exercises. list_files
    publishes its sweep's delta through the session's hub once a snapshot
    exists — the first fetch seeds the snapshot (delta None, hub never
    touched), and a stub that lacks the hub plumbing dies on the SECOND
    fetch with an AttributeError the first one never shows."""
    c, app_state, _root = make_client(tmp_path)
    assert c.get("/api/v1/sessions/s1/files").status_code == 200
    (Path(app_state.state.outbox_for("s1")) / "artifact.md").write_bytes(b"hi")
    r = c.get("/api/v1/sessions/s1/files")
    assert r.status_code == 200, r.text
    assert [f["path"] for f in r.json()["outbox"]] == ["artifact.md"]
