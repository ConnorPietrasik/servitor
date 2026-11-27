"""Unit: the toolkit's gen CLI (deploy/guest/toolkit/gen.py) against a mock
of the panel-api machine gen surface.

The mock implements the four /machine/gen routes (create POST /jobs, list
GET /jobs, record GET /jobs/<id>, content GET /jobs/<id>/content —
panel-api/main.py, the machine_gen router) with a tiny per-job state
machine: a created job walks
the state plan on each GET (queued → running → completed by default), a
QUEUED job's error carries a wait reason, artifacts are magic-byted per
kind so the CLI's ext sniff is exercised, and every route checks the
Bearer key (401 otherwise). The CLI runs as a subprocess from a per-test
copy at <tmp>/bin/gen — the deployed layout (the toolkit is the workdir's
RO lower, so the default outbox anchors to the script's grandparent),
which keeps the test out of the repo tree — with the same env contract
the spawn line gives it (SERVITOR_GEN_BASE_URL + OMNISSIAH_API_KEY,
nothing else load-bearing).
"""
from __future__ import annotations

import base64
import json
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent.parent
GEN = REPO / "deploy" / "guest" / "toolkit" / "gen.py"

KEY = "test-key"
PNG = b"\x89PNG\r\n\x1a\n" + b"img" * 10
WEBP = b"RIFF\x14\x00\x00\x00WEBPVP8 " + b"w" * 12
MP4 = b"\x00\x00\x00\x18ftypmp42" + b"v" * 40  # ≥12 bytes, ftyp at [4:8]
ARTIFACTS = {"image": (PNG, "image/png"), "video": (MP4, "video/mp4")}
WAIT_REASON = "waiting for the lane: slot gpu-gentp1a is on Krea2 (one model per slot at a time)"


class World:
    """The mock's state: job records + per-job state plans + every POST body."""

    def __init__(self) -> None:
        self.seq = 0
        self.jobs: dict[str, dict] = {}
        self.plans: dict[str, list[str]] = {}
        self.creates: list[dict] = []
        self.bork_list = False  # fault hook: the list route answers non-JSON

    def _record(self, jid: str, body: dict) -> dict:
        kind = "video" if body.get("model") == "video" else "image"
        return {
            "id": jid,
            "model": body.get("model"),
            "kind": kind,
            "prompt": body.get("prompt"),
            "enhance": body.get("enhance"),
            "params": body.get("params") or {},
            "user": "machine",
            "state": "queued",
            "slot": None,
            "enhanced_prompt": f"enhanced: {body.get('prompt')}",
            "progress": None,
            "error": WAIT_REASON,  # a QUEUED job's error is its wait reason
            "artifact": None,
            "edit": (
                {"images": [{"url": f"/gen/{jid}-in{i}.png"} for i in range(len(body["images"]))]}
                if body.get("images")
                else None
            ),
            "radiance_video_id": None,
            "created_at": 0.0,
            "started_at": None,
            "finished_at": None,
        }

    def create(self, body: dict) -> dict:
        self.seq += 1
        jid = f"job-{self.seq:06d}"
        job = self._record(jid, body)
        self.jobs[jid] = job
        # one queued tick (the wait reason rides it), then straight to done
        self.plans[jid] = ["queued", "completed"]
        self.creates.append(body)
        return job

    def seed(self, jid: str, model: str, states: list[str], error: str | None = None) -> dict:
        """A pre-existing job (the re-attach path): it reports ``states`` on
        successive GETs, then sticks on the last."""
        self.seq = max(self.seq, int(jid.rsplit("-", 1)[1]))
        body = {"model": model, "prompt": "seeded", "enhance": True, "params": {}}
        job = self._record(jid, body)
        if error:
            job["state"] = "failed"
            job["error"] = error
        self.jobs[jid] = job
        self.plans[jid] = list(states)
        return job


