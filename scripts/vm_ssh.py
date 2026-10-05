"""在 openKylin 虚拟机里远程执行命令（SSH 密码认证）。

用法：
    VM_PASS=xxx uv run python scripts/vm_ssh.py "命令"
    VM_PASS=xxx uv run python scripts/vm_ssh.py "echo xxx | sudo -S apt install -y xxx"

背景：openKylin 3.0 桌面版缺 open-vm-tools-desktop，vmrun 的 guest 文件操作不可用
（认证通过但一律报"文件不存在"），故走 SSH 通道。密码只从环境变量读，不进 git。
IP 来自 VMware NAT DHCP 租约（vmnetdhcp.leases，hostname okim-pc）。
"""

from __future__ import annotations

import os
import sys

import paramiko

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

# 实验室内网默认（VM_H_* 工具链私有；外部使用改环境变量 VM_TOOL_HOST）
import os as _os  # noqa: E402

HOST = _os.environ.get("VM_TOOL_HOST", "192.168.61.133")
USER = "okim"


def run(cmd: str, timeout: int = 900) -> int:
    cli = paramiko.SSHClient()
    cli.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    cli.connect(HOST, username=USER, password=os.environ["VM_PASS"], timeout=15)
    try:
        stdin, stdout, stderr = cli.exec_command(cmd, timeout=timeout)
        stdin.close()
        out = stdout.read().decode("utf-8", "replace")
        err = stderr.read().decode("utf-8", "replace")
        rc = stdout.channel.recv_exit_status()
    finally:
        cli.close()
    if out:
        print(out, end="" if out.endswith("\n") else "\n")
    if err:
        print("[stderr]")
        print(err, end="" if err.endswith("\n") else "\n")
    print(f"[exit {rc}]")
    return rc


def run_bg(cmd: str, settle_s: float = 6.0) -> int:
    """fire-and-forget：发完命令不等退出状态就关 channel。

    为什么需要：远端命令里如果起了常驻后台进程（哪怕重定向了 fd），
    sshd 迟迟不发 exit-status，recv_exit_status 会永久挂死本地进程。
    启动服务类命令一律用这个模式；命令自身需 setsid/nohup 自保。"""
    import time

    cli = paramiko.SSHClient()
    cli.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    cli.connect(HOST, username=USER, password=os.environ["VM_PASS"], timeout=15)
    try:
        ch = cli.get_transport().open_session()
        ch.exec_command(cmd)
        out = b""
        deadline = time.time() + settle_s
        while time.time() < deadline:
            if ch.recv_ready():
                out += ch.recv(65536)
            if ch.exit_status_ready() and not ch.recv_ready():
                break
            time.sleep(0.2)
        rc = ch.recv_exit_status() if ch.exit_status_ready() else None
        ch.close()
    finally:
        cli.close()
    if out:
        print(out.decode("utf-8", "replace"), end="")
    print(f"[bg rc={rc}]")
    return 0


if __name__ == "__main__":
    if len(sys.argv) < 2 or "VM_PASS" not in os.environ:
        sys.exit("用法: VM_PASS=xxx uv run python scripts/vm_ssh.py [--bg] \"命令\"")
    if sys.argv[1] == "--bg":
        if len(sys.argv) < 3:
            sys.exit('用法: vm_ssh.py --bg "命令"')
        sys.exit(run_bg(sys.argv[2]))
    sys.exit(run(sys.argv[1]))
