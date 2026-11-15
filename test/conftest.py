"""Top-level test fixtures (PLAN §8).

Laptop dev-loop layout (root required for the stack suites, DECISIONS D2):

- ``/srv/servitor-test/`` — shared, root-owned 0755 scratch ROOT: the
  resolved pi/extension installs (session-scoped, reused) and per-test run
  dirs. On the root filesystem (a real fs, never overlay — T29 is asserted
  on the state volume itself).
- ``/home/<slot>/servitor-test/<run>/state`` — the per-test STATE ROOT,
  under the slot's own home (ext4 on the laptop): the slot uid trivially
  traverses its own home, root (the app) writes it, and ext4 supports the
  chattr +i sweep-failure injection the quarantine test needs.

Slot allocation across suites (no parallel suites in the plan; this keeps
them disjoint if ever run concurrently):
  conformance -> slot1 · sandbox-smoke -> slot2 · vm-smoke -> slot3 (+slot4)
"""
from __future__ import annotations

import os
import shutil
import sys
import uuid
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))
TEST_DIR = REPO / "test"
if str(TEST_DIR) not in sys.path:
    sys.path.insert(0, str(TEST_DIR))

TEST_ROOT = Path(os.environ.get("SERVITOR_TEST_ROOT", "/srv/servitor-test"))


def is_root() -> bool:
    return hasattr(os, "geteuid") and os.geteuid() == 0


def require_root_or_skip(what: str) -> None:
    """LOUD skip: the suite names what it could not run. Never silently
    green where root was required (PLAN §8)."""
    if not is_root():
        pytest.skip(f"LOUD-SKIP: {what} requires root (run: sudo make <target>)")


@pytest.fixture(scope="session")
def test_root() -> Path:
    """Shared test root (/srv/servitor-test). Created if missing; cleaned
    of stale run dirs (older artifacts are ours to reap — they are test
    scratch, never user data)."""
    if not is_root():
        require_root_or_skip("shared test root")
    TEST_ROOT.mkdir(mode=0o755, exist_ok=True)
    for stale in TEST_ROOT.glob("run-*"):
        shutil.rmtree(stale, ignore_errors=True)
    yield TEST_ROOT


@pytest.fixture
def run_root(test_root) -> Path:
    """A per-test run dir under the shared root (0755: the slot uid must
    traverse the install/state trees)."""
    root = test_root / f"run-{uuid.uuid4().hex[:12]}"
    root.mkdir(mode=0o755)
    yield root
    shutil.rmtree(root, ignore_errors=True)


@pytest.fixture(scope="session")
def pi_install(test_root):
    """The resolved pi + pi-web-access in the shared install prefix
    (session-scoped: npm installs are slow; the resolved versions come
    from the env selections, PLAN §8 stage 1, D6)."""
    require_root_or_skip("pi install prefix")
    from conformance.install import resolve_pi

    return resolve_pi(
        test_root / "pi-install",
        os.environ.get("SERVITOR_PI_VERSION"),
        os.environ.get("SERVITOR_PI_WEB_ACCESS_VERSION"),
        node_bin=os.environ.get("SERVITOR_NODE_BIN", "/usr/bin/node"),
    )