def make_handler(world: World):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args) -> None:  # no test noise
            pass

        def _auth(self) -> bool:
            return self.headers.get("Authorization") == f"Bearer {KEY}"

        def _send(self, code: int, body: bytes, ctype: str = "application/json") -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _json(self, code: int, obj) -> None:
            self._send(code, json.dumps(obj).encode())

        def do_GET(self) -> None:  # noqa: N802
            if not self._auth():
                return self._json(401, {"detail": "bad or absent key"})
            path = self.path
            if path.startswith("/machine/gen/jobs/"):
                tail = path[len("/machine/gen/jobs/"):]
                jid, _, sub = tail.partition("/")
                job = world.jobs.get(jid)
                if sub == "content":
                    if not job or job["state"] != "completed":
                        return self._json(404, {"detail": "no artifact for this job"})
                    blob, ctype = ARTIFACTS[job["kind"]]
                    return self._send(200, blob, ctype)
                if not job:
                    return self._json(404, {"detail": "no such job"})
                plan = world.plans.get(jid, [])
                if plan:  # advance the state machine, one step per GET
                    job["state"] = plan.pop(0)
                    if job["state"] == "completed":
                        job["artifact"] = {"url": f"/gen/{job['id']}.png", "mime": "image/png"}
                        job["error"] = None
                        job["progress"] = 1.0
                    elif job["state"] == "running":
                        job["progress"] = 0.5
                    elif job["state"] == "queued":
                        job["progress"] = None
                return self._json(200, job)
            if path.startswith("/machine/gen/jobs"):
                if world.bork_list:
                    return self._send(200, b"not-json", "application/json")
                return self._json(200, {"jobs": list(world.jobs.values()), "next": None})
            return self._json(404, {"detail": "no route"})

        def do_POST(self) -> None:  # noqa: N802
            if not self._auth():
                return self._json(401, {"detail": "bad or absent key"})
            if self.path == "/machine/gen/jobs":
                n = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(n))
                return self._json(201, world.create(body))
            return self._json(404, {"detail": "no route"})

    return Handler


@pytest.fixture()
def world():
    w = World()
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(w))
    w.port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield w
    server.shutdown()
    server.server_close()


def run_gen(world, *args: str, tmp_path: Path, key: str = KEY, base_url: str | None = None,
            cwd: Path | None = None):
    base = base_url or f"http://127.0.0.1:{world.port}/machine/gen"
    # the deployed layout: the toolkit is the workdir's RO lower, so the
    # CLI runs as <workdir>/bin/gen — the default outbox anchors to the
    # script's grandparent (tmp_path here), never the cwd
    gen = tmp_path / "bin" / "gen"
    gen.parent.mkdir(parents=True, exist_ok=True)
    gen.write_text(GEN.read_text())
    gen.chmod(0o755)
    env = {
        # the spawn line's shape: an explicit allowlist, never the test's
        # inherited environment (PATH is moot — the script spawns nothing)
        "HOME": str(tmp_path),
        "SERVITOR_GEN_BASE_URL": base,
    }
    if key is not None:
        env["OMNISSIAH_API_KEY"] = key
    return subprocess.run(
        [sys.executable, str(gen), *args],
        env=env,
        cwd=str(cwd or tmp_path),
        capture_output=True,
        text=True,
        timeout=120,
    )


# --------------------------------------------------------------------------- #
# the happy paths: submit → wait → artifact in outbox/                         #
# --------------------------------------------------------------------------- #


