"""Unit: LF-only framer.

The framing traps, exactly:
- U+2028/U+2029 are legal inside JSON strings and are NOT record
  boundaries (Node readline splits on them).
- text-mode universal-newline translation rewrites embedded CRLF and
  corrupts JSON strings (we decode per-record from bytes).
- one optional trailing CR is tolerated (CRLF-terminated input).
- records are delimited by LF only; a record is one JSON object.
"""
from __future__ import annotations

import json

import pytest

from app.rpc.framer import LFFramer, encode_command


def records_from_chunks(*chunks: bytes) -> list[dict]:
    out: list[dict] = []
    framer = LFFramer(out.append)
    for c in chunks:
        framer.feed_bytes(c)
    return out


def test_plain_lf_records():
    recs = records_from_chunks(b'{"a": 1}\n{"b": 2}\n')
    assert recs == [{"a": 1}, {"b": 2}]


def test_u2028_u2029_inside_strings_are_not_boundaries():
    """The documented trap: a JSON string containing U+2028 and U+2029
    (raw bytes, legal in JSON) must arrive intact in ONE record."""
    payload = {"text": "line\u2028sep\u2029para", "n": 1}
    wire = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    assert "\u2028".encode("utf-8") in wire and "\u2029".encode("utf-8") in wire
    recs = records_from_chunks(wire + b"\n")
    assert recs == [payload]
    # and the record is a valid re-parse
    assert json.loads(json.dumps(recs[0])) == payload


def test_split_across_chunk_boundaries():
    rec1 = {"text": "a" * 100}
    wire = json.dumps(rec1).encode() + b"\n"
    # split the wire mid-JSON (and mid-UTF8-sequence for a multibyte char)
    payload = {"text": "héllo\u2028world"}
    wire2 = json.dumps(payload, ensure_ascii=False).encode() + b"\n"
    out: list[dict] = []
    framer = LFFramer(out.append)
    for i in range(0, len(wire), 7):
        framer.feed_bytes(wire[i : i + 7])
    for i in range(0, len(wire2), 5):
        framer.feed_bytes(wire2[i : i + 5])
    assert out == [rec1, payload]


def test_crlf_terminated_records_tolerated():
    recs = records_from_chunks(b'{"a": 1}\r\n{"b": 2}\r\n')
    assert recs == [{"a": 1}, {"b": 2}]


def test_embedded_crlf_sequence_in_string_survives():
    """A JSON string containing an escaped \\r\\n sequence: the wire bytes
    carry backslash-r backslash-n (legal). Universal-newline readers would
    rewrite them; we must not."""
    payload = {"text": "a\r\nb"}
    wire = json.dumps(payload).encode()  # escapes as \\r\\n
    recs = records_from_chunks(wire + b"\n")
    assert recs == [payload]
    assert recs[0]["text"] == "a\r\nb"


def test_blank_lines_ignored():
    recs = records_from_chunks(b'\n{"a": 1}\n\n\n')
    assert recs == [{"a": 1}]


def test_malformed_record_reported_not_fatal():
    out: list[dict] = []
    errors: list[tuple[int, str]] = []
    framer = LFFramer(out.append, lambda n, d: errors.append((n, d)))
    framer.feed_bytes(b"{broken\n{\"good\": 1}\n")
    assert out == [{"good": 1}]
    assert len(errors) == 1 and errors[0][0] == 1


def test_non_object_record_reported():
    out: list[dict] = []
    errors: list[tuple[int, str]] = []
    framer = LFFramer(out.append, lambda n, d: errors.append((n, d)))
    framer.feed_bytes(b"[1,2]\n{\"ok\": true}\n")
    assert out == [{"ok": True}]
    assert len(errors) == 1


def test_encode_command_roundtrip_is_lf_terminated_ascii_safe():
    cmd = {"id": "x", "type": "prompt", "message": "héllo\u2028"}
    wire = encode_command(cmd)
    assert wire.endswith(b"\n") and wire.count(b"\n") == 1
    # ensure_ascii=False: U+2028 stays a raw 3-byte sequence inside the
    # string — which is exactly why framing must be byte/LF-based
    assert "\u2028".encode("utf-8") in wire
    recs = records_from_chunks(wire)
    assert recs == [cmd]


def test_pending_tail_without_lf():
    out: list[dict] = []
    framer = LFFramer(out.append)
    framer.feed_bytes(b'{"a":')
    assert out == []
    assert framer.pending == b'{"a":'
    framer.feed_bytes(b' 1}\n')
    assert out == [{"a": 1}]
