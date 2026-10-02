"""LocalQwenAdapter —— Qwen Code CLI 本机适配器（子进程驱动，win/linux 通用）。

- 沙箱:   QWEN_HOME 指向评测沙箱（官方支持的环境变量，等价 HERMES_HOME 的思路），
          settings.json 从 ~/.qwen 拷贝并【剥掉 hooks】——用户配置里挂着 clawd-on-desk
          全家桶钩子，不剥会给评测灌假活跃信号
- 认证:   沙箱 settings.json 自带（用户的 modelProviders/envKey 机制原样生效）
- send:   qwen "<msg>" --approval-mode auto-edit（自动批文件编辑=可写记忆，不放行命令）
- 记忆:   QWEN_HOME/QWEN.md（全局）+ 工作区 QWEN.md（项目）两处
          （auto-memory 实测 2026-09-29 因网关 500 未完成，恢复后复核）
- 拨钟:   本机不支持
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
import time
from datetime import UTC, datetime
from pathlib import Path

from memhall.adapters.base import NO_WINDOW, AdapterError, AgentAdapter, AgentUnavailable
from memhall.schema.evidence import ActionDump, MemoryEntry, MemorySnapshot, Reply

_SEND_MIN_INTERVAL = float(os.environ.get("QWEN_SEND_INTERVAL", "10"))
_send_lock = threading.Lock()
_last_send = 0.0


def _send_throttle() -> None:
    global _last_send
    with _send_lock:
        wait = _SEND_MIN_INTERVAL - (time.monotonic() - _last_send)
        if wait > 0:
            time.sleep(wait)
        _last_send = time.monotonic()


class LocalQwenAdapter(AgentAdapter):
    """被测智能体 Qwen Code CLI（本机安装，QWEN_HOME 沙箱隔离）。"""

    name = "qwen-local"

    def __init__(self, root: Path | None = None):
        self.root = root or Path.home() / ".memhall" / "qwen-sandbox"
        self.qwen_home = self.root / "qwen-home"       # QWEN_HOME 指向这
        self.workspace = self.root / "workspace"
        self._exe: str | None = None

    # ---------- 沙箱 ----------

    def _resolve_exe(self) -> str:
        if self._exe is None:
            from memhall.discovery import ADAPTER_CLI, find_cli
            exe = find_cli(*ADAPTER_CLI["qwen"])
            if not exe:
                raise AgentUnavailable("PATH 与 ~/.local/bin 均找不到 qwen（npm i -g @qwen-code/qwen-code）")
            self._exe = exe
        return self._exe

    def _settings_src(self) -> Path:
        return Path.home() / ".qwen" / "settings.json"

    def reset(self) -> None:
        src = self._settings_src()
        if not src.is_file():
            raise AgentUnavailable(
                f"qwen 未配置：{src} 不存在（先跑一次 qwen 完成网关配置）")
        data = json.loads(src.read_text(encoding="utf-8"))
        data.pop("hooks", None)  # clawd-on-desk 钩子不进评测沙箱
        model = os.environ.get("AGENT_LLM_MODEL", "")  # 与 hermes/kylinbot 同一网关口径
        if model:
            data.setdefault("model", {})["name"] = model
            for provs in data.get("modelProviders", {}).values():
                for prov in provs:
                    prov["id"] = model
        shutil.rmtree(self.root, ignore_errors=True)
        self.qwen_home.mkdir(parents=True, exist_ok=True)
        (self.qwen_home / "settings.json").write_text(
            json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        self.workspace.mkdir(parents=True, exist_ok=True)

    def _sandbox_env(self) -> dict:
        env = os.environ.copy()
        env["QWEN_HOME"] = str(self.qwen_home)
        return env

    # ---------- 契约 01 ----------

    def send(self, session_id: str, message: str) -> Reply:
        _send_throttle()
        sent = datetime.now(UTC)
        t0 = time.time()
        try:
            r = subprocess.run(
                [self._resolve_exe(), message, "--approval-mode", "auto-edit"],
                capture_output=True, encoding="utf-8", errors="replace",
                cwd=str(self.workspace), env=self._sandbox_env(),
                timeout=280, creationflags=NO_WINDOW)
        except subprocess.TimeoutExpired as e:
            raise AgentUnavailable(f"qwen 超时: {e}") from e
        text = (r.stdout or "").strip()
        if not text or text.startswith(("[API Error", "Error:")):
            raise AgentUnavailable(
                f"qwen 无有效回复(rc={r.returncode}): {text[:150]} | {(r.stderr or '')[:150]}")
        return Reply(session_id=session_id, text=text, sent_at=sent,
                     reply_at=datetime.now(UTC),
                     latency_ms=int((time.time() - t0) * 1000),
                     token_usage=None)

    def end_session(self, session_id: str) -> None:
        pass  # 一次性 prompt 每次独立进程，会话隔离天然成立

    def dump_memory(self) -> MemorySnapshot:
        """全局 QWEN.md + 工作区 QWEN.md（+ 备未来 auto-memory 目录）合并。"""
        entries: list[MemoryEntry] = []
        sources = [self.qwen_home / "QWEN.md", self.workspace / "QWEN.md"]
        mem_dir = self.qwen_home / "memories"
        if mem_dir.is_dir():
            sources.extend(sorted(mem_dir.rglob("*.md")))
        seen: set[str] = set()
        for f in sources:
            if not f.is_file():
                continue
            rel = f.relative_to(self.root).as_posix()
            for line in f.read_text(encoding="utf-8", errors="replace").splitlines():
                s = line.strip().lstrip("-* ").strip()
                if not s or s.startswith("#") or s in seen:
                    continue
                seen.add(s)
                entries.append(MemoryEntry(
                    entry_id=f"m-{len(entries):04d}", content=s,
                    created_at=None, source_turn=rel))
        return MemorySnapshot(format="files",
                              dumped_at=datetime.now(UTC),
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

    def clock_shift(self, days: int) -> None:
        if days == 0:
            return
        raise AdapterError("本机不支持拨钟——temporal 用例请在 openKylin VM 评测")

    def clock_restore(self) -> None:
        pass
