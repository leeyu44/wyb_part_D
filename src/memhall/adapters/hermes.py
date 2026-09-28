"""HermesAdapter —— Hermes Agent 适配器（SSH 远程驱动，跑在 openKylin VM 里）。

对接方式（2026-09-28 定，源码核实 D:/hermes/hermes-agent）：
- send:    `hermes -q "<msg>" --oneshot` —— 非 TTY 下即答即退，
           每条消息独立进程 = 天然跨会话（长期记忆只能靠持久层，正中考点）
- 记忆库:  holographic 插件（纯本地 SQLite+FTS5，无云依赖）
           ~/.hermes/memory_store.db 的 facts 表(content/category/trust_score/…)
- reset:   删 memory_store.db（+ WAL/SHM 残留）
- end_session: no-op（oneshot 进程即退）
- dump_actions: 暂无操作日志源，coverage=unknown（W3 接 auditd/日志后升级）
"""

from __future__ import annotations

import time
from datetime import datetime, timezone

from memhall.adapters.base import AgentAdapter
from memhall.adapters.remote import SshChannel, b64, elapsed_ms, now_utc
from memhall.schema.evidence import (
    ActionDump,
    MemoryEntry,
    MemorySnapshot,
    Reply,
)

HERMES_BIN = "~/.hermes/bin/hermes"
MEMORY_DB = "~/.hermes/memory_store.db"

# 只读导出 facts 表（远端 python3 标准库即可，无三方依赖）
_DUMP_SRC = (
    "import json,sqlite3,os\n"
    f"db=os.path.expanduser('{MEMORY_DB}')\n"
    "c=sqlite3.connect('file:'+db+'?mode=ro',uri=True)\n"
    "rows=c.execute('select fact_id,content,category,trust_score,created_at "
    "from facts order by fact_id').fetchall()\n"
    "print(json.dumps(rows,ensure_ascii=False))\n"
)


class HermesAdapter(AgentAdapter):
    """被测智能体 Hermes Agent（v0.21.x，VM 内 ~/.hermes 部署）。"""

    name = "hermes"

    def __init__(self, channel: SshChannel | None = None):
        self.ch = channel or SshChannel()

    def reset(self) -> None:
        rc, _, err = self.ch.run(
            f"rm -f {MEMORY_DB} {MEMORY_DB}-wal {MEMORY_DB}-shm && echo ok")
        if rc != 0:
            raise RuntimeError(f"Hermes 记忆清零失败: {err.strip()[:300]}")

    def send(self, session_id: str, message: str) -> Reply:
        # 消息体 base64 传输，规避 shell 引号与注入面
        cmd = (f"{HERMES_BIN} -q \"$(echo {b64(message)} | base64 -d)\" "
               f"--oneshot --quiet 2>/dev/null")
        sent = now_utc()
        t0 = time.time()
        rc, out, err = self.ch.run(cmd, timeout=300)
        text = out.strip()
        if rc != 0 and not text:
            raise RuntimeError(f"hermes 调用失败({rc}): {err.strip()[:300]}")
        return Reply(session_id=session_id, text=text,
                     sent_at=sent, reply_at=now_utc(),
                     latency_ms=elapsed_ms(t0), token_usage=None)

    def end_session(self, session_id: str) -> None:
        pass  # oneshot 每次独立进程，会话隔离天然成立

    def dump_memory(self) -> MemorySnapshot:
        # python 源码嵌入 shell 单引号：' -> '\'' 转义
        script = "python3 -c '" + _DUMP_SRC.replace("'", "'\\''") + "'"
        rows = self.ch.run_json(script)
        entries = [
            MemoryEntry(
                entry_id=f"m-{fact_id:04d}",
                content=content,
                created_at=_parse_ts(created_at),
                source_turn=f"facts[{category}]",
            )
            for fact_id, content, category, trust, created_at in rows
        ]
        return MemorySnapshot(format="sqlite", dumped_at=now_utc(),
                              entries=entries, raw=None)

    def dump_actions(self) -> ActionDump:
        # W3 接 auditd / hermes 会话日志后升级；先如实标 unknown
        return ActionDump(actions=[], coverage="unknown")


def _parse_ts(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None