def test_image_defaults_and_download(world, tmp_path):
    r = run_gen(world, "image", "--prompt", "a cat in a hat", tmp_path=tmp_path)
    assert r.returncode == 0, r.stderr
    dest = tmp_path / "outbox" / "job-000001.png"
    assert dest.read_bytes() == PNG  # the artifact's magic bytes, sniffed to .png
    # the final line is the machine-readable result
    result = json.loads(r.stdout.strip().splitlines()[-1])
    assert result["id"] == "job-000001"
    assert result["state"] == "completed"
    # the default is workdir-anchored: an absolute path, never cwd-relative
    assert result["path"] == str(tmp_path / "outbox" / "job-000001.png")
    assert result["bytes"] == len(PNG)
    assert "enhanced_prompt" in result  # always-enhanced: the rewrite is surfaced
    # the create: purpose key (never a model), always enhance, the fast default size
    body = world.creates[0]
    assert body["model"] == "image"
    assert body["enhance"] is True
    assert body["params"] == {"size": "512x512"}
    # the id is printed before the wait (the re-attach handle for a killed call)
    assert "job=job-000001" in r.stderr
    assert "queued" in r.stderr and WAIT_REASON in r.stderr
    assert "completed" in r.stderr


def test_size_override_rides_the_request(world, tmp_path):
    r = run_gen(world, "image", "--prompt", "a lighthouse", "--size", "2048x2048",
                tmp_path=tmp_path)
    assert r.returncode == 0, r.stderr
    assert world.creates[0]["params"]["size"] == "2048x2048"


def test_video_seconds_and_mp4_sniff(world, tmp_path):
    r = run_gen(world, "video", "--prompt", "waves at dusk", "--seconds", "6",
                tmp_path=tmp_path)
    assert r.returncode == 0, r.stderr
    body = world.creates[0]
    assert body["model"] == "video"
    assert body["params"] == {"size": "512x512", "seconds": 6.0}
    dest = tmp_path / "outbox" / "job-000001.mp4"  # ftyp magic → .mp4
    assert dest.read_bytes() == MP4


def test_out_flag_controls_the_name(world, tmp_path):
    r = run_gen(world, "image", "--prompt", "x", "--out", "art/hero.png",
                tmp_path=tmp_path)
    assert r.returncode == 0, r.stderr
    assert (tmp_path / "art" / "hero.png").read_bytes() == PNG


def test_unwritable_destination_is_a_clean_local_error(world, tmp_path):
    # a plain file blocks the --out parent: the save failure is a local
    # (1) naming the destination, never a traceback (the same class covers
    # a full disk and the baked-path /opt/outbox PermissionError)
    blocker = tmp_path / "blocker"
    blocker.write_bytes(b"a file")
    r = run_gen(world, "image", "--prompt", "x", "--out", "blocker/x.png",
                tmp_path=tmp_path)
    assert r.returncode == 1
    assert "could not save the artifact" in r.stderr
    assert "blocker/x.png" in r.stderr  # the destination, named (--out is cwd-relative)
    assert "Traceback" not in r.stderr


def test_default_outbox_anchors_to_the_workdir_not_the_cwd(world, tmp_path):
    # a scratch-subdir cwd must not divert the artifact off the delivery
    # outbox: the default anchors to the workdir (the script's grandparent)
    sub = tmp_path / "data"
    sub.mkdir()
    r = run_gen(world, "image", "--prompt", "x", tmp_path=tmp_path, cwd=sub)
    assert r.returncode == 0, r.stderr
    result = json.loads(r.stdout.strip().splitlines()[-1])
    assert result["path"] == str(tmp_path / "outbox" / "job-000001.png")
    assert (tmp_path / "outbox" / "job-000001.png").read_bytes() == PNG
    assert not (sub / "outbox").exists()


# --------------------------------------------------------------------------- #
# the edit path: local files → data URLs, the mirrored doors                   #
# --------------------------------------------------------------------------- #


