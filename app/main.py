"""The servitor app entry (stage 2): JSON-lines control protocol on stdio.

Stage 3 replaces this front door with the HTTP/SSE API — the Lifecycle
it is a thin front door over stays. The stdio protocol exists for the
stage-2 crash drill (the test is the parent, kill -9's the app, restarts
it) and keeps every lifecycle operation scriptable.

Protocol: one JSON object per line on stdin, one JSON object per line on
stdout:

    {"id": 1, "cmd": "start_session", "args": {"display_name": "x"}}
    -> {"id": 1, "ok": true, "result": {...}}
    -> {"id": 1, "ok": false, "error": "TypeError: ..."}

Commands: start_session, prompt, settle, steer, follow_up, abort,
list_sessions, probe, release, events, idle_kill_tick, gc_tick,
boot_report, shutdown, exit.

CLI (env contract wins for keys — the key rides env only, never argv):

    python -m app.main --state ROOT --config-src DIR [--router-url URL]
        --pi-node BIN --pi-cli JS --pi-ext DIR --toolkit DIR
        [--slots slot1,slot2] [--api-key-env OMNISSIAH_API_KEY]
        [--exa-key-env EXA_API_KEY]
        [--idle-kill-sec N] [--shutdown-grace-sec N]
        [--gc-idle-days N] [--gc-interval-sec N]
        [--baseline-ext NAME ...]
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import shutil
import sys
from pathlib import Path

from app.iso.configswap import active_version, next_version, stage_and_swap
from app.iso.layout import state_paths
from app.iso.slots import SlotPool
from app.lifecycle import Lifecycle, LifecycleConfig, PiInstall
from app.registry import Registry

log = logging.getLogger("servitor.app")


def _stage_config(config_root: str, config_src: str, router_url: str | None) -> str | None:
    """Stage a config version if none is active yet (boot from scratch).
    Mirrors deploy/sync.sh: stage + swap; the active pointer is the truth."""
    if active_version(config_root):
        return None
    stage = Path(config_root).parent / f"cfgsrc-{next_version(config_root)}"
    shutil.copytree(config_src, stage)
    if router_url:
        models_path = stage / "models.json"
        models = json.loads(models_path.read_text())
        for prov in models.get("providers", {}).values():
            prov["baseUrl"] = router_url
        models_path.write_text(json.dumps(models, indent=2))
    return stage_and_swap(config_root, str(stage))


def parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="servitor")
    p.add_argument("--state", required=True, help="state volume root")
    p.add_argument("--config-src", required=True, help="agent config source dir")
    p.add_argument("--router-url", default=None, help="test only: mock router base URL")
    p.add_argument("--pi-node", required=True)
    p.add_argument("--pi-cli", required=True)
    p.add_argument("--pi-ext", default=None)
    p.add_argument("--toolkit", default="/opt/toolkit")
    p.add_argument("--slots", default=",".join(f"slot{i}" for i in range(1, 17)))
    p.add_argument("--api-key-env", default="OMNISSIAH_API_KEY")
    p.add_argument("--exa-key-env", default="EXA_API_KEY")
    p.add_argument("--idle-kill-sec", type=float, default=3600.0)
    p.add_argument("--shutdown-grace-sec", type=float, default=90.0)
    p.add_argument("--gc-idle-days", type=float, default=14.0)
    p.add_argument("--gc-interval-sec", type=float, default=300.0)
    p.add_argument("--baseline-ext", action="append", default=[])
    p.add_argument("--registry", default=None, help="registry db path (default <state>/registry.db)")
    return p.parse_args(argv)


class App:
    def __init__(self, args: argparse.Namespace):
        self.args = args
        self.state = state_paths(args.state)
        self.registry = Registry(args.registry or f"{args.state}/registry.db")
        staged = _stage_config(self.state.config, args.config_src, args.router_url)
        if staged:
            log.info("staged config version %s at boot", staged)
        self.pool = SlotPool(tuple(s.strip() for s in args.slots.split(",") if s.strip()))
        self.lc = Lifecycle(
            LifecycleConfig(
                state=self.state,
                pool=self.pool,
                registry=self.registry,
                pi=PiInstall(node_bin=args.pi_node, cli_js=args.pi_cli, ext_path=args.pi_ext),
                api_key=os.environ.get(args.api_key_env, ""),
                exa_key=os.environ.get(args.exa_key_env) or None,
                toolkit_dir=args.toolkit,
                baseline_extensions=tuple(args.baseline_ext),
                idle_kill_sec=args.idle_kill_sec,
                shutdown_grace_sec=args.shutdown_grace_sec,
                gc_idle_days=args.gc_idle_days,
                gc_interval_sec=args.gc_interval_sec,
            )
        )
        self.boot_report = self.lc.boot_reconcile()

    async def handle(self, cmd: str, args: dict) -> object:
        lc = self.lc
        if cmd == "start_session":
            return await lc.start_session(args.get("display_name"))
        if cmd == "prompt":
            return {"disposition": await lc.prompt(args["session_id"], args["text"])}
        if cmd == "settle":
            sid = args["session_id"]
            live = lc.live.get(sid)
            if live is None or live.driver is None:
                return {"aborted": False, "no_process": True}
            st = await live.driver.get_state()
            run_active = bool(
                st.get("isStreaming") or st.get("isCompacting")
                or st.get("pendingMessageCount", 0) > 0
            )
            if not run_active:
                # 'settle' = wait until idle: already idle ⇒ return at once
                # (a settle that fired before this command is still the
                # answer — a waiter installed after the fact would hang)
                return {"aborted": bool(live.driver.last_settled_aborted),
                        "already_settled": True}
            fut = live.driver.settle_waiter()
            return {"aborted": await fut, "already_settled": False}
        if cmd == "steer":
            live = lc.live.get(args["session_id"])
            return {"disposition": await live.driver.steer(args["text"])}
        if cmd == "follow_up":
            live = lc.live.get(args["session_id"])
            return {"disposition": await live.driver.follow_up(args["text"])}
        if cmd == "abort":
            live = lc.live.get(args["session_id"])
            await live.driver.abort()
            return {"ok": True}
        if cmd == "commands":
            live = lc.live.get(args["session_id"])
            names = [c.get("name") for c in await live.driver.get_commands()]
            return {"commands": names}
        if cmd == "list_sessions":
            rows = self.registry.list_sessions()
            return {
                "sessions": [
                    {
                        "id": r.id,
                        "display_name": r.display_name,
                        "slot": r.slot,
                        "state": r.state,
                        "stable": r.stable,
                        "interrupted": r.interrupted,
                        "session_file": r.session_file,
                        "config_version": r.config_version,
                        "junk_bytes": r.junk_bytes,
                        "waiting": r.waiting,
                        "update_waiting": r.update_waiting,
                        "new_extensions": list(r.new_extensions),
                        "last_activity": r.last_activity,
                        "live_process": r.id in lc.live
                        and lc.live[r.id].driver is not None
                        and not lc.live[r.id].driver.exited,
                    }
                    for r in rows
                ]
            }
        if cmd == "probe":
            return {"result": await lc.probe(args["session_id"], args.get("which", "messages"))}
        if cmd == "release":
            out = await lc.release(args["session_id"], reason=args.get("reason", "user close"))
            return {
                "released": out.released,
                "quarantined": out.quarantined,
                "errors": out.errors,
            }
        if cmd == "set_stable":
            self.registry.set_stable(args["session_id"], bool(args.get("stable", True)))
            return {"ok": True}
        if cmd == "events":
            return {"items": self.lc.events.items(args.get("since", 0.0))}
        if cmd == "idle_kill_tick":
            return {"killed": await lc.idle_kill_tick()}
        if cmd == "gc_tick":
            return {"released": await lc.gc_tick()}
        if cmd == "boot_report":
            return {"boot_reconciled": self.boot_report}
        if cmd == "shutdown":
            await lc.shutdown(cause=args.get("cause", "app stop"))
            return {"ok": True}
        if cmd == "exit":
            await lc.shutdown(cause=args.get("cause", "app stop"))
            return {"ok": True}
        raise ValueError(f"unknown command {cmd}")

    async def run(self) -> None:
        self.lc.start_gc()
        reader = asyncio.StreamReader()
        protocol = asyncio.StreamReaderProtocol(reader)
        await asyncio.get_running_loop().connect_read_pipe(lambda: protocol, sys.stdin)
        while True:
            line = await reader.readline()
            if not line:
                break
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError as e:
                self._send({"ok": False, "error": f"bad json: {e}"})
                continue
            mid = msg.get("id")
            try:
                result = await self.handle(str(msg.get("cmd", "")), msg.get("args", {}))
                self._send({"id": mid, "ok": True, "result": result})
            except Exception as e:
                log.exception("command %s failed", msg.get("cmd"))
                self._send({"id": mid, "ok": False, "error": f"{type(e).__name__}: {e}"})
            if str(msg.get("cmd", "")) in ("shutdown", "exit"):
                break

    def _send(self, obj: dict) -> None:
        sys.stdout.write(json.dumps(obj) + "\n")
        sys.stdout.flush()


def main(argv: list[str]) -> int:
    logging.basicConfig(level=logging.INFO, stream=sys.stderr, format="%(name)s %(message)s")
    args = parse_args(argv)
    app = App(args)
    try:
        asyncio.run(app.run())
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
