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
    # the allowlist minus the optional gen surface (absent when not given):
    # exact set AND order
    assert list(l.env_dict.keys()) == [k for k in ENV_ALLOWLIST if k != "SERVITOR_GEN_BASE_URL"]
    # the key is in the env — and nowhere in the argv
    assert l.env_dict["OMNISSIAH_API_KEY"] == "key-value"
    assert "key-value" not in " ".join(l.argv)
    assert "exa-value" not in " ".join(l.argv)
    # never inherit the app's environment: HOME/USER are explicit
    assert l.env_dict["HOME"] == "/home/slot1"
    assert l.env_dict["USER"] == "slot1"
    # $HOME follows the uid: the home matches the slot
    assert l.env_dict["HOME"].startswith("/home/slot")


def test_gen_url_rides_env_nonstrict_only():
    url = "http://127.0.0.1:8010/machine/gen"
    l = _launch(session_id="abc", gen_base_url=url)
    assert l.env_dict.get("SERVITOR_GEN_BASE_URL") == url
    # env-only: never on the argv (and the argv is unchanged by it)
    assert url not in " ".join(l.argv)
    assert "SERVITOR_GEN_BASE_URL" not in " ".join(l.argv)


def test_gen_url_absent_when_not_given():
    l = _launch(session_id="abc")
    assert "SERVITOR_GEN_BASE_URL" not in l.env_dict
    assert all(k in ENV_ALLOWLIST for k in l.env_dict)


def test_strict_drops_the_gen_url():
    # a strict session has no bash — the gen CLI is unreachable, and the
    # surface stays closed: the URL never rides its env
    l = _strict_launch(session_id="abc", gen_base_url="http://127.0.0.1:8010/machine/gen")
    assert "SERVITOR_GEN_BASE_URL" not in l.env_dict
    assert "SERVITOR_GEN_BASE_URL" not in " ".join(l.argv)


def test_no_exa_key_still_valid_launch():
    l = _launch(session_id="abc", exa_api_key=None)
    assert "EXA_API_KEY" not in l.env_dict
    assert all(k in ENV_ALLOWLIST for k in l.env_dict)


# --------------------------------------------------------------------------- #
# strict: the read-only launch line (one constant surface, golden argv)       #
# --------------------------------------------------------------------------- #

STRICT_PATH = "/var/lib/servitor/config/v3/STRICT_SYSTEM.md"


def _strict_launch(**kw):
    base = dict(
        node_bin="/usr/bin/node",
        cli_js="/opt/servitor/pi/node_modules/@earendil-works/pi-coding-agent/dist/bundle/cli.js",
        session_dir="/var/lib/servitor/sessions/s1",
        extension_paths=(),  # a strict launch loads no extension
        home="/home/slot1",
        user="slot1",
        agent_dir="/var/lib/servitor/scratch/s1/agent-dir",
        omnissiah_api_key="key-value",
        exa_api_key="exa-value",  # present at the caller; the builder drops it
        web_cache_root="/var/lib/servitor/scratch/s1/web-cache",
        strict=True,
        strict_system_path=STRICT_PATH,
    )
    base.update(kw)
    return build_launch(**base)


def test_strict_golden_argv_first_spawn():
    l = _strict_launch(session_id="11111111-2222-3333-4444-555555555555")
    assert list(l.argv) == [
        "/usr/bin/node",
        "/opt/servitor/pi/node_modules/@earendil-works/pi-coding-agent/dist/bundle/cli.js",
        "--mode", "rpc",
        "--no-approve",
        "--no-context-files",
        "--session-dir", "/var/lib/servitor/sessions/s1",
        "--session-id", "11111111-2222-3333-4444-555555555555",
        "--tools", "read,grep,find,ls",
        "--exclude-tools", "mcp__*",
        "--system-prompt", STRICT_PATH,
    ]
    assert "-e" not in l.argv


def test_strict_golden_argv_respawn_uses_recorded_session_path():
    l = _strict_launch(session_file="/var/lib/servitor/sessions/s1/1111.jsonl")
    assert list(l.argv)[-8:] == [
        "--session", "/var/lib/servitor/sessions/s1/1111.jsonl",
        "--tools", "read,grep,find,ls",
        "--exclude-tools", "mcp__*",
        "--system-prompt", STRICT_PATH,
    ]
    assert "--session-id" not in l.argv
    assert "-e" not in l.argv


def test_strict_env_drops_web_keys_only():
    l = _strict_launch(session_id="abc")
    assert list(l.env_dict.keys()) == [
        "PATH", "HOME", "USER", "LANG",
        "PI_CODING_AGENT_DIR", "OMNISSIAH_API_KEY",
    ]
    # the model key still rides env-only; the web-extension keys are gone
    # (nothing loads them without the extension)
    assert l.env_dict["OMNISSIAH_API_KEY"] == "key-value"
    assert "EXA_API_KEY" not in l.env_dict
    assert "PI_WEB_ACCESS_CACHE_ROOT" not in l.env_dict
    assert "exa-value" not in " ".join(l.argv)


def test_strict_requires_the_system_prompt_path():
    with pytest.raises(ValueError):
        _strict_launch(session_id="abc", strict_system_path=None)
    with pytest.raises(ValueError):
        _strict_launch(session_id="abc", strict_system_path="/var/lib/servitor/config/v3/SYSTEM.md")


def test_non_strict_refuses_the_system_prompt_path():
    with pytest.raises(ValueError):
        _launch(session_id="abc", strict_system_path=STRICT_PATH)


def test_strict_refuses_extensions():
    # the fail-closed coupling: a strict line that loaded the search
    # extension would silently reopen web reach
    with pytest.raises(ValueError):
        _strict_launch(
            session_id="abc", extension_paths=("/opt/pi-ext/pi-web-access",)
        )
