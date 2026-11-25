"""Unit: human-side file deletion (inbox uploads and outbox artifacts).

Delete is a first-class human action, and it must be exactly as careful as
a download: same path resolution, so it can never reach a byte a download
could not. A manifest entry whose file is gone would be a row the human
cannot use, so the file and its entry go together — file first (a crash
between the two leaves a filtered-out row, never an unaccounted file).

Pinned here:
- the upload's manifest entry disappears with the file (and the entry is
  matched through ``_safe_relpath``, so a legacy ``.env`` row goes too);
- a nested upload (a folder upload) deletes by path and prunes the folders
  it empties, but NEVER the inbox root (the bind source);
- deleting an artifact prunes the directories it empties, but NEVER the
  outbox root (that is the bind target / the workdir convention);
- a symlinked name inside the outbox is not deletable and its target
  survives (the delete must not be a way to unlink through a link);
- a path that escapes is a 404, not a 500, and deletes nothing;
- deleting a name that is not there is a 404.
"""
from __future__ import annotations

import time
from pathlib import Path

import pytest

from app.delivery.store import (
    DeliveredFile,
    FileNotDelivered,
    SessionFiles,
)


def _sf(tmp_path) -> SessionFiles:
    sf = SessionFiles(
        session_dir=tmp_path / "sessions" / "s1",
        inbox=tmp_path / "inbox" / "s1",
        outbox=tmp_path / "outbox",
        slot=None,
    )
    sf.outbox.mkdir(parents=True, exist_ok=True)
    return sf


def _deliver(sf: SessionFiles, tmp_path, name: str, body: bytes = b"x") -> str:
    part = tmp_path / f"part-{time.time_ns()}"
    part.write_bytes(body)
    landed = sf.deliver(part, name)
    sf.register_upload(
        DeliveredFile(name=landed, sha256="ab" * 32, size=len(body),
                      mime="text/plain", ts=time.time())
    )
    return landed


# ---------------------------------------------------------------------- #
# uploads: the file and its manifest row go together
# ---------------------------------------------------------------------- #


def test_delete_upload_removes_file_and_manifest_row(tmp_path):
    sf = _sf(tmp_path)
    landed = _deliver(sf, tmp_path, "notes.txt", b"hello")
    assert [u["name"] for u in sf.list_uploads()] == [landed]

    gone = sf.delete_upload(landed)

    assert gone == {"name": landed, "size": 5}
    assert not (sf.inbox / landed).exists()
    assert sf.list_uploads() == []
    assert sf.read_manifest()["uploads"] == []


def test_delete_upload_removes_a_legacy_dot_name_row(tmp_path):
    """A manifest written before the landed-name fix holds ``.env`` while
    the file on disk is ``env`` — deleting the upload must clear both."""
    sf = _sf(tmp_path)
    landed = _deliver(sf, tmp_path, ".env", b"A=1\n")
    man = sf.read_manifest()
    man["uploads"][0]["name"] = ".env"  # the legacy shape
    sf._write_manifest(man)

    sf.delete_upload(landed)

    assert sf.read_manifest()["uploads"] == []


def test_delete_upload_unknown_name_raises(tmp_path):
    sf = _sf(tmp_path)
    with pytest.raises(FileNotDelivered):
        sf.delete_upload("nope.txt")


def test_delete_upload_cannot_walk_out_of_the_inbox(tmp_path):
    sf = _sf(tmp_path)
    outside = tmp_path / "victim.txt"
    outside.write_bytes(b"do not delete me")
    with pytest.raises(FileNotDelivered):
        sf.delete_upload("../victim.txt")
    assert outside.exists()


def test_delete_upload_of_a_symlink_leaves_the_target(tmp_path):
    sf = _sf(tmp_path)
    target = tmp_path / "victim.txt"
    target.write_bytes(b"do not delete me")
    link = sf.inbox / "link.txt"
    link.symlink_to(target)

    with pytest.raises(FileNotDelivered):
        sf.delete_upload("link.txt")

    assert target.exists() and link.is_symlink()


# ---------------------------------------------------------------------- #
# artifacts: delete, and prune only what the delete emptied
# ---------------------------------------------------------------------- #


def test_delete_outbox_removes_the_artifact(tmp_path):
    sf = _sf(tmp_path)
    (sf.outbox / "report.md").write_bytes(b"# hi\n")
    gone = sf.delete_outbox("report.md")
    assert gone["path"] == "report.md" and gone["size"] == 5
    assert not (sf.outbox / "report.md").exists()


def test_delete_outbox_prunes_emptied_dirs_but_not_the_root(tmp_path):
    sf = _sf(tmp_path)
    deep = sf.outbox / "frames" / "batch1"
    deep.mkdir(parents=True)
    (deep / "0001.png").write_bytes(b"png")
    (sf.outbox / "keepme").mkdir()
    (sf.outbox / "keepme" / "x.txt").write_bytes(b"x")

    sf.delete_outbox("frames/batch1/0001.png")

    assert not (sf.outbox / "frames").exists()  # emptied: pruned
    assert sf.outbox.is_dir()  # the root never goes
    assert (sf.outbox / "keepme" / "x.txt").is_file()  # siblings untouched


