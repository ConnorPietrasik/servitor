#!/usr/bin/env python3
"""Media gen CLI — pictures, video, and picture edits via the panel's gen
service (the panel-api machine gen surface).

Stdlib only: this runs under whatever python the session's PATH gives it
(the toolkit is not a venv consumer), so nothing here may import a package
pip installed.

Config (env, fail closed — both ride the pi subprocess env, never argv):
  SERVITOR_GEN_BASE_URL  the machine surface base (…/machine/gen)
  OMNISSIAH_API_KEY      bearer for the machine door (the LLM router key)

Contract points (kept in lockstep with panel-api/gen_jobs.py — the cheap
doors are mirrored locally to fail fast; the server is the authority):
  - purpose keys image/video/edit are the ONLY model names; the operator's
    Gen settings resolve them server-side (this CLI never sees a model);
  - enhance is ALWAYS on (explicit true: the server 400s loudly when the
    resolved model has no enhance prompt configured);
  - the fast default size is 512x512 for image and video; an edit sends no
    size (it follows the source picture);
  - an edit takes at most 10 pictures, at most 16 MiB each (the server
    re-checks and owns the format/64 MP/mask-size doors — the client never
    gates a format: an unknown extension rides as octet-stream and the
    server's 400 is the verdict);
  - the default destination is the session workdir's outbox/ (this file's
    grandparent — the toolkit is the workdir's RO lower), never the cwd:
    a scratch-subdir cwd would divert the artifact off the delivery outbox;
  - jobs SURVIVE this process (the queue lives in panel-api): the id
    printed at submit is the re-attach handle — `gen wait <id>` works after
    a killed wait, an app crash, or a VM reboot. There is deliberately no
    timeout of our own: the session's bash has no default timeout, and a
    job's clock is server-side.

Exit codes: 0 artifact saved · 1 local error (env, files, shape, bad
args) · 2 server error (create rejected, job failed/canceled, auth,
unreachable). argparse's own usage errors exit 1, not its default 2: 2 is
the server's code, and a typo is a local failure like any other.
"""
from __future__ import annotations

import argparse
import base64
import json
import mimetypes
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import NoReturn

DEFAULT_SIZE = "512x512"  # the fast default (image + video); an edit: none
POLL_SECS = 5.0  # the engine ticks at ≤5 s; the job's clock is server-side
REQ_TIMEOUT = 30  # per-request socket timeout — a transport hang guard, never a job timeout
TERMINAL = ("completed", "failed", "canceled")
SIZE_RE = re.compile(r"^\d{2,4}x\d{2,4}$")  # the server's shape (WIDTHxHEIGHT)
# mirrored server doors (panel-api/gen_jobs.py) — same facts, earlier
# failure. Format is deliberately NOT one: the server decodes the bytes
# and owns that door (mirroring it client-side would over-reject — an
# unknown extension is not a bad picture)
MAX_IMAGES = 10
MAX_IMAGE_BYTES = 16 * 1024 * 1024


class GenError(Exception):
    """A failure with a ready message. code: 1 local, 2 server-side."""

    def __init__(self, message: str, code: int = 2) -> None:
        super().__init__(message)
        self.code = code


class _Parser(argparse.ArgumentParser):
    """argparse exits its own usage errors with 2 — the contract reserves
    2 for the server, so a typo is a local (1) failure like every other
    bad arg. Subparsers inherit this class (add_subparsers' default)."""

    def error(self, message: str) -> NoReturn:
        self.print_usage(sys.stderr)
        raise GenError(f"error: {message}", 1)


def _env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise GenError(
            f"{name} is not set — media generation is unavailable on this "
            "session (a config gap, not a retry)",
            1,
        )
    return value


def _http(method: str, base: str, path: str, payload: dict | None = None):
    """One request to the machine surface → (status, bytes, content-type).

    The bearer is re-read from the env per request and NEVER printed (this
    CLI's stderr lands in the transcript). HTTP errors become GenError (2)
    carrying the server's detail — the server's 400s are user-ready."""
    headers = {"Authorization": f"Bearer {_env('OMNISSIAH_API_KEY')}"}
    data = None
    if payload is not None:
        data = json.dumps(payload).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(base + path, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=REQ_TIMEOUT) as resp:
            return resp.status, resp.read(), resp.headers.get("Content-Type", "")
    except urllib.error.HTTPError as e:
        if e.code == 401:
            raise GenError(
                "the gen service rejected the key (401) — OMNISSIAH_API_KEY "
                "does not match the panel's machine door",
                2,
            ) from None
        detail = ""
        try:
            body = json.loads(e.read())
            detail = body.get("detail", "") if isinstance(body, dict) else str(body)
        except Exception:
            pass
        raise GenError(f"{e.code} on {method} {path}: {detail or e.reason}", 2) from None
    except urllib.error.URLError as e:
        raise GenError(f"cannot reach the gen service at {base}: {e.reason}", 2) from None
    except (ValueError, TimeoutError, OSError) as e:
        raise GenError(f"the gen service request failed: {e}", 2) from None


