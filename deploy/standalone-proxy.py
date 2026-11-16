#!/usr/bin/env python3
"""Standalone/loopback dev: a tiny local reverse proxy (PLAN stage 5).

The app runs loopback-only on the laptop (SERVITOR_BIND set to the
loopback address in the local .env; the one published port). To reach it from another device on
the LAN, point a reverse proxy at it — this is the minimal dependency-free
one (python stdlib): it accepts on the LAN interface and forwards to
loopback, so the app's bind never widens.

    python3 deploy/standalone-proxy.py --bind <lan-iface-addr> \
        --listen <port> --target <loopback-addr>:<app-port>

(Production does NOT use this: the guest firewall + the edge's own
reverse proxy are the real gates.)
"""

from __future__ import annotations

import argparse
import socket
import threading


def _pipe(src: socket.socket, dst: socket.socket) -> None:
    try:
        while True:
            data = src.recv(65536)
            if not data:
                break
            dst.sendall(data)
    except OSError:
        pass
    finally:
        try:
            dst.shutdown(socket.SHUT_WR)
        except OSError:
            pass


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--listen", type=int, required=True)
    ap.add_argument("--bind", required=True,
                    help="address to accept on (the LAN interface)")
    ap.add_argument("--target", required=True,
                    help="loopback target of the app (the loopback address:app port)")
    args = ap.parse_args()
    t_host, t_port = args.target.rsplit(":", 1)

    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind((args.bind, args.listen))
    srv.listen(16)
    print(f"standalone-proxy: {args.bind}:{args.listen} -> {args.target}", flush=True)

    def handle(client: socket.socket) -> None:
        try:
            up = socket.create_connection((t_host, int(t_port)), timeout=5)
        except OSError as e:
            client.close()
            print(f"upstream error: {e}", flush=True)
            return
        threading.Thread(target=_pipe, args=(client, up), daemon=True).start()
        threading.Thread(target=_pipe, args=(up, client), daemon=True).start()

    while True:
        client, _ = srv.accept()
        threading.Thread(target=handle, args=(client,), daemon=True).start()


if __name__ == "__main__":
    main()
