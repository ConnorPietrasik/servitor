"""Unit: saving session files into the shared library (lib/).

The promise these pin is about BYTES and about the library being a HUMAN
area:

- a save goes through the store, like a keep: the library's link is the
  app's (content-addressed) inode, never the agent's own outbox inode —
  a still-running agent rewrites what it owns, and the library must not
  follow;
- a save never overwrites: a name the library already holds earns a
  suffix, as on the keep shelf;
- only the download's addressing reaches a byte (traversal is a 404);
- the library listing/download/delete gate on containment, and the
  listing is ETag'd like the kept shelf.

Everything here runs against the real Delivery over a tmp state volume.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
from types import SimpleNamespace

from _filesapp import make_client, upload

from app.iso.layout import state_paths


def _inodes(*paths: str) -> set[tuple[int, int]]:
    return {(os.stat(p).st_dev, os.stat(p).st_ino) for p in paths}


def _sha_of(path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _lib(root, rel: str):
    return root / "lib" / rel


# ---------------------------------------------------------------------- #
# saving
# ---------------------------------------------------------------------- #


def test_an_artifact_can_be_saved_into_lib(tmp_path):
    """The save lands under the store's inode: same bytes, one copy. The
    session's area then goes away the way release deletes it (wholesale)
    and the library's link keeps the bytes alive."""
    c, _state, root = make_client(tmp_path)
    (root / "outbox/s1").mkdir(parents=True, exist_ok=True)
    (root / "outbox/s1/report.md").write_bytes(b"# done\n")
    r = c.post(
        "/api/v1/sessions/s1/files/lib", json={"path": "outbox/report.md"}
    )
    assert r.status_code == 201
    row = r.json()
    assert row["path"] == "report.md"
    assert row["sha256"] == hashlib.sha256(b"# done\n").hexdigest()

    dest = _lib(root, "report.md")
    assert dest.read_bytes() == b"# done\n"
    blob = root / "blobs" / row["sha256"][:2] / row["sha256"]
    assert _inodes(str(dest), str(blob)) == {
        (os.stat(dest).st_dev, os.stat(dest).st_ino)
    }
    assert os.stat(dest).st_nlink >= 2

    shutil.rmtree(root / "outbox/s1")  # what release does to the area
    assert dest.read_bytes() == b"# done\n"


def test_an_upload_can_be_saved_and_keeps_its_folder(tmp_path):
    """A folder upload arrives in the folder it arrived in; a save keeps
    that folder inside the library, and an explicit as_name replaces it."""
    c, _state, root = make_client(tmp_path)
    assert upload(c, "photos/a.jpg", b"\xff\xd8jpeg", sid="s1").status_code == 201
    r = c.post(
        "/api/v1/sessions/s1/files/lib", json={"path": "uploads/photos/a.jpg"}
    )
    assert r.status_code == 201
    assert r.json()["path"] == "photos/a.jpg"
    assert _lib(root, "photos/a.jpg").read_bytes() == b"\xff\xd8jpeg"

    r = c.post(
        "/api/v1/sessions/s1/files/lib",
        json={"path": "uploads/photos/a.jpg", "as_name": "picked.jpg"},
    )
    assert r.status_code == 201
    assert _lib(root, "picked.jpg").read_bytes() == b"\xff\xd8jpeg"


def test_a_save_keeps_the_dots_of_a_dot_file(tmp_path):
    """The default landing name is the source's own gated relpath — a dot
    file the agent wrote (outbox/.env) keeps its dots in the library; the
    safe-form renaming is for names a human TYPES, so an explicit as_name
    still resolves to its safe form, as an upload does."""
    c, _state, root = make_client(tmp_path)
    (root / "outbox/s1").mkdir(parents=True, exist_ok=True)
    (root / "outbox/s1" / ".env").write_bytes(b"K=1\n")
    r = c.post("/api/v1/sessions/s1/files/lib", json={"path": "outbox/.env"})
    assert r.status_code == 201
    assert r.json()["path"] == ".env"
    assert _lib(root, ".env").read_bytes() == b"K=1\n"

    (root / "outbox/s1" / "plain.md").write_bytes(b"p\n")
    r = c.post(
        "/api/v1/sessions/s1/files/lib",
        json={"path": "outbox/plain.md", "as_name": ".secret.md"},
    )
    assert r.status_code == 201
    assert r.json()["path"] == "secret.md"


