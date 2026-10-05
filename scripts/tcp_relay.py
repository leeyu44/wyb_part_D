"""宿主机 TCP 中转：VM → 宿主机 → api.mazhuoran.cloud:443。

背景：openKylin VM（VMware NAT）直连该网关时大请求体 TLS 流被破坏
（SSL bad record mac，降 MTU 无效）；宿主机出站稳定。VM 侧用 /etc/hosts
把域名指到宿主机 vmnet8 IP，TLS 端到端不变，本脚本只转发 TCP 字节。

用法：python scripts/tcp_relay.py [listen_port]（默认 8443）
"""

from __future__ import annotations

import contextlib
import socket
import sys
import threading

UPSTREAM_HOST = "api.mazhuoran.cloud"
UPSTREAM_PORT = 443


def pipe(src: socket.socket, dst: socket.socket) -> None:
    with contextlib.suppress(OSError):
        while chunk := src.recv(65536):
            dst.sendall(chunk)
    with contextlib.suppress(OSError):
        dst.shutdown(socket.SHUT_WR)


def handle(client: socket.socket) -> None:
    try:
        up = socket.create_connection((UPSTREAM_HOST, UPSTREAM_PORT), timeout=30)
    except OSError as e:
        print(f"upstream connect failed: {e}", file=sys.stderr)
        client.close()
        return
    threading.Thread(target=pipe, args=(client, up), daemon=True).start()
    threading.Thread(target=pipe, args=(up, client), daemon=True).start()


def main() -> None:
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8443
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("0.0.0.0", port))
    srv.listen(64)
    print(f"relay listening on 0.0.0.0:{port} -> {UPSTREAM_HOST}:{UPSTREAM_PORT}",
          flush=True)
    while True:
        client, addr = srv.accept()
        print(f"conn from {addr}", flush=True)
        threading.Thread(target=handle, args=(client,), daemon=True).start()


if __name__ == "__main__":
    main()
