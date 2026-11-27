"""Unit: keeping bytes past a session, and reusing them across sessions.

The promise these pin is about BYTES, not features:

- two sessions holding one file is one copy of bytes (inode identity, not
  "it looked the same");
- a keep is a link that outlives the session's areas — release deletes the
  session's links, the kept link keeps the bytes alive;
- an upload streamed twice is stored once (the sha is the address);
- reclaim is the filesystem's refcount: the bytes go when the last link
  goes, and a kept or live link makes them un-reclaimable by construction.

Everything here runs against the real Delivery over a tmp state volume.
"""
from __future__ import annotations

import hashlib
import json
import os
from types import SimpleNamespace

from _filesapp import make_client, upload

from app.delivery.service import Delivery


def _inodes(*paths: str) -> set[tuple[int, int]]:
    return {(os.stat(p).st_dev, os.stat(p).st_ino) for p in paths}


def _upload(c, name="a.bin", body=b"payload-bytes", sid="s1"):
    assert upload(c, name, body, sid=sid).status_code == 201


# ---------------------------------------------------------------------- #
# dedup
# ---------------------------------------------------------------------- #


def test_the_same_bytes_uploaded_twice_are_one_copy(tmp_path):
    """The store is content-addressed: a re-upload of a file the volume
    already holds costs no second copy and no second write."""
    c, _state, root = make_client(tmp_path)
    _upload(c, "first.bin")
    _upload(c, "second.bin")
    a, b = root / "inbox/s1/first.bin", root / "inbox/s1/second.bin"
    assert _inodes(str(a), str(b)) == _inodes(str(_blob(root, _sha_of(a))))
    assert list((root / "blobs").glob("*/*")) == [_blob(root, _sha_of(a))]


def _blob(root, sha: str):
    """The store's path for a sha (the fanout is the layout's, not a
    convention a test gets to guess at)."""
    return root / "blobs" / sha[:2] / sha


def _sha_of(path) -> str:
    import hashlib

    return hashlib.sha256(path.read_bytes()).hexdigest()


# ---------------------------------------------------------------------- #
# keep
# ---------------------------------------------------------------------- #


def test_keep_links_the_same_bytes_and_survives_the_session(tmp_path):
    """Keep is not a backup copy. The session's area then goes away the way
    release deletes it (wholesale) and the kept file is still there, same
    inode: the link the human made is what holds the bytes."""
    c, _state, root = make_client(tmp_path)
    _upload(c, "dataset.csv", b"col,val\n1,2\n")
    r = c.post("/api/v1/sessions/s1/files/keep", json={"path": "uploads/dataset.csv"})
    assert r.status_code == 201
    kept = r.json()
    assert kept["path"] == "s1/dataset.csv" and kept["size"] == 12

    src = root / "inbox/s1/dataset.csv"
    dest = root / "keep/s1/dataset.csv"
    assert _inodes(str(src), str(dest)) == {(os.stat(src).st_dev, os.stat(src).st_ino)}
    assert os.stat(dest).st_nlink >= 2

    import shutil

    shutil.rmtree(root / "inbox/s1")  # what release does to the area
    assert dest.read_bytes() == b"col,val\n1,2\n"


def test_an_artifact_can_be_kept_and_renamed(tmp_path):
    c, _state, root = make_client(tmp_path)
    (root / "outbox/s1").mkdir(parents=True, exist_ok=True)
    (root / "outbox/s1/report.md").write_bytes(b"# done\n")
    r = c.post(
        "/api/v1/sessions/s1/files/keep",
        json={"path": "outbox/report.md", "as_name": "final-report.md"},
    )
    assert r.status_code == 201
    assert (root / "keep/s1/final-report.md").read_bytes() == b"# done\n"


def test_a_keep_never_overwrites_a_keep(tmp_path):
    """Two keeps of the same name are two files: silently replacing bytes the
    human asked to keep forever is not an option."""
    c, _state, root = make_client(tmp_path)
    (root / "outbox/s1").mkdir(parents=True, exist_ok=True)
    for body in (b"one\n", b"two\n"):
        (root / "outbox/s1/x.txt").write_bytes(body)
        assert c.post(
            "/api/v1/sessions/s1/files/keep", json={"path": "outbox/x.txt"}
        ).status_code == 201
    assert (root / "keep/s1/x.txt").read_bytes() == b"one\n"
    assert (root / "keep/s1/x.1.txt").read_bytes() == b"two\n"


def test_the_keep_event_carries_the_source_mapping(tmp_path):
    """The file_kept event's detail is the source→shelf mapping as JSON:
    a keep from another tab sets the kept badge in this one with no fetch
    (the shelf listing cannot map a shelf path back to its source)."""
    c, state, root = make_client(tmp_path)
    events = []
    state.lc.events = SimpleNamespace(add=lambda *a: events.append(a))
    (root / "outbox/s1").mkdir(parents=True, exist_ok=True)
    (root / "outbox/s1/report.md").write_bytes(b"# done\n")
    r = c.post("/api/v1/sessions/s1/files/keep", json={"path": "outbox/report.md"})
    assert r.status_code == 201
    assert len(events) == 1
    sid, kind, detail = events[0]
    assert sid == "s1" and kind == "file_kept"
    assert json.loads(detail) == {"source": "outbox/report.md", "path": "s1/report.md"}


def test_keep_only_reaches_files_the_human_can_see(tmp_path):
    """The keep addressing is the download's addressing: outbox/<rel> or
    uploads/<name>, through the same gates. Nothing else is keepable —
    including a traversal to a file the listing never showed."""
    c, _state, root = make_client(tmp_path)
    (root / "victim.txt").write_bytes(b"no\n")
    assert c.post(
        "/api/v1/sessions/s1/files/keep", json={"path": "../victim.txt"}
    ).status_code == 404
    assert c.post(
        "/api/v1/sessions/s1/files/keep", json={"path": "outbox/%2e%2e%2fvictim.txt"}
    ).status_code == 404
    assert (root / "victim.txt").exists()