def test_edit_data_urls_and_mask(world, tmp_path):
    photo = tmp_path / "photo.png"
    photo.write_bytes(PNG)
    second = tmp_path / "second.webp"
    second.write_bytes(WEBP)
    mask = tmp_path / "mask.png"
    mask.write_bytes(PNG)
    r = run_gen(
        world, "edit", "--prompt", "remove the dog",
        "--image", str(photo), "--image", str(second), "--mask", str(mask),
        tmp_path=tmp_path,
    )
    assert r.returncode == 0, r.stderr
    body = world.creates[0]
    assert body["model"] == "edit"
    assert body["enhance"] is True
    assert "size" not in body["params"]  # an edit follows the source picture
    assert body["images"][0].startswith("data:image/png;base64,")
    assert base64.b64decode(body["images"][0].split(",", 1)[1]) == PNG
    assert body["images"][1].startswith("data:image/webp;base64,")
    assert body["mask"].startswith("data:image/png;base64,")


def test_edit_doors_fail_fast_locally(world, tmp_path):
    photo = tmp_path / "photo.png"
    photo.write_bytes(PNG)
    # more than 10 pictures (the server's cap, mirrored)
    images = ["--image", str(photo)] * 11
    r = run_gen(world, "edit", "--prompt", "x", *images, tmp_path=tmp_path)
    assert r.returncode == 1
    assert "at most 10" in r.stderr
    assert world.creates == []  # nothing reached the server
    # an unknown format is NOT a local door (the server decodes the
    # bytes and owns the format door): no local rejection, whatever mime
    # the extension maps to — the server's 400 is the verdict
    bmp = tmp_path / "x.bmp"
    bmp.write_bytes(b"BM" + b"0" * 16)
    r = run_gen(world, "edit", "--prompt", "x", "--image", str(bmp), tmp_path=tmp_path)
    assert r.returncode == 0, r.stderr
    assert world.creates[-1]["images"][0].startswith("data:")
    # an extensionless file: no mime to guess — rides as octet-stream
    raw = tmp_path / "photo"
    raw.write_bytes(PNG)
    r = run_gen(world, "edit", "--prompt", "x", "--image", str(raw), tmp_path=tmp_path)
    assert r.returncode == 0, r.stderr
    assert world.creates[-1]["images"][0].startswith("data:application/octet-stream;base64,")
    # an oversize picture (the server's 16 MiB door, mirrored)
    big = tmp_path / "big.png"
    big.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * (17 * 1024 * 1024))
    r = run_gen(world, "edit", "--prompt", "x", "--image", str(big), tmp_path=tmp_path)
    assert r.returncode == 1
    assert "at most 16 MiB" in r.stderr
    # an edit without a picture
    r = run_gen(world, "edit", "--prompt", "x", tmp_path=tmp_path)
    assert r.returncode == 1
    assert "needs at least one --image" in r.stderr


def test_missing_picture_is_local(world, tmp_path):
    r = run_gen(world, "edit", "--prompt", "x", "--image", "nope.jpg", tmp_path=tmp_path)
    assert r.returncode == 1
    assert "not found" in r.stderr


# --------------------------------------------------------------------------- #
# job tracking: wait re-attach, status, failure                                #
# --------------------------------------------------------------------------- #


def test_wait_reattaches_to_a_running_job(world, tmp_path):
    world.seed("job-000042", "video", ["running", "completed"])
    r = run_gen(world, "wait", "job-000042", "--out", "clipped.mp4", tmp_path=tmp_path)
    assert r.returncode == 0, r.stderr
    assert (tmp_path / "clipped.mp4").read_bytes() == MP4
    assert "running" in r.stderr and "completed" in r.stderr


def test_wait_on_a_failed_job_reports_the_error(world, tmp_path):
    world.seed("job-000043", "image", [], error="radiance: the prompt is too long for this model")
    r = run_gen(world, "wait", "job-000043", tmp_path=tmp_path)
    assert r.returncode == 2
    assert "did not complete: failed" in r.stderr
    assert "too long" in r.stderr


def test_unknown_job_is_a_server_error(world, tmp_path):
    r = run_gen(world, "wait", "job-999999", tmp_path=tmp_path)
    assert r.returncode == 2
    assert "404" in r.stderr


