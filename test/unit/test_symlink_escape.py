"""Unit: F2 — the app (root) must never write or chown THROUGH a symlink
the slot planted in a slot-owned tree.

The attack surface this pins down:
- ``state/sessions/<s>/`` is slot-owned (mount_session chowns it), and
  ``register_upload`` used a FIXED temp name written with ``write_text``
  (follows symlinks) — a planted ``files.json.tmp -> <victim>`` made the
  next human upload truncate/overwrite <victim> as root.
- ``_chown_tree`` / ``os.chown(outbox)`` dereferenced entries: on a
  re-mount after a reboot (the state disk survives, the mounts don't) a
  planted link made the app chown the TARGET to the slot uid — e.g.
  /etc/servitor (the key slice) or /opt/servitor/app (the root app's
  import path = code execution at the next restart).
- the outbox/upload path guards used a string-prefix check (missing
  separator) that let a resolved sibling dir (``outbox2/...``) pass.

All tests run unprivileged (pure fs semantics + call recording).
"""
from __future__ import annotations

import os
import threading
import time
from pathlib import Path

import pytest

from app.delivery.store import (
    DeliveredFile,
    DeliveryError,
    FileNotDelivered,
    SessionFiles,
)
import app.iso.mounts as M
from app.iso.mounts import MountError


def _sfiles(tmp_path: Path) -> SessionFiles:
    """SessionFiles with no slot (chown no-ops) — the pure fs paths."""
    return SessionFiles(
        session_dir=tmp_path / "sessions" / "s1",
        inbox=tmp_path / "inbox" / "s1",
        outbox=tmp_path / "scratch" / "s1" / "workdir" / "outbox",
        slot=None,
    )


def _upload(name: str = "report.csv") -> DeliveredFile:
    return DeliveredFile(
        name=name, sha256="ab" * 32, size=3, mime="text/csv", ts=time.time()
    )


# ---------------------------------------------------------------------- #
# register_upload: the fixed temp name must never be followed
# ---------------------------------------------------------------------- #


def test_register_upload_refuses_planted_tmp_symlink(tmp_path):
    sf = _sfiles(tmp_path)
    victim = tmp_path / "victim.txt"
    victim.write_text("A" * 560)
    # the plant: the slot owns the session dir, links the fixed temp name
    (sf.session_dir / "files.json.tmp").symlink_to(victim)

    with pytest.raises(DeliveryError):
        sf.register_upload(_upload())

    # the target is untouched — this is the whole finding
    assert victim.read_text() == "A" * 560
    # and nothing was registered (no manifest write happened)
    assert sf.read_manifest() == {"uploads": []}


def test_register_upload_recovers_stale_regular_tmp(tmp_path):
    """A crash between open and replace leaves a REGULAR tmp file behind:
    that is recoverable (unlink + retry), unlike a planted link."""
    sf = _sfiles(tmp_path)
    stale = sf.session_dir / "files.json.tmp"
    stale.write_text("garbage from a crash")

    sf.register_upload(_upload())

    assert not stale.exists()  # consumed by the atomic replace
    man = sf.read_manifest()
    assert [u["name"] for u in man["uploads"]] == ["report.csv"]
    assert sf.manifest_path.is_file() and not sf.manifest_path.is_symlink()


def test_register_upload_upsert_still_works(tmp_path):
    sf = _sfiles(tmp_path)
    sf.register_upload(_upload("a.csv"))
    sf.register_upload(_upload("a.csv"))  # same name: replace, not append
    sf.register_upload(_upload("b.csv"))
    names = [u["name"] for u in sf.read_manifest()["uploads"]]
    assert names == ["a.csv", "b.csv"]