def test_keep_listing_download_and_unkeep(tmp_path):
    c, _state, root = make_client(tmp_path)
    _upload(c, "photo.jpg", b"\xff\xd8\xff\xe0jpeg")
    c.post("/api/v1/sessions/s1/files/keep", json={"path": "uploads/photo.jpg"})

    rows = c.get("/api/v1/files").json()["keep"]
    assert [r["path"] for r in rows] == ["s1/photo.jpg"]
    assert rows[0]["size"] == 8 and rows[0]["mime"] == "image/jpeg"

    d = c.get("/api/v1/files/keep/s1/photo.jpg")
    assert d.status_code == 200 and d.content == b"\xff\xd8\xff\xe0jpeg"

    r = c.delete("/api/v1/files/keep/s1/photo.jpg")
    assert r.status_code == 200 and r.json()["size"] == 8
    assert c.get("/api/v1/files").json()["keep"] == []
    # the emptied bucket is pruned; the keep root stays
    assert not (root / "keep/s1").exists() and (root / "keep").is_dir()


def test_the_shelf_listing_is_etag_d_and_revalidates(tmp_path):
    """The shelf listing joins the session listings in the "listings are
    ETag'd" contract: an unchanged shelf revalidates at ~200 bytes (a 304
    with no body), and a keep is a change that moves the etag."""
    import time

    c, _state, _root = make_client(tmp_path)
    _upload(c, "photo.jpg", b"\xff\xd8\xff\xe0jpeg")
    c.post("/api/v1/sessions/s1/files/keep", json={"path": "uploads/photo.jpg"})

    # the first listing may still hold a background sha fill-in pending (the
    # fill-in is a change by design): revalidate until the body settles
    r1 = c.get("/api/v1/files")
    assert r1.status_code == 200
    etag = r1.headers["etag"]
    for _ in range(200):
        r = c.get("/api/v1/files", headers={"if-none-match": etag})
        if r.status_code == 304:
            break
        etag = r.headers["etag"]
        time.sleep(0.01)
    else:
        raise AssertionError("the shelf listing never settled")

    r2 = c.get("/api/v1/files", headers={"if-none-match": etag})
    assert r2.status_code == 304 and r2.content == b""

    # a keep is a change: the old etag no longer matches
    _upload(c, "second.bin", b"more bytes", sid="s2")
    c.post("/api/v1/sessions/s2/files/keep", json={"path": "uploads/second.bin"})
    r3 = c.get("/api/v1/files", headers={"if-none-match": etag})
    assert r3.status_code == 200
    assert [r["path"] for r in r3.json()["keep"]] == ["s2/second.bin", "s1/photo.jpg"]


# ---------------------------------------------------------------------- #
# the shelf's sha is the inode's address, not a computed value
# ---------------------------------------------------------------------- #


def _cold_process(root):
    """A restart: nothing the old process learned survives — the per-version
    sha cache is empty and the new Delivery's store index is cold."""
    import app.delivery.store as store_mod

    store_mod._SHA_CACHE.clear()
    store_mod._SHA_PENDING.clear()
    return Delivery(str(root))


def _pending() -> set:
    import app.delivery.store as store_mod

    return store_mod._SHA_PENDING


def test_a_cold_listing_of_the_shelf_needs_no_hash(tmp_path):
    """A keep is a hardlink of a blob and the blob's name is its sha: a
    cold process lists the shelf with every sha in hand — no null row, no
    background pass, no re-read of the bytes the store already names."""
    c, _state, root = make_client(tmp_path)
    body = b"the kept bytes"
    _upload(c, "k.bin", body)
    assert (
        c.post("/api/v1/sessions/s1/files/keep", json={"path": "uploads/k.bin"})
        .status_code
        == 201
    )

    fresh = _cold_process(root)
    rows = fresh.list_keep()
    assert [r["path"] for r in rows] == ["s1/k.bin"]
    assert rows[0]["sha256"] == hashlib.sha256(body).hexdigest()
    assert not _pending(), "the listing queued a re-read of stored bytes"


def test_a_cold_keep_sha_is_the_inode_address_not_a_hash(tmp_path):
    """The attach route asks keep_sha for the shelf's sha: on a cold
    process the answer comes from the inode's address in the store — the
    first attach of a kept multi-GB file is a free link, not a re-hash."""
    c, _state, root = make_client(tmp_path)
    body = b"kept artifact bytes"
    _upload(c, "k.bin", body)
    c.post("/api/v1/sessions/s1/files/keep", json={"path": "uploads/k.bin"})

    fresh = _cold_process(root)
    assert fresh.keep_sha("s1/k.bin") == hashlib.sha256(body).hexdigest()
    assert not _pending()


def test_an_artifact_keep_also_answers_from_the_inode(tmp_path):
    """A keep of an outbox artifact is copied into the store first
    (adopt_hashed) and then linked: the shelf row of a cold process still
    carries the sha, answered by the inode the copy made."""
    c, _state, root = make_client(tmp_path)
    body = b"an artifact worth keeping"
    (root / "outbox/s1").mkdir(parents=True, exist_ok=True)
    (root / "outbox/s1/report.bin").write_bytes(body)
    assert (
        c.post("/api/v1/sessions/s1/files/keep", json={"path": "outbox/report.bin"})
        .status_code
        == 201
    )

    fresh = _cold_process(root)
    rows = fresh.list_keep()
    assert [r["path"] for r in rows] == ["s1/report.bin"]
    assert rows[0]["sha256"] == hashlib.sha256(body).hexdigest()
    assert not _pending()


def test_unkeep_cannot_escape_the_keep_area(tmp_path):
    c, _state, root = make_client(tmp_path)
    _upload(c, "a.bin")
    c.post("/api/v1/sessions/s1/files/keep", json={"path": "uploads/a.bin"})
    victim = root / "keep" / "s1" / "a.bin"  # exists; a traversal must not reach it twice
    assert c.delete("/api/v1/files/keep/%2e%2e%2f%2e%2e%2fetc").status_code == 404
    assert c.delete("/api/v1/files/keep/s1/nope.bin").status_code == 404
    assert victim.exists()


