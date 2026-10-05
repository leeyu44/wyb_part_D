"""SSH transport and target fingerprint helpers for openKylin adapters."""

from __future__ import annotations

import base64
import json
import os
import shlex
import socket
import time
from dataclasses import dataclass
from datetime import datetime, timezone

import paramiko


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default)


class SshChannel:
    """Reusable SSH channel with deterministic connection retries."""

    def __init__(self, host: str | None = None, user: str | None = None,
                 password: str | None = None, port: int | None = None):
        self.host = host or _env("VM_HOST", "192.168.61.133")
        self.user = user or _env("VM_USER", "okim")
        self.password = password if password is not None else _env("VM_PASS")
        self.sudo_password = _env("VM_SUDO_PASS", self.password)
        self.key_file = _env("VM_KEY_FILE") or None
        self.port = port or int(_env("VM_PORT", "22"))
        self.connect_timeout = int(_env("VM_CONNECT_TIMEOUT", "15"))
        self.connect_retries = max(1, int(_env("VM_CONNECT_RETRIES", "3")))
        self.strict_host_key = _env("VM_STRICT_HOST_KEY", "0") == "1"
        self._cli: paramiko.SSHClient | None = None

    def _client(self) -> paramiko.SSHClient:
        if self._cli is not None:
            transport = self._cli.get_transport()
            if transport is not None and transport.is_active():
                return self._cli
            self.close()

        last_error: Exception | None = None
        for attempt in range(1, self.connect_retries + 1):
            cli = paramiko.SSHClient()
            cli.load_system_host_keys()
            if self.strict_host_key:
                cli.set_missing_host_key_policy(paramiko.RejectPolicy())
            else:
                cli.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            try:
                cli.connect(
                    self.host,
                    port=self.port,
                    username=self.user,
                    password=self.password or None,
                    key_filename=self.key_file,
                    timeout=self.connect_timeout,
                    banner_timeout=self.connect_timeout,
                    auth_timeout=self.connect_timeout,
                    allow_agent=True,
                    look_for_keys=True,
                )
                transport = cli.get_transport()
                if transport is not None:
                    transport.set_keepalive(20)
                self._cli = cli
                return cli
            except (OSError, paramiko.SSHException) as error:
                last_error = error
                cli.close()
                if attempt < self.connect_retries:
                    time.sleep(attempt)
        raise RuntimeError(
            f"SSH 连接失败 {self.user}@{self.host}:{self.port}: {last_error}"
        )

    def run(self, cmd: str, timeout: int = 300,
            input_text: str | None = None) -> tuple[int, str, str]:
        """Execute one command and return exit code, stdout, and stderr."""
        try:
            cli = self._client()
            stdin, stdout, stderr = cli.exec_command(cmd, timeout=timeout)
            if input_text is not None:
                stdin.write(input_text)
                stdin.flush()
            stdin.close()
            out = stdout.read().decode("utf-8", "replace")
            err = stderr.read().decode("utf-8", "replace")
            rc = stdout.channel.recv_exit_status()
            return rc, out, err
        except (EOFError, OSError, socket.timeout, paramiko.SSHException) as error:
            self.close()
            raise RuntimeError(
                f"SSH 命令失败 {self.user}@{self.host}: {type(error).__name__}: {error}"
            ) from error

    def run_sudo(self, cmd: str, timeout: int = 300) -> tuple[int, str, str]:
        """Run a command through sudo without embedding its password in argv."""
        if not self.sudo_password:
            raise RuntimeError("需要 VM_SUDO_PASS 或 VM_PASS 才能执行 sudo")
        wrapped = f"sudo -S -p '' -- sh -c {shlex.quote(cmd)}"
        return self.run(wrapped, timeout=timeout,
                        input_text=self.sudo_password + "\n")

    def run_json(self, cmd: str, timeout: int = 300) -> list | dict:
        rc, out, err = self.run(cmd, timeout)
        if rc != 0:
            raise RuntimeError(f"远程命令失败({rc}): {err.strip()[:500]}")
        try:
            return json.loads(out)
        except json.JSONDecodeError as error:
            raise RuntimeError(
                f"远程命令未返回 JSON: {out.strip()[:500]}"
            ) from error

    def wait_ready(self, timeout_s: int = 300, interval_s: int = 5) -> bool:
        self.close()
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            try:
                rc, out, _ = self.run("printf ready", timeout=15)
                if rc == 0 and out == "ready":
                    return True
            except RuntimeError:
                pass
            self.close()
            time.sleep(interval_s)
        return False

    def reboot_and_wait(self, timeout_s: int = 300) -> None:
        rc, out, err = self.run(
            "cat /proc/sys/kernel/random/boot_id", timeout=15)
        previous_boot_id = out.strip()
        if rc != 0 or not previous_boot_id:
            raise RuntimeError(
                f"重启前读取 boot_id 失败: {err.strip()[:200]}")
        try:
            rc, _, err = self.run_sudo("shutdown -r now", timeout=10)
        except RuntimeError:
            pass  # sshd may close the channel before returning an exit status
        else:
            if rc != 0:
                raise RuntimeError(f"重启命令失败: {err.strip()[:200]}")
        self.close()
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            try:
                rc, out, _ = self.run(
                    "cat /proc/sys/kernel/random/boot_id", timeout=15)
                if rc == 0 and out.strip() not in {"", previous_boot_id}:
                    return
            except RuntimeError:
                pass
            self.close()
            time.sleep(5)
        raise RuntimeError(
            f"虚拟机重启后 {timeout_s}s 内 boot_id 未变化或 SSH 未恢复")

    def host_key_sha256(self) -> str | None:
        transport = self._client().get_transport()
        if transport is None:
            return None
        key = transport.get_remote_server_key()
        digest = __import__("hashlib").sha256(key.asbytes()).digest()
        return base64.b64encode(digest).decode("ascii").rstrip("=")

    def close(self) -> None:
        if self._cli is not None:
            self._cli.close()
            self._cli = None