def test_register_upload_serializes_concurrent_writers(tmp_path, monkeypatch):
    """Every request builds a FRESH SessionFiles: two concurrent uploads
    to the same session must still serialize the read-modify-write on
    the process-wide lock, or the last write loses the earlier upload
    (it lands in the inbox but never in the manifest — invisible to the
    UI)."""
    sf_a = _sfiles(tmp_path)
    sf_b = _sfiles(tmp_path)  # the second request's fresh instance

    inside = 0
    max_inside = 0
    probe = threading.Lock()
    orig_read = SessionFiles.read_manifest

    def slow_read(self):
        nonlocal inside, max_inside
        with probe:
            inside += 1
            max_inside = max(max_inside, inside)
        time.sleep(0.05)  # widen the window: unserialized writers overlap
        with probe:
            inside -= 1
        return orig_read(self)

    monkeypatch.setattr(SessionFiles, "read_manifest", slow_read)

    threads = [
        threading.Thread(target=sf_a.register_upload, args=(_upload("a.csv"),)),
        threading.Thread(target=sf_b.register_upload, args=(_upload("b.csv"),)),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    man = orig_read(sf_a)
    assert [u["name"] for u in man["uploads"]] == ["a.csv", "b.csv"]  # no lost update
    assert max_inside == 1  # the read-modify-write was serialized


def test_symlinked_manifest_is_never_followed_on_read(tmp_path):
    """A symlinked files.json could forge the upload list served to the
    human (and make root read a root-readable target): reads must not
    follow it, and the next upload self-heals the name atomically."""
    sf = _sfiles(tmp_path)
    forged = tmp_path / "forged.json"
    forged.write_text('{"uploads": [{"name": "stolen.env"}]}')
    sf.manifest_path.symlink_to(forged)

    assert sf.read_manifest() == {"uploads": []}  # not the forged list

    # self-heal: os.replace swaps the link itself, target untouched
    sf.register_upload(_upload())
    assert sf.manifest_path.is_file() and not sf.manifest_path.is_symlink()
    assert forged.read_text() == '{"uploads": [{"name": "stolen.env"}]}'
    assert [u["name"] for u in sf.read_manifest()["uploads"]] == ["report.csv"]


# ---------------------------------------------------------------------- #
# _chown_tree / _assert_real_dir: never follow a link, refuse fake dirs
# ---------------------------------------------------------------------- #


def test_chown_tree_never_follows_links(tmp_path, monkeypatch):
    calls = {"chown": [], "lchown": []}
    monkeypatch.setattr(os, "chown", lambda p, *a: calls["chown"].append(str(p)))
    monkeypatch.setattr(os, "lchown", lambda p, *a: calls["lchown"].append(str(p)))

    tree = tmp_path / "scratch-s1"
    (tree / "upper").mkdir(parents=True)
    (tree / "upper" / "real.txt").write_text("x")
    victim_file = tmp_path / "etc" / "servitor"  # stand-in root file
    victim_file.parent.mkdir(parents=True)
    victim_file.write_text("SECRET")
    (tree / "upper" / "evil-file").symlink_to(victim_file)
    victim_dir = tmp_path / "opt-servitor-app"
    victim_dir.mkdir()
    (tree / "upper" / "evil-dir").symlink_to(victim_dir)

    M._chown_tree(str(tree), 9001, 9001)

    # only the TOP (a verified real dir) goes through plain chown
    assert calls["chown"] == [str(tree)]
    # every entry — links included — via lchown (the link itself, never
    # the target); os.walk order is fs-dependent, compare as a set
    assert set(calls["lchown"]) == {
        str(tree / "upper"),
        str(tree / "upper" / "evil-dir"),
        str(tree / "upper" / "evil-file"),
        str(tree / "upper" / "real.txt"),
    }
    # the targets were never chown'd
    for t in (str(victim_file), str(victim_dir)):
        assert t not in calls["chown"] and t not in calls["lchown"]


def test_assert_real_dir_refuses_symlink_and_missing(tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    M._assert_real_dir(str(real))  # passes

    link = tmp_path / "link"
    link.symlink_to(real)
    with pytest.raises(MountError):
        M._assert_real_dir(str(link))

    with pytest.raises(MountError):
        M._assert_real_dir(str(tmp_path / "gone"))


def test_mount_session_refuses_symlinked_scratch_child(tmp_path, monkeypatch):
    """The re-mount-after-reboot trigger: the agent replaces scratch/<s>/
    upper with a link to a root-owned dir; mount_session must fail closed
    BEFORE any chown or mount touches it."""
    from app.iso.layout import SessionMountPaths, state_paths

    sp = SessionMountPaths.for_session("s1", str(tmp_path / "scratch"))
    state = state_paths(str(tmp_path / "state"))
    lower = tmp_path / "config" / "v1"
    lower.mkdir(parents=True)
    (tmp_path / "scratch").mkdir()
    (tmp_path / "state" / "inbox").mkdir(parents=True)
    (tmp_path / "state" / "sessions").mkdir(parents=True)
    M.ensure_session_dirs(sp, state)

    # the plant (what the slot does while live; the state disk survives a
    # reboot, the mounts don't)
    target = tmp_path / "etc-servitor"
    target.mkdir()
    os.rmdir(sp.upper)  # empty: ensure_session_dirs just made it
    os.symlink(target, sp.upper)

    # record: no chown of the target may happen, and no mount may run
    chowns = []
    monkeypatch.setattr(os, "chown", lambda p, *a: chowns.append(str(p)))
    monkeypatch.setattr(os, "lchown", lambda p, *a: chowns.append("l:" + str(p)))
    mounted = []
    monkeypatch.setattr(
        M.MountOp, "mount", lambda self: mounted.append(self.target)
    )

    with pytest.raises(MountError):
        M.mount_session(sp, str(lower), state, 9001, 9001, toolkit_dir=str(lower))

    assert not mounted  # the tamper gate fired before any mount
    assert str(target) not in chowns
    assert not any(str(target) in c for c in chowns)


# ---------------------------------------------------------------------- #
# outbox/upload path guards: path-wise containment, not string prefix
# ---------------------------------------------------------------------- #


def test_outbox_path_blocks_sibling_prefix_escape(tmp_path):
    """The old startswith check let /base/outbox2/x pass (string prefix of
    /base/outbox): a link in the outbox resolving into the agent's own
    sibling dir must be refused — the containment is path-wise."""
    sf = _sfiles(tmp_path)
    sf.outbox.mkdir(parents=True)
    sibling = tmp_path / "scratch" / "s1" / "workdir" / "outbox2"
    sibling.mkdir()
    (sibling / "x.txt").write_text("x")
    (sf.outbox / "link").symlink_to(sibling / "x.txt")

    with pytest.raises(FileNotDelivered):
        sf.outbox_path("link")
    # legitimate in-outbox files still resolve
    (sf.outbox / "ok.txt").write_text("ok")
    assert sf.outbox_path("ok.txt") == (sf.outbox / "ok.txt").resolve()


def test_upload_path_blocks_symlink_escape(tmp_path):
    sf = _sfiles(tmp_path)
    secret = tmp_path / "etc" / "hostname"
    secret.parent.mkdir(parents=True)
    secret.write_text("secret-host")
    (sf.inbox / "esc").symlink_to(secret)

    with pytest.raises(FileNotDelivered):
        sf.upload_path("esc")
    assert secret.read_text() == "secret-host"


# ---------------------------------------------------------------------- #
# same class, other routes: transcript reads, outbox listing,
# and the start abort-guard
# ---------------------------------------------------------------------- #


def test_read_entries_never_follows_symlink(tmp_path):
    """The session dir is slot-owned: a planted link at the transcript
    name would make the root app read a root-readable target and serve it
    in the transcript UI. Reads must not follow it."""
    from app.api.transcript import read_entries

    real = tmp_path / "transcript.jsonl"
    real.write_text('{"id": "a", "parentId": null}\n')
    assert len(read_entries(str(real))) == 1

    secret = tmp_path / "secret.jsonl"
    secret.write_text('{"id": "s", "parentId": null}\n')
    link = tmp_path / "sessions" / "link.jsonl"
    link.parent.mkdir()
    link.symlink_to(secret)
    assert read_entries(str(link)) == []
    assert secret.read_text() == '{"id": "s", "parentId": null}\n'


def test_transcript_unsettled_refuses_symlink(tmp_path):
    """Boot reconciliation reads the transcript as root: a planted link is
    judged UNSETTLED (the session gets marked interrupted and lands in
    the operator's view), never followed."""
    from app.lifecycle import transcript_unsettled

    secret = tmp_path / "secret.jsonl"
    secret.write_text("")
    link = tmp_path / "link.jsonl"
    link.symlink_to(secret)
    assert transcript_unsettled(str(link)) is True

    # a real, settled transcript: last entry is a non-empty assistant msg
    real = tmp_path / "real.jsonl"
    real.write_text(
        '{"id": "m1", "parentId": null, "type": "message", '
        '"message": {"role": "assistant", '
        '"content": [{"type": "text", "text": "done"}]}}\n'
    )
    assert transcript_unsettled(str(real)) is False


def test_list_outbox_skips_planted_links(tmp_path):
    """Listing sniffs + hashes as root: a planted link in the slot-owned
    outbox must not be root-read or advertised in the listing."""
    sf = _sfiles(tmp_path)
    sf.outbox.mkdir(parents=True)
    (sf.outbox / "artifact.txt").write_text("x")
    secret = tmp_path / "root-secret"
    secret.write_text("SECRET" * 100)
    (sf.outbox / "evil").symlink_to(secret)

    paths = {e["path"] for e in sf.list_outbox()}
    assert paths == {"artifact.txt"}
    assert secret.read_text() == "SECRET" * 100


async def test_start_failure_releases_slot_and_row(tmp_path, monkeypatch):
    """A failure at ANY start step (config missing, dir creation, mount —
    the tamper gate included — spawn) must release the slot and delete
    the row: a leaked ALLOCATED row pins its slot for the life of the
    process (boot reconciliation only runs at boot)."""
    from types import SimpleNamespace

    from app.lifecycle import Lifecycle, LifecycleConfig, PiInstall
    from app.iso.layout import state_paths
    from app.iso.slots import SlotPool
    from app.registry import Registry

    monkeypatch.setattr(
        "app.iso.slots.pwd.getpwnam",
        lambda name: SimpleNamespace(
            pw_uid=9001, pw_gid=9001, pw_dir=f"/home/{name}"
        ),
    )
    state = state_paths(str(tmp_path / "state"))
    for area in (state.config, state.lib, state.inbox, state.scratch, state.sessions):
        Path(area).mkdir(parents=True, exist_ok=True)
    Path(state.config, "v1").mkdir()
    os.symlink("v1", str(Path(state.config) / "active"))

    pool = SlotPool(("slot1",))
    reg = Registry(tmp_path / "registry.db")
    cfg = LifecycleConfig(
        state=state,
        pool=pool,
        registry=reg,
        pi=PiInstall(node_bin="/usr/bin/node", cli_js="/opt/pi/cli.js", ext_path=None),
        api_key="k",
    )
    lc = Lifecycle(cfg)

    monkeypatch.setattr(M, "ensure_session_dirs", lambda sp, st: None)

    def boom(*a, **k):
        raise M.MountError("tamper gate (simulated)")

    monkeypatch.setattr(M, "mount_session", boom)

    with pytest.raises(M.MountError):
        await lc.start_session()

    # no leak: slot back to free, row gone, no live entry
    assert lc.live == {}
    assert pool.get("slot1").state == "free"
    assert pool.get("slot1").session_id is None
    assert reg.list_sessions() == []
