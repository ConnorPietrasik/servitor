"""Lifecycle gate: session registry + lifecycle.

The gates, verbatim:
- app-crash/restart drill: kill -9 the app mid-run — restart ⇒ boot
  reconciliation kills any orphan via the ladder, registry reconciles,
  no double slot allocation, session marked interrupted, transcript not
  interleaved; pickup respawns from transcript as the sole process.
- probe rules: list = registry only, O(1) (zero pi processes); probe as
  slot uid with the run mounts; one process per session file; probe
  never first to touch an agent dir.
- idle-kill never fires mid-run/mid-compaction and never unmounts (a
  long synthetic run that outlasts the idle threshold is never killed —
  compressed timescale: the rule is threshold-independent).
- respawn of a transcript-less row = "idle, nothing to resume", not an
  error.
- planted-extension test: drop an extension into the agent upper,
  respawn, assert the warning lands in the event feed + registry and
  the run is unaffected (warn, never block).
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
import uuid
from pathlib import Path

import pytest

from app.iso.spawn import slot_identity
from test.conftest import require_root_or_skip
from test.conformance.install import resolve_pi
from test.mock_router.server import MockRouter

REPO = Path(__file__).resolve().parents[2]
AGENT_CONFIG = REPO / "agent-config"
SLOT = "slot1"


# ---------------------------------------------------------------------- #
# app client                                                              #
# ---------------------------------------------------------------------- #


class AppClient:
    """JSON-lines client for `python -m app.main` (the stdio app)."""

    def __init__(self, proc: subprocess.Popen):
        self.proc = proc
        self._next_id = 0
        self.buf = b""
        self._lock = __import__("threading").Lock()

    def _read_line(self, timeout: float | None = None) -> bytes:
        while b"\n" not in self.buf:
            if timeout is not None:
                import select

                r, _, _ = select.select([self.proc.stdout], [], [], timeout)
                if not r:
                    raise TimeoutError("app did not respond in time")
            chunk = os.read(self.proc.stdout.fileno(), 65536)
            if not chunk:
                raise RuntimeError("app stdout closed")
            self.buf += chunk
        line, self.buf = self.buf.split(b"\n", 1)
        return line

    def send(self, cmd: str, args: dict | None = None, timeout: float = 90.0) -> dict:
        with self._lock:  # the stdio protocol is one request at a time
            self._next_id += 1
            msg = {"id": self._next_id, "cmd": cmd, "args": args or {}}
            self.proc.stdin.write((json.dumps(msg) + "\n").encode())
            self.proc.stdin.flush()
            deadline = time.time() + timeout
            while True:
                remaining = max(0.1, deadline - time.time())
                line = self._read_line(remaining if timeout else None)
                if not line.strip():
                    continue
                resp = json.loads(line)
                if resp.get("id") == self._next_id:
                    if not resp.get("ok"):
                        raise RuntimeError(f"{cmd} failed: {resp.get('error')}")
                    return resp["result"]
                # out-of-band lines (shouldn't happen on this protocol)
                continue

    def alive(self) -> bool:
        return self.proc.poll() is None


def start_app(state_root: Path, *, pi, slot: str, router: MockRouter, run_root: Path,
              idle_kill_sec: float = 3600.0, shutdown_grace_sec: float = 5.0,
              extra: list[str] | None = None) -> AppClient:
    argv = [
        "/usr/bin/env", "python3", "-m", "app.main",
        "--state", str(state_root / "state"),
        "--config-src", str(AGENT_CONFIG),
        "--router-url", router.base_url,
        "--pi-node", pi.node_bin,
        "--pi-cli", str(pi.cli_js),
        "--pi-ext", str(pi.ext_path),
        "--toolkit", str(run_root / "toolkit"),
        "--slots", slot,
        "--idle-kill-sec", str(idle_kill_sec),
        "--shutdown-grace-sec", str(shutdown_grace_sec),
        "--gc-interval-sec", "30",
        "--stdio",
    ]
    if extra:
        argv += extra
    env = dict(os.environ)
    env["OMNISSIAH_API_KEY"] = "test-key"
    env["PYTHONPATH"] = str(REPO)
    err = open(run_root / f"app-stderr-{uuid.uuid4().hex[:8]}.log", "wb")
    proc = subprocess.Popen(
        argv, cwd=str(REPO), env=env,
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=err,
    )
    return AppClient(proc)


# ---------------------------------------------------------------------- #
# fixtures / helpers                                                      #
# ---------------------------------------------------------------------- #


def _slot_uid_pids(uid: int) -> list[int]:
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
            continue
    return out


def _findmnt(path: str) -> bool:
    r = subprocess.run(["findmnt", "-T", path, "-n"], capture_output=True, text=True)
    return bool(r.stdout.strip())


def _transcript_lines(session_file: str) -> list[dict]:
    lines = []
    for raw in Path(session_file).read_text().splitlines():
        if raw.strip():
            lines.append(json.loads(raw))  # raises on interleaved/garbled
    return lines


@pytest.fixture
def stage2_env(run_root):
    """Per-test state (slot-traversable ext4) + toolkit + pi install."""
    require_root_or_skip("stage2 lifecycle (setuid spawn, mounts)")
    # env-driven selections (the same pattern as the conformance pi_install
    # fixture) — never a stale pin: this install is what the test asserts against
    pi = resolve_pi(
        run_root / "pi-install",
        os.environ.get("SERVITOR_PI_VERSION"),
        os.environ.get("SERVITOR_PI_WEB_ACCESS_VERSION"),
        # `or`-semantics: a blank .env line (SERVITOR_PI_NODE_BIN=) is exported
        # EMPTY by the Makefile ENV_LOAD and must not defeat the default
        node_bin=os.environ.get("SERVITOR_PI_NODE_BIN") or "/usr/bin/node",
    )
    state_root = Path(f"/home/{SLOT}/servitor-test/run-{uuid.uuid4().hex[:8]}")
    (state_root / "state").mkdir(parents=True, exist_ok=True)
    toolkit = run_root / "toolkit"
    toolkit.mkdir(parents=True, exist_ok=True)
    (toolkit / "toolkit-file.txt").write_text("lower file\n")
    env = {
        "state_root": state_root,
        "pi": pi,
        "run_root": run_root,
        "state": state_root / "state",
    }
    yield env
    for root in (state_root,):
        shutil.rmtree(root, ignore_errors=True)


# ---------------------------------------------------------------------- #
# the crash drill                                                         #
# ---------------------------------------------------------------------- #


def test_crash_restart_drill(stage2_env):
    """kill -9 the app mid-run with the mock's long tool call still in
    flight. The orphan's stdin closes at app death; pi's EOF shutdown
    aborts the in-flight run and exits. Restart ⇒ boot reconciliation:
    any still-alive orphan is killed via the ladder, the registry
    reconciles (interrupted), no double slot allocation, the transcript
    is not interleaved, and pickup respawns as the sole process."""
    env = stage2_env
    state_root, pi, run_root = env["state_root"], env["pi"], env["run_root"]
    uid = slot_identity(SLOT).uid

    router = MockRouter()
    router.start()
    # the long tool call: bash sleep 20 — in flight when the app dies
    router.set_script({"gap_seconds": 0.5, "tools": [{"name": "bash", "arguments": {"command": "sleep 20"}}]})

    app = start_app(state_root, pi=pi, slot=SLOT, router=router, run_root=run_root,
                    shutdown_grace_sec=5.0)
    try:
        res = app.send("start_session", {"display_name": "drill"})
        sid = res["session_id"]
        assert res["slot"] == SLOT
        # a stable session: pinned slot — the drill's no-double-allocation
        # case (slot pressure must not steal it after the crash)
        app.send("set_stable", {"session_id": sid, "stable": True})

        # a run with a long tool call in flight (sleep 20)
        d = app.send("prompt", {"session_id": sid, "text": "run the tool"})
        assert d["disposition"] == "started"

        # wait for the tool call to be issued (request 1 at the mock)
        deadline = time.time() + 30
        while len(router.requests()) < 1 and time.time() < deadline:
            time.sleep(0.2)
        assert len(router.requests()) >= 1
        time.sleep(2.0)  # the bash sleep 20 is now in flight

        # the child is alive (slot uid)
        assert _slot_uid_pids(uid), "no slot-uid process mid-run"

        # KILL -9 THE APP — the drain never runs. The crash sends the
        # orphan no signal (SIGKILL is not propagated): the orphan's
        # stdin simply closes, and pi's EOF shutdown aborts the in-flight
        # run and exits. What is deterministic and asserted: the run
        # never completed (no second request), and after restart nothing
        # slot-uid is alive, the registry is reconciled (interrupted),
        # and the session survived for pickup.
        app.proc.kill()
        app.proc.wait()

        # the orphan must be gone on its own (EOF shutdown); the aborted
        # run must never have completed (no second request at the mock)
        deadline = time.time() + 20
        while _slot_uid_pids(uid) and time.time() < deadline:
            time.sleep(0.3)
        assert not _slot_uid_pids(uid), "orphan survived (reconciliation window)"
        assert len(router.requests()) == 1, "the aborted run must not have completed"

        # RESTART: boot reconciliation reconciles the registry. The
        # orphan self-exited in this timing; if it had still been alive
        # (a slower shutdown), the ladder kills it — boot_report records
        # either case
        app2 = start_app(state_root, pi=pi, slot=SLOT, router=router, run_root=run_root,
                         shutdown_grace_sec=5.0)
        try:
            app2.send("boot_report")
            rows = {r["id"]: r for r in app2.send("list_sessions")["sessions"]}
            assert sid in rows
            assert rows[sid]["interrupted"] is True
            assert rows[sid]["state"] == "live"
            assert rows[sid]["slot"] == SLOT
            assert rows[sid]["stable"] is True
            assert rows[sid]["session_file"] and os.path.exists(rows[sid]["session_file"])
            assert not _slot_uid_pids(uid)

            # transcript not interleaved: every line parses, header first,
            # the single prompt exactly once, the aborted run's trace at
            # the tail
            lines = _transcript_lines(rows[sid]["session_file"])
            assert lines[0].get("type") == "session"
            flat = json.dumps(lines, ensure_ascii=False)
            assert flat.count('"run the tool"') == 1
            from app.lifecycle import transcript_unsettled

            assert transcript_unsettled(rows[sid]["session_file"]), "crash signature missing"

            # NO DOUBLE SLOT ALLOCATION: slot1 is still pinned by the
            # crashed stable session; a new session is REFUSED (no slot
            # and nothing releasable) — and leaves no row behind (there
            # is no queue: a parked row could neither be woken nor
            # released)
            with pytest.raises(RuntimeError, match="NoFreeSlot"):
                app2.send("start_session", {"display_name": "second"})
            rows2 = {r["id"]: r for r in app2.send("list_sessions")["sessions"]}
            assert len(rows2) == 1, "the refused start must not leave a row"
            assert rows2[sid]["slot"] == SLOT, "the crashed session lost its slot"

            # PICKUP: respawn from the transcript as the sole process
            d2 = app2.send("prompt", {"session_id": sid, "text": "second run"})
            assert d2["disposition"] == "started"
            time.sleep(1.0)
            procs = _slot_uid_pids(uid)
            assert len(procs) == 1, f"expected exactly one process, got {procs}"
            settle = app2.send("settle", {"session_id": sid}, timeout=90)
            assert settle["aborted"] is False

            # transcript not interleaved after the pickup
            lines2 = _transcript_lines(rows[sid]["session_file"])
            flat2 = json.dumps(lines2, ensure_ascii=False)
            assert flat2.count('"second run"') == 1
            assert flat2.count('"run the tool"') == 1
            for ln in lines2:
                assert isinstance(ln.get("type"), str)  # well-formed records
        finally:
            if app2.alive():
                try:
                    app2.send("shutdown", {"cause": "test end"}, timeout=30)
                except Exception:
                    app2.proc.kill()
                app2.proc.wait()
    finally:
        if app.alive():
            app.proc.kill()
            app.proc.wait()
        router.stop()


# ---------------------------------------------------------------------- #
# probe rules                                                             #
# ---------------------------------------------------------------------- #


def test_probe_rules(stage2_env):
    """list = registry only (zero pi processes); probe as slot uid with
    the run mounts; one process per session file; probe never first to
    touch an agent dir (transcript-less row refused)."""
    env = stage2_env
    state_root, pi, run_root = env["state_root"], env["pi"], env["run_root"]
    state = env["state"]
    uid = slot_identity(SLOT).uid

    router = MockRouter()
    router.start()
    app = start_app(state_root, pi=pi, slot=SLOT, router=router, run_root=run_root,
                    idle_kill_sec=1.0, shutdown_grace_sec=5.0)
    try:
        sid = app.send("start_session")["session_id"]
        app.send("prompt", {"session_id": sid, "text": "hello probe"})
        app.send("settle", {"session_id": sid}, timeout=90)

        # list = registry only, O(1): list_sessions must not WAKE a pi
        # process (a resident idle process is the idle-kill contract,
        # not the list's)
        before = set(_slot_uid_pids(uid))
        listed = app.send("list_sessions")["sessions"]
        after = set(_slot_uid_pids(uid))
        assert any(r["id"] == sid for r in listed)
        assert after == before, "list_sessions woke a pi process"

        # idle-kill (1 s) to get a dead process with mounts kept
        time.sleep(1.5)
        app.send("idle_kill_tick")
        assert not _slot_uid_pids(uid)
        sp_workdir = f"{state}/scratch/{sid}/workdir"
        assert _findmnt(sp_workdir), "idle-kill unmounted (it must never)"

        # the probe: as slot uid, one process per session file
        seen_pids: set[int] = set()
        import threading

        stop = []

        def _watcher_thread():
            while not stop:
                seen_pids.update(_slot_uid_pids(uid))
                time.sleep(0.05)

        t = threading.Thread(target=_watcher_thread, daemon=True)
        t.start()
        msgs = app.send("probe", {"session_id": sid, "which": "messages"}, timeout=60)
        stop.append(1)
        t.join(timeout=2)

        assert msgs, "probe returned nothing"
        assert seen_pids, "no slot-uid process was observed during the probe"
        assert len(seen_pids) == 1, f"one process per session file; saw {seen_pids}"
        assert not _slot_uid_pids(uid), "probe process did not exit"
        # the probe's transcript read saw the history
        flat = json.dumps(msgs, ensure_ascii=False)
        assert "hello probe" in flat

        # probe is NEVER first to touch an agent dir: a transcript-less
        # row is refused (create the row directly in the registry)
        from app.registry import Registry, ALLOCATED

        reg = Registry(state / "registry.db")
        ghost = str(uuid.uuid4())
        reg.upsert_session(ghost, slot=SLOT, state=ALLOCATED)
        reg.close()
        with pytest.raises(RuntimeError, match="no transcript"):
            app.send("probe", {"session_id": ghost, "which": "messages"}, timeout=30)
        reg = Registry(state / "registry.db")
        reg.delete(ghost)
        reg.close()

        # two concurrent probes coalesce (one process per session file)
        import concurrent.futures

        with concurrent.futures.ThreadPoolExecutor(2) as ex:
            f1 = ex.submit(app.send, "probe", {"session_id": sid, "which": "stats"}, 60)
            f2 = ex.submit(app.send, "probe", {"session_id": sid, "which": "stats"}, 60)
            r1, r2 = f1.result(), f2.result()
        assert r1["result"] == r2["result"]
    finally:
        if app.alive():
            try:
                app.send("shutdown", {"cause": "test end"}, timeout=30)
            except Exception:
                app.proc.kill()
            app.proc.wait()
        router.stop()


# ---------------------------------------------------------------------- #
# idle-kill                                                               #
# ---------------------------------------------------------------------- #


def test_idle_kill_never_mid_run_never_unmounts(stage2_env):
    """A long synthetic run (long scripted tool call via the mock) that
    outlasts the idle threshold is NEVER killed — the kill fires only
    after agent_settled. And it never unmounts."""
    env = stage2_env
    state_root, pi, run_root = env["state_root"], env["pi"], env["run_root"]
    state = env["state"]
    uid = slot_identity(SLOT).uid

    router = MockRouter()
    router.start()
    router.set_script({"tools": [{"name": "bash", "arguments": {"command": "sleep 8"}}]})
    app = start_app(state_root, pi=pi, slot=SLOT, router=router, run_root=run_root,
                    idle_kill_sec=2.0, shutdown_grace_sec=5.0)
    try:
        sid = app.send("start_session")["session_id"]
        app.send("prompt", {"session_id": sid, "text": "long run"})

        # mid-run, well past the 2 s idle threshold: the tick must NOT kill
        deadline = time.time() + 30
        while len(router.requests()) < 1 and time.time() < deadline:
            time.sleep(0.2)
        time.sleep(3.0)  # run (sleep 8) active, threshold long passed
        killed = app.send("idle_kill_tick")["killed"]
        assert killed == [], f"mid-run idle-kill fired: {killed}"
        assert _slot_uid_pids(uid), "the run was killed mid-run"

        # it finishes (agent_settled), and only then is it eligible
        app.send("settle", {"session_id": sid}, timeout=120)
        time.sleep(2.5)  # settled past the threshold
        killed = app.send("idle_kill_tick")["killed"]
        assert killed == [sid]
        assert not _slot_uid_pids(uid), "process survived the idle-kill"
        assert _findmnt(f"{state}/scratch/{sid}/workdir"), "idle-kill unmounted"

        # respawn on pickup: mounts reused, transcript intact
        d = app.send("prompt", {"session_id": sid, "text": "after idle kill"})
        assert d["disposition"] == "started"
        app.send("settle", {"session_id": sid}, timeout=120)
    finally:
        if app.alive():
            try:
                app.send("shutdown", {"cause": "test end"}, timeout=30)
            except Exception:
                app.proc.kill()
            app.proc.wait()
        router.stop()


# ---------------------------------------------------------------------- #
# transcript-less row                                                     #
# ---------------------------------------------------------------------- #


def test_respawn_transcriptless_is_idle_not_error(stage2_env):
    """A registry row without a transcript: prompt/pickup =
    'idle, nothing to resume' — a disposition, not an error; no process
    is spawned."""
    env = stage2_env
    state_root, pi, run_root = env["state_root"], env["pi"], env["run_root"]
    state = env["state"]
    uid = slot_identity(SLOT).uid

    router = MockRouter()
    router.start()
    app = start_app(state_root, pi=pi, slot=SLOT, router=router, run_root=run_root)
    try:
        from app.registry import Registry, ALLOCATED

        reg = Registry(state / "registry.db")
        ghost = str(uuid.uuid4())
        reg.upsert_session(ghost, display_name="ghost", slot=SLOT, state=ALLOCATED)
        reg.close()

        d = app.send("prompt", {"session_id": ghost, "text": "resume?"})
        assert d["disposition"] == "idle_no_transcript"
        assert not _slot_uid_pids(uid), "a transcript-less pickup spawned a process"
        assert len(router.requests()) == 0, "a transcript-less pickup called the model"

        # probe is refused too (first-touch rule)
        with pytest.raises(RuntimeError, match="no transcript"):
            app.send("probe", {"session_id": ghost, "which": "messages"}, timeout=30)

        reg = Registry(state / "registry.db")
        reg.delete(ghost)
        reg.close()
    finally:
        if app.alive():
            try:
                app.send("shutdown", {"cause": "test end"}, timeout=30)
            except Exception:
                app.proc.kill()
            app.proc.wait()
        router.stop()


# ---------------------------------------------------------------------- #
# planted extension (warn, never block)                               #
# ---------------------------------------------------------------------- #


PLANTED = """import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
export default function (pi: ExtensionAPI) {
  pi.registerCommand("svplanted", {
    description: "planted extension test",
    handler: async () => "planted-ok",
  });
}
"""


def test_planted_extension_warns_and_run_unaffected(stage2_env):
    """Drop an extension into the agent upper, respawn, assert the warning
    lands in the event feed + registry and the run is unaffected (pi
    loaded it — warn, never block)."""
    env = stage2_env
    state_root, pi, run_root = env["state_root"], env["pi"], env["run_root"]
    state = env["state"]
    uid = slot_identity(SLOT).uid

    router = MockRouter()
    router.start()
    app = start_app(
        state_root, pi=pi, slot=SLOT, router=router, run_root=run_root,
        idle_kill_sec=1.0, shutdown_grace_sec=5.0,
        extra=["--baseline-ext", "pi-web-access"],
    )
    try:
        sid = app.send("start_session")["session_id"]
        app.send("prompt", {"session_id": sid, "text": "first run"})
        app.send("settle", {"session_id": sid}, timeout=90)
        cmds = app.send("commands", {"session_id": sid})["commands"]
        assert "svplanted" not in cmds, "planted before it was planted"

        # plant the extension THROUGH the merged agent dir — exactly how
        # the agent does it (its writes land in the upper). Creating it
        # directly in the upperdir would hit a kernel overlayfs quirk:
        # pi's startup scan already looked up the absent `extensions`
        # dir (cached negative dentry), and an externally created upper
        # entry stays invisible to lookups until the overlay is remounted.
        ext_dir = Path(state) / "scratch" / sid / "agent-dir" / "extensions"
        ext_dir.mkdir(parents=True, exist_ok=True)
        planted = ext_dir / "planted.ts"
        planted.write_text(PLANTED)
        os.chown(planted, uid, uid)
        os.chown(ext_dir, uid, uid)

        # kill the process, respawn (pickup), the warning must land
        time.sleep(1.5)
        app.send("idle_kill_tick")
        d = app.send("prompt", {"session_id": sid, "text": "second run"})
        assert d["disposition"] == "started"
        app.send("settle", {"session_id": sid}, timeout=90)  # run unaffected

        events = app.send("events")["items"]
        planted_events = [e for e in events if e["kind"] == "new_extension" and e["session_id"] == sid]
        assert planted_events, f"no new-extension warning in the feed: {events[-5:]}"
        assert any("planted.ts" in e["detail"] for e in planted_events)

        rows = {r["id"]: r for r in app.send("list_sessions")["sessions"]}
        assert "agent:planted.ts" in rows[sid]["new_extensions"]

        # the run is unaffected: pi LOADED the planted extension (its
        # command is registered) and the session still works
        cmds2 = app.send("commands", {"session_id": sid})["commands"]
        assert "svplanted" in cmds2, "pi did not load the planted extension"
    finally:
        if app.alive():
            try:
                app.send("shutdown", {"cause": "test end"}, timeout=30)
            except Exception:
                app.proc.kill()
            app.proc.wait()
        router.stop()
