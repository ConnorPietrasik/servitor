"""``state.fs`` — the fs-type self-check, root-free unit tests.

The check decides on the SUPERBLOCK MAGIC ``stat -f %T`` reports, and
ext2/ext3/ext4 share one magic: an ext4 state volume reports the literal
``ext2/ext3``. That is the guest's real shape (INSTALL.md creates ext4), so
the check must neither FAIL nor warn on it — and it must not tell the
operator their ext4 volume is ext3 either: when the state root is its own
mount the report names what ``findmnt`` says. Overlay/tmpfs are decided on
the magic alone, so an unmounted dev fallback (no findmnt row) still reads
correctly.
"""
from __future__ import annotations

import pytest

from app import doctor
from app.doctor import FAIL, PASS, WARN, Report, check_state_fs


@pytest.fixture
def fake_sh(monkeypatch):
    """Route doctor's shell calls: ``stat -f %T`` answers ``stat_out``,
    ``findmnt --mountpoint`` answers ``findmnt_out`` ("" + rc 1 = no row,
    the unmounted fallback dir)."""

    def install(stat_out: str, findmnt_out: str = "", rc: int = 0) -> None:
        def _sh(argv):  # doctor._sh's own shape: one argv list
            if argv[:2] == ["stat", "-f"]:
                return 0, f"{stat_out}\n"
            if argv[0] == "findmnt":
                return (rc, f"{findmnt_out}\n") if findmnt_out else (1, "")
            raise AssertionError(f"unexpected shell call: {argv}")

        monkeypatch.setattr(doctor, "_sh", _sh)

    return install


def _run() -> tuple[str, str]:
    rep = Report()
    check_state_fs(rep, "/var/lib/servitor")
    assert len(rep.results) == 1
    return rep.results[0].status, rep.results[0].detail


def test_ext4_reports_ext4_not_the_magic_name(fake_sh):
    """The guest: ext4 (one shared magic => stat says "ext2/ext3") on its
    own mount. PASS, and the operator-visible name is the real type."""
    fake_sh("ext2/ext3", "ext4")
    status, detail = _run()
    assert status == PASS
    assert " is ext4 " in detail, detail
    assert "ext2/ext3" not in detail, detail


def test_unmounted_fallback_keeps_stat_name(fake_sh):
    """Laptop dev: a plain directory, no findmnt row. Still a PASS, named by
    the magic — the type is not the contract, local-and-non-overlay is."""
    fake_sh("ext2/ext3")
    status, detail = _run()
    assert status == PASS
    assert "ext2/ext3" in detail, detail


def test_other_local_filesystems_pass(fake_sh):
    for fstype in ("xfs", "btrfs", "ext4"):
        fake_sh(fstype, fstype)
        status, detail = _run()
        assert status == PASS, (fstype, detail)
        assert fstype in detail, detail


def test_overlay_fails_never_warns(fake_sh):
    """overlayfs upper-on-overlay is fatal for the session scratch: red, on
    the magic alone (the unmounted fallback sits on an overlay too)."""
    fake_sh("overlayfs")
    status, _ = _run()
    assert status == FAIL


def test_tmpfs_and_unknown_are_warnings(fake_sh):
    fake_sh("tmpfs", "tmpfs")
    assert _run()[0] == WARN
    fake_sh("fuse.sshfs")
    assert _run()[0] == WARN
