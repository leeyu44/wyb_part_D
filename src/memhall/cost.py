"""token 成本记账与预估（跑前弹窗数据源）。

数据闭环：网关逐请求记账（gateway-usage.jsonl）→ run_suite 前后各拍一次
快照求差 → manifest.token_usage → 下轮开跑前 estimate() 用历史 run 的
每 case 均摊值（中位数，抗废轮离群）预估本轮消耗。

注意两点：判卷流量不走网关，不在此账内（UI/CLI 文案已注明）；两轮评测
并发时差值会互相污染——麟阁 UI 单会话串行，现状可接受。
"""

from __future__ import annotations

import json
import logging
import statistics
from pathlib import Path

from memhall.gateway import aggregate_usage, default_log_path

log = logging.getLogger(__name__)

_KEYS = ("n", "errors", "prompt_tokens", "completion_tokens", "total_tokens")


def usage_snapshot(log_path: Path | None = None) -> dict[str, dict]:
    """网关记账当前累计（按智能体 tag）；无记账文件返回空表。"""
    return aggregate_usage(log_path or default_log_path()).get("agents", {})


def usage_delta(before: dict[str, dict], after: dict[str, dict]) -> dict[str, dict]:
    """两份快照之差：这段时间内各 tag 的净增流量。"""
    out: dict[str, dict] = {}
    for tag, a in after.items():
        b = before.get(tag, {})
        d = {k: a.get(k, 0) - b.get(k, 0) for k in _KEYS}
        if d["n"] > 0 or d["errors"] > 0:
            out[tag] = d
    return out


def summarize(delta: dict[str, dict]) -> dict | None:
    """memhall-* tag 的差值 → 可直接落 manifest 的单 run 汇总。"""
    rows = {t: v for t, v in delta.items() if t.startswith("memhall-")}
    if not rows:
        return None
    return {
        "requests": sum(v["n"] for v in rows.values()),
        "errors": sum(v["errors"] for v in rows.values()),
        "prompt_tokens": sum(v["prompt_tokens"] for v in rows.values()),
        "completion_tokens": sum(v["completion_tokens"] for v in rows.values()),
        "total_tokens": sum(v["total_tokens"] for v in rows.values()),
        "tags": rows,
    }


def estimate(adapter: str, n_cases: int, runs_root: Path) -> dict | None:
    """按历史 run 的 token_usage 均摊到 case，预估本轮总消耗。

    只认 run 目录名以数字开头的历史（跳过 _void/_agg/_compare 等产物目录）；
    无任何记账历史（首跑 / 直连模式）返回 None，调用方提示后照常开跑。"""
    if not runs_root.is_dir() or n_cases <= 0:
        return None
    per_case: list[float] = []
    per_req: list[float] = []
    for mf in sorted(runs_root.glob("*/manifest.json")):
        if not mf.parent.name[:1].isdigit():
            continue
        try:
            m = json.loads(mf.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if m.get("adapter") != adapter:
            continue
        tu = m.get("token_usage") or {}
        n = len(m.get("cases") or [])
        if not n or not tu.get("total_tokens"):
            continue
        per_case.append(tu["total_tokens"] / n)
        if tu.get("requests"):
            per_req.append(tu["requests"] / n)
    if not per_case:
        return None
    out: dict = {
        "total_tokens": int(statistics.median(per_case) * n_cases),
        "tokens_per_case": int(statistics.median(per_case)),
        "basis_runs": len(per_case),
        "n_cases": n_cases,
    }
    if per_req:
        out["requests"] = int(statistics.median(per_req) * n_cases)
    return out


def fmt_tokens(n: int) -> str:
    return f"{n / 1e6:.1f}M" if n >= 1e6 else f"{n / 1e3:.0f}k"