def test_unkeeping_a_file_that_just_went_is_a_404(tmp_path):
    """Two un-keeps of the same row (a double tap, two devices): the loser's
    delete answers 404 — a file that is simply gone — never a 500 from a
    bare FileNotFoundError."""
    from app.delivery.store import FileNotDelivered
    import pytest

    c, state, root = make_client(tmp_path)
    (root / "outbox/s1").mkdir(parents=True, exist_ok=True)
    (root / "outbox/s1/twice.bin").write_bytes(b"once is enough\n")
    kept = c.post(
        "/api/v1/sessions/s1/files/keep", json={"path": "outbox/twice.bin"}
    ).json()
    assert c.delete(f"/api/v1/files/keep/{kept['path']}").status_code == 200
    assert c.delete(f"/api/v1/files/keep/{kept['path']}").status_code == 404
    with pytest.raises(FileNotDelivered):
        state.delivery.delete_keep(kept["path"])


# ---------------------------------------------------------------------- #
# cross-session attach
# ---------------------------------------------------------------------- #


def test_attach_gives_the_other_session_the_same_inode(tmp_path):
    """Reuse costs zero bytes: session 2's inbox entry is the same inode
    session 1's is, and it shows up in session 2's listing as an upload."""
    c, _state, root = make_client(tmp_path)
    _upload(c, "model.bin", b"weights" * 100, sid="s1")
    r = c.post(
        "/api/v1/sessions/s2/files/attach",
        json={"source": "s1", "path": "uploads/model.bin"},
    )
    assert r.status_code == 201
    assert r.json()["size"] == 700
    a = root / "inbox/s1/model.bin"
    b = root / "inbox/s2/model.bin"
    assert _inodes(str(a), str(b)) == _inodes(str(_blob(root, _sha_of(a))))

    listing = c.get("/api/v1/sessions/s2/files").json()["uploads"]
    assert [u["name"] for u in listing] == ["model.bin"]
    assert listing[0]["sha256"] == _sha_of(a)  # the sha that proves the link


def test_attach_can_rename_and_repeats_are_free(tmp_path):
    c, _state, root = make_client(tmp_path)
    _upload(c, "x.bin", b"shared", sid="s1")
    for name in ("first.bin", "second.bin"):
        assert c.post(
            "/api/v1/sessions/s2/files/attach",
            json={"source": "s1", "path": "uploads/x.bin", "as_name": name},
        ).status_code == 201
    names = sorted(p.name for p in (root / "inbox/s2").iterdir())
    assert names == ["first.bin", "second.bin"]
    assert len(_inodes(*[str(root / "inbox/s2" / n) for n in names])) == 1


def test_attach_from_the_keep_shelf_needs_no_source_session(tmp_path):
    c, _state, root = make_client(tmp_path, sids=("s1", "s2", "s3"))
    _upload(c, "base.csv", b"a,b\n", sid="s1")
    c.post("/api/v1/sessions/s1/files/keep", json={"path": "uploads/base.csv"})
    # the source session is released: its area is gone, the shelf is not
    import shutil

    shutil.rmtree(root / "inbox/s1")
    r = c.post(
        "/api/v1/sessions/s3/files/attach",
        json={"source": "keep", "path": "s1/base.csv"},
    )
    assert r.status_code == 201
    assert (root / "inbox/s3/base.csv").read_bytes() == b"a,b\n"


def test_attaching_a_kept_file_is_a_free_link_not_a_recopy(tmp_path, monkeypatch):
    """The shelf's listing hashes each kept row in the background; with that
    sha the attach is the inode fast path — a link, never a re-hash and
    re-copy of bytes the store already holds."""
    import time

    import app.delivery.blobs as blobsmod

    c, _state, root = make_client(tmp_path, sids=("s1", "s2"))
    (root / "outbox/s1").mkdir(parents=True, exist_ok=True)
    (root / "outbox/s1/big.bin").write_bytes(b"payload" * 1000)
    kept = c.post(
        "/api/v1/sessions/s1/files/keep", json={"path": "outbox/big.bin"}
    ).json()

    # the shelf listing is what the human sees before attaching: it queues
    # the background hash — wait for it to land
    deadline = time.time() + 5
    row = None
    while time.time() < deadline:
        rows = c.get("/api/v1/files").json()["keep"]
        row = next((r for r in rows if r["path"] == kept["path"]), None)
        if row and row["sha256"]:
            break
        time.sleep(0.01)
    assert row and row["sha256"], "the shelf's background hash never landed"

    calls = []
    orig = blobsmod.Blobs.adopt_hashed
    monkeypatch.setattr(
        blobsmod.Blobs,
        "adopt_hashed",
        lambda self, src, **kw: (calls.append(src), orig(self, src, **kw))[1],
    )
    r = c.post(
        "/api/v1/sessions/s2/files/attach",
        json={"source": "keep", "path": kept["path"]},
    )
    assert r.status_code == 201
    assert calls == [], "the store already holds the bytes: no re-hash/re-copy"
    assert _inodes(str(root / "keep" / kept["path"]), str(root / "inbox/s2/big.bin")) == {
        (os.stat(root / "keep" / kept["path"]).st_dev, os.stat(root / "keep" / kept["path"]).st_ino)
    }


