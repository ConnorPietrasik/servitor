"""Conformance: pi-web-access key plumbing (verified against the
pinned 0.48.0 dist, recorded here as a standing conformance fact).

The 0.48.0 dist: config lookup is agent-dir-based
(`getWebSearchConfigDir()` returns PI_CODING_AGENT_DIR when set — which
servitor always sets — before the XDG/homedir fallbacks), and the Exa
credential resolves ENV-FIRST (`EXA_API_KEY` beats any web-search.json
literal). So:

- the key rides the spawn env allowlist (like OMNISSIAH_API_KEY);
- servitor NEVER writes a key file: no web-search.json exists anywhere in
  the session's trees after a run;
- nothing credential-shaped enters the synced config tree or a session
  file.

(The LIVE keyed search — a real Exa round trip — is the doctor
gate, which has the real key; here we assert the plumbing statically.)
"""
from __future__ import annotations

import os

from app.rpc.launch import ENV_ALLOWLIST


async def test_exa_key_is_env_first_and_no_key_file_written(harness):
    live = await harness.start_session()
    # the env allowlist carried the key to the child
    env = dict(live.driver.launch.env)
    assert env.get("EXA_API_KEY") == harness.exa_key
    assert "EXA_API_KEY" in ENV_ALLOWLIST
    await harness.prompt_and_settle(live, "key plumbing probe")
    # no web-search.json anywhere in the session's trees (agent dir,
    # upper, sessions, workdir upper, scratch root)
    roots = [
        live.sp.agent_dir,
        live.sp.agent_upper,
        live.sp.upper,
        live.sp.scratch,
        os.path.join(harness.state.sessions, live.session_id),
    ]
    for root in roots:
        for dirpath, dirnames, filenames in os.walk(root):
            for name in filenames:
                assert name != "web-search.json", f"key file written: {os.path.join(dirpath, name)}"
                p = os.path.join(dirpath, name)
                try:
                    with open(p, "rb") as f:
                        blob = f.read(2_000_000)
                    assert harness.exa_key.encode() not in blob, p
                except OSError:
                    continue
    # ...and the synced config tree itself (the lower) is key-free
    for dirpath, dirnames, filenames in os.walk(live.config_lower):
        for name in filenames:
            with open(os.path.join(dirpath, name), "rb") as f:
                assert harness.exa_key.encode() not in f.read()
    await harness.release(live)
