"""Conformance: the declared tool set (codemode off).

Gates assert ROUTER-SIDE: the captured request's `tools` array is
authoritative (the system prompt string is a red herring — it mentions
"codemode" in its docs index even when the tool is gone).

v1 declared set: the four pi-web-access tools present
(`web_search`, `source_check`, `fetch_content`, `get_search_content`) +
the four default tools (`read`, `bash`, `edit`, `write`), and NO
`codemode`. Re-enabling codemode flips this assertion deliberately.
"""
from __future__ import annotations

WEB_TOOLS = {"web_search", "source_check", "fetch_content", "get_search_content"}
DEFAULT_TOOLS = {"read", "bash", "edit", "write"}


async def test_tools_array_is_the_declared_set(harness):
    live = await harness.start_session()
    await harness.prompt_and_settle(live, "which tools do you have?")
    reqs = harness.router.requests()
    assert reqs, "no request reached the router"
    tool_names = {t["function"]["name"] for t in reqs[0]["body"].get("tools", [])}
    assert WEB_TOOLS <= tool_names, f"missing pi-web-access tools: {WEB_TOOLS - tool_names}"
    assert DEFAULT_TOOLS <= tool_names, f"missing default tools: {DEFAULT_TOOLS - tool_names}"
    assert "codemode" not in tool_names, "codemode must be OFF"
    await harness.release(live)


async def test_web_tools_present_on_every_call(harness):
    """The extension is loaded per spawn: every model call in a session
    (including the post-tool-call one) declares the same set."""
    live = await harness.start_session()
    await harness.prompt_and_settle(live, "run the mock tool")
    reqs = harness.router.requests()
    assert len(reqs) >= 2  # run -> tool call -> tool result -> final call
    for r in reqs:
        names = {t["function"]["name"] for t in r["body"].get("tools", [])}
        assert WEB_TOOLS <= names
        assert "codemode" not in names
    await harness.release(live)
