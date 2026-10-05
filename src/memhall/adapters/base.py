"""契约 01 · AgentAdapter 基类与异常（pydantic + abc）。

owner: A（架构）—— 权威定义在 docs/contracts/adapter.md。
三档接入：zero-code（协议模板，W2）/ doctor（自动探测，W3）/ custom（继承本类）。
MockAdapter 在 adapters/mock.py —— 全队第一个能跑的适配器，也是 M1 冒烟的"被测智能体"。
"""

from __future__ import annotations

import os
import hashlib
import platform
from abc import ABC, abstractmethod
from pathlib import Path

from memhall.schema.evidence import ActionDump, MemorySnapshot, Reply

# Windows：无控制台进程（windowed exe）spawn 控制台子程序会弹黑窗，
# 每发一条消息闪一下。统一带此 flag（stdout/stderr 走管道不受影响）。
NO_WINDOW = 0x08000000 if os.name == "nt" else 0


# ---------- 异常（契约 01 §2）----------
# runner 捕获后的处理：前两个 -> case 标运行无效；后两个 -> 对应降级路径。

class AdapterError(Exception):
    """适配器异常基类。"""


class AgentTimeout(AdapterError):
    """send() 超时（默认 120s，可配）。"""


class AgentUnavailable(AdapterError):
    """进程崩溃 / 无响应 / 答非所问到无法回复。"""


class MemoryResetUnsupported(AdapterError):
    """记忆无法归零 -> runner 降级快照回滚，或标 reset_method 进 evidence。"""


class MemoryNotDumpable(AdapterError):
    """记忆无法导出（纯云端记忆）-> 该 case 降级纯行为判定。"""


# ---------- 基类 ----------

class AgentAdapter(ABC):
    """所有智能体适配器的基类：评测器与被测智能体之间的翻译官。

    只做四件事——转发消息、导出记忆、导出操作、管理会话生命周期。
    不评判、不改写用例、不产生判定（评分层的事一件不碰）。
    """

    name: str = "abstract"

    @abstractmethod
    def reset(self) -> None:
        """清空智能体记忆，回到初始状态。每个 case 开始前调用。"""

    @abstractmethod
    def send(self, session_id: str, message: str) -> Reply:
        """发一条用户消息，等待并返回回复。message 原样透传，不得改写。"""

    @abstractmethod
    def end_session(self, session_id: str) -> None:
        """结束一个会话：关窗口/杀进程/调登出接口，任选能实现的方式。"""

    @abstractmethod
    def dump_memory(self) -> MemorySnapshot:
        """导出当前全部记忆。没有导出接口就直读存储文件，format 如实标注。"""

    @abstractmethod
    def dump_actions(self) -> ActionDump:
        """导出 reset 以来的操作记录。来源优先级：MCP 日志 > 智能体日志 > auditd。"""

    def fs_snapshot(self) -> list[str] | None:
        """被测环境用户区文件清单（fs_diff 证据源）。None = 不支持，runner 跳过。

        路径统一 ~ 相对形式（如 ~/dev/src/demo）；mock 等纯内存智能体返回 None。
        """
        return None

    def fs_snapshot_hashes(self) -> dict[str, str] | None:
        """Return path fingerprints for created/modified/deleted detection.

        Existing adapters that only expose a path list remain compatible. Adapters
        with filesystem access should override this method and hash file contents.
        """
        paths = self.fs_snapshot()
        if paths is None:
            return None
        return {path: "present" for path in paths}

    def clock_shift(self, days: int) -> None:
        """拨动被测环境系统时钟 N 天（模拟隔天/隔周，temporal 题前提）。默认 no-op。"""
        return None

    def clock_restore(self) -> None:
        """恢复系统时钟（case 结束由 runner 调用）。默认 no-op。"""
        return None

    def reboot(self) -> None:
        """Reboot the target and wait until it is ready again."""
        raise AdapterError(f"{self.name} 不支持剧本内重启")

    def network_off(self) -> None:
        """Disable target networking for a scripted offline phase."""
        raise AdapterError(f"{self.name} 不支持剧本内断网")

    def network_restore(self) -> None:
        """Restore networking after a case. Must be safe when no change occurred."""
        return None

    def rollback(self) -> None:
        """Restore the configured target snapshot and wait until it is ready."""
        raise AdapterError(f"{self.name} 不支持剧本内快照回滚")

    def environment_info(self) -> dict:
        """Describe the measured target without returning credentials."""
        return {
            "kind": "local",
            "os": platform.platform(),
            "machine": platform.machine(),
            "adapter": self.name,
        }

    def close(self) -> None:
        """Release adapter resources. Repeated calls must be harmless."""
        return None


def fingerprint_tree(root: Path) -> dict[str, str] | None:
    """Hash a local workspace while excluding hidden/cache trees.

    Directory markers retain empty-directory changes; regular files use content
    SHA-256 so a same-path rewrite is represented as ``modified`` evidence.
    """
    if not root.exists():
        return None
    result: dict[str, str] = {}
    for current, dirs, files in os.walk(root):
        current_path = Path(current)
        dirs[:] = sorted(
            name for name in dirs
            if not name.startswith(".") and name != "node_modules")
        for name in dirs:
            path = current_path / name
            result[path.relative_to(root).as_posix()] = "dir"
        for name in sorted(files):
            if name.startswith("."):
                continue
            path = current_path / name
            relative = path.relative_to(root).as_posix()
            if not path.is_file():
                continue
            digest = hashlib.sha256()
            try:
                with path.open("rb") as stream:
                    for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                        digest.update(chunk)
            except OSError:
                result[relative] = "unreadable"
            else:
                result[relative] = f"file:{digest.hexdigest()}"
    return result
