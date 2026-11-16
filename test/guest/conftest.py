"""Stage-5 guest test setup: root only, session-scoped VM."""

from __future__ import annotations

import atexit
import os
import time

import pytest

from harness import GuestVM, active_image, ensure_bridge, remove_bridge, REPO


def require_root_or_skip() -> None:
    if os.geteuid() != 0:
        pytest.skip("stage-5 guest tests need root (qemu, bridge, mkfs, sshd)")


@pytest.fixture(autouse=True)
def _restore_repo_baseline(request):
    """After every test that used the shared guest, converge it back to the
    repo baseline.

    The session-scoped VM is shared by the whole suite, and several tests
    re-sync with a DIFFERENT config (the shape-change test's temp dir, the
    waiting drill's mock-router config, the fault drill's borked artifact,
    the extension bump's bumped selection) and leave ``config/active``
    pointed at it. The next test would then run the doctor / model probe /
    no-op update against a foreign or corrupted models.json (the suite-only
    ``JSONDecodeError: Extra data`` corruption was exactly this). An
    unconditional repo sync is the convergence: the config is
    content-addressed, so when the guest already matches the repo the sync
    is a deterministic NO-OP (nothing touched, no restart); when it does
    not, the sync re-stages the repo config and the app's tick applies it —
    and a shape-changing sync certifies itself with its own post-deploy
    doctor, so the suite ends every test on a certified baseline."""
    yield
    if "guest" not in request.fixturenames:
        return
    vm = request.getfixturevalue("guest")
    r = vm.sync()
    assert r.returncode == 0, (
        f"post-test baseline restore sync failed (rc={r.returncode}):\n"
        f"{r.stdout[-2000:]}\n{r.stderr[-1000:]}")


@pytest.fixture(scope="session")
def guest(tmp_path_factory):
    """Boot the ACTIVE manifest image, PROVISION it (first sync), stop at
    session end.

    The provisioning sync is part of fresh-guest certification: the image
    ships no keys and no extension by design, and the app boot-stages its
    config as v1 — the first sync from the current repo injects the key
    slice, installs the extension at the .env selection, and re-stages the
    config under the content-addressed name. Exactly one restart; after
    it, an unchanged sync is a deterministic NO-OP (asserted by the gate
    that runs later)."""
    require_root_or_skip()
    ensure_bridge()  # br=servitor-edge must exist before QEMU starts; idempotent
    name, entry = active_image()
    work = tmp_path_factory.mktemp(f"guest-{time.strftime('%H%M%S')}")
    vm = GuestVM(REPO / "deploy" / "guest" / "images" / name, work)
    try:
        vm.start()
        vm.wait_ssh(timeout=240)
        vm.wait_healthz(timeout=240)
        r = vm.sync()
        if r.returncode != 0:
            pytest.fail(f"provisioning sync failed (rc={r.returncode}):\n"
                        f"{r.stdout[-3000:]}\n{r.stderr[-1000:]}")
        vm.wait_healthz(timeout=240)
        yield vm
    finally:
        # always tear down, even if setup failed (no orphaned QEMU/bridge)
        vm.stop()
        remove_bridge()
