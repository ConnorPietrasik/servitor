"""Unit: the launch line constant (CI asserts the exact argv).

The golden argv is the contract: any change to the launch line is a
deliberate, reviewed change (plus the sentinel conformance tests as a
standing regression, since an argv check alone would not catch a future
pi default change).
"""
from __future__ import annotations

import pytest

from app.rpc.launch import ENV_ALLOWLIST, FORBIDDEN_ARGV, build_launch


def _launch(**kw):
    base = dict(
        node_bin="/usr/bin/node",
        cli_js="/opt/servitor/pi/node_modules/@earendil-works/pi-coding-agent/dist/bundle/cli.js",
        session_dir="/var/lib/servitor/sessions/s1",
        extension_paths=("/opt/pi-ext/pi-web-access",),
        home="/home/slot1",
        user="slot1",
        agent_dir="/var/lib/servitor/scratch/s1/agent-dir",
        omnissiah_api_key="key-value",
        exa_api_key="exa-value",
        web_cache_root="/var/lib/servitor/scratch/s1/web-cache",
    )
    base.update(kw)
    return build_launch(**base)


def test_golden_argv_first_spawn():
    l = _launch(session_id="11111111-2222-3333-4444-555555555555")
    assert list(l.argv) == [
        "/usr/bin/node",
        "/opt/servitor/pi/node_modules/@earendil-works/pi-coding-agent/dist/bundle/cli.js",
        "--mode", "rpc",
        "--no-approve",
        "--no-context-files",
        "--session-dir", "/var/lib/servitor/sessions/s1",
        "--session-id", "11111111-2222-3333-4444-555555555555",
        "-e", "/opt/pi-ext/pi-web-access",
    ]


def test_golden_argv_respawn_uses_recorded_session_path():
    l = _launch(session_file="/var/lib/servitor/sessions/s1/1111.jsonl")
    tail = list(l.argv)[-4:]
    assert tail == ["--session", "/var/lib/servitor/sessions/s1/1111.jsonl", "-e", "/opt/pi-ext/pi-web-access"]
    assert "--session-id" not in l.argv


def test_exactly_one_session_mechanism():
    with pytest.raises(ValueError):
        _launch(session_id="abc", session_file="/x")
    with pytest.raises(ValueError):
        _launch()


def test_forbidden_flags_never_appear():
    l = _launch(session_id="abc")
    argv = " ".join(l.argv)
    for forbidden in FORBIDDEN_ARGV:
        assert forbidden not in argv, forbidden
    assert "--approve" not in argv  # not even as a substring of anything
    assert "PI_OFFLINE" not in l.env_dict
    assert "PI_CODING_AGENT_SESSION_DIR" not in l.env_dict


def test_env_is_the_closed_allowlist():
    l = _launch(session_id="abc")
    keys = list(l.env_dict.keys())
    assert keys == [k for k in ENV_ALLOWLIST]  # exact set AND order
    # the key is in the env — and nowhere in the argv
    assert l.env_dict["OMNISSIAH_API_KEY"] == "key-value"
    assert "key-value" not in " ".join(l.argv)
    assert "exa-value" not in " ".join(l.argv)
    # never inherit the app's environment: HOME/USER are explicit
    assert l.env_dict["HOME"] == "/home/slot1"
    assert l.env_dict["USER"] == "slot1"
    # $HOME follows the uid: the home matches the slot
    assert l.env_dict["HOME"].startswith("/home/slot")


def test_no_exa_key_still_valid_launch():
    l = _launch(session_id="abc", exa_api_key=None)
    assert "EXA_API_KEY" not in l.env_dict
    assert all(k in ENV_ALLOWLIST for k in l.env_dict)
