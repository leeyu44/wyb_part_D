"""从 openKylin 虚拟机拉取文件（paramiko SFTP，vm_put 的反方向）。

用法：MSYS_NO_PATHCONV=1 VM_PASS=xxx uv run python scripts/vm_get.py <远端路径> <本地文件>

背景：openKylin 裁剪版 open-vm-tools 无剪贴板/拖拽/共享文件夹，VM→宿主只能走网络。
远端路径以 / 开头时在 Git Bash 下必须加 MSYS_NO_PATHCONV=1，否则被 MSYS 转成
Windows 路径报 ENOENT（与 vm_put 同坑）。
"""

from __future__ import annotations

import os
import sys
import time

import paramiko

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

HOST = "192.168.61.133"
USER = "okim"


def main() -> int:
    if len(sys.argv) != 3 or "VM_PASS" not in os.environ:
        print("用法: MSYS_NO_PATHCONV=1 VM_PASS=xxx uv run python scripts/vm_get.py <远端路径> <本地文件>")
        return 2
    remote, local = sys.argv[1], sys.argv[2]
    t0 = time.time()

    cli = paramiko.SSHClient()
    cli.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    cli.connect(HOST, username=USER, password=os.environ["VM_PASS"], timeout=15)
    try:
        sftp = cli.open_sftp()
        size = sftp.stat(remote).st_size

        def cb(done: int, total: int) -> None:
            pct = done * 100 // total
            print(f"\r{pct:3d}%  {done//1024//1024}/{total//1024//1024} MB", end="")

        sftp.get(remote, local, callback=cb)
        print()
    finally:
        cli.close()
    ok = os.path.getsize(local) == size
    dt = time.time() - t0
    print(f"{'OK' if ok else 'SIZE-MISMATCH'}: {size//1024//1024} MB in {dt:.0f}s ({size/1024/1024/max(dt,0.1):.1f} MB/s)")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
