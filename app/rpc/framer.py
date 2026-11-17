"""LF-only JSONL framer.

The pi RPC protocol is strict JSONL: one JSON object per record, LF-
terminated. Splitting rules, exactly:

- split records ONLY on ``b"\\n"`` (0x0A). ``U+2028``/``U+2029`` are legal
  inside JSON strings and are NOT record boundaries — Node's ``readline``
  splits on them (the documented trap), and so does Python text-mode
  ``readline()``.
- strip ONE optional trailing ``b"\\r"`` (tolerate CRLF input).
- decode each record as UTF-8 from bytes — never run the stream through
  text-mode universal-newline translation, which rewrites embedded CRLF and
  corrupts JSON strings.

This class is deliberately synchronous (a feed loop) so the same code runs
under asyncio (``StreamReader.read``) and in unit tests (byte chunks).
"""
from __future__ import annotations

import json
from typing import Any, Awaitable, Callable, Iterable

OnRecord = Callable[[dict[str, Any]], Any]
OnParseError = Callable[[int, str], Any]


class FramingError(RuntimeError):
    """A record failed to decode as JSON (logged, never crashes the loop)."""

    def __init__(self, record_no: int, detail: str):
        super().__init__(f"record {record_no}: {detail}")
        self.record_no = record_no
        self.detail = detail


class LFFramer:
    """Accumulates bytes and emits LF-terminated JSON records."""

    def __init__(
        self,
        on_record: OnRecord,
        on_parse_error: OnParseError | None = None,
    ) -> None:
        self._buf = bytearray()
        self._on_record = on_record
        self._on_parse_error = on_parse_error
        self._record_no = 0

    def feed_bytes(self, chunk: bytes) -> None:
        """Feed raw bytes (one read chunk). May emit zero or more records."""
        for record in self.split_records(chunk):
            self._on_record(record)

    def split_records(self, chunk: bytes) -> list[dict[str, Any]]:
        """Accumulate bytes and return the complete JSON records in this
        chunk (parse errors recorded, never raised). Callers may dispatch
        with an ``await`` between records (backpressure gating)."""
        out: list[dict[str, Any]] = []
        self._buf.extend(chunk)
        while True:
            nl = self._buf.find(b"\n")
            if nl < 0:
                return out
            line = bytes(self._buf[:nl])
            del self._buf[: nl + 1]
            if line.endswith(b"\r"):  # tolerate CRLF-terminated records
                line = line[:-1]
            if not line.strip():
                continue
            self._record_no += 1
            try:
                record = json.loads(line.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                if self._on_parse_error:
                    self._on_parse_error(self._record_no, str(exc))
                continue
            if not isinstance(record, dict):
                if self._on_parse_error:
                    self._on_parse_error(
                        self._record_no, f"record is not a JSON object: {type(record).__name__}"
                    )
                continue
            out.append(record)

    @property
    def pending(self) -> bytes:
        """Undelivered tail (diagnostics only — a live pi always LF-terminates)."""
        return bytes(self._buf)


async def read_stream(
    reader,
    on_record: OnRecord,
    on_parse_error: OnParseError | None = None,
    chunk_size: int = 65536,
) -> None:
    """Drain an asyncio StreamReader through an LFFramer until EOF.

    Returns on EOF. Callers must keep reading stdout continuously (a client
    that stops reading can stall pi when the pipe buffer fills — rpc.md).
    """
    framer = LFFramer(on_record, on_parse_error)
    while True:
        chunk = await reader.read(chunk_size)
        if not chunk:
            framer.feed_bytes(b"")  # no-op; keep the code path explicit
            return
        framer.feed_bytes(chunk)


def encode_command(cmd: dict[str, Any]) -> bytes:
    """Serialize one command for stdin: compact JSON + LF, UTF-8."""
    return json.dumps(cmd, ensure_ascii=False, separators=(",", ":")).encode("utf-8") + b"\n"
