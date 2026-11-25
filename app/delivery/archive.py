"""Streaming archive: a directory tree, zipped as it is read.

One file the human asked for, N files in a folder, or a whole area — and the
bytes go out as they come off the disk. Nothing is staged first, because the
alternative is a second full copy of a tree on the same volume that may be the
reason the disk is nearly full: an "archive" that needs as much free space as
the archive is not an archive, it is a time bomb.

The format is a plain ZIP, written forward with no seeks:

- a stored or deflated entry, then a data descriptor carrying its CRC and
  sizes (flag bit 3), so a size never has to be known up front;
- the central directory and end record after the last entry, built from the
  per-entry metadata the pass already collected;
- ZIP64 fields only where a value does not fit 32 bits (decided per entry from
  ``stat``, before the entry is written). A folder of ordinary files is a
  100 % classic ZIP; a 9 GB video binder is a legal ZIP64 — and neither path
  refuses, because a size limit on an archive would be a cap (invariant 2).

Entry bytes are read with the CALLER's gate applied: this module is handed a
list of files the delivery layer already resolved and gated (contained,
symlink-free, readable by this session). It never walks a directory itself —
the listing is the gate, and an archiver that re-walks would be a quiet way to
read past it.
"""
from __future__ import annotations

import os
import stat
import struct
import time
import zlib
from collections.abc import Iterator, Sequence

ZIP64_LIMIT = 0xFFFFFFFF
_DEFLATE_CHUNK = 1 << 20  # 1 MB reads: a big tree streams at disk speed

# only the data descriptor's signature is written separately (it prefixes the
# sizes mid-stream); every other record carries its signature inside the pack
_DESCRIPTOR_SIG = b"PK\x07\x08"
#: the "this field lives in the ZIP64 extra field" marker the spec fixes — not
#: a threshold, a sentinel value written into the 32-bit field
_ZIP64_MARKER = 0xFFFFFFFF


