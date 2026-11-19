#!/usr/bin/env python3
"""The update's float rule (deploy/update.sh's resolve_target) — pure,
stdlib-only, unit-tested (test/unit/test_update_resolve.py).

The .env version line is a FLOOR: the target is the newest npm stable
release in the declared version's MAJOR (same major for everything —
the 0.x extension floats across minors, stable across them in
practice; a real 1.0.0 is a major and gets reported, not applied). A
newer out-of-major release is never applied: it shows up as
"<target> <latest-overall>" with target < latest-overall — the
update's MAJOR AVAILABLE report.

Usage: resolve_target.py <declared> <versions-json>
  versions-json: the output of `npm view <pkg> versions --json`
  (may be empty or unparseable — a network failure means NO MOVEMENT,
  never a failed update; the deployed world is left alone).
       resolve_target.py --exact <version>
  exit 0 iff <version> is an exact X.Y.Z (the .env floor-line contract);
  the update's preflight for the .env version lines (see below).

The data arrives via ARGV, never stdin: the natural
`npm view … --json | python3 - <<EOF` form cannot work — the heredoc
wins the stdin fight (stdin is the program, the pipe is discarded) and
the versions list would never arrive: a silent degrade to exact-pin on
every run. So deploy/update.sh feeds the npm JSON as an argument.

The .env line is an EXACT version (X.Y.Z) — is_exact() is the update's
preflight for it (deploy/update.sh refuses to run on anything else):
a range or tag ("^1.5.0", "1.5.x", "latest") would hit the float rule's
unparseable fail-safe (NO MOVEMENT), but its literal string still
differs from what is deployed, so the run would gate, float the .env
line to the garbage, and fail red at the guest's installed==env assert
—a full rollback cycle for a typo-class mistake. The preflight turns
that into a one-line error before anything moves.
"""
from __future__ import annotations

import json
import re
import sys


def vkey(v: str) -> tuple[int, ...]:
    """(1, 5, 2) for "1.5.2": numeric tuple compare (1.10.0 > 1.9.7,
    never lexicographic); a "+build" suffix is dropped."""
    return tuple(int(x) for x in v.split("+")[0].split(".")[:3])


def resolve(declared: str, versions: list[str]) -> tuple[str, str]:
    """Returns (target, latest-overall). target == latest-overall exactly
    when the newest release is in slot (or nothing exists)."""
    declared = declared.strip()
    try:
        # stable only: a prerelease marker is a "-" anywhere, a build
        # suffix is a "+"
        vers = [v for v in versions if "-" not in v and "+" not in v]
        in_slot = [v for v in vers if vkey(v)[0] == vkey(declared)[0]]
        # the declared floor is in the max: the target never goes DOWN
        target = max([declared] + in_slot, key=vkey)
        latest = max(vers, key=vkey) if vers else declared
    except ValueError:
        # a malformed declaration (or a registry entry we cannot parse):
        # no movement — same fail-safe as an empty list.
        target = latest = declared
    return target, latest


# the .env floor-line contract: an EXACT stable version, nothing else —
# ranges (^1.5.0, ~1.5.0, 1.5.x, latest), tags, and prerelease/build
# markers are not floors: the float rule walks stable releases only, so
# a non-exact line could never round-trip a deploy (the guest doctor
# asserts installed == env)
EXACT_VERSION_RE = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+$")


def is_exact(declared: str) -> bool:
    """True iff declared is a plain X.Y.Z version (the .env line's contract)."""
    return EXACT_VERSION_RE.match(declared.strip()) is not None


def main() -> None:
    if len(sys.argv) == 3 and sys.argv[1] == "--exact":
        v = sys.argv[2]
        if not is_exact(v):
            print(
                f"not an exact version (X.Y.Z): {v!r} — ranges, tags and "
                f"prerelease markers are not floors", file=sys.stderr)
        raise SystemExit(0 if is_exact(v) else 1)
    if len(sys.argv) != 3:
        print(
            f"usage: {sys.argv[0]} <declared> <versions-json> | "
            f"{sys.argv[0]} --exact <version>", file=sys.stderr)
        raise SystemExit(2)
    declared, raw = sys.argv[1], sys.argv[2]
    try:
        versions = json.loads(raw) if raw.strip() else []
        if not isinstance(versions, list):
            raise ValueError("versions is not a list")
    except ValueError:
        versions = []
    target, latest = resolve(declared, versions)
    print(f"{target} {latest}")


if __name__ == "__main__":
    main()