def test_a_save_never_overwrites_a_library_name(tmp_path):
    """Two saves of the same name are two files: silently replacing bytes
    a human curated (from the repo or an earlier save) is not an option."""
    c, _state, root = make_client(tmp_path)
    (root / "outbox/s1").mkdir(parents=True, exist_ok=True)
    for body in (b"one\n", b"two\n"):
        (root / "outbox/s1/x.txt").write_bytes(body)
        assert c.post(
            "/api/v1/sessions/s1/files/lib", json={"path": "outbox/x.txt"}
        ).status_code == 201
    assert _lib(root, "x.txt").read_bytes() == b"one\n"
    assert _lib(root, "x.1.txt").read_bytes() == b"two\n"


def test_saving_reaches_only_files_the_human_can_see(tmp_path):
    """The save addressing is the download's addressing: outbox/<rel> or
    uploads/<name>, through the same gates. Nothing else is savable —
    including a traversal to a file the listing never showed."""
    c, _state, root = make_client(tmp_path)
    (root / "victim.txt").write_bytes(b"no\n")
    for bad in (
        "victim.txt",
        "outbox/../../victim.txt",
        "uploads/../../victim.txt",
        "keep/s1/whatever",
        "scratch/s1/workdir/outbox",
    ):
        assert (
            c.post("/api/v1/sessions/s1/files/lib", json={"path": bad}).status_code
            == 404
        )
    assert list((root / "lib").iterdir()) == []


def test_saving_a_gone_file_is_a_404(tmp_path):
    c, _state, root = make_client(tmp_path)
    (root / "outbox/s1").mkdir(parents=True, exist_ok=True)
    f = root / "outbox/s1/gone.md"
    f.write_bytes(b"bye\n")
    f.unlink()
    assert (
        c.post(
            "/api/v1/sessions/s1/files/lib", json={"path": "outbox/gone.md"}
        ).status_code
        == 404
    )


def test_the_save_event_carries_the_source_mapping(tmp_path):
    """The lib_saved event's detail is the source→library mapping as JSON:
    a save from another tab sets the in-lib badge in this one with no fetch
    (the library listing cannot map a lib path back to the source that
    produced it)."""
    c, state, root = make_client(tmp_path)
    events = []
    state.lc.events = SimpleNamespace(add=lambda *a: events.append(a))
    (root / "outbox/s1").mkdir(parents=True, exist_ok=True)
    (root / "outbox/s1/report.md").write_bytes(b"# done\n")
    r = c.post(
        "/api/v1/sessions/s1/files/lib", json={"path": "outbox/report.md"}
    )
    assert r.status_code == 201
    assert len(events) == 1
    sid, kind, detail = events[0]
    assert sid == "s1" and kind == "lib_saved"
    assert json.loads(detail) == {"source": "outbox/report.md", "path": "report.md"}


# ---------------------------------------------------------------------- #
# listing / download / delete
# ---------------------------------------------------------------------- #