def test_attaching_a_kept_file_without_a_listing_costs_no_second_copy(tmp_path):
    """Cold cache (the shelf was never listed): the keep's bytes are still
    the store's, and the immutable in-place hash finds them — the store
    gains no second copy and no temp, and the source is left alone."""
    c, _state, root = make_client(tmp_path, sids=("s1", "s2"))
    (root / "outbox/s1").mkdir(parents=True, exist_ok=True)
    body = os.urandom(1 << 20)
    (root / "outbox/s1/cold.bin").write_bytes(body)
    kept = c.post(
        "/api/v1/sessions/s1/files/keep", json={"path": "outbox/cold.bin"}
    ).json()

    blobs_before = sorted(str(p) for p in (root / "blobs").glob("*/*"))
    assert len(blobs_before) == 1
    r = c.post(
        "/api/v1/sessions/s2/files/attach",
        json={"source": "keep", "path": kept["path"]},
    )
    assert r.status_code == 201
    assert sorted(str(p) for p in (root / "blobs").glob("*/*")) == blobs_before
    assert list((root / "blobs").glob(".adopt-*")) == []  # no copy temp
    assert _inodes(str(root / "keep" / kept["path"]), str(root / "inbox/s2/cold.bin")) == {
        (os.stat(blobs_before[0]).st_dev, os.stat(blobs_before[0]).st_ino)
    }


def test_attach_of_an_artifact_copies_once_then_is_free(tmp_path):
    """An agent artifact has no blob yet, so reuse pays for exactly one copy
    into the store; a second attach of the same file adds no copy."""
    c, _state, root = make_client(tmp_path, sids=("s1", "s2", "s3"))
    (root / "outbox/s1").mkdir(parents=True, exist_ok=True)
    (root / "outbox/s1/report.md").write_bytes(b"# artifact\n")

    assert c.post(
        "/api/v1/sessions/s2/files/attach", json={"source": "s1", "path": "outbox/report.md"}
    ).status_code == 201
    blobs = list((root / "blobs").glob("*/*"))
    assert len(blobs) == 1
    assert c.post(
        "/api/v1/sessions/s3/files/attach", json={"source": "s1", "path": "outbox/report.md"}
    ).status_code == 201
    assert list((root / "blobs").glob("*/*")) == blobs
    # both targets link the stored inode; the artifact itself is untouched
    assert _inodes(
        str(root / "inbox/s2/report.md"), str(root / "inbox/s3/report.md"), str(blobs[0])
    ) == {(os.stat(blobs[0]).st_dev, os.stat(blobs[0]).st_ino)}


def test_attach_never_re_sniffs_a_version_a_listing_already_knows(tmp_path, monkeypatch):
    """The row's MIME comes from the cache the source's listing already
    warmed — an attach that re-read the bytes would read them a third time
    (the upload's stream, the listing's pass, the attach). Pinned per source
    kind: outbox (the source's own listing key), upload (the manifest's
    record — no sniff of any kind), and keep (the shelf's key, sniffed once
    at the keep itself)."""
    import time

    import app.delivery.store as store_mod

    calls: list[str] = []
    monkeypatch.setattr(
        store_mod,
        "sniff_mime",
        lambda path, head_bytes=8192: calls.append(path) or "application/x-counted",
    )
    monkeypatch.setattr(store_mod, "_MIME_CACHE", {})
    c, _state, root = make_client(tmp_path, sids=("s1", "s2"))

    # outbox: the source's listing warms the pass; the attach must hit it
    (root / "outbox/s1").mkdir(parents=True, exist_ok=True)
    (root / "outbox/s1/report.md").write_bytes(b"# artifact\n")
    c.get("/api/v1/sessions/s1/files")
    p = root / "outbox/s1/report.md"
    st = p.stat()
    key = ("s1", "report.md", st.st_size, st.st_mtime)
    deadline = time.time() + 5
    while time.time() < deadline and store_mod.mime_cached(key) is None:
        time.sleep(0.01)
    assert store_mod.mime_cached(key) == "application/x-counted"
    r = c.post(
        "/api/v1/sessions/s2/files/attach",
        json={"source": "s1", "path": "outbox/report.md"},
    )
    assert r.status_code == 201, r.text
    assert r.json()["mime"] == "application/x-counted"
    assert calls == [str(p)], "the attach re-read the source's bytes"

    # upload: the manifest's record is the MIME — no sniff at all on attach
    _upload(c, "notes.csv", b"a,b\n", sid="s1")
    n_after_upload = len(calls)
    r = c.post(
        "/api/v1/sessions/s2/files/attach",
        json={"source": "s1", "path": "uploads/notes.csv"},
    )
    assert r.status_code == 201, r.text
    assert len(calls) == n_after_upload, "an upload attach must not sniff"

    # keep: the keep's one sniff is the shelf's; the attach reuses it
    r = c.post("/api/v1/sessions/s1/files/keep", json={"path": "outbox/report.md"})
    assert r.status_code == 201, r.text
    kept = r.json()
    n_after_keep = len(calls)
    assert n_after_keep == n_after_upload + 1, "the keep sniffs its link once"
    r = c.post(
        "/api/v1/sessions/s2/files/attach",
        json={"source": "keep", "path": kept["path"]},
    )
    assert r.status_code == 201, r.text
    assert r.json()["mime"] == "application/x-counted"
    assert len(calls) == n_after_keep, "the keep attach must reuse the shelf's sniff"


def test_attach_refuses_a_source_the_other_session_cannot_read(tmp_path):
    """The source is resolved through the SOURCE session's gates, so a file
    that session cannot serve cannot be laundered into another one."""
    c, _state, root = make_client(tmp_path)
    (root / "outbox/s1").mkdir(parents=True, exist_ok=True)
    assert c.post(
        "/api/v1/sessions/s2/files/attach", json={"source": "s1", "path": "outbox/ghost.md"}
    ).status_code == 404
    assert c.post(
        "/api/v1/sessions/s2/files/attach", json={"source": "nosuch", "path": "uploads/a"}
    ).status_code == 404


