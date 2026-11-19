"""Version-parameterized pi/extension installer.

The conformance suite takes a pi version + extension selection (npm semver
specs, from the env — defaults to the .env selections) and installs the
resolved versions into a scratch prefix with ``npm install
--ignore-scripts``. The resolved version is read back from the installed
package.json (the registry records installed versions). This is also
the host-side preflight of ``deploy/update.sh`` (the update gate): the
guest is not touched until it passes.

Reused across tests at the session scope — installs are slow; the prefix
is read-only input to every test.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

PI_PACKAGE = "@earendil-works/pi-coding-agent"


class InstallError(RuntimeError):
    pass


def _npm_install(prefix: Path, spec: str) -> None:
    prefix.mkdir(parents=True, exist_ok=True)
    cmd = [
        "npm",
        "install",
        "--ignore-scripts",
        "--no-audit",
        "--no-fund",
        f"--prefix={prefix}",
        spec,
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise InstallError(f"npm install {spec} failed:\n{proc.stdout}\n{proc.stderr}")


def _installed_version(prefix: Path, pkg: str) -> str:
    pj = prefix / "node_modules" / pkg / "package.json"
    if not pj.exists():
        raise InstallError(f"installed package missing: {pj}")
    return json.loads(pj.read_text())["version"]


def make_readable_by_slot(path: Path) -> None:
    """npm/mkdtemp artifacts are root-owned under the test scratch; the slot
    uid (the agent) must be able to TRAVERSE and READ the install tree.
    mkdtemp's 0700 and umask quirks are fixed here (the guest image does
    not need this — the slot owns its scratch there)."""
    try:
        path.chmod(0o755)
    except OSError:
        pass


@dataclass(frozen=True)
class PiInstall:
    """The resolved, installed pi (and optional extension) in a scratch prefix."""

    prefix: Path
    requested: str
    version: str  # read back from the installed package.json
    cli_js: Path
    node_bin: str
    ext_requested: str | None = None
    ext_version: str | None = None
    ext_path: Path | None = None  # the -e path (package dir)
    ext_prefix: Path | None = None  # the npm prefix holding the extension


def resolve_pi(
    scratch: Path,
    pi_version: str | None = None,
    ext_version: str | None = None,
    node_bin: str | None = None,
) -> PiInstall:
    """Install pi (+ pi-web-access when ext_version is given) into
    ``scratch``. Idempotent: a matching resolved version is reused."""
    pi_version = pi_version or os.environ.get("SERVITOR_PI_VERSION", "1.5.2")
    ext_version = ext_version if ext_version is not None else os.environ.get(
        "SERVITOR_PI_WEB_ACCESS_VERSION", "0.47.0"
    )
    node_bin = node_bin or os.environ.get("SERVITOR_NODE_BIN") or "node"

    pi_prefix = scratch / "pi"
    ext_prefix = scratch / "ext"

    # reuse when the resolved version already matches an exact spec
    if re.fullmatch(r"\d+\.\d+\.\d+", pi_version):
        try:
            have = _installed_version(pi_prefix, PI_PACKAGE)
            if have == pi_version:
                pi_prefix_ok = True
            else:
                pi_prefix_ok = False
        except InstallError:
            pi_prefix_ok = False
    else:
        pi_prefix_ok = False

    if not pi_prefix_ok:
        _npm_install(pi_prefix, f"{PI_PACKAGE}@{pi_version}")
    make_readable_by_slot(pi_prefix)
    version = _installed_version(pi_prefix, PI_PACKAGE)
    cli_js = (
        pi_prefix
        / "node_modules"
        / PI_PACKAGE
        / "dist"
        / "bundle"
        / "cli.js"
    )
    if not cli_js.exists():
        raise InstallError(f"pi CLI entry missing after install: {cli_js}")

    ext_requested, ext_version_resolved, ext_path = None, None, None
    if ext_version:
        ext_spec = f"pi-web-access@{ext_version}"
        ext_pkg_dir = ext_prefix / "node_modules" / "pi-web-access"
        reuse = False
        if re.fullmatch(r"\d+\.\d+\.\d+", ext_version):
            try:
                reuse = _installed_version(ext_prefix, "pi-web-access") == ext_version
            except InstallError:
                reuse = False
        if not reuse:
            _npm_install(ext_prefix, ext_spec)
        make_readable_by_slot(ext_prefix)
        ext_version_resolved = _installed_version(ext_prefix, "pi-web-access")
        ext_requested = ext_spec
        ext_path = ext_pkg_dir
        if not ext_path.exists():
            raise InstallError(f"extension missing after install: {ext_path}")

    return PiInstall(
        prefix=pi_prefix,
        requested=pi_version,
        version=version,
        cli_js=cli_js,
        node_bin=node_bin,
        ext_requested=ext_requested,
        ext_version=ext_version_resolved,
        ext_path=ext_path,
        ext_prefix=ext_prefix if ext_version else None,
    )