def _dos_time(ts: float) -> tuple[int, int]:
    tm = time.localtime(ts)
    dos_date = ((max(tm.tm_year, 1980) - 1980) << 9) | (tm.tm_mon << 5) | tm.tm_mday
    dos_time = (tm.tm_hour << 11) | (tm.tm_min << 5) | (tm.tm_sec // 2)
    return dos_time, dos_date


class _EntryResult:
    """What the central directory needs, filled in when the entry's bytes are
    exhausted (a size is only known after the last chunk)."""

    __slots__ = ("central",)

    def __init__(self) -> None:
        self.central = b""


class ArchiveError(RuntimeError):
    """The archive cannot be completed as the headers promised (an entry
    grew past the 32-bit fields it was written with). The stream aborts
    with a reason instead of yielding a zip a reader cannot parse — a
    corrupt archive is worse than a truncated one the human can retry."""


def _entry_impl(
    arcname: str,
    source: str,
    offset: int,
    *,
    zip64_threshold: int = ZIP64_LIMIT,
    level: int = 6,
) -> tuple[Iterator[bytes], _EntryResult]:
    """Build one entry. Returns ``(byte_source, result)``: the result's
    ``central`` record is complete once ``byte_source`` is exhausted (the
    sizes are only known then)."""
    st = os.stat(source)
    usize_hint = st.st_size
    # deflate can INFLATE incompressible bytes slightly, so the 32-bit fields
    # are abandoned a little early rather than overflowing at the last block
    margin = min(1 << 20, max(zip64_threshold // 8, 0))
    zip64 = usize_hint > zip64_threshold - margin or offset > zip64_threshold
    dos_time, dos_date = _dos_time(st.st_mtime)
    name = arcname.encode("utf-8", "surrogateescape")
    # stored only when empty: deflate has no bytes to work on
    method = 8 if usize_hint > 0 else 0
    # bit 3: sizes/CRC travel in the data descriptor; bit 11: the name is
    # UTF-8 — the reader's only cue that it is not CP437, so it is set
    # whenever the encoded bytes are valid UTF-8 (always for a clean name,
    # ASCII included, where readers that ignore the flag are unaffected)
    try:
        name.decode("utf-8")
        flags = 0x08 | 0x800  # bit 3 (descriptor) + bit 11: the name is UTF-8
    except UnicodeDecodeError:
        flags = 0x08  # raw bytes in the name: no claim to make

    extra = b""
    if zip64:
        # placeholders: the real sizes ride the (8-byte) data descriptor
        extra = struct.pack("<HH", 0x0001, 16) + struct.pack("<QQ", 0, 0)
    local = struct.pack(
        "<IHHHHHIIIHH",
        0x04034B50,
        45 if zip64 else 20,
        flags,
        method,
        dos_time,
        dos_date,
        0,  # crc (descriptor)
        0,  # compressed size (descriptor)
        0,  # uncompressed size (descriptor)
        len(name),
        len(extra),
    )

    result = _EntryResult()

    def body() -> Iterator[bytes]:
        yield local + name + extra
        # sizes of the DATA only — the header is not part of either
        crc = 0
        csize = 0
        usize = 0
        if method == 8:
            co = zlib.compressobj(level, zlib.DEFLATED, -15)
            with open(source, "rb") as f:
                while True:
                    raw = f.read(_DEFLATE_CHUNK)
                    if not raw:
                        break
                    crc = zlib.crc32(raw, crc)
                    usize += len(raw)
                    out = co.compress(raw)
                    if out:
                        csize += len(out)
                        yield out
            tail = co.flush()
            if tail:
                csize += len(tail)
                yield tail
        else:
            with open(source, "rb") as f:
                while True:
                    raw = f.read(_DEFLATE_CHUNK)
                    if not raw:
                        break
                    crc = zlib.crc32(raw, crc)
                    usize += len(raw)
                    csize += len(raw)
                    yield raw
        # the zip64 decision was made from stat, before the bytes left disk:
        # a file still being written (an unstable file downloads by design)
        # can grow past the 32-bit fields it was promised. This is the first
        # moment the true sizes are known — detect the overflow here and
        # abort with a reason instead of letting struct.pack raise over an
        # archive a reader would have to reject as corrupt anyway.
        if not zip64 and (usize > zip64_threshold or csize > zip64_threshold):
            raise ArchiveError(
                f"entry {arcname!r} grew past the 32-bit ZIP limit mid-read — "
                "the archive was aborted; re-download once the file settles"
            )
        # data descriptor (8-byte sizes when the entry is ZIP64)
        if zip64:
            yield _DESCRIPTOR_SIG + struct.pack("<IQQ", crc, csize, usize)
        else:
            yield _DESCRIPTOR_SIG + struct.pack("<III", crc, csize, usize)

        # a value that does not fit 32 bits goes into the ZIP64 extra field,
        # and its 32-bit twin is set to the "see ZIP64" marker (in the order
        # the spec fixes: size, compressed size, local offset)
        c_extra = b""
        c_usize, c_csize, c_offset = usize, csize, offset
        if zip64:
            fields: list[int] = []
            if usize > zip64_threshold:
                fields.append(usize)
                c_usize = _ZIP64_MARKER
            if csize > zip64_threshold:
                fields.append(csize)
                c_csize = _ZIP64_MARKER
            if offset > zip64_threshold:
                fields.append(offset)
                c_offset = _ZIP64_MARKER
            if fields:
                c_extra = struct.pack("<HH", 0x0001, 8 * len(fields)) + b"".join(
                    struct.pack("<Q", v) for v in fields
                )
        mode = stat.S_IMODE(st.st_mode) | (0o040000 if stat.S_ISDIR(st.st_mode) else 0o100000)
        result.central = struct.pack(
            "<IHHHHHHIIIHHHHHII",
            0x02014B50,
            45 if zip64 else 20,  # version made by: ZIP64 when the entry is one
            45 if zip64 else 20,
            flags,
            method,
            dos_time,
            dos_date,
            crc,
            c_csize,
            c_usize,
            len(name),
            len(c_extra),
            0,  # comment
            0,  # disk
            0,  # internal attrs
            mode << 16,
            c_offset,
        ) + name + c_extra

    return body(), result


def stream_zip(
    files: Sequence[tuple[str, str]], *, zip64_threshold: int = ZIP64_LIMIT, level: int = 6
) -> Iterator[bytes]:
    """Yield a ZIP archive of ``files`` — ``(arcname, abspath)`` pairs.

    The caller resolves and gates every path; order is preserved, so a folder
    archives as the human saw it.
    """
    entries: list[_EntryResult] = []
    offset = 0
    for arcname, source in files:
        body, result = _entry_impl(
            arcname, source, offset, zip64_threshold=zip64_threshold, level=level
        )
        for chunk in body:
            offset += len(chunk)
            yield chunk
        entries.append(result)

    cd_start = offset
    cd = b"".join(e.central for e in entries)
    cd_size = len(cd)
    yield cd

    count = len(entries)
    need64 = (
        count > 0xFFFF
        or cd_size > zip64_threshold
        or cd_start > zip64_threshold
    )
    if need64:
        yield struct.pack(
            "<IQHHIIQQQQ",
            0x06064B50,  # zip64 end of central directory record
            44,  # this record's size, minus its first 12 bytes
            45,
            45,
            0,
            0,
            count,
            count,
            cd_size,
            cd_start,
        )
        # locator: signature, disk with the zip64 record, its offset, disk count
        yield struct.pack("<IIQI", 0x07064B50, 0, cd_start + cd_size, 1)
        eocd_count = 0xFFFF if count > 0xFFFF else count
        eocd_cdsize = _ZIP64_MARKER if cd_size > zip64_threshold else cd_size
        eocd_off = _ZIP64_MARKER if cd_start > zip64_threshold else cd_start
    else:
        eocd_count, eocd_cdsize, eocd_off = count, cd_size, cd_start

    yield struct.pack(
        "<IHHHHIIH", 0x06054B50, 0, 0, eocd_count, eocd_count, eocd_cdsize, eocd_off, 0
    )
