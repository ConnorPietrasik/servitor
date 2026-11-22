"""``os.machine_id`` — the guest-OS identity check, root-free unit tests.

The image build writes /etc/machine-id directly (the floating
systemd-machine-id-setup is not trusted with it), and the [6/7] artifact
gate certifies the image on the same shape: 32 lowercase hex chars, nothing
else. The doctor certifies the DEPLOYED system the same way — an invalid id
breaks logind and hangs a firstboot prompt on a serial console nobody
watches, and no dev-mode shape is exempt."""
from __future__ import annotations

from app.doctor import FAIL, PASS, Report, check_machine_id

VALID = "0123456789abcdef0123456789abcdef"


def _run(path) -> tuple[str, str]:
    rep = Report()
    check_machine_id(rep, str(path))
    assert len(rep.results) == 1
    return rep.results[0].status, rep.results[0].detail


def test_valid_id_passes(tmp_path):
    status, detail = _run(_write(tmp_path, VALID + "\n"))
    assert status == PASS
    assert "valid" in detail


def _write(tmp_path, content: str):
    p = tmp_path / "machine-id"
    p.write_text(content)
    return p


def test_uninitialized_fails_with_repair_hint(tmp_path):
    status, detail = _run(_write(tmp_path, "uninitialized\n"))
    assert status == FAIL
    assert "uninitialized" in detail, detail
    assert "reboot" in detail, detail


def test_empty_and_blank_fail(tmp_path):
    assert _run(_write(tmp_path, ""))[0] == FAIL
    assert _run(_write(tmp_path, "\n"))[0] == FAIL


def test_wrong_length_fails(tmp_path):
    assert _run(_write(tmp_path, VALID[:31] + "\n"))[0] == FAIL
    assert _run(_write(tmp_path, VALID + "0" + "\n"))[0] == FAIL


def test_uppercase_and_nonhex_fail(tmp_path):
    assert _run(_write(tmp_path, VALID.upper() + "\n"))[0] == FAIL
    assert _run(_write(tmp_path, VALID.replace("a", "g").replace("b", "h").replace("c", "i").replace("d", "j").replace("e", "k").replace("f", "l") + "\n"))[0] == FAIL


def test_missing_file_fails(tmp_path):
    status, detail = _run(tmp_path / "nope")
    assert status == FAIL
    assert "unreadable" in detail, detail