def test_keep_and_attach_preserve_the_folder_the_file_lived_in(tmp_path):
    """A keep of a file in a folder lands under that folder in the shelf's
    bucket — not flattened to the basename, where a sibling folder's same
    basename would suffix-collide — and an attach from an outbox subfolder or
    a folder upload lands in the target's inbox under the same relative path,
    so a tree the human organized survives into the shelf and into other
    sessions."""
    c, _state, root = make_client(tmp_path)
    _upload(c, "photos/a.jpg", b"\xff\xd8\xff\xe0a", sid="s1")
    _upload(c, "videos/a.jpg", b"\xff\xd8\xff\xe0b", sid="s1")
    (root / "outbox/s1" / "v2").mkdir(parents=True, exist_ok=True)
    (root / "outbox/s1" / "v2" / "report.md").write_bytes(b"# v2")

    # keep: the shelf shows s1/photos/a.jpg and s1/videos/a.jpg — two files,
    # no suffix, because the folder is part of the path
    r = c.post("/api/v1/sessions/s1/files/keep", json={"path": "uploads/photos/a.jpg"})
    assert r.status_code == 201, r.text
    assert r.json()["path"] == "s1/photos/a.jpg"
    r = c.post("/api/v1/sessions/s1/files/keep", json={"path": "uploads/videos/a.jpg"})
    assert r.status_code == 201, r.text
    assert r.json()["path"] == "s1/videos/a.jpg"
    rows = sorted(x["path"] for x in c.get("/api/v1/files").json()["keep"])
    assert rows == ["s1/photos/a.jpg", "s1/videos/a.jpg"]
    # an explicit as_name still wins (and may itself be a path)
    r = c.post(
        "/api/v1/sessions/s1/files/keep",
        json={"path": "outbox/v2/report.md", "as_name": "handoff/report.md"},
    )
    assert r.status_code == 201, r.text
    assert r.json()["path"] == "s1/handoff/report.md"

    # attach: the target's inbox mirrors the source's folder shape
    r = c.post(
        "/api/v1/sessions/s2/files/attach",
        json={"source": "s1", "path": "uploads/photos/a.jpg"},
    )
    assert r.status_code == 201, r.text
    assert r.json()["name"] == "photos/a.jpg"
    assert (root / "inbox/s2" / "photos" / "a.jpg").read_bytes() == b"\xff\xd8\xff\xe0a"
    r = c.post(
        "/api/v1/sessions/s2/files/attach",
        json={"source": "s1", "path": "outbox/v2/report.md"},
    )
    assert r.status_code == 201, r.text
    assert r.json()["name"] == "v2/report.md"
    assert (root / "inbox/s2" / "v2" / "report.md").read_bytes() == b"# v2"
    # from the keep shelf: the path lands after the bucket; the same bytes
    # already landed — the dedup no-op keeps the path
    r = c.post(
        "/api/v1/sessions/s2/files/attach",
        json={"source": "keep", "path": "s1/photos/a.jpg"},
    )
    assert r.status_code == 201, r.text
    assert r.json()["name"] == "photos/a.jpg"
    r = c.post(
        "/api/v1/sessions/s2/files/attach",
        json={"source": "keep", "path": "s1/videos/a.jpg"},
    )
    assert r.status_code == 201, r.text
    assert r.json()["name"] == "videos/a.jpg"
    assert (root / "inbox/s2" / "videos" / "a.jpg").read_bytes() == b"\xff\xd8\xff\xe0b"


def test_keep_keeps_the_dots_of_a_dot_file(tmp_path):
    """The default landing name is the source's own gated relpath — a dot
    file the agent wrote (outbox/.hidden/…) keeps its dots on the shelf; the
    safe-form renaming is for names a human TYPES, so an explicit as_name
    still resolves to its safe form, as an upload does."""
    c, _state, root = make_client(tmp_path)
    (root / "outbox/s1" / ".hidden").mkdir(parents=True, exist_ok=True)
    (root / "outbox/s1" / ".hidden" / "notes.md").write_bytes(b"n\n")
    r = c.post(
        "/api/v1/sessions/s1/files/keep",
        json={"path": "outbox/.hidden/notes.md"},
    )
    assert r.status_code == 201, r.text
    assert r.json()["path"] == "s1/.hidden/notes.md"
    (root / "outbox/s1" / "plain.md").write_bytes(b"p\n")
    r = c.post(
        "/api/v1/sessions/s1/files/keep",
        json={"path": "outbox/plain.md", "as_name": ".secret.md"},
    )
    assert r.status_code == 201, r.text
    assert r.json()["path"] == "s1/secret.md"


# ---------------------------------------------------------------------- #
# reclaim (the filesystem's own refcount)
# ---------------------------------------------------------------------- #


def test_unreferenced_bytes_are_reclaimed_and_referenced_ones_are_not(tmp_path):
    c, state, root = make_client(tmp_path)
    _upload(c, "gone.bin", b"temporary", sid="s1")
    assert c.delete("/api/v1/sessions/s1/files/uploads/gone.bin").status_code == 200
    _upload(c, "kept.bin", b"durable", sid="s1")
    c.post("/api/v1/sessions/s1/files/keep", json={"path": "uploads/kept.bin"})

    state.delivery.blobs.gc(grace_sec=-1.0)  # grace satisfied: nlink decides
    blobs = list((root / "blobs").glob("*/*"))
    assert [b.name for b in blobs] == [_sha_of(root / "keep/s1/kept.bin")]

    # both links must go — the session's and the keep's — before the bytes
    # are nobody's
    assert c.delete("/api/v1/sessions/s1/files/uploads/kept.bin").status_code == 200
    assert c.delete("/api/v1/files/keep/s1/kept.bin").status_code == 200
    assert state.delivery.gc_blobs(grace_sec=-1.0) == 1
    assert list((root / "blobs").glob("*/*")) == []


def test_a_blob_inside_the_grace_window_survives(tmp_path):
    """A delivery is adopting its blob and linking it into an inbox right
    now; a concurrent reclaim must not race it."""
    c, state, root = make_client(tmp_path)
    _upload(c, "fresh.bin", b"just-landed", sid="s1")
    import shutil

    shutil.rmtree(root / "inbox/s1")  # last link gone this instant
    assert state.delivery.gc_blobs(grace_sec=3600.0) == 0
    assert len(list((root / "blobs").glob("*/*"))) == 1


