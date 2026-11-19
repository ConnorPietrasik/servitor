"""Transcript fetch (reattach on load) + fork gate.

- /messages is the reattach base: authoritative active-branch messages,
  fetch (never replay). Works with a LIVE process (pi RPC get_entries)
  AND with a DEAD process (app restarted over the same state — disk
  path).
- /fork moves the active branch ("edit that message"): a new prompt
  continues from the fork, the registry tracks the forked session file,
  and the abandoned branch file is kept until the fork is CONFIRMED by
  a write (the resend), then deleted — no dead prefix copies accumulate
  in the session dir, and an abandoned/mis-clicked fork is still
  recoverable until the user sends (or the grace-bounded GC sweep —
  ORPHAN_IDLE_GRACE_SEC, 24 h — reclaims it).
"""
from __future__ import annotations

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
    finally:
        proc.kill()
        proc.wait()
    # the original app is dead (crash drill pattern): no release — the
    # fixture teardown sweeps the state


def test_fork_edit_message_branches(api, api_env):
    res = api.create_session("fork")
    sid = res["session_id"]
    try:
        api.prompt(sid, "first question")
        api.settle(sid)
        api.prompt(sid, "second question")
        api.settle(sid)

        # fork point: the first user message (its id is the entry id)
        r = api.get(f"/api/v1/sessions/{sid}/messages")
        assert r.status_code == 200, r.text
        first = next(m for m in r.json()["messages"] if m["role"] == "user")
        assert first["text"] == "first question"
        first_id = first["id"]

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

        # deferred retirement: the abandoned file SURVIVES between the
        # fork and the resend — retirement waits for the fork to be
        # confirmed by a write, so an abandoned/mis-clicked fork is
        # still recoverable until the user sends
        sdir = api_env["state_root"] / "sessions" / sid
        pre = sorted(f.name for f in sdir.glob("*.jsonl"))
        assert file0.rsplit("/", 1)[-1] in pre, (
            f"abandoned file must survive until the fork is confirmed: {pre}"
        )

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

        # the abandoned branch file is gone: the resend confirmed the
        # fork, and the prompt's post-write sweep retired the orphan
        # (no dead prefix copies accumulate in the session dir)
        sdir = api_env["state_root"] / "sessions" / sid
        files = sorted(f.name for f in sdir.glob("*.jsonl"))
        assert files == [row1["session_file"].rsplit("/", 1)[-1]], (
            f"session dir must hold only the active file: {files}"
        )
    finally:
        api.release(sid)
