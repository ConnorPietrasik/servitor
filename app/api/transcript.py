"""Transcript reading (reattach on load — fetch, never replay).

The session file is an append-only tree of entries with stable ids
(``--session-dir`` flat layout: one jsonl per session); message entries
carry ``message: {role: user|assistant|toolResult, content: [parts]}``.

A live process is authoritative (pi RPC: get_entries); when the process
is dead the file on disk is the same truth. The ACTIVE BRANCH is the
parent chain from the leaf: pi reports the leaf for a live process; for
a dead process the leaf is the last entry in append order (after a fork
all new entries belong to the new branch — the file's tail is the active
branch's tail).
"""
from __future__ import annotations

import json
import os
import stat
from typing import Any

__all__ = [
    "read_entries",
    "active_branch",
    "entry_to_message",
]


def read_entries(path: str) -> list[dict[str, Any]]:
    """All entries in append order (the session header line is skipped —
    it has no id/parentId pair). Malformed trailing lines (a torn write)
    are dropped, not fatal: the run is crashed anyway and the UI must
    still render what exists."""
    out: list[dict[str, Any]] = []
    if not os.path.isfile(path):
        return out
    try:
        # a planted link would make the root app read a root-readable target
        # and serve it in the transcript UI: never follow it (fail closed,
        # same as a missing transcript)
        if stat.S_ISLNK(os.lstat(path).st_mode):
            return out
    except OSError:
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


def entry_to_message(entry: dict[str, Any]) -> dict[str, Any] | None:
    """Map a transcript entry to the UI message shape. Non-message entries
    (compaction, labels) return None."""
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
