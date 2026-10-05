"""Markdown 报告：manifest 摘要 + 六维表 + 判定明细（可下钻证据）。"""

from __future__ import annotations

import json
from pathlib import Path

from memhall.report.metrics import CAP_LABELS_ZH, CAP_ORDER, compute_metrics
from memhall.schema.evidence import Verdict
from memhall.schema.models_case import MemoryCase

VERDICT_ZH = {
    "correct": "✅ 正确", "omission": "遗漏", "confusion": "混淆",
    "fabrication": "记错(编造)", "over_persist": "错误持久化",
    "wrong_reuse": "错误复用", "invalid_run": "⚠️ 运行无效",
    "human_review": "⏳ 判卷未决(转人工)",
}


def render_report(run_dir: Path, run_id: str, manifest: dict,
                  verdicts: list[Verdict], cases: dict[str, MemoryCase],
                  metrics: dict) -> str:
    lines: list[str] = []
    lines.append(f"# 麟阁 MemHall 评测报告 · {run_id}")
    lines.append("")
    lines.append(f"- 被测智能体：`{manifest.get('adapter', '?')}`（适配器模式）")
    code_version = manifest.get("code_version", manifest.get("git_hash", "?"))
    lines.append(f"- 代码版本：`{code_version}`　用例数："
                 f"{len(manifest.get('cases', []))}　探测点：{metrics['n_probes_total']}")
    lines.append(f"- 总体正确率：**{metrics['overall_score']:.1%}**"
                 f"（有效 {metrics['n_valid']}/{metrics['n_probes_total']}，"
                 f"规则判卷率 {metrics['rule_scoring_rate']:.0%}）")
    wh = metrics.get("write_hygiene")
    if wh is not None:
        lines.append(f"- 写入卫生（不该记的记了）：{wh:.1%}")
    jm = manifest.get("judge", {})
    if jm:
        model = f"（{jm.get('model_a', '')}）" if jm.get("model_a") else ""
        lines.append(f"- 判卷口径：{jm.get('mode', '?')}{model}"
                     f" · 提示词版本 {jm.get('prompt_version', '?')}")
    cost = manifest.get("cost", {})
    agent_tokens = cost.get("agent_tokens", {})
    if agent_tokens.get("requests"):
        coverage = agent_tokens.get("coverage")
        value = (str(agent_tokens.get("total", 0))
                 if agent_tokens.get("reported_replies") else "未上报")
        coverage_text = f"{coverage:.1%}" if coverage is not None else "未知"
        lines.append(f"- 智能体 token：{value}（usage 覆盖 {coverage_text}）")
    judge_tokens = cost.get("judge_tokens", {})
    if judge_tokens.get("completions"):
        coverage = judge_tokens.get("coverage")
        coverage_text = f"{coverage:.1%}" if coverage is not None else "未知"
        lines.append(
            f"- Judge token：{judge_tokens.get('total', 0)}"
            f"（usage 覆盖 {coverage_text}）")
    lines.append("")
    lines.append("![六维雷达图](radar.png)")
    lines.append("")
    lines.append("## 六维能力")
    lines.append("")
    lines.append("| 能力 | 得分 | 正确/有效 | 错误分解 |")
    lines.append("|---|---|---|---|")
    for cap in CAP_ORDER:
        d = metrics["capability_detail"][cap]
        errs = "、".join(f"{VERDICT_ZH.get(k, k)}×{n}"
                         for k, n in d["error_breakdown"].items()) or "—"
        lines.append(f"| {CAP_LABELS_ZH[cap]} | {d['score']:.0%} | "
                     f"{d['n_correct']}/{d['n_valid']} | {errs} |")
    lines.append("")
    lines.append("## 判定明细")
    lines.append("")
    lines.append("| 探测点 | 能力 | 判定 | 判卷 | 置信 | 说明 |")
    lines.append("|---|---|---|---|---|---|")
    for v in sorted(verdicts, key=lambda x: x.probe_id):
        cap = cases[v.case_id].capability.value if v.case_id in cases else "?"
        reason = v.explanation.replace("|", "\\|")
        if len(reason) > 60:
            reason = reason[:60] + "…"
        lines.append(f"| {v.probe_id} | {cap} | {VERDICT_ZH.get(v.verdict.value, v.verdict.value)} "
                     f"| {v.decided_by.value} | {v.confidence:.2f} | {reason} |")
    lines.append("")
    lines.append(f"> 证据下钻：`runs/{run_id}/cases/<case_id>/evidence.jsonl`"
                 f"（每条判定引用对应证据哈希）")
    lines.append("")
    if manifest.get("adapter") == "mock":
        from memhall.adapters.mock import DESIGNED_PROFILE
        lines.append("---")
        lines.append("**mock 为缺陷注入基线**：分数是下列设计模式的确定输出，"
                     "用作管线回归与判卷自检，不是难度地板。")
        for cap, mode in DESIGNED_PROFILE.items():
            lines.append(f"- {cap}: {mode}")
        lines.append("")
    return "\n".join(lines)
