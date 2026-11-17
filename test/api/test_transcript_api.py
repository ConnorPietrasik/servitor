"""Transcript fetch (reattach on load) + fork/branch gates.

- /messages is the reattach base: authoritative active-branch messages,
  fetch (never replay). Works with a LIVE process (pi RPC get_entries)
  AND with a DEAD process (app restarted over the same state — disk
  path).
- /tree shows all branches (read-only); /fork-messages lists the fork
  points; /fork moves the active branch ("edit that message") and a new
  prompt continues from the fork — the abandoned branch is still in the
  tree.
- /switch navigates the live process to another session file.
"""
from __future__ import annotations

import json

from conftest import relaunch_app  # noqa: F401  (importable in tests)


def _user_texts(messages: list[dict]) -> list[str]:
    return [m["text"] for m in messages if m["role"] == "user"]


def test_messages_fetch_live_and_dead(api, api_env):
    res = api.create_session("reattach")
    sid = res["session_id"]
    api.prompt(sid, "hello one")
    api.settle(sid)
    api.prompt(sid, "hello two")
    api.settle(sid)

    # live: authoritative active branch (2 user prompts in order,
    # assistant answers present, leaf = last entry)
    r = api.get(f"/api/v1/sessions/{sid}/messages")
    assert r.status_code == 200, r.text
    d = r.json()
    assert _user_texts(d["messages"]) == ["hello one", "hello two"]
    assert any(m["role"] == "assistant" and m["text"] for m in d["messages"])
    assert d["leaf_id"] is not None
    last_id = d["messages"][-1]["id"]
    assert last_id == d["leaf_id"], "leaf must be the branch's last entry"

    # every message's parent chain is consistent
    ids = {m["id"] for m in d["messages"]}
    for m in d["messages"][1:]:
        assert m["parent_id"] in ids

    # --- dead process: kill the app, restart over the SAME state ------
    env_proc = api_env["proc"]
    env_proc.kill()
    env_proc.wait()
    proc, base2 = relaunch_app(api_env)
    api2 = type(api)(base2)
    try:
        # boot reconcile kills the crash-orphan pi and marks the session
        # interrupted; the process is dead — /messages still returns the
        # full transcript (disk path)
        r = api2.get(f"/api/v1/sessions/{sid}/messages", timeout=30)
        assert r.status_code == 200, r.text
        d2 = r.json()
        assert _user_texts(d2["messages"]) == ["hello one", "hello two"]
        assert d2["leaf_id"] == d["leaf_id"]
        # and the tree agrees
        r = api2.get(f"/api/v1/sessions/{sid}/tree", timeout=30)
        assert r.status_code == 200
        assert r.json()["leaf_id"] == d["leaf_id"]
    finally:
        proc.kill()
        proc.wait()
    # the original app is dead (crash drill pattern): no release — the
    # fixture teardown sweeps the state


def test_fork_edit_message_branches(api):
    res = api.create_session("fork")
    sid = res["session_id"]
    try:
        api.prompt(sid, "first question")
        api.settle(sid)
        api.prompt(sid, "second question")
        api.settle(sid)

        # fork points: both user messages, oldest first
        r = api.get(f"/api/v1/sessions/{sid}/fork-messages")
        assert r.status_code == 200, r.text
        pts = r.json()["messages"]
        texts = [p["text"] for p in pts]
        assert texts == ["first question", "second question"]
        first_id = pts[0]["entryId"]

        row0 = api.get(f"/api/v1/sessions/{sid}").json()
        file0 = row0["session_file"]

        # edit "first question": fork from it. Verified pi 1.5.2
        # semantics: the fork creates a NEW session file (same session
        # dir) and the process switches to it — a fresh session at the
        # fork point (the forked message is NOT re-appended; the client
        # re-sends it, edited). The registry must follow the new file.
        r = api.post(f"/api/v1/sessions/{sid}/fork",
                     json_body={"entry_id": first_id})
        assert r.status_code == 200, r.text
        assert r.json()["forked"] is True
        assert r.json()["text"] == "first question"

        # the new prompt continues the FORK (a fresh session at the fork
        # point) — the old branch ("second question") is not on it
        api.prompt(sid, "revised continuation")
        api.settle(sid)
        r = api.get(f"/api/v1/sessions/{sid}/messages")
        assert r.status_code == 200
        branch_users = _user_texts(r.json()["messages"])
        assert branch_users == ["revised continuation"], (
            f"active branch after fork: {branch_users}"
        )

        # the registry followed the new session file
        row1 = api.get(f"/api/v1/sessions/{sid}").json()
        assert row1["session_file"] != file0, "registry must track the forked file"

        # the forest keeps EVERYTHING: both files, both branches
        r = api.get(f"/api/v1/sessions/{sid}/tree")
        assert r.status_code == 200
        t = r.json()
        assert len(t["files"]) == 2, f"expected original + fork file: {t['files']}"
        blob = json.dumps(t)
        assert "second question" in blob, "abandoned branch must survive in the tree"
        assert "revised continuation" in blob

        # fork points on the NEW active session
        r = api.get(f"/api/v1/sessions/{sid}/fork-messages")
        pts2 = [p["text"] for p in r.json()["messages"]]
        assert pts2 == ["revised continuation"]
        assert r.status_code == 200
    finally:
        api.release(sid)


def test_switch_same_file_is_noop(api):
    res = api.create_session("switch")
    sid = res["session_id"]
    try:
        row = api.get(f"/api/v1/sessions/{sid}").json()
        path = row["session_file"]
        assert path
        r = api.post(f"/api/v1/sessions/{sid}/switch",
                     json_body={"session_path": path})
        assert r.status_code == 200, r.text
        assert r.json()["switched"] is True
        # still the same session, still usable
        d = api.prompt(sid, "after switch")
        assert d["disposition"] == "started"
        api.settle(sid)
    finally:
        api.release(sid)
