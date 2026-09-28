"""HermesAdapter —— Hermes Agent 适配器（SSH 远程驱动，跑在 openKylin VM 里）。

实测对接方式（2026-09-28 VM 联调定案）：
- 传输:    provider=deepseek（transport=openai_chat）+ DEEPSEEK_BASE_URL 指自定义网关；
           openai-api provider 会被硬性 overlay 成 codex_responses 传输，网关不吃，弃用
- 网络:    VM 直连网关大请求体 TLS 断流，已配 /etc/hosts 指宿主机 + 宿主 tcp_relay 中转
- send:    hermes chat --query-file - --oneshot（stdin 传消息，免 shell 转义；
           每次独立进程 = 天然跨会话，长期记忆只靠持久层，正中考点）
- 记忆:    内置记忆系统（memory tool → ~/.hermes/memories/MEMORY.md / USER.md）
- reset:   清 memories/*.md（上下文无需清：oneshot 每次新 session）
- dump_actions: 暂无操作日志源，coverage=unknown（W3 接日志后升级）
"""

from __future__ import annotations

import os
import re
import time
from datetime import datetime, timezone

from memhall.adapters.base import AgentAdapter
from memhall.adapters.remote import SshChannel, b64, elapsed_ms, now_utc
from memhall.schema.evidence import ActionDump, MemoryEntry, MemorySnapshot, Reply

HERMES_BIN = "~/.hermes/bin/hermes"
MEM_DIR = "~/.hermes/memories"

# 用例注入的虚构工作区（评测专用 VM，reset 一并清掉防跨轮污染）
EVAL_WORKDIRS = ["~/dev", "~/work", "~/proj", "~/docs", "~/notes",
                 "~/out", "~/scripts", "~/templates", "~/demo"]

_BOX_NOISE = re.compile(r"[╭╮╰╯│┌┐└┘]|\s─")


class HermesAdapter(AgentAdapter):
    """被测智能体 Hermes Agent（v0.21.x，VM 内 ~/.hermes 部署，内置记忆）。"""

    name = "hermes"

    def __init__(self, channel: SshChannel | None = None):
        self.ch = channel or SshChannel()
        self._base_env = (
            f"export DEEPSEEK_API_KEY='{os.environ.get('AGENT_LLM_KEY', '')}' "
            f"DEEPSEEK_BASE_URL='{os.environ.get('AGENT_LLM_BASE_URL', '')}'; "
        )
        self._model = os.environ.get("AGENT_LLM_MODEL", "qwen3.7-plus")

    def reset(self) -> None:
        rc, _, err = self.ch.run(
            f"rm -f {MEM_DIR}/MEMORY.md {MEM_DIR}/USER.md && "
            f"rm -rf {' '.join(EVAL_WORKDIRS)} && echo ok")
        if rc != 0:
            raise RuntimeError(f"Hermes 记忆清零失败: {err.strip()[:300]}")

    def send(self, session_id: str, message: str) -> Reply:
        cmd = (f"{self._base_env}"
               f"echo {b64(message)} | base64 -d | timeout 280 {HERMES_BIN} chat "
               f"--query-file - --oneshot --provider deepseek --model {self._model} "
               f"2>/dev/null")
        sent = now_utc()
        t0 = time.time()
        rc, out, _ = self.ch.run(cmd, timeout=300)
        text = _strip_tui(out)
        if rc != 0 and not text:
            raise RuntimeError(f"hermes 调用失败({rc})，原始输出被 TUI 噪声吞没")
        return Reply(session_id=session_id, text=text,
                     sent_at=sent, reply_at=now_utc(),
                     latency_ms=elapsed_ms(t0), token_usage=None)

    def end_session(self, session_id: str) -> None:
        pass  # oneshot 每次独立进程，会话隔离天然成立

    def dump_memory(self) -> MemorySnapshot:
        cmd = (f"for f in {MEM_DIR}/MEMORY.md {MEM_DIR}/USER.md; do "
               f"[ -f $f ] && echo \"=== $f\" && cat $f; done")
        rc, out, _ = self.ch.run(cmd)
        entries: list[MemoryEntry] = []
        current = ""
        for line in out.splitlines():
            if line.startswith("=== "):
                current = line[4:].strip()
                continue
            s = line.strip().lstrip("-* ").strip()
            if s:
                entries.append(MemoryEntry(entry_id=f"m-{len(entries):04d}",
                                           content=s, created_at=None,
                                           source_turn=current or "unknown"))
        return MemorySnapshot(format="files", dumped_at=now_utc(),
                              entries=entries, raw=None)

    def dump_actions(self) -> ActionDump:
        return ActionDump(actions=[], coverage="unknown")

    def fs_snapshot(self) -> list[str] | None:
        """VM 用户区文件清单（~ 下 4 层，排除 hermes 自身与缓存噪音）。"""
        cmd = ("find ~ -maxdepth 4 \\( -name .hermes -o -name .cache -o -name .config "
               "-o -name node_modules -o -name .local -o -name .kylinbot \\) -prune -o "
               "-printf '%p\\n' 2>/dev/null | sed 's|^/home/okim|~|'")
        rc, out, _ = self.ch.run(cmd, timeout=60)
        return [ln for ln in out.splitlines() if ln.strip()] if rc == 0 else None


def _strip_tui(out: str) -> str:
    """取 Hermes 回复框（╭─ ☤ Hermes ─╮…╰─╯）内正文；无框时退化为去噪。"""
    lines = out.splitlines()
    blocks: list[str] = []
    i = 0
    while i < len(lines):
        if ("╭" in lines[i] or "┌" in lines[i]) and "Hermes" in lines[i]:
            j = i + 1
            block: list[str] = []
            while j < len(lines) and "╰" not in lines[j] and "└" not in lines[j]:
                block.append(lines[j].lstrip("│ ").rstrip())
                j += 1
            text = "\n".join(block).strip()
            if text:
                blocks.append(text)
            i = j
        i += 1
    if blocks:
        return "\n".join(blocks)
    noise = ("Query:", "Initializing", "⚠", "⏳", "❌", "Session:", "Resume", "Duration",
             "Title:", "Messages:")
    keep = [ln.rstrip() for ln in lines
            if ln.strip() and not any(n in ln for n in noise)
            and not set(ln.strip()) & set("╭╮╰╯│┌┐└┘")]
    return "\n".join(keep).strip()
