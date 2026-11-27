#!/usr/bin/env python3
"""Python lint gate: pyflakes over app/, test/, deploy/ (venv python).

The design contract: the tree carries no unused imports, no undefined
names, and no dead local assignments — a flake is a broken gate, not a
style preference. ``check_contract.py`` asserts the interface contracts;
this asserts the hygiene floor, and both ride ``make check``.

Usage: python test/lint_python.py   (venv python — pyflakes is a
declared dev dependency)
"""
from __future__ import annotations

import os
from pathlib import Path

from pyflakes import api

# anchor to the repo root, not the cwd: the gate checks the repo's own
# tree however it is invoked, and fails loud on a missing top dir —
# a gate that walks nothing must never go green
REPO = Path(__file__).resolve().parent.parent
TOPS = ("app", "test", "deploy")
# compiled or vendored trees — foreign code that is never ours to lint
SKIP_DIRS = ("__pycache__", ".venv", "node_modules")


def _py_files() -> list[str]:
    out: list[str] = []
    for top in TOPS:
        for root, dirs, names in os.walk(REPO / top):
            dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
            for n in sorted(names):
                if n.endswith(".py"):
                    out.append(os.path.relpath(os.path.join(root, n), REPO))
    return sorted(out)


def main() -> int:
    for top in TOPS:
        if not (REPO / top).is_dir():
            print(f"python-lint: FAIL — {top}/ missing under {REPO}")
            return 1
    files = _py_files()
    total = 0
    for path in files:
        try:
            with open(REPO / path, "rb") as f:
                source = f.read().decode("utf-8")
        except (OSError, UnicodeDecodeError) as e:
            print(f"FAIL {path}: unreadable ({e})")
            return 1
        # the default reporter prints `path:line:col: message` per flake
        total += api.check(source, path)
    if total:
        print(f"python-lint: FAIL — {total} flake(s) in {len(files)} files")
        return 1
    print(f"python-lint: PASS ({len(files)} files, 0 flakes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
