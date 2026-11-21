"""Conformance: the launch line AS EXECUTED.

The unit suite asserts the golden argv from the builder; this asserts
what the kernel actually ran (and what environment the child actually
got) — catching drift between the constant and the spawn path.

Evidence source: the driver's SPAWN-TIME /proc/<pid>/cmdline snapshot
(launched_argv). Node rewrites its own process title to "pi" shortly
after boot, so a late /proc read is not valid launch-line evidence.

- launched_argv == the exact golden argv (node, absolute cli, --mode
  rpc, --no-approve, --no-context-files, --session-dir,
  --session-id/--session, -e extension).
- /proc/<pid>/environ == the closed env allowlist — the child NEVER
  inherits the app's environment (environ is not rewritten by node).
- The key is in the child env and NOWHERE on argv.
- Never --approve / --api-key / PI_OFFLINE / PI_CODING_AGENT_SESSION_DIR.
"""
from __future__ import annotations

import os


def _proc_environ(pid: int) -> dict[str, str]:
    out: dict[str, str] = {}
    with open(f"/proc/{pid}/environ", "rb") as f:
        for item in f.read().split(b"\0"):
            if b"=" in item:
                k, v = item.split(b"=", 1)
                out[k.decode("utf-8", "replace")] = v.decode("utf-8", "replace")
    return out


async def test_executed_argv_is_the_golden_launch_line(harness):
    live = await harness.start_session()
    pid = live.driver.pid
    assert pid is not None
    argv = list(live.driver.launched_argv)
    expected = list(live.driver.launch.argv)
    assert argv == expected, f"executed argv drifted from the constant:\n{argv}\nvs\n{expected}"
    # the trust flags, exactly as constant (invariant: --no-approve
    # always; --approve never)
    assert "--no-approve" in argv
    assert "--no-context-files" in argv
    assert "--mode" in argv and "rpc" in argv
    assert "--approve" not in argv and "--api-key" not in argv
    # the search extension by explicit path — the one surface the agent
    # cannot rewrite
    e_idx = argv.index("-e")
    assert argv[e_idx + 1].endswith("pi-web-access")
    # exactly ONE session mechanism
    assert ("--session-id" in argv) ^ ("--session" in argv)
    assert "--session-dir" in argv
    await harness.release(live)


async def test_executed_env_is_the_closed_allowlist(harness):
    live = await harness.start_session()
    pid = live.driver.pid
    assert pid is not None
    env = _proc_environ(pid)
    from app.rpc.launch import ENV_ALLOWLIST, FORBIDDEN_ENV

    assert set(env.keys()) == set(ENV_ALLOWLIST), (
        f"child env is not exactly the allowlist: extra={set(env) - set(ENV_ALLOWLIST)} "
        f"missing={set(ENV_ALLOWLIST) - set(env)}"
    )
    for forbidden in FORBIDDEN_ENV:
        assert forbidden not in env
    # the key rode env-only: present in the child env, absent from argv
    assert env["OMNISSIAH_API_KEY"] == harness.config_key
    argv = " ".join(live.driver.launched_argv)
    assert harness.config_key not in argv
    assert harness.exa_key not in argv
    assert "PATH" in env and harness.config_key not in env["PATH"]
    await harness.release(live)
