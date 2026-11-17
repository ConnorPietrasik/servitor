"""Portcullis panel action endpoint — the exact
whitelist (start|steer|prompt|abort|release), nothing else; unknown
actions are 400; missing sessions are 404; per-session authz."""
from __future__ import annotations

import time

WHITELIST = {"start", "steer", "prompt", "abort", "release"}


def test_whitelist_enforced(api):
    res = api.create_session("panel")
    sid = res["session_id"]
    try:
        # unknown actions: 400 + the whitelist in the detail (checked
        # per-action; the session is still alive at this point)
        for action in ("rm", "restart", "exec", "SHELL", "Start", "release_all"):
            r = api.post(
                f"/panel/sessions/{sid}/action",
                json_body={"action": action},
            )
            assert r.status_code == 400, f"{action} must be rejected (got {r.status_code})"
            assert "whitelist" in r.json().get("detail", "")
        # whitelisted actions on a live session: 200 (start = no-op,
        # abort = no-op on an idle session, release = final)
        r = api.post(
            f"/panel/sessions/{sid}/action",
            json_body={"action": "start"},
            timeout=60,
        )
        assert r.status_code == 200, f"start: {r.status_code} {r.text}"
        assert r.json()["alive"] is True
        r = api.post(
            f"/panel/sessions/{sid}/action",
            json_body={"action": "abort"},
            timeout=60,
        )
        assert r.status_code == 200, f"abort: {r.status_code} {r.text}"
        # release is whitelisted AND terminal: it is the last positive
        # case (after it, the session is gone and further actions 404)
        r = api.post(
            f"/panel/sessions/{sid}/action",
            json_body={"action": "release"},
            timeout=120,
        )
        assert r.status_code == 200, f"release: {r.status_code} {r.text}"
        assert r.json()["released"] is True
        # missing/released sessions are 404 (existence never leaks)
        r = api.post(
            f"/panel/sessions/{sid}/action",
            json_body={"action": "start"},
        )
        assert r.status_code == 404
        r = api.post(
            "/panel/sessions/00000000-0000-4000-8000-000000000000/action",
            json_body={"action": "start"},
        )
        assert r.status_code == 404
    finally:
        # idempotent teardown (release on an already-released session
        # 404s; swallow)
        api.post(f"/panel/sessions/{sid}/action", json_body={"action": "release"}, timeout=120)


def test_panel_start_respawns_dead_process(api, api_env):
    """`start` on a session whose process is dead (idle-killed) respawns
    from the transcript — the panel can wake a session, and a new prompt
    reaches it. It never injects a prompt it does not have."""
    res = api.create_session("panel-start")
    sid = res["session_id"]
    try:
        d = api.prompt(sid, "wake me later")
        assert d["disposition"] == "started"
        api.settle(sid)

        # kill the process via idle-kill (the panel cannot see the driver;
        # the API has no direct kill — idle-kill is the production path)
        # (the test env runs idle-kill at 3600s; use release-free kill via
        # the app: there is no API kill — so: abort, wait for exit, then
        # start must respawn.)
        # Simplest deterministic death: the process is still alive but
        # idle — start must be a no-op:
        r = api.post(f"/panel/sessions/{sid}/action", json_body={"action": "start"}, timeout=60)
        assert r.status_code == 200
        assert r.json()["alive"] is True
        assert r.json()["respawned"] is False

        # now kill the process directly (simulating a crash/death) and
        # start must respawn from the transcript
        proc_pids = _slot_pids(9001)
        assert proc_pids, "expected a live process"
        import os

        for p in proc_pids:
            os.kill(p, 15)  # TERM: pi's clean exit (stdin stays open)
        deadline = time.time() + 30
        while _slot_pids(9001) and time.time() < deadline:
            time.sleep(0.3)
        assert not _slot_pids(9001), "process did not die"

        r = api.post(f"/panel/sessions/{sid}/action", json_body={"action": "start"}, timeout=90)
        assert r.status_code == 200
        d = r.json()
        assert d["alive"] is True and d["respawned"] is True

        # and it is usable: a prompt settles
        d = api.prompt(sid, "hello again")
        assert d["disposition"] == "started"
        st = api.settle(sid)
        assert st["aborted"] is False
    finally:
        api.release(sid)


def _slot_pids(uid: int) -> list[int]:
    import os

    out = []
    for e in os.listdir("/proc"):
        if not e.isdigit():
            continue
        try:
            with open(f"/proc/{e}/status") as f:
                for line in f:
                    if line.startswith("Uid:"):
                        if int(line.split()[1]) == uid:
                            out.append(int(e))
                        break
        except OSError:
            pass
    return out
