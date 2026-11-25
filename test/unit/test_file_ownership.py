"""Unit: root reads only what the session's own uid owns.

The app is root, so it CAN read any inode on the state volume — it must only
read the ones the session owns. The outbox is slot-writable, so a planted
link is in play: a symlink is caught by resolve-then-contain, but a HARDLINK
is a plain file whose inode somebody else owns — listing one would advertise
it, hash it (root reading the target's bytes), and hand it to the human.

The test container is unprivileged, so a foreign-owned file cannot be made
with chown; the gate is pinned from both sides instead:

- ``uid`` set to a uid no file can have ⇒ every read/delete path refuses
  (the hardlink shape: the file exists, is regular, is inside the dir, and
  is not the session's);
- ``uid`` set to the running uid ⇒ the same paths serve normally (the gate
  has not simply broken reads).

One exception, with its own proof: a delivered upload is the APP's inode
(the blob store holds it, which is what makes it un-writable and un-deletable
by the agent), so an inbox file is also readable when its inode IS the blob
for the sha the app computed at upload time — pinned as a property of the
inode, not the recorded hash, because the manifest is slot-writable and a
forged entry is the attack the exception would otherwise reopen.
"""
from __future__ import annotations

import os
from types import SimpleNamespace

import pytest

from _filesapp import make_client, slot_identity, upload
from app.delivery.blobs import Blobs
from app.delivery.store import FileNotDelivered, SessionFiles
from app.iso.layout import state_paths

FOREIGN_UID = 0xFFFFFFFF  # no file in a test tree can be owned by this


def _sf(tmp_path, uid: int | None) -> SessionFiles:
    slot = (
        None
        if uid is None
        else SimpleNamespace(name="slot9", uid=uid, gid=uid, home=str(tmp_path))
    )
    sf = SessionFiles(
        session_dir=tmp_path / "sessions" / "s1",
        inbox=tmp_path / "inbox" / "s1",
        outbox=tmp_path / "outbox",
        slot=slot,
    )
    sf.outbox.mkdir(parents=True, exist_ok=True)
    return sf


def _artifacts(sf: SessionFiles) -> None:
    (sf.outbox / "report.md").write_bytes(b"# report\n")
    (sf.inbox / "data.csv").write_bytes(b"a,b\n1,2\n")


# ---------------------------------------------------------------------- #
# policy
# ---------------------------------------------------------------------- #


def test_only_the_session_uid_is_readable(tmp_path):
    sf = _sf(tmp_path, 4242)
    assert sf._serves_uid(4242) is True
    assert sf._serves_uid(4243) is False
    assert sf._serves_uid(0) is False


def test_no_slot_identity_means_no_uid_bound(tmp_path):
    """Nothing to bind reads to (dev/test layout): the gate is inert rather
    than refusing every file — but it is never *guessed* at."""
    assert _sf(tmp_path, None)._serves_uid(1) is True


# ---------------------------------------------------------------------- #
# the gate is applied on every read/delete path
# ---------------------------------------------------------------------- #


def test_foreign_outbox_file_is_not_listed(tmp_path):
    sf = _sf(tmp_path, FOREIGN_UID)
    _artifacts(sf)
    assert sf.list_outbox() == []


def test_foreign_outbox_file_path_is_not_delivered(tmp_path):
    sf = _sf(tmp_path, FOREIGN_UID)
    _artifacts(sf)
    with pytest.raises(FileNotDelivered):
        sf.outbox_path("report.md")


def test_foreign_upload_path_is_not_delivered(tmp_path):
    sf = _sf(tmp_path, FOREIGN_UID)
    _artifacts(sf)
    with pytest.raises(FileNotDelivered):
        sf.upload_path("data.csv")


def test_foreign_files_cannot_be_deleted(tmp_path):
    sf = _sf(tmp_path, FOREIGN_UID)
    _artifacts(sf)
    with pytest.raises(FileNotDelivered):
        sf.delete_outbox("report.md")
    with pytest.raises(FileNotDelivered):
        sf.delete_upload("data.csv")
    # refused means untouched
    assert (sf.outbox / "report.md").exists() and (sf.inbox / "data.csv").exists()


def test_own_uid_serves_the_same_files(tmp_path):
    sf = _sf(tmp_path, os.getuid())
    _artifacts(sf)
    assert [e["path"] for e in sf.list_outbox()] == ["report.md"]
    assert sf.outbox_path("report.md").is_file()
    assert sf.upload_path("data.csv").is_file()


# ---------------------------------------------------------------------- #
# over HTTP: a foreign file is invisible and unservable, never a 500
# ---------------------------------------------------------------------- #


@pytest.fixture
def foreign_client(tmp_path, monkeypatch):
    """A session whose slot identity resolves to a uid no file can have."""
    return make_client(
        tmp_path, slot=FOREIGN_UID, slot_ident=slot_identity(FOREIGN_UID), monkeypatch=monkeypatch
    )


@pytest.fixture
def own_client(tmp_path, monkeypatch):
    uid = os.getuid()
    return make_client(
        tmp_path, slot=uid, slot_ident=slot_identity(uid), monkeypatch=monkeypatch
    )


