"""LocalHermesAdapter —— Hermes Agent 本机适配器（子进程驱动，win/linux 通用）。

与 SSH 版（adapters/hermes.py，VM 内）同源知识、不同通道：
- send:   `hermes chat --query-file - --oneshot --provider deepseek --model <m>`
          stdin 传消息；每次独立进程 = 天然跨会话，长期记忆只靠持久层
- 隔离:   HERMES_HOME 指向评测沙箱（hermes 源码官方支持该变量，profile-scoped），
          凭据/记忆/日志与用户日常 ~/.hermes 完全隔离
- 网关:   .env 的 AGENT_LLM_*（映射为 DEEPSEEK_BASE_URL/KEY），
          send 内置 10s 节流（网关 RPM 低）
- 记忆:   $HERMES_HOME/memories/MEMORY.md / USER.md（与 SSH 版同解析）
- 拨钟:   本机不支持（orchestrator 兜 AdapterError，temporal 用例标无效）
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from memhall.adapters.base import (
    AdapterError, AgentAdapter, AgentUnavailable, NO_WINDOW, fingerprint_tree,
)
from memhall.adapters.hermes import _strip_tui
from memhall.schema.evidence import ActionDump, MemoryEntry, MemorySnapshot, Reply

_ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")

_SEND_MIN_INTERVAL = float(os.environ.get("HERMES_SEND_INTERVAL", "10"))
_send_lock = threading.Lock()
_last_send = 0.0


def _send_throttle() -> None:
    global _last_send
    with _send_lock:
        wait = _SEND_MIN_INTERVAL - (time.monotonic() - _last_send)
        if wait > 0:
            time.sleep(wait)
        _last_send = time.monotonic()


class LocalHermesAdapter(AgentAdapter):
    """被测智能体 Hermes Agent（本机安装，HERMES_HOME 沙箱隔离）。"""

    name = "hermes-local"

    def __init__(self, root: Path | None = None):
        self.root = root or Path.home() / ".memhall" / "hermes-sandbox"
        self.home = self.root / "hermes-home"
        self.mem_dir = self.home / "memories"
        self.workspace = self.root / "workspace"
        self._exe: str | None = None

    # ---------- 沙箱 ----------

    def _resolve_exe(self) -> str:
        if self._exe is None:
            from memhall.discovery import _which
            exe = _which("hermes")
            if not exe:
                raise AgentUnavailable("PATH 里找不到 hermes（安装后重开终端）")
            self._exe = exe
        return self._exe

    def _sandbox_env(self) -> dict:
        base = os.environ.get("AGENT_LLM_BASE_URL", "").rstrip("/")
        key = os.environ.get("AGENT_LLM_KEY", "")
        if not (base and key):
            raise AgentUnavailable("缺 AGENT_LLM_BASE_URL / AGENT_LLM_KEY（检查 .env）")
        env = os.environ.copy()
        env["HERMES_HOME"] = str(self.home)
        env["DEEPSEEK_BASE_URL"] = base
        env["DEEPSEEK_API_KEY"] = key
        return env

    # ---------- 契约 01 ----------

    def reset(self) -> None:
        self._sandbox_env()  # 网关缺失提前失败，别等 send 才炸
        shutil.rmtree(self.root, ignore_errors=True)
        self.workspace.mkdir(parents=True, exist_ok=True)
        self.mem_dir.mkdir(parents=True, exist_ok=True)

    def send(self, session_id: str, message: str) -> Reply:
        _send_throttle()
        model = os.environ.get("AGENT_LLM_MODEL", "qwen3.7-plus")
        sent = datetime.now(timezone.utc)
        t0 = time.time()
        try:
            r = subprocess.run(
                [self._resolve_exe(), "chat", "--query-file", "-", "--oneshot",
                 "--provider", "deepseek", "--model", model],
                input=message, capture_output=True, encoding="utf-8",
                errors="replace", cwd=str(self.workspace),
                env=self._sandbox_env(), timeout=280,
                creationflags=NO_WINDOW)
        except subprocess.TimeoutExpired as e:
            raise AgentUnavailable(f"hermes 超时: {e}") from e
        text = _strip_tui(_ANSI.sub("", r.stdout or ""))
        if not text:
            raise AgentUnavailable(
                f"hermes 无有效回复(rc={r.returncode}): "
                f"{(r.stdout or '')[:150]} | {(r.stderr or '')[:150]}")
        if "API failed after" in text or "Final error" in text:
            raise AgentUnavailable(f"hermes 后端不可用: {text[:200]}")
        return Reply(session_id=session_id, text=text, sent_at=sent,
                     reply_at=datetime.now(timezone.utc),
                     latency_ms=int((time.time() - t0) * 1000),
                     token_usage=None)

    def end_session(self, session_id: str) -> None:
        pass  # oneshot 每次独立进程，会话隔离天然成立

    def dump_memory(self) -> MemorySnapshot:
        entries: list[MemoryEntry] = []
        for name in ("MEMORY.md", "USER.md"):
            f = self.mem_dir / name
            if not f.exists():
                continue
            for line in f.read_text(encoding="utf-8", errors="replace").splitlines():
                s = line.strip().lstrip("-* ").strip()
                if s:
                    entries.append(MemoryEntry(
                        entry_id=f"m-{len(entries):04d}", content=s,
                        created_at=None, source_turn=name))
        return MemorySnapshot(format="files",
                              dumped_at=datetime.now(timezone.utc),
                              entries=entries, raw=None)

    def dump_actions(self) -> ActionDump:
        return ActionDump(actions=[], coverage="unknown")

    def fs_snapshot(self) -> list[str] | None:
        if not self.workspace.exists():
            return None
        out = []
        for p in sorted(self.workspace.rglob("*")):
            if (p.is_file()
                    and not any(part.startswith(".") for part in p.parts)):
                out.append(p.relative_to(self.workspace).as_posix())
        return out

    def fs_snapshot_hashes(self) -> dict[str, str] | None:
        return fingerprint_tree(self.workspace)

    def clock_shift(self, days: int) -> None:
        if days == 0:
            return
        raise AdapterError("本机不支持拨钟——temporal 用例请在 openKylin VM 评测")

    def clock_restore(self) -> None:
        pass