def test_boot_sweeps_an_adopt_temp_a_crash_left_behind(tmp_path):
    """A process that died mid-``adopt_hashed`` leaves its temp copy in the
    blob root. The GC tick never reaches it (it is not a blob), so the boot
    sweep is its only reclaim — and it must not touch a real blob."""
    c, state, root = make_client(tmp_path)
    _upload(c, "real.bin", b"survivor")
    blob_root = root / "blobs"
    stray = blob_root / ".adopt-deadbeefcafe"
    stray.write_bytes(b"a copy nobody will finish")
    not_a_temp = blob_root / "other"
    not_a_temp.write_bytes(b"left alone")

    report = state.delivery.gc_boot()
    assert report["adopt_temps_swept"] == [".adopt-deadbeefcafe"]
    assert not stray.exists()
    assert not_a_temp.exists()
    assert len(list(blob_root.glob("*/*"))) == 1  # the real blob is intact


def test_a_link_is_create_only_and_leaves_nothing_to_sweep(tmp_path):
    """``Blobs.link`` either makes the name or fails: a name a concurrent
    delivery or keep just took is refused (FileExistsError, the caller
    re-chooses), never overwritten. A link is one directory entry, so a
    crash leaves no temp a sweep would have to clean up."""
    c, state, root = make_client(tmp_path)
    _upload(c, "real.bin", b"survivor")
    blob = _blob(root, _sha_of(root / "inbox/s1/real.bin"))
    dest_dir = str(root / "inbox/s1")

    state.delivery.blobs.link(str(blob), dest_dir, "other.bin")
    assert (root / "inbox/s1/other.bin").read_bytes() == b"survivor"

    foreign = root / "inbox/s1/taken.bin"
    foreign.write_bytes(b"someone else's")
    import pytest

    with pytest.raises(FileExistsError):
        state.delivery.blobs.link(str(blob), dest_dir, "taken.bin")
    assert foreign.read_bytes() == b"someone else's"  # never overwritten
    # nothing hidden left behind on success or refusal
    assert [p.name for p in (root / "inbox/s1").iterdir() if p.name.startswith(".")] == []


# ---------------------------------------------------------------------- #
# a keep that the agent could still rewrite is not a keep
# ---------------------------------------------------------------------- #


def test_adopt_hashed_names_the_bytes_it_stores_even_while_writing(tmp_path, monkeypatch):
    """The source may move under the keep: the agent is still writing the
    artifact while the human keeps it. The blob's name must describe the
    bytes ACTUALLY stored — the pre-read's hash is trusted only when the
    store already holds the bytes; here it does not, so the hash rides the
    copy and the pre-read's name is thrown away."""
    import hashlib

    from app.delivery.blobs import Blobs
    from app.iso.layout import state_paths

    state = state_paths(str(tmp_path))
    blobs = Blobs(state)
    src = tmp_path / "moving.bin"
    head = b"x" * 300_000
    src.write_bytes(head)

    opens = {"n": 0}
    real_open = open

    class _MovingFile:
        def __init__(self, f):
            self._f = f
            opens["n"] += 1
            if opens["n"] == 2:  # the copy's open: the agent writes again
                with real_open(str(src), "ab") as g:
                    g.write(b"tail")

        def read(self, n=-1):
            return self._f.read(n)

        def __enter__(self):
            return self

        def __exit__(self, *a):
            self._f.close()

        def __getattr__(self, item):
            return getattr(self._f, item)

    def sneaky(path, *a, **k):
        f = real_open(path, *a, **k)
        if str(path) == str(src) and a and "b" in str(a[0]):
            return _MovingFile(f)
        return f

    monkeypatch.setattr("builtins.open", sneaky)
    try:
        blob, sha = blobs.adopt_hashed(str(src))
    finally:
        monkeypatch.undo()
    ondisk = real_open(blob, "rb").read()
    assert ondisk == head + b"tail", "the copy holds what was there when it read"
    assert sha == hashlib.sha256(ondisk).hexdigest(), "name = promise about the stored bytes"
    assert sha != hashlib.sha256(head).hexdigest(), "the pre-read's name was discarded"
    assert os.path.basename(blob) == sha
    assert not list((tmp_path / "blobs").glob(".adopt-*")), "no temp copy lingers"


def test_a_repeat_adoption_keeps_the_stored_inode(tmp_path):
    """Adopting content the store already holds must not swap the blob's
    inode: other keeps and inboxes link it, and replacing the name would
    strand their links on the old copy (two inodes for one sha = the dedup
    the store exists to prevent)."""
    import hashlib

    from app.delivery.blobs import Blobs
    from app.iso.layout import state_paths

    state = state_paths(str(tmp_path))
    blobs = Blobs(state)
    body = b"same-bytes\n"
    f1 = tmp_path / "f1.bin"
    f1.write_bytes(body)
    blob1, sha1 = blobs.adopt_hashed(str(f1))
    inode1 = (os.stat(blob1).st_dev, os.stat(blob1).st_ino)

    f2 = tmp_path / "f2.bin"
    f2.write_bytes(body)
    blob2, sha2 = blobs.adopt_hashed(str(f2))
    assert sha1 == sha2 == hashlib.sha256(body).hexdigest()
    assert (os.stat(blob2).st_dev, os.stat(blob2).st_ino) == inode1, "one copy, ever"


