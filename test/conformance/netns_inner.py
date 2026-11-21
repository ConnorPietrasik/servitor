"""Inner script for the netns zero-registry test.

Runs INSIDE a network namespace with only loopback. Uses the SAME
installed prefix as the outer suite (read-only, on disk — no network
needed) and asserts: the session spawns, the model resolves, a prompt
runs to settle THROUGH the loopback mock, and the tools array carries
the pi-web-access tools (the extension loaded from disk, not from the
registry). Any registry contact would fail here (no route to the outside).

Exit 0 + "ZERO-REGISTRY-OK" on success.
"""
from __future__ import annotations

import asyncio
import os
import shutil
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "test"))

from conformance.harness import Harness  # noqa: E402
from conformance.install import PiInstall  # noqa: E402


async def main() -> int:
    run_id = f"run-netns-{uuid.uuid4().hex[:10]}"
    run = Path("/srv/servitor-test") / run_id
    run.mkdir(parents=True, exist_ok=True)
    run.chmod(0o755)
    slot = "slot1"
    state_root = Path(f"/home/{slot}/servitor-test") / run_id / "state"
    try:
        pi = PiInstall(
            prefix=Path(os.environ["PI_PREFIX"]),
            requested=os.environ["SERVITOR_PI_VERSION"],
            version=os.environ["SERVITOR_PI_VERSION"],
            cli_js=Path(os.environ["PI_PREFIX"])
            / "node_modules"
            / "@earendil-works"
            / "pi-coding-agent"
            / "dist"
            / "bundle"
            / "cli.js",
            node_bin=os.environ["SERVITOR_PI_NODE_BIN"],
            ext_requested=f"pi-web-access@{os.environ['SERVITOR_PI_WEB_ACCESS_VERSION']}",
            ext_version=os.environ["SERVITOR_PI_WEB_ACCESS_VERSION"],
            ext_path=Path(os.environ["EXT_PREFIX"]) / "node_modules" / "pi-web-access",
            ext_prefix=Path(os.environ["EXT_PREFIX"]),
        )
        assert pi.cli_js.exists(), f"pi prefix unusable: {pi.cli_js}"
        assert pi.ext_path.exists(), f"ext prefix unusable: {pi.ext_path}"
        h = Harness(root=state_root, pi=pi, slot_name=slot)
        h.stage_config()
        live = await h.start_session()
        await h.prompt_and_settle(live, "netns probe: does search load?")
        reqs = h.router.requests()
        if not reqs:
            # diagnostics: what did the run actually do
            for e in live.events:
                if e.get("type") == "message_end":
                    m = e["message"]
                    print("MSG:", m.get("role"), str(m.get("content"))[:300], "|", m.get("errorMessage") or m.get("error"))
            print("DRIVER STDERR:", live.driver.stderr_tail[-2000:])
            print("SESSION DIR:", __import__("os").listdir(h.state.sessions_for(live.session_id)) if __import__("os").path.isdir(h.state.sessions_for(live.session_id)) else "missing")
        assert reqs, "no request reached the loopback mock"
        names = {t["function"]["name"] for t in reqs[0]["body"].get("tools", [])}
        need = {"web_search", "source_check", "fetch_content", "get_search_content"}
        assert need <= names, f"pi-web-access tools missing in netns: {need - names}"
        await h.release(live)
        h.stop()
        print("ZERO-REGISTRY-OK")
        return 0
    finally:
        try:
            from app.iso import mounts as M

            for ops in list(getattr(h, "mount_ops_by_session", {}).values()):
                M.aunmount_all_safe(ops)
        except Exception:
            pass
        shutil.rmtree(state_root.parent, ignore_errors=True)
        shutil.rmtree(run, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