def b64(text: str) -> str:
    """Encode UTF-8 text for a shell-safe SSH payload."""
    return base64.b64encode(text.encode("utf-8")).decode("ascii")


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def elapsed_ms(start: float) -> int:
    return int((time.time() - start) * 1000)


@dataclass(frozen=True)
class RemoteClockState:
    epoch: int
    started_monotonic: float
    ntp_enabled: bool | None


def shift_remote_clock(channel: SshChannel, days: int) -> RemoteClockState:
    """Shift guest time while preventing NTP from undoing the test."""
    rc, out, err = channel.run("date +%s")
    if rc != 0:
        raise RuntimeError(f"拨钟前读取系统时间失败: {err.strip()[:200]}")
    try:
        epoch = int(out.strip())
    except ValueError as error:
        raise RuntimeError(f"拨钟前系统时间格式错误: {out.strip()[:80]}") from error

    ntp_rc, ntp_out, _ = channel.run(
        "timedatectl show -p NTP --value 2>/dev/null", timeout=30)
    ntp_enabled = (ntp_out.strip().lower() == "yes") if ntp_rc == 0 else None
    if ntp_enabled:
        rc, _, err = channel.run_sudo("timedatectl set-ntp false", timeout=30)
        if rc != 0:
            raise RuntimeError(f"关闭 NTP 失败: {err.strip()[:200]}")

    state = RemoteClockState(epoch, time.monotonic(), ntp_enabled)
    rc, _, err = channel.run_sudo(
        f"date -s '+{int(days)} days' >/dev/null", timeout=30)
    if rc != 0:
        if ntp_enabled:
            channel.run_sudo("timedatectl set-ntp true", timeout=30)
        raise RuntimeError(f"拨钟失败: {err.strip()[:200]}")
    return state