def test_a_second_adoption_of_stored_bytes_writes_nothing(tmp_path, monkeypatch):
    """The hot re-adopt: the store already holds the bytes, so the second
    adoption is one read and a free link — no temp copy. A full-size temp
    is the first thing to fail on a near-full volume, and it would fail
    even though every byte is already stored."""
    import builtins
    import hashlib

    from app.delivery.blobs import Blobs
    from app.iso.layout import state_paths

    state = state_paths(str(tmp_path))
    blobs = Blobs(state)
    body = b"again\n" * 1000
    f1 = tmp_path / "f1.bin"
    f1.write_bytes(body)
    blob1, sha1 = blobs.adopt_hashed(str(f1))
    inode1 = (os.stat(blob1).st_dev, os.stat(blob1).st_ino)

    f2 = tmp_path / "f2.bin"
    f2.write_bytes(body)
    written = []
    real_open = builtins.open

    def counting(path, mode="r", *a, **k):
        f = real_open(path, mode, *a, **k)
        if any(ch in str(mode) for ch in "wax+"):
            written.append(str(path))
        return f

    monkeypatch.setattr("builtins.open", counting)
    try:
        blob2, sha2 = blobs.adopt_hashed(str(f2))
    finally:
        monkeypatch.undo()
    assert (sha1, sha2) == (hashlib.sha256(body).hexdigest(),) * 2
    assert (os.stat(blob2).st_dev, os.stat(blob2).st_ino) == inode1, "one copy, ever"
    assert written == [], "a re-adopt of stored bytes is one read: nothing is written"


