"""Conformance: strict sessions — read-only tools, no web, replacement
prompt (root: real pi + setuid spawn + overlay mounts).

Router-side gates: the captured request's `tools` array is authoritative
for the tool surface (test_tools_array.py's doctrine). The strict prompt
is the one thing where the leading system message IS the evidence, so it
is asserted directly off the wire (config-gate style).

Strict, per request:
- the tools array is EXACTLY {read, grep, find, ls} — no bash/edit/write,
  no pi-web-access tools, no codemode, no MCP;
- the leading system message is the replacement STRICT_SYSTEM.md (gate
  marker, no web claims of the base prompt);
- the executed argv carries --tools/--exclude-tools/--system-prompt and
  NO -e; the child env has no EXA/web-cache keys;
- a respawn re-runs the same strict line (create-time, registry-carried).
"""
from __future__ import annotations

MARKER = "[servitor-config v1]"
READ_ONLY = {"read", "grep", "find", "ls"}


def _tools_of(reqs) -> list[set[str]]:
    return [
        {t["function"]["name"] for t in r["body"].get("tools", [])} for r in reqs
    ]


def _leading_system_text(reqs) -> str:
    for r in reqs:
        msgs = r["body"].get("messages", [])
        if msgs and msgs[0].get("role") == "system":
            c = msgs[0].get("content")
            if isinstance(c, str):
                return c
            if isinstance(c, list):
                return "".join(b.get("text", "") for b in c if b.get("type") == "text")
    return ""


def _proc_environ(pid: int) -> dict[str, str]:
    out: dict[str, str] = {}
    with open(f"/proc/{pid}/environ", "rb") as f:
        for item in f.read().split(b"\0"):
            if b"=" in item:
                k, v = item.split(b"=", 1)
                out[k.decode("utf-8", "replace")] = v.decode("utf-8", "replace")
    return out


async def test_strict_tools_are_exactly_read_only(harness):
    live = await harness.start_session(strict=True)
    await harness.prompt_and_settle(live, "list the files you can see")
    reqs = harness.router.requests()
    assert reqs, "no request reached the router"
    for names in _tools_of(reqs):
        assert names == READ_ONLY, f"strict tool surface drifted: {names}"
    await harness.release(live)


async def test_strict_prompt_replaces_the_base_prompt(harness):
    live = await harness.start_session(strict=True)
    await harness.prompt_and_settle(live, "what can you do in this session?")
    sys_text = _leading_system_text(harness.router.requests())
    assert sys_text, "no leading system message on the wire"
    # the replacement prompt reached the model (config gate, strict half)
    assert MARKER in sys_text, "STRICT_SYSTEM.md did not reach the model"
    assert "STRICT" in sys_text
    # the base prompt's web/run claims are GONE (a hard replacement, not
    # an append the model must reconcile)
    assert "search the web" not in sys_text
    await harness.release(live)


async def test_strict_argv_and_child_env(harness):
    live = await harness.start_session(strict=True)
    pid = live.driver.pid
    assert pid is not None
    argv = list(live.driver.launched_argv)
    # the kernel ran the constant
    assert argv == list(live.driver.launch.argv)
    assert argv[argv.index("--tools") + 1] == "read,grep,find,ls"
    assert argv[argv.index("--exclude-tools") + 1] == "mcp__*"
    assert argv[argv.index("--system-prompt") + 1].endswith("/STRICT_SYSTEM.md")
    assert "-e" not in argv  # no search extension, ever, on a strict line
    assert "--no-approve" in argv and "--approve" not in argv
    # the closed env minus the web-extension keys
    env = _proc_environ(pid)
    assert set(env) == {
        "PATH", "HOME", "USER", "LANG", "PI_CODING_AGENT_DIR", "OMNISSIAH_API_KEY"
    }, f"strict child env drifted: {sorted(env)}"
    assert env["OMNISSIAH_API_KEY"] == harness.config_key
    assert harness.exa_key not in " ".join(argv)
    await harness.release(live)


async def test_respawn_keeps_the_strict_line(harness):
    live = await harness.start_session(strict=True)
    await harness.prompt_and_settle(live, "hello")
    live2 = await harness.respawn(live)
    argv = list(live2.driver.launched_argv)
    assert argv[argv.index("--tools") + 1] == "read,grep,find,ls"
    assert argv[argv.index("--system-prompt") + 1].endswith("/STRICT_SYSTEM.md")
    assert "-e" not in argv
    await harness.prompt_and_settle(live2, "still strict?")
    for names in _tools_of(harness.router.requests()):
        assert names == READ_ONLY, f"post-respawn tool surface drifted: {names}"
    await harness.release(live2)