def test_the_library_is_listed_downloaded_and_deleted(tmp_path):
    c, _state, root = make_client(tmp_path)
    (root / "outbox/s1").mkdir(parents=True, exist_ok=True)
    (root / "outbox/s1/a.txt").write_bytes(b"alpha\n")
    assert (
        c.post("/api/v1/sessions/s1/files/lib", json={"path": "outbox/a.txt"}).status_code
        == 201
    )

    r = c.get("/api/v1/files/lib")
    assert r.status_code == 200
    etag = r.headers["etag"]
    rows = r.json()["lib"]
    assert [row["path"] for row in rows] == ["a.txt"]
    assert rows[0]["sha256"] == hashlib.sha256(b"alpha\n").hexdigest()

    # the revalidation costs ~200 bytes, not the body
    r2 = c.get("/api/v1/files/lib", headers={"if-none-match": etag})
    assert r2.status_code == 304

    # download: the bytes, named for the last path component
    r = c.get("/api/v1/files/lib/a.txt")
    assert r.status_code == 200
    assert r.content == b"alpha\n"

    # delete: explicit, and the second one is a 404
    r = c.delete("/api/v1/files/lib/a.txt")
    assert r.status_code == 200 and r.json()["size"] == 6
    assert not _lib(root, "a.txt").exists()
    # the empty folder a save made is pruned, the lib root itself is not
    assert not (root / "lib").exists() or (root / "lib").is_dir()
    assert c.delete("/api/v1/files/lib/a.txt").status_code == 404
    assert c.get("/api/v1/files/lib/nope").status_code == 404
    assert c.get("/api/v1/files/lib/../../victim").status_code in (404, 307)


def test_the_lib_area_exists_before_any_save(tmp_path):
    """The library is a mount source even while empty: the area is created
    with the Delivery, and an empty listing is a listing, not an error."""
    c, _state, root = make_client(tmp_path)
    state = state_paths(str(root))
    assert os.path.isdir(state.lib)
    r = c.get("/api/v1/files/lib")
    assert r.status_code == 200 and r.json()["lib"] == []


def test_a_lib_version_is_sniffed_once_not_twice(tmp_path):
    """The listing reads each file at most its 8 KB MIME head —
    synchronously, under the ABSOLUTE path (the download reuses the same
    key) — and never the whole file: a repo-pushed file (the library's
    normal content, not a stored blob) earns a null sha and NO background
    pass (a full read per file per cold process would bill the volume for
    a field no consumer reads); a save, a hardlink of a blob, answers its
    sha off the inode for free."""
    from app.delivery import store

    c, _state, root = make_client(tmp_path)
    (root / "lib" / "scripts").mkdir(parents=True)
    (root / "lib" / "scripts" / "foo.sh").write_bytes(b"#!/bin/sh\n")
    (root / "outbox/s1").mkdir(parents=True, exist_ok=True)
    (root / "outbox/s1/saved.txt").write_bytes(b"saved\n")
    assert (
        c.post(
            "/api/v1/sessions/s1/files/lib", json={"path": "outbox/saved.txt"}
        ).status_code
        == 201
    )

    saved = dict(store._MIME_CACHE)
    store._MIME_CACHE.clear()
    try:
        r = c.get("/api/v1/files/lib")
        assert r.status_code == 200
        rows = {row["path"]: row for row in r.json()["lib"]}
        # the repo file: one synchronous head read, absolute-path key,
        # null sha — the whole file is never read for the listing
        assert rows["scripts/foo.sh"]["mime"].startswith("text/")
        assert rows["scripts/foo.sh"]["sha256"] is None
        # the save: the store's name for the bytes, off the inode
        assert rows["saved.txt"]["sha256"] == hashlib.sha256(b"saved\n").hexdigest()
        # no background sha pass is queued for the library at all
        with store._sha_lock:
            assert not [k for k in store._SHA_PENDING if k[0] == "lib"]
        lib_keys = [k for k in store._MIME_CACHE if k[0] == "lib"]
        assert len(lib_keys) == 2, f"double sniff: {lib_keys}"
        # and they are the keys the listing and the download read:
        # absolute paths, one per file
        assert {k[1] for k in lib_keys} == {
            str(root / "lib" / "scripts" / "foo.sh"),
            str(root / "lib" / "saved.txt"),
        }
    finally:
        store._MIME_CACHE.clear()
        store._MIME_CACHE.update(saved)