def test_concurrent_adoptions_of_identical_bytes_keep_one_store_inode(tmp_path):
    """The dedup use case under concurrency: several uploads of the same
    bytes finishing at once. The insertion must be create-only — if a late
    adopt replaced the store's inode, the earlier sessions' linked copies no
    longer pass the read gate (its proof: the inode IS the blob) and their
    uploads vanish from their listings, undeletable until release."""
    import hashlib
    import threading

    from app.delivery.blobs import Blobs
    from app.iso.layout import state_paths

    state = state_paths(str(tmp_path))
    blobs = Blobs(state)

    def _part(name, body):
        p = tmp_path / name
        p.write_bytes(body)
        return p

    # sequential: the repeat is a dedup hit, the inode is untouched, and the
    # staged part is dropped in both cases
    body = b"concurrent-bytes\n"
    sha = hashlib.sha256(body).hexdigest()
    blob1 = blobs.adopt(_part("part-0.bin", body), sha)[0]
    inode1 = (os.stat(blob1).st_dev, os.stat(blob1).st_ino)
    assert not (tmp_path / "part-0.bin").exists(), "the part is dropped on ingest"
    blob2, existed = blobs.adopt(_part("part-1.bin", body), sha)
    assert existed and not (tmp_path / "part-1.bin").exists()
    assert (os.stat(blob2).st_dev, os.stat(blob2).st_ino) == inode1, "one copy, ever"

    # concurrent: N uploads of the same bytes finish at once, each linking
    # its delivery into its own inbox the way deliver() does
    n, rounds = 8, 3
    for r in range(rounds):
        body_r = f"round-{r}\n".encode()
        sha_r = hashlib.sha256(body_r).hexdigest()
        inboxes = [tmp_path / f"inbox-{r}-{i}" for i in range(n)]
        for d in inboxes:
            d.mkdir()
        results = [None] * n
        barrier = threading.Barrier(n)

        def worker(i, d):
            part = _part(f"cpart-{r}-{i}.bin", body_r)
            barrier.wait()  # maximize the window: everyone adopts at once
            blob, _existed = blobs.adopt(part, sha_r)
            os.link(blob, str(d / "file"))
            results[i] = os.lstat(d / "file")

        threads = [threading.Thread(target=worker, args=(i, d)) for i, d in enumerate(inboxes)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        store = os.stat(state.blob_for(sha_r))
        for i, st in enumerate(results):
            assert (st.st_dev, st.st_ino) == (store.st_dev, store.st_ino), (
                f"session {i}'s delivery is no longer the store's blob"
            )
            assert blobs.is_same_blob(sha_r, st), "the read gate must hold for every delivery"


def test_rewriting_the_artifact_never_touches_the_kept_bytes(tmp_path):
    """An artifact's inode belongs to the slot: the agent writes it by
    truncating the same file (every editor, every ``> file``), which would
    rewrite any link to it — including the one the human just made
    permanent. So a keep points at the STORE's copy, which no session can
    write, and this pins that the kept bytes do not follow the artifact."""
    c, _state, root = make_client(tmp_path)
    art = root / "outbox/s1/report.md"
    art.write_bytes(b"# the real result\n")
    assert c.post(
        "/api/v1/sessions/s1/files/keep", json={"path": "outbox/report.md"}
    ).status_code == 201

    art.write_bytes(b"truncated by a later run\n")  # same inode, in place
    assert (root / "keep/s1/report.md").read_bytes() == b"# the real result\n"
    # and unlinking the artifact (a rewrite by rename) cannot take the keep
    art.unlink()
    assert (root / "keep/s1/report.md").read_bytes() == b"# the real result\n"


def test_keeping_what_is_already_stored_costs_no_copy(tmp_path):
    """A keep of an upload adds a link and no bytes: one inode, one blob,
    however many hands hold it."""
    c, _state, root = make_client(tmp_path)
    _upload(c, "data.bin", b"x" * 5000)
    before = sorted(p.name for p in (root / "blobs").glob("*/*"))
    assert c.post(
        "/api/v1/sessions/s1/files/keep", json={"path": "uploads/data.bin"}
    ).status_code == 201
    assert sorted(p.name for p in (root / "blobs").glob("*/*")) == before
    assert _inodes(
        str(root / "inbox/s1/data.bin"),
        str(root / "keep/s1/data.bin"),
        str(_blob(root, _sha_of(root / "keep/s1/data.bin"))),
    ) == {(os.stat(root / "keep/s1/data.bin").st_dev, os.stat(root / "keep/s1/data.bin").st_ino)}


def test_keep_of_a_file_unlinked_under_the_gate_is_a_404_not_a_500(tmp_path, monkeypatch):
    """The route's gate and the keep's own stat are syscalls apart, and the
    outbox is agent-writable: an artifact unlinked in the window answers
    the gate's 404, never a 500 — and no shelf link is made for a file
    that is already gone."""
    c, _state, root = make_client(tmp_path)
    out = root / "outbox" / "s1"
    out.mkdir(parents=True, exist_ok=True)
    src = out / "gone.md"
    src.write_bytes(b"bye")
    real_stat = os.stat
    seen = {"n": 0}

    def vanished(path, *a, **k):
        if str(path) == str(src):
            seen["n"] += 1
            if seen["n"] >= 2:  # the gate's stat passes; the keep's does not
                raise FileNotFoundError(path)
        return real_stat(path, *a, **k)

    monkeypatch.setattr("app.delivery.service.os.stat", vanished)
    r = c.post("/api/v1/sessions/s1/files/keep", json={"path": "outbox/gone.md"})
    assert r.status_code == 404, r.text
    assert not list((root / "keep").glob("s1/*"))


def test_attach_of_a_file_unlinked_under_the_copy_is_a_404_not_a_500(tmp_path, monkeypatch):
    """A source the store does not hold is copied into it, and a multi-GB
    artifact is a copy that runs for minutes: the agent may unlink the
    source partway. A source that is gone at the open is the gate's 404
    shape, never a 500, and nothing is stored under bytes that were never
    read."""
    c, _state, root = make_client(tmp_path)
    out = root / "outbox" / "s2"
    out.mkdir(parents=True, exist_ok=True)
    src = out / "big.bin"
    src.write_bytes(b"x" * 4096)
    real_open = open

    def vanished(path, *a, **k):
        if str(path) == str(src):
            raise FileNotFoundError(path)
        return real_open(path, *a, **k)

    monkeypatch.setattr("app.delivery.blobs.open", vanished, raising=False)
    r = c.post(
        "/api/v1/sessions/s1/files/attach",
        json={"source": "s2", "path": "outbox/big.bin"},
    )
    assert r.status_code == 404, r.text
    assert not list((root / "blobs").glob("*/*"))
    assert not list((root / "inbox" / "s1").glob("*"))


def test_keep_of_a_reclaimed_blob_is_a_404_not_a_500(tmp_path, monkeypatch):
    """An upload's inode IS the store's blob, so its keep is a free link:
    but a reclaim can race the window between the gate and the link, and
    the link's FileNotFoundError is a vanished file — the 404 shape,
    never a 500 off the missing blob, and no shelf link for bytes that
    are already gone."""
    c, _state, root = make_client(tmp_path)
    _upload(c, "data.bin", b"x" * 5000)

    def link_gone(self, blob_path, dest_dir, name):
        raise FileNotFoundError(blob_path)

    monkeypatch.setattr("app.delivery.blobs.Blobs.link", link_gone)
    r = c.post(
        "/api/v1/sessions/s1/files/keep", json={"path": "uploads/data.bin"}
    )
    assert r.status_code == 404, r.text
    assert not list((root / "keep").glob("s1/*"))


def test_attach_landing_on_a_reclaimed_blob_is_a_404_not_a_500(tmp_path, monkeypatch):
    """An attach whose source IS a stored blob lands as a free link into
    the target's inbox: a reclaim racing the landing link is a vanished
    file — the 404 shape, never a 500 — and nothing lands."""
    c, _state, root = make_client(tmp_path)
    _upload(c, "src.bin", b"y" * 4000, sid="s2")

    def link_gone(self, blob_path, dest_dir, name):
        raise FileNotFoundError(blob_path)

    monkeypatch.setattr("app.delivery.blobs.Blobs.link", link_gone)
    r = c.post(
        "/api/v1/sessions/s1/files/attach",
        json={"source": "s2", "path": "uploads/src.bin"},
    )
    assert r.status_code == 404, r.text
    assert not list((root / "inbox" / "s1").glob("*"))
    assert not list((root / "keep").glob("*/*"))


def test_attach_of_a_keep_unlinked_under_the_sha_lookup_is_a_404_not_a_500(
    tmp_path, monkeypatch
):
    """The attach's sha lookup lstats the keep after its containment gate,
    and an un-keep from another tab lands exactly in that window: the
    lookup's FileNotFoundError is the gate's 404 shape, never a 500 off the
    catch-all, and nothing lands in the target's inbox."""
    c, _state, root = make_client(tmp_path, sids=("s1", "s2"))
    (root / "outbox/s1").mkdir(parents=True, exist_ok=True)
    (root / "outbox/s1/keepme.bin").write_bytes(b"keep me\n")
    kept = c.post(
        "/api/v1/sessions/s1/files/keep", json={"path": "outbox/keepme.bin"}
    ).json()
    keep_path = str(root / "keep" / kept["path"])

    real_lstat = os.lstat

    def vanished(path, *a, **k):
        if str(path) == keep_path:
            raise FileNotFoundError(path)
        return real_lstat(path, *a, **k)

    monkeypatch.setattr("app.delivery.service.os.lstat", vanished)
    r = c.post(
        "/api/v1/sessions/s2/files/attach",
        json={"source": "keep", "path": kept["path"]},
    )
    assert r.status_code == 404, r.text
    assert not list((root / "inbox" / "s2").glob("*"))


def test_download_of_a_keep_unlinked_under_the_lstat_is_a_404_not_a_500(
    tmp_path, monkeypatch
):
    """The kept-file download lstats after the route's gate: an un-keep
    landing in the window is the gate's 404 shape, never a 500 off the
    vanished path."""
    c, _state, root = make_client(tmp_path)
    (root / "outbox/s1").mkdir(parents=True, exist_ok=True)
    (root / "outbox/s1/keepme.bin").write_bytes(b"keep me\n")
    kept = c.post(
        "/api/v1/sessions/s1/files/keep", json={"path": "outbox/keepme.bin"}
    ).json()
    keep_path = str(root / "keep" / kept["path"])

    real_lstat = os.lstat

    def vanished(path, *a, **k):
        if str(path) == keep_path:
            raise FileNotFoundError(path)
        return real_lstat(path, *a, **k)

    monkeypatch.setattr("app.api.app.os.lstat", vanished)
    r = c.get(f"/api/v1/files/keep/{kept['path']}")
    assert r.status_code == 404, r.text
