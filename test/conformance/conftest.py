"""Conformance suite fixtures.

The suite is VERSION-PARAMETERIZED: it runs against the pi version +
extension selection resolved from the env (defaults: the .env
selections), installed into the shared prefix (test.conftest.pi_install).
This is the permanent conformance suite — it also serves as the
host-side preflight of `servitorctl update`.

Slot: slot1 (see test/conftest.py for the suite-slot allocation).
Root: required — loud-skip otherwise, never silently green.
"""
from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from conformance.harness import Harness, require_root_or_skip

SLOT = "slot1"


@pytest.fixture
def harness(run_root, pi_install):
    require_root_or_skip("conformance stack (real pi + setuid spawn + overlay mounts)")
    slot_home_test = Path(f"/home/{SLOT}/servitor-test")
    state_root = slot_home_test / run_root.name / "state"
    h = Harness(root=state_root, pi=pi_install, slot_name=SLOT)
    h.stage_config()
    yield h
    h.stop()
    # the session's areas are released by the tests; reap what remains
    # (crashed-test residue) — test scratch under the slot's home, never
    # user data
    shutil.rmtree(slot_home_test / run_root.name, ignore_errors=True)


@pytest.fixture
def fixture_ext(run_root) -> Path:
    """A minimal extension registering the command `/svtest` — used to hit
    the `handled` disposition (extension commands are consumed by prompt).
    Loaded via an EXTRA -e in the variant run; the production launch line
    (asserted separately) does not include it.

    The source is repo-tracked, but the RUNTIME copy must live outside
    /home/connor (the slot uid cannot traverse the owner's home): the run
    root under /srv is slot-traversable.
    """
    src = Path(__file__).parent / "fixture-ext.ts"
    dst = run_root / "fixture-ext.ts"
    dst.write_text(src.read_text())
    return dst