def _upload(c, name="report.md", body=b"# report\n") -> None:
    assert upload(c, name, body).status_code == 201


def _plant(root, rel: str, body=b"a,b\n1,2\n") -> None:
    """A file appears in a session area without the app putting it there —
    the shape of a hardlink the agent planted (regular file, inside the dir,
    somebody else's inode)."""
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(body)


def test_a_sessions_own_files_are_listed_served_and_deletable(own_client):
    c, _state, _root = own_client
    _upload(c)
    body = c.get("/api/v1/sessions/s1/files").json()
    assert [u["name"] for u in body["uploads"]] == ["report.md"]
    assert c.get("/api/v1/sessions/s1/files/uploads/report.md").status_code == 200
    assert c.delete("/api/v1/sessions/s1/files/uploads/report.md").status_code == 200
    assert c.get("/api/v1/sessions/s1/files/uploads/report.md").status_code == 404


def test_everything_is_unreadable_for_a_foreign_slot(foreign_client):
    """The hardlink shape from the app's side: files exist on disk, and the
    session that cannot own them gets nothing — not even a 500."""
    c, _state, root = foreign_client
    _plant(root, "inbox/s1/data.csv")
    _plant(root, "outbox/s1/report.md")
    body = c.get("/api/v1/sessions/s1/files").json()
    assert body["uploads"] == [] and body["outbox"] == []
    assert c.get("/api/v1/sessions/s1/files/uploads/data.csv").status_code == 404
    assert c.delete("/api/v1/sessions/s1/files/uploads/data.csv").status_code == 404
    assert c.get("/api/v1/sessions/s1/files/outbox/report.md").status_code == 404
    assert c.delete("/api/v1/sessions/s1/files/outbox/report.md").status_code == 404
    # refused means untouched
    assert (root / "inbox/s1/data.csv").exists() and (root / "outbox/s1/report.md").exists()


def test_a_forged_manifest_entry_does_not_make_a_planted_file_readable(
    tmp_path, foreign_client
):
    """The manifest lives in a slot-owned dir, so the agent CAN rewrite it.

    The blob proof must therefore be about the INODE, not about the name or
    the recorded hash: the app delivers a real upload (its blob exists), then
    a planted root-owned file claims that sha under another name. Same sha,
    different inode — refused. (For the link to be accepted, those exact bytes
    would have had to arrive through a human upload.)
    """
    c, state, root = foreign_client
    _upload(c, "real.md", b"real bytes\n")
    sha = c.get("/api/v1/sessions/s1/files").json()["uploads"][0]["sha256"]
    _plant(root, "inbox/s1/claimed.txt", b"somebody else's bytes\n")
    manifest = state.delivery.files(state.state, "s1", None)
    data = manifest.read_manifest()
    data["uploads"].append(
        {"name": "claimed.txt", "sha256": sha, "size": 10, "mime": "text/plain", "ts": 1.0}
    )
    manifest._write_manifest(data)

    body = c.get("/api/v1/sessions/s1/files").json()
    assert [u["name"] for u in body["uploads"]] == ["real.md"]  # the claim is not listed
    assert c.get("/api/v1/sessions/s1/files/uploads/claimed.txt").status_code == 404


# ---------------------------------------------------------------------- #
# delivery must never hand the inbox to the session it is delivering to
# ---------------------------------------------------------------------- #


def test_delivering_never_chowns_the_inbox_directory(tmp_path, monkeypatch):
    """The read-only inbox rests on one fact: the inbox DIRECTORY is root's.
    A delivery that chowned it to the session would hand back the power to
    rename or unlink what the human sent — on the first upload after the
    mount, long after the mount code had set it right.

    Pinned structurally: no chown on this path may target the inbox
    directory, with a slot identity attached (so the chown path is live), both
    with and without a blob store.
    """
    calls: list[str] = []
    monkeypatch.setattr("app.delivery.store.os.chown",
                        lambda path, uid, gid: calls.append(str(path)))
    sf = _sf(tmp_path, os.getuid())
    sf.inbox.mkdir(parents=True, exist_ok=True)
    part = tmp_path / "staged.part"
    part.write_bytes(b"uploaded bytes")
    sf.deliver(part, "a.txt")  # no store: the rename+chown fallback
    assert calls, "the fallback path should still chown FILES (not the dir)"
    assert str(sf.inbox) not in calls
    part.write_bytes(b"more bytes")
    sf_with_store = SessionFiles(
        session_dir=tmp_path / "sessions" / "s1",
        inbox=tmp_path / "inbox" / "s1",
        outbox=tmp_path / "outbox",
        slot=SimpleNamespace(name="slot9", uid=os.getuid(), gid=os.getuid(),
                             home=str(tmp_path)),
        blobs=Blobs(state_paths(str(tmp_path))),
    )
    calls.clear()
    sf_with_store.deliver(part, "b.txt", "0" * 64)
    assert calls == []  # the store path owns nothing it chowns