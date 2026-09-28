"""KylinBotAdapter —— openKylin 3.0 内置智能体「小K」适配器（SSH 远程驱动）。

CLI 实测（2026-09-28 探明，environment.md §7 与 okim-bench/adapters/kylinbot.py 佐证）：
- send:    `kylin-bot agent -m "<msg>"` 免交互单发；LLM 后端可 --provider/--model 覆盖
- 记忆库:  ~/.kylinbot/workspace/memory/brain.db（SQLite+FTS5）
           memories(id,key,content,category,session_id,importance,superseded_by)
           superseded_by = 动态更新版本链证据（update 族判定金矿）
- reset:   `kylin-bot memory clear`（官方清库命令，比删文件稳）
- dump_actions: 暂 unknown（auditd/日志 W3 接）
"""

from __future__ import annotations

import os
import time
from datetime import datetime

from memhall.adapters.base import AgentAdapter
from memhall.adapters.remote import SshChannel, b64, elapsed_ms, now_utc
from memhall.schema.evidence import (
    ActionDump,
    MemoryEntry,
    MemorySnapshot,
    Reply,
)

BRAIN_DB = "~/.kylinbot/workspace/memory/brain.db"

_DUMP_SRC = (
    "import json,sqlite3,os\n"
    f"db=os.path.expanduser('{BRAIN_DB}')\n"
    "c=sqlite3.connect('file:'+db+'?mode=ro',uri=True)\n"
    "rows=c.execute('select id,key,content,category,superseded_by,created_at "
    "from memories order by id').fetchall()\n"
    "print(json.dumps(rows,ensure_ascii=False))\n"
)


class KylinBotAdapter(AgentAdapter):
    """被测智能体 KylinBot（openKylin 3.0 桌面版内置）。"""

    name = "kylinbot"

    def __init__(self, channel: SshChannel | None = None):
        self.ch = channel or SshChannel()
        self._model = os.environ.get("AGENT_LLM_MODEL", "")

    def reset(self) -> None:
        rc, out, err = self.ch.run("kylin-bot memory clear --all 2>&1 | tail -2")
        if rc != 0:
            raise RuntimeError(f"KylinBot 记忆清零失败: {(out + err).strip()[:300]}")

    def send(self, session_id: str, message: str) -> Reply:
        provider = os.environ.get("AGENT_LLM_PROVIDER", "")
        flags = f" --provider {provider}" if provider else ""
        flags += f" --model {self._model}" if self._model else ""
        cmd = (f'kylin-bot agent -m "$(echo {b64(message)} | base64 -d)"{flags} '
               f"2>/dev/null")
        sent = now_utc()
        t0 = time.time()
        rc, out, err = self.ch.run(cmd, timeout=300)
        text = out.strip()
        if rc != 0 and not text:
            raise RuntimeError(f"kylin-bot 调用失败({rc}): {err.strip()[:300]}")
        return Reply(session_id=session_id, text=text,
                     sent_at=sent, reply_at=now_utc(),
                     latency_ms=elapsed_ms(t0), token_usage=None)

    def end_session(self, session_id: str) -> None:
        pass  # agent 单发模式无长会话

    def dump_memory(self) -> MemorySnapshot:
        script = "python3 -c '" + _DUMP_SRC.replace("'", "'\\''") + "'"
        try:
            rows = self.ch.run_json(script)
        except RuntimeError:
            return MemorySnapshot(format="sqlite", dumped_at=now_utc(),
                                  entries=[], raw=None)
        entries = [
            MemoryEntry(
                entry_id=f"m-{mid:04d}",
                content=(f"[已被{superseded}取代] {content}" if superseded
                         else f"[{category}] {key}: {content}"),
                created_at=_parse_ts(created_at),
                source_turn=f"memories[{key}]",
            )
            for mid, key, content, category, superseded, created_at in rows
        ]
        return MemorySnapshot(format="sqlite", dumped_at=now_utc(),
                              entries=entries, raw=None)

    def dump_actions(self) -> ActionDump:
        return ActionDump(actions=[], coverage="unknown")

    def fs_snapshot(self) -> list[str] | None:
        cmd = ("find ~ -maxdepth 4 \\( -name .hermes -o -name .cache -o -name .config "
               "-o -name node_modules -o -name .local -o -name .kylinbot \\) -prune -o "
               "-printf '%p\\n' 2>/dev/null | sed 's|^/home/okim|~|'")
        rc, out, _ = self.ch.run(cmd, timeout=60)
        return [ln for ln in out.splitlines() if ln.strip()] if rc == 0 else None


def _parse_ts(ts):
    if not ts:
        return None
    try:
        return datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except ValueError:
        return None
