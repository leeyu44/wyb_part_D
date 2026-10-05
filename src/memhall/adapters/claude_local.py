"""LocalClaudeAdapter —— Claude Code 本机适配器（子进程驱动，win/linux 通用）。

2026-09-29 win 实测：CLAUDE_CONFIG_DIR 指向沙箱后 auto-memory 照常工作——
teach 写 <config>/projects/<slug>/memory/*.md + MEMORY.md 索引，
新进程（-p oneshot，每次独立进程=天然跨会话）召回正常。
openKylin 侧：npm 装 claude-code 后同一套代码可用，
认证走公网 anthropic 协议端点（如 bigmodel），与 15721 本机代理无关。

- 认证:   CLAUDE_LLM_BASE_URL/CLAUDE_LLM_KEY（anthropic 协议）优先；
          缺省回落进程环境里已有的 ANTHROPIC_BASE_URL/AUTH_TOKEN（从已配置 shell 继承）
- send:   claude -p <msg> --output-format text --permission-mode acceptEdits
          （acceptEdits=放行记忆/工作区文件写入，不放行命令执行）
- 记忆:   projects/*/memory/*.md（auto-memory）+ 工作区 CLAUDE.md（项目记忆）
- 拨钟:   本机不支持（orchestrator 兜 AdapterError，temporal 用例标无效）
"""

from __future__ import annotations

import os
import shutil
import subprocess
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from memhall.adapters.base import (
    AdapterError, AgentAdapter, AgentUnavailable, NO_WINDOW, fingerprint_tree,
)
from memhall.schema.evidence import ActionDump, MemoryEntry, MemorySnapshot, Reply

_SEND_MIN_INTERVAL = float(os.environ.get("CLAUDE_SEND_INTERVAL", "2"))
_send_lock = threading.Lock()
_last_send = 0.0


def _send_throttle() -> None:
    global _last_send
    with _send_lock:
        wait = _SEND_MIN_INTERVAL - (time.monotonic() - _last_send)
        if wait > 0:
            time.sleep(wait)
        _last_send = time.monotonic()


class LocalClaudeAdapter(AgentAdapter):
    """被测智能体 Claude Code（本机安装，CLAUDE_CONFIG_DIR 沙箱隔离）。"""

    name = "claude-local"

    def __init__(self, root: Path | None = None):
        self.root = root or Path.home() / ".memhall" / "claude-sandbox"
        self.config_dir = self.root / "claude-home"   # CLAUDE_CONFIG_DIR 指向这
        self.workspace = self.root / "workspace"
        self._exe: str | None = None

    # ---------- 沙箱 ----------

    def _resolve_exe(self) -> str:
        if self._exe is None:
            from memhall.discovery import _which
            exe = _which("claude")
            if not exe:
                raise AgentUnavailable("PATH 里找不到 claude（npm i -g @anthropic-ai/claude-code）")
            self._exe = exe
        return self._exe

    def _sandbox_env(self) -> dict:
        env = os.environ.copy()
        base = os.environ.get("CLAUDE_LLM_BASE_URL", "").rstrip("/")
        key = os.environ.get("CLAUDE_LLM_KEY", "")
        if base and key:
            env["ANTHROPIC_BASE_URL"] = base
            env["ANTHROPIC_AUTH_TOKEN"] = key
        elif not (env.get("ANTHROPIC_BASE_URL") and env.get("ANTHROPIC_AUTH_TOKEN")):
            raise AgentUnavailable(
                "缺 claude 认证：设 CLAUDE_LLM_BASE_URL/CLAUDE_LLM_KEY"
                "（anthropic 协议端点），或在已配置 ANTHROPIC_* 的 shell 里跑")
        # 注意不映射 AGENT_LLM_MODEL：那是 openai 网关的模型名，claude 不认
        model = os.environ.get("CLAUDE_LLM_MODEL", "")
        if model:
            for tier in ("SONNET", "OPUS", "HAIKU", "FABLE"):
                env[f"ANTHROPIC_DEFAULT_{tier}_MODEL"] = model
        env["CLAUDE_CONFIG_DIR"] = str(self.config_dir)
        return env

    # ---------- 契约 01 ----------

    def reset(self) -> None:
        self._sandbox_env()  # 认证缺失提前失败，别等 send 才炸
        shutil.rmtree(self.root, ignore_errors=True)
        self.config_dir.mkdir(parents=True, exist_ok=True)
        self.workspace.mkdir(parents=True, exist_ok=True)

    def send(self, session_id: str, message: str) -> Reply:
        _send_throttle()
        sent = datetime.now(timezone.utc)
        t0 = time.time()
        try:
            r = subprocess.run(
                [self._resolve_exe(), "-p", message,
                 "--output-format", "text", "--permission-mode", "acceptEdits"],
                capture_output=True, encoding="utf-8", errors="replace",
                cwd=str(self.workspace), env=self._sandbox_env(),
                timeout=280, creationflags=NO_WINDOW)
        except subprocess.TimeoutExpired as e:
            raise AgentUnavailable(f"claude 超时: {e}") from e
        text = (r.stdout or "").strip()
        if not text or r.returncode != 0 or text.startswith(("API Error", "Error:")):
            raise AgentUnavailable(
                f"claude 无有效回复(rc={r.returncode}): {text[:150]} | {(r.stderr or '')[:150]}")
        return Reply(session_id=session_id, text=text, sent_at=sent,
                     reply_at=datetime.now(timezone.utc),
                     latency_ms=int((time.time() - t0) * 1000),
                     token_usage=None)

    def end_session(self, session_id: str) -> None:
        pass  # -p oneshot 每次独立进程，会话隔离天然成立

    def dump_memory(self) -> MemorySnapshot:
        """auto-memory（按项目路径编码分目录）+ 工作区 CLAUDE.md 两处合并。"""
        entries: list[MemoryEntry] = []
        sources: list[Path] = []
        mem_root = self.config_dir / "projects"
        if mem_root.is_dir():
            sources.extend(sorted(mem_root.rglob("*.md")))
        cl = self.workspace / "CLAUDE.md"
        if cl.is_file():
            sources.append(cl)
        seen: set[str] = set()
        for f in sources:
            rel = f.relative_to(self.root).as_posix()
            for line in f.read_text(encoding="utf-8", errors="replace").splitlines():
                s = line.strip().lstrip("-* ").strip()
                if not s or s.startswith("#"):
                    continue
                if s in seen:  # MEMORY.md 索引行与主题文件正文会重复
                    continue
                seen.add(s)
                entries.append(MemoryEntry(
                    entry_id=f"m-{len(entries):04d}", content=s,
                    created_at=None, source_turn=rel))
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
