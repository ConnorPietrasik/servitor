"""Conformance: pi-web-access loads via explicit -e with ZERO registry
contact.

Method: a network namespace with ONLY loopback. The mock router is on
loopback (it works); the npm registry is not (it cannot). If pi or the
extension attempts ANY registry contact at load/startup — an unpinned
re-check, a fetch, a telemetry call — the run fails in the netns and the
test fails.

Root required (unshare -n); loud-skip otherwise. The inner script runs
the same harness code path in the netns (spawn, get_state, prompt,
settle, tools-array assert) and is the single source of the assertion.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from conformance.harness import require_root_or_skip

HERE = Path(__file__).resolve().parent


def test_zero_registry_contact_in_netns(pi_install):
    require_root_or_skip("netns zero-registry test (unshare -n needs root)")
    inner = HERE / "netns_inner.py"
    env = {
        "SERVITOR_TEST_ROOT": "/srv/servitor-test",
        "SERVITOR_PI_VERSION": "1.5.2",
        "SERVITOR_PI_WEB_ACCESS_VERSION": "0.48.0",
        "SERVITOR_NODE_BIN": pi_install.node_bin,
        "PI_PREFIX": str(pi_install.prefix),
        "EXT_PREFIX": str(pi_install.ext_prefix),
    }
    cmd = [
        "unshare", "-n", "--",
        "bash", "-c",
        # a fresh netns has loopback DOWN: bring it up (the mock router is
        # loopback-only — which is the point: the registry is unreachable,
        # the mock is not)
        'ip link set lo up && exec "$0" "$@"',
        sys.executable, str(inner),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, env=env, timeout=180)
    assert proc.returncode == 0, (
        "zero-registry run FAILED in the netns (registry contact or broken "
        f"loopback path):\nSTDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}"
    )
    assert "ZERO-REGISTRY-OK" in proc.stdout, proc.stdout