def restore_remote_clock(channel: SshChannel, state: RemoteClockState) -> None:
    """Restore wall time with elapsed-time compensation and original NTP state."""
    elapsed = max(0, int(time.monotonic() - state.started_monotonic))
    target_epoch = state.epoch + elapsed
    errors: list[str] = []
    rc, _, err = channel.run_sudo(
        f"date -s @{target_epoch} >/dev/null", timeout=30)
    if rc != 0:
        errors.append(f"恢复系统时间失败: {err.strip()[:200]}")
    if state.ntp_enabled:
        rc, _, err = channel.run_sudo("timedatectl set-ntp true", timeout=30)
        if rc != 0:
            errors.append(f"恢复 NTP 失败: {err.strip()[:200]}")
    if errors:
        raise RuntimeError("; ".join(errors))


_FS_SNAPSHOT_SOURCE = """import hashlib
import json
import os
from pathlib import Path

home = Path.home()
excluded = {'.hermes', '.cache', '.config', '.local', '.kylinbot', 'node_modules'}
result = {}
for current, dirs, files in os.walk(home):
    root = Path(current)
    relative_root = root.relative_to(home)
    if len(relative_root.parts) >= 4:
        dirs[:] = []
    else:
        dirs[:] = sorted(d for d in dirs if d not in excluded and not d.startswith('.'))
    for dirname in dirs:
        rel = (relative_root / dirname).as_posix()
        result['~/' + rel] = 'dir'
    for filename in sorted(files):
        if filename.startswith('.'):
            continue
        path = root / filename
        rel = '~/' + (relative_root / filename).as_posix()
        try:
            digest = hashlib.sha256()
            with path.open('rb') as stream:
                for chunk in iter(lambda: stream.read(1048576), b''):
                    digest.update(chunk)
            result[rel] = 'file:' + digest.hexdigest()
        except OSError:
            result[rel] = 'unreadable'
print(json.dumps(result, ensure_ascii=False, sort_keys=True))
"""


def remote_fs_snapshot(channel: SshChannel) -> dict[str, str] | None:
    """Fingerprint the remote user's visible workspace up to four levels deep."""
    command = f"echo {b64(_FS_SNAPSHOT_SOURCE)} | base64 -d | python3"
    try:
        data = channel.run_json(command, timeout=120)
    except RuntimeError:
        return None
    if not isinstance(data, dict):
        return None
    return {str(path): str(fingerprint) for path, fingerprint in data.items()}


def remote_environment(channel: SshChannel, adapter_name: str,
                       agent_version_command: str | None = None) -> dict:
    """Collect a compact, credential-free fingerprint of the openKylin target."""
    source = """import json
import os
import platform
from pathlib import Path

release = {}
path = Path('/etc/os-release')
if path.exists():
    for line in path.read_text(errors='replace').splitlines():
        if '=' in line:
            key, value = line.split('=', 1)
            release[key] = value.strip().strip(chr(34))
print(json.dumps({
    'kind': 'remote',
    'os': release.get('PRETTY_NAME', platform.platform()),
    'os_id': release.get('ID', ''),
    'os_version': release.get('VERSION_ID', ''),
    'kernel': platform.release(),
    'machine': platform.machine(),
    'python': platform.python_version(),
    'hostname': platform.node(),
}, ensure_ascii=False))
"""
    command = f"echo {b64(source)} | base64 -d | python3"
    data = channel.run_json(command, timeout=30)
    if not isinstance(data, dict):
        raise RuntimeError("目标环境指纹格式错误")
    data["adapter"] = adapter_name
    data["agent_version"] = None
    if agent_version_command:
        rc, out, _ = channel.run(agent_version_command, timeout=30)
        lines = [line.strip() for line in out.splitlines() if line.strip()]
        if rc == 0 and lines:
            data["agent_version"] = lines[0][:200]
    data["ssh_host_key_sha256"] = channel.host_key_sha256()
    data["vm_snapshot"] = _env("VM_SNAPSHOT", "unknown")
    return data
