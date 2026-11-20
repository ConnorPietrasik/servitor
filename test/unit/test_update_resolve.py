"""deploy/resolve_target.py — the update's float rule (pure, no network).

Pins the rule against a synthetic registry — and its ARGV contract,
which is the rule's only input path: the natural
`npm view … --json | python3 - <<EOF` form cannot feed a piped payload
to a heredoc program (the heredoc wins the stdin fight — the pipe is
discarded — and the float would silently degrade to exact-pin), so
deploy/update.sh feeds the versions list as an argument. A list is
what `npm view <pkg> versions --json` emits.

The rule itself, pinned against a synthetic registry:

- the declaration is a FLOOR — floats to the newest stable in the same
  major, never down;
- the 0.x extension floats across minors (a real 1.0.0 is a major);
- out-of-major releases are REPORTED (target < latest-overall), never
  applied;
- an empty or unparseable registry is NO MOVEMENT, not a failure;
- the .env floor-line preflight (--exact) accepts only a plain X.Y.Z:
  ranges, tags and prerelease markers are rejected (installed == env
  is doctor-asserted).
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
RESOLVER = REPO / "deploy" / "resolve_target.py"


def run(declared: str, raw_versions: str) -> list[str]:
    r = subprocess.run(
        [sys.executable, str(RESOLVER), declared, raw_versions],
        capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    return r.stdout.split()


def test_floats_to_newest_release_in_major():
    assert run("1.4.0", json.dumps(["1.4.0", "1.5.0", "1.5.1", "1.5.2"])) == ["1.5.2", "1.5.2"]


def test_out_of_major_reported_not_applied():
    assert run("1.5.2", json.dumps(["1.5.2", "2.0.0"])) == ["1.5.2", "2.0.0"]


def test_zero_major_floats_across_minors_but_not_to_one_dot_0():
    assert run("0.40.0", json.dumps(["0.40.0", "0.47.0", "0.48.0"])) == ["0.48.0", "0.48.0"]
    assert run("0.47.0", json.dumps(["0.47.0", "0.48.0", "1.0.0"])) == ["0.48.0", "1.0.0"]


def test_floor_never_ceiling_declared_stays_when_current():
    assert run("1.5.2", json.dumps(["1.5.0", "1.5.2"])) == ["1.5.2", "1.5.2"]


def test_prerelease_and_build_versions_excluded():
    assert run("1.5.2", json.dumps(["1.5.2", "1.9.0-rc.1", "1.8.0+build5"])) == ["1.5.2", "1.5.2"]


def test_numeric_not_lexicographic_compare():
    assert run("1.9.0", json.dumps(["1.9.0", "1.10.0"])) == ["1.10.0", "1.10.0"]


def exact(version: str) -> int:
    r = subprocess.run(
        [sys.executable, str(RESOLVER), "--exact", version],
        capture_output=True, text=True)
    return r.returncode


def test_exact_version_accepted():
    assert exact("1.5.2") == 0
    assert exact("0.47.0") == 0
    assert exact("10.20.30") == 0
    assert exact(" 1.5.2 ") == 0  # .env whitespace is stripped


def test_range_and_tag_specs_rejected():
    # the regression guard for the typo-class failure: a range line would
    # float the .env to the range string and fail red at the guest's
    # installed==env assert — the preflight must stop it before that
    for v in ("^1.5.0", "~1.5.0", "1.5.x", "1.5", "1.5.2.3", "latest",
              ">=1.5.0", "1.5.2-rc.1", "1.5.2+build5", "v1.5.2", ""):
        assert exact(v) == 1, f"{v!r} must be rejected"


def test_declared_not_in_registry_still_floats():
    assert run("1.0.0", json.dumps(["1.5.2"])) == ["1.5.2", "1.5.2"]


def test_empty_or_garbage_registry_is_no_movement():
    # these must all equal "declared declared" — a network failure is
    # NO MOVEMENT (the deployed world is left alone), never a failed
    # update, and never a MAJOR AVAILABLE report.
    assert run("1.5.2", "") == ["1.5.2", "1.5.2"]
    assert run("1.5.2", json.dumps([])) == ["1.5.2", "1.5.2"]
    assert run("1.5.2", "not json") == ["1.5.2", "1.5.2"]
    assert run("1.5.2", json.dumps({"versions": ["1.5.2"]})) == ["1.5.2", "1.5.2"]
