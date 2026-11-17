"""Transcript reading (reattach on load — fetch, never replay).

The session file is an append-only tree of entries with stable ids
(``--session-dir`` flat layout: one jsonl per session). Every entry is
``{type, id, parentId, timestamp, ...}``; message entries carry
``message: {role: user|assistant|toolResult, content: [parts], ...}``.

A live process is authoritative (pi RPC: get_entries/get_tree). When the
process is dead (idle-killed, crashed, app restarted) the file on disk is
the same truth — the readers here work on it directly so the UI can
reattach to any session without a live process.

The ACTIVE BRANCH is the parent chain from the current leaf. For a live
process pi reports the leaf (get_entries.leafId / get_tree.leafId); for a
dead process the leaf is the last entry in append order (pi appends to
the file in write order, and after a fork all new entries belong to the
new branch — the file's tail is the active branch's tail).
"""
from __future__ import annotations

import json
import os
from typing import Any

__all__ = [
    "read_entries",
    "active_branch",
    "build_tree",
    "build_forest",
    "entry_to_message",
    "forkable_messages",
]


def read_entries(path: str) -> list[dict[str, Any]]:
    """All entries in append order (the session header line is skipped —
    it has no id/parentId pair). Malformed trailing lines (a torn write)
    are dropped, not fatal: the run is crashed anyway and the UI must
    still render what exists."""
    out: list[dict[str, Any]] = []
    if not os.path.isfile(path):
        return out
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue  # torn write at the tail
            if isinstance(rec, dict) and "id" in rec and "parentId" in rec:
                out.append(rec)
    return out


def active_branch(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The active branch, root-first, from the leaf (last entry in
    append order)."""
    if not entries:
        return []
    by_id = {e["id"]: e for e in entries}
    leaf = entries[-1]
    chain: list[dict[str, Any]] = []
    seen: set[str] = set()
    cur: dict[str, Any] | None = leaf
    while cur is not None and cur["id"] not in seen:
        seen.add(cur["id"])
        chain.append(cur)
        pid = cur.get("parentId")
        cur = by_id.get(pid) if pid else None
    return list(reversed(chain))


def build_tree(entries: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], str | None]:
    """The full entry tree (all branches), pi get_tree shape:
    ``[{entry, children: [...]}, ...]`` roots + leafId (last entry in
    append order). Orphaned entries (broken parent chain) are roots —
    same as pi's documented behavior."""
    by_id = {e["id"]: e for e in entries}
    nodes: dict[str, dict[str, Any]] = {
        e["id"]: {"entry": e, "children": []} for e in entries
    }
    roots: list[dict[str, Any]] = []
    for e in entries:
        pid = e.get("parentId")
        if pid and pid in by_id:
            nodes[pid]["children"].append(nodes[e["id"]])
        else:
            roots.append(nodes[e["id"]])
    leaf_id = entries[-1]["id"] if entries else None
    return roots, leaf_id


def build_forest(session_dir: str) -> tuple[list[dict[str, Any]], str | None, list[str]]:
    """All session FILES in the session dir as one read-only forest.

    pi's fork creates a NEW session file (same dir, new id) and the
    process switches to it; the original file stays. The session's full
    branch picture = every file in the dir. Returns (roots, leaf_id,
    files): roots = concatenated per-file trees; leaf_id = the last
    entry of the newest file; files = file names, oldest first."""
    import glob

    files = sorted(glob.glob(os.path.join(session_dir, "*.jsonl")))
    if not files:
        return [], None, []
    all_roots: list[dict[str, Any]] = []
    leaf_id: str | None = None
    newest = files[-1]
    for f in files:
        roots, leaf = build_tree(read_entries(f))
        all_roots.extend(roots)
        if f == newest:
            leaf_id = leaf
    return all_roots, leaf_id, [os.path.basename(f) for f in files]


def entry_to_message(entry: dict[str, Any]) -> dict[str, Any] | None:
    """Map a transcript entry to the UI message shape. Non-message
    entries (compaction, labels) return None.

    UI shape (fidelity-preserving; the client renders per its mode):
    {id, parent_id, timestamp, role, text, thinking, thinking_signature,
     tool_calls: [{id, name, arguments}], tool_call_id, tool_name,
     is_error, result, model, usage}
    """
    if entry.get("type") != "message":
        return None
    msg = entry.get("message") or {}
    role = msg.get("role")
    if role not in ("user", "assistant", "toolResult"):
        return None
    content = msg.get("content")
    parts: list[Any] = []
    if isinstance(content, str):
        parts = [{"type": "text", "text": content}]
    elif isinstance(content, list):
        parts = content
    text_bits: list[str] = []
    thinking_bits: list[str] = []
    signature = ""
    tool_calls: list[dict[str, Any]] = []
    for p in parts:
        if not isinstance(p, dict):
            continue
        t = p.get("type")
        if t == "text" and isinstance(p.get("text"), str):
            text_bits.append(p["text"])
        elif t == "thinking" and isinstance(p.get("thinking"), str):
            thinking_bits.append(p["thinking"])
            if p.get("thinkingSignature"):
                signature = str(p["thinkingSignature"])
        elif t == "toolCall":
            tool_calls.append({
                "id": str(p.get("id") or ""),
                "name": str(p.get("name") or ""),
                "arguments": p.get("arguments") or {},
            })
    out: dict[str, Any] = {
        "id": entry["id"],
        "parent_id": entry.get("parentId"),
        "timestamp": entry.get("timestamp"),
        "role": "tool_result" if role == "toolResult" else role,
        "text": "".join(text_bits),
        "thinking": "".join(thinking_bits),
        "thinking_signature": signature,
        "tool_calls": tool_calls,
    }
    if role == "toolResult":
        res_content = msg.get("content")
        result_text = ""
        if isinstance(res_content, str):
            result_text = res_content
        elif isinstance(res_content, list):
            result_text = "".join(
                p.get("text", "") for p in res_content
                if isinstance(p, dict) and p.get("type") == "text"
            )
        out["tool_call_id"] = str(msg.get("toolCallId") or "")
        out["tool_name"] = str(msg.get("toolName") or "")
        out["is_error"] = bool(msg.get("isError", False))
        out["result"] = result_text
    for extra in ("model", "usage", "api", "stopReason"):
        if extra in msg:
            out[extra] = msg[extra]
    return out


def forkable_messages(branch: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """User messages on the active branch (pi get_fork_messages shape):
    [{entryId, text}]. These are the fork points ("edit that message")."""
    out = []
    for e in branch:
        m = entry_to_message(e)
        if m and m["role"] == "user" and m["text"]:
            out.append({"entryId": m["id"], "text": m["text"]})
    return out