def test_malformed_queue_listing_is_a_server_error(world, tmp_path):
    # a non-JSON listing is the server's verdict: the clean code-2
    # message, never a traceback
    world.bork_list = True
    r = run_gen(world, "status", tmp_path=tmp_path)
    assert r.returncode == 2
    assert "queue listing is malformed" in r.stderr
    assert "Traceback" not in r.stderr


def test_status_one_job_and_queue(world, tmp_path):
    world.seed("job-000044", "image", ["queued"])
    r = run_gen(world, "status", "job-000044", tmp_path=tmp_path)
    assert r.returncode == 0, r.stderr
    assert "job=job-000044" in r.stdout and "queued" in r.stdout
    r = run_gen(world, "status", tmp_path=tmp_path)
    assert r.returncode == 0, r.stderr
    assert "job=job-000044" in r.stdout


# --------------------------------------------------------------------------- #
# config: env fail-closed, auth, reachability                                  #
# --------------------------------------------------------------------------- #


def test_missing_env_fails_closed(world, tmp_path):
    r = run_gen(world, "image", "--prompt", "x", tmp_path=tmp_path, key=None)
    assert r.returncode == 1
    assert "OMNISSIAH_API_KEY" in r.stderr
    # and the base URL (a closed port is unreachable; a missing name is local)
    env = {
        "HOME": str(tmp_path),
        "OMNISSIAH_API_KEY": KEY,
    }
    r = subprocess.run(
        [sys.executable, str(GEN), "status"],
        env=env, cwd=str(tmp_path), capture_output=True, text=True, timeout=60,
    )
    assert r.returncode == 1
    assert "SERVITOR_GEN_BASE_URL" in r.stderr


def test_wrong_key_is_rejected(world, tmp_path):
    r = run_gen(world, "image", "--prompt", "x", tmp_path=tmp_path, key="wrong")
    assert r.returncode == 2
    assert "401" in r.stderr
    assert "rejected the key" in r.stderr
    # the key must not leak into any output
    assert "wrong" not in r.stdout


def test_unreachable_service(world, tmp_path):
    r = run_gen(world, "image", "--prompt", "x", tmp_path=tmp_path,
                base_url="http://127.0.0.1:1/machine/gen")
    assert r.returncode == 2
    assert "cannot reach" in r.stderr


def test_mis_set_base_url_is_a_clean_local_error(world, tmp_path):
    # a scheme-less URL is an env mis-set: the clean message, the local
    # code (1), nothing reaches the server, never a traceback
    r = run_gen(world, "image", "--prompt", "x", tmp_path=tmp_path,
                base_url="panel.local/machine/gen")
    assert r.returncode == 1
    assert "not an absolute http(s) URL" in r.stderr
    assert "Traceback" not in r.stderr
    assert world.creates == []


def test_trailing_slash_base_url_is_normalized(world, tmp_path):
    # a trailing slash in the env URL would 404 every path join — the CLI
    # normalizes it before building any path
    r = run_gen(world, "image", "--prompt", "x", tmp_path=tmp_path,
                base_url=f"http://127.0.0.1:{world.port}/machine/gen/")
    assert r.returncode == 0, r.stderr
    assert (tmp_path / "outbox" / "job-000001.png").read_bytes() == PNG


def test_usage_errors_exit_one_not_two(world, tmp_path):
    # the contract: 1 local/usage, 2 server — argparse's default exit 2
    # would misread a typo as a server failure
    for argv in (
        (),  # no subcommand
        ("frobnicate",),  # invalid choice
        ("image",),  # required --prompt missing
        ("image", "--bogus"),  # unrecognized flag
        ("wait",),  # required job id missing
        ("edit", "--prompt", "x", "--image", "a.png", "--size", "512x512"),  # --size is not an edit flag
    ):
        r = run_gen(world, *argv, tmp_path=tmp_path)
        assert r.returncode == 1, (argv, r.returncode, r.stderr)
        assert "Traceback" not in r.stderr
        assert "usage:" in r.stderr  # the usage line, then the gen: message