def _data_url(path: str) -> str:
    """A local file → the `data:` URL the create request takes. Only the
    byte cap is mirrored (fail fast, same facts); the format door is the
    server's (it decodes the bytes) — an unknown extension rides as
    application/octet-stream and the server's 400 is the verdict."""
    if not os.path.isfile(path):
        raise GenError(f"picture not found: {path}", 1)
    with open(path, "rb") as f:
        blob = f.read()
    if len(blob) > MAX_IMAGE_BYTES:
        raise GenError(
            f"{path} is {len(blob)} bytes — the gen service takes at most "
            f"{MAX_IMAGE_BYTES // (1024 * 1024)} MiB per picture",
            1,
        )
    mime = mimetypes.guess_type(path)[0] or "application/octet-stream"
    return f"data:{mime};base64," + base64.b64encode(blob).decode()


def _submit(base: str, kind: str, prompt: str, size: str | None,
            seconds: float | None, images: list[str], mask: str | None) -> dict:
    if kind in ("image", "video") and size is not None and not SIZE_RE.match(size):
        raise GenError(f"--size: want WxH like 1344x768 (got {size!r})", 1)
    if seconds is not None and not seconds > 0:
        raise GenError("--seconds: want a positive number of seconds", 1)
    if len(images) > MAX_IMAGES:
        raise GenError(f"an edit takes at most {MAX_IMAGES} pictures", 1)
    if mask and not images:
        raise GenError("a mask belongs to the first picture — send that picture too", 1)
    params: dict = {}
    if kind in ("image", "video"):
        params["size"] = size or DEFAULT_SIZE  # the fast default
    if seconds is not None:
        params["seconds"] = seconds
    body: dict = {
        "model": kind,  # a purpose key — this CLI never names a model
        "prompt": prompt,
        "enhance": True,  # always: a loud 400 if unconfigured, never a silent flat prompt
        "params": params,
    }
    if images:
        body["images"] = images
    if mask:
        body["mask"] = mask
    _status, blob, _ctype = _http("POST", base, "/jobs", body)
    try:
        job = json.loads(blob)
    except json.JSONDecodeError:
        raise GenError("the create returned no job record", 2) from None
    if not isinstance(job, dict) or not job.get("id"):
        raise GenError("the create returned a malformed job record", 2)
    return job


def _job(base: str, jid: str) -> dict:
    _status, blob, _ctype = _http("GET", base, f"/jobs/{urllib.parse.quote(jid)}")
    try:
        job = json.loads(blob)
    except json.JSONDecodeError:
        raise GenError("the job record is malformed", 2) from None
    if not isinstance(job, dict) or not job.get("id"):
        raise GenError(f"no such job: {jid}", 2)
    return job


def _wait(base: str, jid: str, current: dict | None = None) -> dict:
    """Block until the job is terminal — no clock of our own (the session's
    bash has no default timeout; the job's clock is server-side). A killed
    wait re-attaches with `gen wait <id>`; the job keeps running either way.
    ``current`` seeds the loop with a record already in hand (the create or
    the re-attach probe), so its first state is seen without an extra GET."""
    last: tuple | None = None
    while True:
        job = current if current is not None else _job(base, jid)
        current = None
        state = job.get("state")
        progress = job.get("progress")
        mark = (state, progress)
        if mark != last:
            line = f"job={jid} state={state}"
            if progress is not None:
                line += f" progress={progress}"
            if state == "queued" and job.get("error"):
                line += f" — {job['error']}"  # a QUEUED job's error is its wait reason
            print(line, file=sys.stderr, flush=True)
            last = mark
        if state in TERMINAL:
            return job
        time.sleep(POLL_SECS)


def _sniff_ext(blob: bytes, ctype: str) -> str:
    """The artifact's extension from magic bytes (the server's sniff,
    mirrored), Content-Type as the fallback."""
    if blob[:4] == b"\x89PNG":
        return ".png"
    if blob[:3] == b"\xff\xd8\xff":
        return ".jpg"
    if blob[:4] == b"RIFF" and blob[8:12] == b"WEBP":
        return ".webp"
    if blob[:4] == b"\x1a\x45\xdf\xa3":
        return ".webm"
    if len(blob) >= 12 and blob[4:8] == b"ftyp":
        return ".mp4"
    if "mp4" in ctype:
        return ".mp4"
    if "webm" in ctype:
        return ".webm"
    return ".bin"


def _workdir() -> Path:
    """The session workdir: the script's grandparent (the toolkit is the
    workdir's RO lower — <workdir>/bin/gen). The default outbox anchors
    here, never the cwd: an artifact under a scratch subdir would die
    with the session, invisible to the delivery outbox."""
    return Path(__file__).resolve().parent.parent


