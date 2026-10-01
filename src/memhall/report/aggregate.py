"""多次重跑的方差聚合：六维与总分 mean±std（design.md 方差机制化）。

同一智能体同一题库跑 N 轮 → 报均值±样本标准差，替代单轮裸分数；
单轮分数的随机波动（如概率性丢写）由此从"素材"变成"报告字段"。
主流评测（LongMemEval/LoCoMo 系）均以多轮统计为口径。
"""

from __future__ import annotations

import json
import math
from pathlib import Path

from memhall.report.metrics import CAP_LABELS_ZH

_CAP_ORDER = ["persist", "recall", "dynamic_update",
              "discriminate", "boundary", "reuse"]


def _mean_std(xs: list[float]) -> tuple[float, float]:
    n = len(xs)
    mean = sum(xs) / n
    if n < 2:
        return mean, 0.0
    var = sum((x - mean) ** 2 for x in xs) / (n - 1)  # 样本方差 ddof=1
    return mean, math.sqrt(var)


def aggregate_runs(run_dirs: list[Path], out_dir: Path) -> dict:
    """聚合 N 轮运行：要求同智能体同题库，否则视为口径混杂直接报错。"""
    runs = []
    for d in run_dirs:
        manifest = json.loads((d / "manifest.json").read_text(encoding="utf-8"))
        metrics = json.loads((d / "metrics.json").read_text(encoding="utf-8"))
        runs.append((manifest, metrics))

    keys = {(m.get("adapter"), m.get("cases")) for m, _ in runs}
    if len(keys) != 1:
        raise ValueError(f"口径混杂，拒绝聚合：{sorted(map(str, keys))}（应同智能体同题库）")

    caps = {}
    for cap in _CAP_ORDER:
        scores = [mt["capability_scores"].get(cap) for _, mt in runs]
        if any(s is None for s in scores):
            continue
        mean, std = _mean_std(scores)
        caps[cap] = {"label_zh": CAP_LABELS_ZH[cap], "n": len(scores),
                     "mean": round(mean, 4), "std": round(std, 4),
                     "min": round(min(scores), 4), "max": round(max(scores), 4)}
    overall_m, overall_s = _mean_std([mt["overall_score"] for _, mt in runs])

    result = {
        "adapter": runs[0][0].get("adapter"),
        "cases": runs[0][0].get("cases"),
        "n_runs": len(runs),
        "overall": {"mean": round(overall_m, 4), "std": round(overall_s, 4)},
        "capabilities": caps,
        "runs": [{"run_id": m.get("run_id"),
                  "overall_score": round(mt["overall_score"], 4)} for m, mt in runs],
    }
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "aggregate.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return result


def format_table(result: dict) -> str:
    lines = [f"{result['adapter']} × {result['cases']}　{result['n_runs']} 轮",
             f"总分：{result['overall']['mean']:.1%} ± {result['overall']['std']:.1%}",
             "维度｜均值±标准差（min~max）"]
    for cap, c in result["capabilities"].items():
        lines.append(f"  {c['label_zh']}：{c['mean']:.1%} ± {c['std']:.1%}"
                     f"（{c['min']:.0%}~{c['max']:.0%}，n={c['n']}）")
    return "\n".join(lines)
