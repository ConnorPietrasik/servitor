"""The update-waiting mechanism, tested over the stdio app.

- a run is active  -> the tick defers: the flag SURVIVES, the running
  sessions get ``update_waiting`` set (visible in the panel), the tick
  result says so; the restart command is NOT issued.
- no run active    -> the tick applies: per-session flags cleared, the
  restart bit consumed BEFORE the restart is issued (crash-safe), the
  restart command runs once.
- doctor-only flag -> applied inline (the doctor command runs); the
  image_swap bit is NEVER consumed by the tick (explicit operator
  downtime) — the flag survives with it.
- no flag          -> no-op (the steady state).
"""
from __future__ import annotations

import time

from test.conformance.install import resolve_pi  # noqa: F401 (fixture dep)
from test.mock_router.server import MockRouter  # noqa: F401 (fixture dep)

from test.stage2.test_lifecycle import stage2_env, start_app  # noqa: F401 (fixture import)


def _fake_script(run_root, name: str, marker: str) -> str:
    """A stand-in for `systemctl restart servitor` / the doctor: appends
    a line to a marker file so the test can assert it ran (how often)."""
    p = run_root / name
    p.write_text(
        "#!/usr/bin/env bash\n"
        f"echo {name} >> {run_root / marker}\n"
    )
    p.chmod(0o755)
    return str(p)


def test_update_waiting_full_cycle(stage2_env):
    from app.update_pending import UpdatePending, load, save

    run_root = stage2_env["run_root"]
    pi = stage2_env["pi"]
    state_root = stage2_env["state_root"]
    flag_path = run_root / "update-pending.json"
    restart_marker = run_root / "restart.marker"
    doctor_marker = run_root / "doctor.marker"
    restart_cmd = _fake_script(run_root, "fake_restart", "restart.marker")
    doctor_cmd = _fake_script(run_root, "fake_doctor", "doctor.marker")

    router = MockRouter()
    router.start()
    client = start_app(
        state_root,
        pi=pi,
        slot="slot1",
        router=router,
        run_root=run_root,
        extra=[
            "--update-pending-path", str(flag_path),
            "--restart-cmd", restart_cmd,
            "--doctor-cmd", doctor_cmd,
        ],
    )
    try:
        router.set_script(
            {"final_text": "done", "include_usage": False,
             "gap_seconds": 15, "chunk_count": 4}
        )
        row = client.send("start_session", {"display_name": "uw"})
        sid = row["session_id"]

        # 1. no flag: no-op (the steady state)
        assert client.send("update_tick") == {"no_flag": True}

        # 2. run ACTIVE: the tick defers — the flag survives, the
        #    per-session flag is set, and NOTHING is issued
        save(flag_path, UpdatePending(reason="sync", restart=True, doctor=True))
        client.send("prompt", {"session_id": sid, "text": "go"}, timeout=30)
        r = None
        for _ in range(100):  # wait for the run to be in flight
            r = client.send("update_tick")
            if r.get("deferred"):
                break
            time.sleep(0.1)
        assert r is not None and r.get("deferred") is True, r
        assert sid in r["active"], r
        assert not restart_marker.exists(), "restart must not be issued mid-run"
        f = load(flag_path)
        assert f is not None and f.restart, "the flag must survive the defer"
        rows = client.send("list_sessions").get("sessions", [])
        row = next(x for x in rows if x["id"] == sid)
        assert row.get("update_waiting") is True, "per-session flag must be visible"

        # 3. settle: run ends -> the next tick applies the restart
        #    (the doctor bit also applies inline, before the restart)
        client.send("settle", {"session_id": sid}, timeout=300)
        r = None
        for _ in range(60):
            r = client.send("update_tick")
            if r.get("restarted"):
                break
            time.sleep(0.1)
        assert r is not None and r.get("restarted") is True, r
        assert r["rc"] == 0, r
        assert restart_marker.read_text().count("fake_restart") == 1
        assert doctor_marker.read_text().count("fake_doctor") >= 1, (
            "the doctor bit must apply (inline, before the restart)"
        )
        f = load(flag_path)
        assert f is None or not f.restart, "the restart bit must be consumed"
        rows = client.send("list_sessions").get("sessions", [])
        row = next(x for x in rows if x["id"] == sid)
        assert row.get("update_waiting") is False, "per-session flag must clear"

        # 4. doctor-only flag: applied inline; image_swap SURVIVES
        save(flag_path, UpdatePending(reason="sync", doctor=True, image_swap=True))
        r = client.send("update_tick")
        assert r.get("restarted") is not True, r
        assert doctor_marker.read_text().count("fake_doctor") >= 1
        f = load(flag_path)
        assert f is not None and f.image_swap is True and not f.restart, (
            f"image_swap must survive the tick (explicit operator downtime): {f}"
        )
        # 5. and the surviving image-swap flag no-ops further ticks
        r = client.send("update_tick")
        assert r.get("no_op") is True, r
    finally:
        try:
            client.send("shutdown", {"cause": "test"}, timeout=60)
        except Exception:
            client.proc.kill()
        client.proc.wait()
        router.stop()