def _finish(base: str, job: dict, out: str | None) -> None:
    """Terminal job → the artifact in outbox (or --out) + one JSON line."""
    jid = job["id"]
    state = job.get("state")
    if state != "completed":
        raise GenError(
            f"job {jid} did not complete: {state}"
            + (f" — {job['error']}" if job.get("error") else ""),
            2,
        )
    _status, blob, ctype = _http("GET", base, f"/jobs/{jid}/content")
    if out is None:
        # the default anchors to the workdir (never the cwd) — see _workdir
        dest = str(_workdir() / "outbox" / f"{jid}{_sniff_ext(blob, ctype)}")
    else:
        dest = out
    try:
        parent = os.path.dirname(dest)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(dest, "wb") as f:
            f.write(blob)
    except OSError as e:
        raise GenError(f"could not save the artifact to {dest}: {e}", 1) from None
    result = {
        "id": jid,
        "state": state,
        "model": job.get("model"),
        "kind": job.get("kind"),
        "path": dest,
        "bytes": len(blob),
    }
    if job.get("enhanced_prompt"):
        result["enhanced_prompt"] = job["enhanced_prompt"]
    print(json.dumps(result), flush=True)


def _status_view(base: str, jid: str | None) -> None:
    if jid:
        job = _job(base, jid)
        line = f"job={job['id']} state={job.get('state')} kind={job.get('kind')} model={job.get('model')}"
        if job.get("progress") is not None:
            line += f" progress={job['progress']}"
        if job.get("error"):
            line += f" — {job['error']}"
        if (job.get("artifact") or {}).get("url"):
            line += " artifact=ready"
        print(line, flush=True)
        return
    # the machine door lists its own jobs only (never the operator's)
    _status, blob, _ctype = _http("GET", base, "/jobs?limit=10&filter=all")
    try:
        page = json.loads(blob)
    except json.JSONDecodeError:
        raise GenError("the queue listing is malformed", 2) from None
    jobs = page.get("jobs", []) if isinstance(page, dict) else []
    for job in jobs:
        if not isinstance(job, dict):
            continue
        line = f"job={job.get('id')} state={job.get('state')} kind={job.get('kind')} model={job.get('model')}"
        if job.get("error"):
            line += f" — {job.get('error')}"
        print(line, flush=True)


def main(argv: list[str] | None = None) -> int:
    p = _Parser(
        prog="gen",
        description="Make pictures and video, or edit pictures, via the panel "
                    "gen service. Output lands in outbox/ (or --out).",
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    for name, help_ in (("image", "make a picture"), ("video", "make a video")):
        sp = sub.add_parser(name, help=help_)
        sp.add_argument("--prompt", required=True, help="what to make, in plain words")
        sp.add_argument("--size", default=None,
                        help=f"WxH like 1344x768 (default {DEFAULT_SIZE} — fast; bigger is slower)")
        if name == "video":
            sp.add_argument("--seconds", type=float, default=None,
                            help="video length in seconds")
        sp.add_argument("--out", default=None, help="destination file (default outbox/<job-id><ext>)")

    sp = sub.add_parser("edit", help="edit picture(s)")
    sp.add_argument("--prompt", required=True, help="what to change, in plain words")
    sp.add_argument("--image", action="append", default=[],
                    help="source picture (repeatable)")
    sp.add_argument("--mask", default=None,
                    help="mask for the FIRST picture (same size as it)")
    sp.add_argument("--out", default=None, help="destination file (default outbox/<job-id><ext>)")

    sp = sub.add_parser("wait", help="wait for a job and save its artifact (re-attach)")
    sp.add_argument("job", help="the job id (job-000123)")
    sp.add_argument("--out", default=None, help="destination file (default outbox/<job-id><ext>)")

    sp = sub.add_parser("status", help="show one job, or the recent queue")
    sp.add_argument("job", nargs="?", default=None, help="a job id (omit: the queue)")

    try:
        args = p.parse_args(argv)  # usage errors are local (1): _Parser
        # the path join is exact: a trailing slash would 404 every route
        base = _env("SERVITOR_GEN_BASE_URL").rstrip("/")
        if not base.startswith(("http://", "https://")):
            raise GenError(
                f"SERVITOR_GEN_BASE_URL is not an absolute http(s) URL ({base!r}) — "
                "a config gap, not a retry",
                1,
            )
        if args.cmd == "status":
            _status_view(base, args.job)
        elif args.cmd == "wait":
            job = _job(base, args.job)
            if job.get("state") not in TERMINAL:
                job = _wait(base, args.job, job)
            _finish(base, job, args.out)
        else:
            images = [_data_url(x) for x in args.image] if args.cmd == "edit" else []
            mask = _data_url(args.mask) if args.cmd == "edit" and args.mask else None
            if args.cmd == "edit" and not images:
                raise GenError("an edit needs at least one --image", 1)
            job = _submit(
                base, args.cmd, args.prompt,
                getattr(args, "size", None), getattr(args, "seconds", None),
                images, mask,
            )
            # the id is the re-attach handle: print it BEFORE waiting, so a
            # killed call still leaves it in the transcript
            print(f"job={job['id']}", file=sys.stderr, flush=True)
            job = _wait(base, job["id"], job)
            _finish(base, job, args.out)
    except GenError as e:
        print(f"gen: {e}", file=sys.stderr, flush=True)
        return e.code
    return 0


if __name__ == "__main__":
    sys.exit(main())
