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

from memhall.adapters.base import AgentAdapter, AgentUnavailable
from memhall.adapters.remote import SshChannel, b64, elapsed_ms, now_utc
from memhall.schema.evidence import ActionDump, MemoryEntry, MemorySnapshot, Reply

HERMES_BIN = "~/.hermes/bin/hermes"
MEM_DIR = "~/.hermes/memories"
AGENT_LOG = "~/.hermes/logs/agent.log"

# 用例注入的虚构工作区（评测专用 VM，reset 一并清掉防跨轮污染）
EVAL_WORKDIRS = ["~/dev", "~/work", "~/proj", "~/docs", "~/notes",
                 "~/out", "~/scripts", "~/templates", "~/demo"]

_BOX_NOISE = re.compile(r"[╭╮╰╯│┌┐└┘]|\s─")


class HermesAdapter(AgentAdapter):
    """被测智能体 Hermes Agent（v0.21.x，VM 内 ~/.hermes 部署，内置记忆）。"""

    name = "hermes"

    def __init__(self, channel: SshChannel | None = None):
        self.ch = channel or SshChannel()
        self._log_offset = 0
        self._clock_epoch: int | None = None
        # 统一模型模式（GATEWAY_VM_URL，VM 走宿主网关的明文 http 腿）优先：
        # 网关改写 model、真凭据只在网关侧
        from memhall.gateway import gateway_settings
        gw = gateway_settings("hermes", vm_lane=True) or {}
        self._key = gw.get("key") or os.environ.get("AGENT_LLM_KEY", "")
        self._url = gw.get("base_url") or os.environ.get("AGENT_LLM_BASE_URL", "")
        self._model = gw.get("model") or os.environ.get("AGENT_LLM_MODEL", "qwen3.7-plus")

    def _llm_env_lines(self) -> str:
        """网关凭据的 env 文件内容（send 走 stdin；systest 离线脚本 source 用）。

        必须 export——`. file` source 裸赋值只设 shell 局部变量，hermes 子进程
        看不到（2026-10-02 VM 实跑逮到的坑）。值加单引号防空白/特殊字符劈碎。
        """
        def _q(v: str) -> str:
            return "'" + v.replace("'", "'\\''") + "'"
        return (f"export DEEPSEEK_API_KEY={_q(self._key)}\n"
                f"export DEEPSEEK_BASE_URL={_q(self._url)}\n"
                f"export DEEPSEEK_MODEL={_q(self._model)}\n")

    def reset(self) -> None:
        rc, _, err = self.ch.run(
            f"rm -f {MEM_DIR}/MEMORY.md {MEM_DIR}/USER.md && "
            f"rm -rf {' '.join(EVAL_WORKDIRS)} && echo ok")
        if rc != 0:
            raise RuntimeError(f"Hermes 记忆清零失败: {err.strip()[:300]}")
        # 记 agent.log 偏移：dump_actions 只解析本 case 增量
        rc, out, _ = self.ch.run(f"wc -c < {AGENT_LOG} 2>/dev/null || echo 0")
        self._log_offset = int(out.strip() or 0)

    def send(self, session_id: str, message: str) -> Reply:
        # 凭据与消息都走 stdin（base64），命令行零明文：VM 内 ps/history
        # 不可见，值含引号/特殊字符也不会把命令拼碎。
        # stdin 先 cat 成临时文件再按行拆（管道上 head/tail 直接分段会丢数据——
        # head 无法回退 seek，超读部分即吞掉，VM 实跑逮到 query 为空）。
        # env 文件 600 权限即删；hermes 从消息文件读，不再用 stdin。
        script = (
            'd=$(mktemp -t mh-stdin.XXXXXX) && cat > "$d" && '
            'e=$(mktemp -t mh-env.XXXXXX) && head -n1 "$d" | base64 -d > "$e" '
            '&& chmod 600 "$e" && . "$e" && rm -f "$e" && '
            'm=$(mktemp -t mh-msg.XXXXXX) && tail -n +2 "$d" | base64 -d > "$m" '
            '&& rm -f "$d" && '
            f'timeout 280 {HERMES_BIN} chat --query-file "$m" '
            '--oneshot --provider deepseek --model "$DEEPSEEK_MODEL" 2>/dev/null; '
            'rm -f "$m"')
        sent = now_utc()
        t0 = time.time()
        rc, out, _ = self.ch.run(script, timeout=300,
                                 stdin_data=b64(self._llm_env_lines()) + "\n"
                                            + b64(message))
        text = _strip_tui(out)
        if rc != 0 and not text:
            raise AgentUnavailable(f"hermes 调用失败({rc})")
        # hermes 后端故障文案（已实测两种）：不让错误文本混进答案被当行为评分
        if ("API failed after" in text or "Final error" in text
                or "server error" in text.lower()):
            raise AgentUnavailable(f"hermes 后端不可用: {text[:200]}")
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
        """解析 agent.log 本 case 增量里的工具调用（agent.tool_executor 行）。"""
        from memhall.schema.evidence import Action, ActionSource
        cmd = (f"tail -c +{self._log_offset + 1} {AGENT_LOG} 2>/dev/null | "
               f"grep -oE 'tool_executor: tool [a-z_0-9-]+ (completed|returned)' | "
               f"sed 's/tool_executor: //'")
        rc, out, _ = self.ch.run(cmd, timeout=30)
        actions: list[Action] = []
        if rc == 0:
            for i, line in enumerate(out.splitlines(), 1):
                parts = line.split()
                if len(parts) >= 3 and parts[0] == "tool":
                    actions.append(Action(
                        action_id=f"a-{i:03d}",
                        ts=now_utc(),
                        tool=parts[1],
                        args={},
                        result=parts[2],
                        source=ActionSource.AGENT_LOG,
                    ))
        return ActionDump(actions=actions,
                          coverage="partial" if actions else "unknown")

    def clock_shift(self, days: int) -> None:
        """VM 拨钟（sudo date -s，密码走 stdin），记录原时刻供恢复。"""
        if days == 0:
            return
        rc, out, _ = self.ch.run("date +%s")
        if rc != 0:
            raise RuntimeError("拨钟前读取系统时间失败")
        self._clock_epoch = int(out.strip())
        rc, _, err = self.ch.sudo(f"date -s '+{days} days' >/dev/null 2>&1 && echo ok")
        if rc != 0:
            raise RuntimeError(f"拨钟失败: {err.strip()[:200]}")

    def clock_restore(self) -> None:
        epoch = getattr(self, "_clock_epoch", None)
        if epoch is None:
            return
        self.ch.sudo(f"date -s @{epoch} >/dev/null 2>&1 && echo ok")
        self._clock_epoch = None

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
    # 无框退化路径：滤 TUI 状态行（API 重试/加载提示）与噪声头
    noise = ("Query:", "Initializing", "⚠", "⏳", "❌", "Session:", "Resume", "Duration",
             "Title:", "Messages:", "🔁", "💀", "Transient", "Retrying", "rebuilt client",
             "Provider said", "API failed")
    keep = [ln.rstrip() for ln in lines
            if ln.strip() and not any(n in ln for n in noise)
            and not set(ln.strip()) & set("╭╮╰╯│┌┐└┘─")]
    return "\n".join(keep).strip()
