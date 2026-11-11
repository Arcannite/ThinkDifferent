#!/usr/bin/env python3
"""Expose the loopback HTTP proxy to Docker containers.

Why: host-side egress to github.com / huggingface.co is DNS-poisoned on this network
(answers point at 205.164.50.0/24, which serves a self-signed "Scholar Verify" chain).
The real fix for containers is to send their HTTPS through the same local proxy that
already works on the host.

Clash listens on 127.0.0.1:7897, which is unreachable from a container's network
namespace. This forwards the docker bridge gateway address (172.17.0.1:7898) to it.
No privileges required; binds only the bridge address, never the LAN.

Managed by scripts/docker-proxy.sh.
"""

from __future__ import annotations

import argparse
import socket
import sys
import threading

LISTEN = ("172.17.0.1", 7898)
TARGET = ("127.0.0.1", 7897)


def pipe(src: socket.socket, dst: socket.socket) -> None:
    try:
        while True:
            data = src.recv(65536)
            if not data:
                break
            dst.sendall(data)
    except OSError:
        pass
    finally:
        for s in (dst, src):
            try:
                s.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass


def serve(listen: tuple[str, int], target: tuple[str, int]) -> None:
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(listen)
    srv.listen(256)
    print(f"forwarding {listen[0]}:{listen[1]} -> {target[0]}:{target[1]}", flush=True)
    while True:
        client, _ = srv.accept()
        try:
            upstream = socket.create_connection(target, timeout=10)
        except OSError as exc:
            print(f"upstream failed: {exc}", file=sys.stderr, flush=True)
            client.close()
            continue
        threading.Thread(target=pipe, args=(client, upstream), daemon=True).start()
        threading.Thread(target=pipe, args=(upstream, client), daemon=True).start()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--listen-host", default=LISTEN[0])
    ap.add_argument("--listen-port", type=int, default=LISTEN[1])
    ap.add_argument("--target-host", default=TARGET[0])
    ap.add_argument("--target-port", type=int, default=TARGET[1])
    a = ap.parse_args()
    serve((a.listen_host, a.listen_port), (a.target_host, a.target_port))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())