def test_delete_outbox_keeps_a_non_empty_parent(tmp_path):
    sf = _sf(tmp_path)
    d = sf.outbox / "frames"
    d.mkdir()
    (d / "a.png").write_bytes(b"a")
    (d / "b.png").write_bytes(b"b")
    sf.delete_outbox("frames/a.png")
    assert (d / "b.png").is_file() and d.is_dir()


def test_delete_outbox_cannot_walk_out(tmp_path):
    sf = _sf(tmp_path)
    victim = tmp_path / "victim.txt"
    victim.write_bytes(b"do not delete me")
    with pytest.raises(FileNotDelivered):
        sf.delete_outbox("../victim.txt")
    assert victim.exists()


def test_delete_outbox_of_a_symlink_leaves_the_target(tmp_path):
    """A planted link is not a way to delete something else: the resolve
    lands outside the outbox, so it is refused before any unlink."""
    sf = _sf(tmp_path)
    target = tmp_path / "victim.txt"
    target.write_bytes(b"do not delete me")
    (sf.outbox / "link.txt").symlink_to(target)
    with pytest.raises(FileNotDelivered):
        sf.delete_outbox("link.txt")
    assert target.exists()


def test_prune_stops_at_a_symlinked_dir(tmp_path):
    """A planted symlinked dir under the outbox must not become a way to
    rmdir outside the outbox."""
    sf = _sf(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (sf.outbox / "linkdir").symlink_to(outside)
    sf._prune_empty(sf.outbox / "linkdir", sf.outbox.resolve())
    assert outside.is_dir()


# ---------------------------------------------------------------------- #
# over HTTP: 200 with the bytes it freed, 404 for anything unreachable
# ---------------------------------------------------------------------- #


def _client(tmp_path):
    from _filesapp import make_client

    c, state, root = make_client(tmp_path)
    return c, state


def _outbox_dir(tmp_path):
    from app.iso.layout import state_paths

    return Path(state_paths(str(tmp_path / "state")).outbox_for("s1"))


def test_http_delete_upload(tmp_path):
    c, _ = _client(tmp_path)
    c.post(
        "/api/v1/sessions/s1/files/upload?name=a.txt",
        content=b"12345",
        headers={"content-type": "application/octet-stream"},
    )
    r = c.delete("/api/v1/sessions/s1/files/uploads/a.txt")
    assert r.status_code == 200
    assert r.json() == {"deleted": "uploads/a.txt", "size": 5}
    assert c.get("/api/v1/sessions/s1/files").json()["uploads"] == []


def test_http_delete_a_nested_upload_prunes_its_folder_but_not_the_inbox(tmp_path):
    """A folder upload deletes by path: the manifest entry, the file, and the
    emptied folders go together — the inbox root itself never (it is the
    bind source), and a folder that still holds files stays."""
    c, _ = _client(tmp_path)
    from app.iso.layout import state_paths

    inbox = Path(state_paths(str(tmp_path / "state")).inbox_for("s1"))
    for name in ("deep/one/a.txt", "deep/one/b.txt", "deep/two/c.txt"):
        c.post(
            f"/api/v1/sessions/s1/files/upload?name={name}",
            content=b"x",
            headers={"content-type": "application/octet-stream"},
        )
    r = c.delete("/api/v1/sessions/s1/files/uploads/deep/one/a.txt")
    assert r.status_code == 200, r.text
    assert r.json() == {"deleted": "uploads/deep/one/a.txt", "size": 1}
    assert (inbox / "deep/one").is_dir(), "the folder still holds b.txt"
    r = c.delete("/api/v1/sessions/s1/files/uploads/deep/one/b.txt")
    assert r.status_code == 200, r.text
    assert not (inbox / "deep/one").exists(), "the emptied folder is pruned"
    assert (inbox / "deep").is_dir() and (inbox / "deep/two").is_dir()
    r = c.delete("/api/v1/sessions/s1/files/uploads/deep/two/c.txt")
    assert r.status_code == 200, r.text
    assert not (inbox / "deep").exists()
    assert inbox.is_dir(), "the inbox root itself is never pruned"
    # a delete of a folder name (not a file) is a 404, not a rmtree
    c.post(
        "/api/v1/sessions/s1/files/upload?name=keep/a.txt",
        content=b"x",
        headers={"content-type": "application/octet-stream"},
    )
    assert c.delete("/api/v1/sessions/s1/files/uploads/keep").status_code == 404
    assert (inbox / "keep/a.txt").is_file()


def test_http_delete_upload_unknown_is_404(tmp_path):
    c, _ = _client(tmp_path)
    assert c.delete("/api/v1/sessions/s1/files/uploads/ghost").status_code == 404


def test_http_delete_outbox_artifact(tmp_path):
    c, _ = _client(tmp_path)
    out = _outbox_dir(tmp_path)
    (out / "sub").mkdir(parents=True)
    (out / "sub" / "big.bin").write_bytes(b"z" * 128)
    r = c.delete("/api/v1/sessions/s1/files/outbox/sub/big.bin")
    assert r.status_code == 200 and r.json()["size"] == 128
    assert not (out / "sub").exists()


def test_http_delete_outbox_escape_is_404_and_harmless(tmp_path):
    c, _ = _client(tmp_path)
    victim = tmp_path / "victim.txt"
    victim.write_bytes(b"keep me")
    # percent-encoded so the client cannot normalise the traversal away and
    # "fix" the attack into a harmless URL
    r = c.delete("/api/v1/sessions/s1/files/outbox/%2e%2e%2f%2e%2e%2fvictim.txt")
    assert r.status_code in (404, 400)
    assert victim.exists()
    # the whole-outbox delete is refused too (it is a directory, not a file)
    assert c.delete("/api/v1/sessions/s1/files/outbox/").status_code in (404, 400)


def test_http_download_of_a_link_name_is_404_and_harmless(tmp_path):
    """The download gate is the literal-name gate too: a hand-crafted link
    into the area does not answer for its target, and nothing follows the
    link. The target serves under its own name."""
    c, _ = _client(tmp_path)
    out = _outbox_dir(tmp_path)
    (out / "real.txt").write_bytes(b"do not delete me")
    (out / "link.txt").symlink_to("real.txt")
    assert c.get("/api/v1/sessions/s1/files/outbox/link.txt").status_code == 404
    assert (out / "real.txt").is_file()
    assert c.get("/api/v1/sessions/s1/files/outbox/real.txt").content == b"do not delete me"


def test_delete_invalidates_the_usage_meter(tmp_path):
    c, app_state = _client(tmp_path)
    c.post(
        "/api/v1/sessions/s1/files/upload?name=a.txt",
        content=b"12345",
        headers={"content-type": "application/octet-stream"},
    )
    app_state.lc.usage.report()  # warm the cache
    cached = app_state.lc.usage._report
    assert cached is not None and cached["areas"]["inbox"]["files"] == 1
    c.delete("/api/v1/sessions/s1/files/uploads/a.txt")
    # the delete forces a re-walk: the next report is a fresh dict that sees
    # the reference gone (a cache hit would still bill the link the human
    # just unmade). The blob itself survives until the GC tick reclaims it —
    # the store's own refcount, not the meter's, is what frees the bytes.
    fresh = app_state.lc.usage.report()
    assert fresh is not cached
    assert fresh["areas"]["inbox"]["files"] == 0


# ---------------------------------------------------------------------- #
# a file that vanished after its gate passed: the gate's 404, never a 500
# ---------------------------------------------------------------------- #


def test_http_delete_of_a_file_unlinked_under_the_gate_is_a_404(tmp_path, monkeypatch):
    """The gate and the unlink are two syscalls apart, and the outbox is
    agent-writable: an artifact unlinked in the window answers the gate's
    404, never a 500 off a vanished path."""
    import os

    import app.delivery.store as store

    c, _ = _client(tmp_path)
    out = _outbox_dir(tmp_path)
    (out / "gone.md").write_bytes(b"bye")
    real_unlink = os.unlink

    def vanished(path, *a, **k):
        if str(path).endswith("/gone.md"):
            raise FileNotFoundError(path)
        return real_unlink(path, *a, **k)

    monkeypatch.setattr(store.os, "unlink", vanished)
    r = c.delete("/api/v1/sessions/s1/files/outbox/gone.md")
    assert r.status_code == 404, r.text
    # the race "won": the unlink never ran, so the file is still there and
    # a plain re-delete still works
    assert (out / "gone.md").exists()
    monkeypatch.undo()
    assert c.delete("/api/v1/sessions/s1/files/outbox/gone.md").status_code == 200


def test_http_delete_upload_unlinked_under_the_gate_is_a_404(tmp_path, monkeypatch):
    """The same window on the inbox side — a delete from a second tab
    loses to the unlink as a 404, never a 500, and the manifest row
    survives for the file that (in the real race) still has a name."""
    import os

    import app.delivery.store as store

    c, _ = _client(tmp_path)
    c.post(
        "/api/v1/sessions/s1/files/upload?name=a.txt",
        content=b"12345",
        headers={"content-type": "application/octet-stream"},
    )
    real_unlink = os.unlink

    def vanished(path, *a, **k):
        if str(path).endswith("/a.txt"):
            raise FileNotFoundError(path)
        return real_unlink(path, *a, **k)

    monkeypatch.setattr(store.os, "unlink", vanished)
    assert c.delete("/api/v1/sessions/s1/files/uploads/a.txt").status_code == 404
    monkeypatch.undo()
    # the row is intact: the failed delete dropped neither file nor entry
    assert [u["name"] for u in c.get("/api/v1/sessions/s1/files").json()["uploads"]] == [
        "a.txt"
    ]
