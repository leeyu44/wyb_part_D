"""两次运行对比：对比雷达 + 六维差值 + 判定翻转明细（design.md §10 结果追踪）。

同一智能体两次运行 → 看稳定性（哪些判定翻转）；
两个智能体 → 看能力轮廓差（分数差在哪一维）。
"""

from __future__ import annotations

import json
from pathlib import Path

from memhall.report.metrics import CAP_LABELS_ZH
from memhall.report.radar import render_radar
from memhall.report.report import VERDICT_ZH
from memhall.schema.evidence import Verdict

_CAP_ORDER = ["persist", "recall", "dynamic_update",
              "discriminate", "boundary", "reuse"]


def _load(run_dir: Path):
    manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    metrics = json.loads((run_dir / "metrics.json").read_text(encoding="utf-8"))
    verdicts: dict[str, Verdict] = {}
    for line in (run_dir / "verdicts.jsonl").read_text(encoding="utf-8").splitlines():
        v = Verdict.model_validate(json.loads(line))
        verdicts[v.probe_id] = v
    return manifest, metrics, verdicts


def _label(manifest: dict) -> str:
    return f"{manifest.get('adapter', '?')}·{manifest.get('run_id', '')[9:15] or '?'}"


def compare_runs(dir_a: Path, dir_b: Path, out_dir: Path) -> dict:
    ma, mta, va = _load(dir_a)
    mb, mtb, vb = _load(dir_b)
    out_dir.mkdir(parents=True, exist_ok=True)

    la, lb = _label(ma), _label(mb)
    render_radar({la: mta["capability_scores"], lb: mtb["capability_scores"]},
                 str(out_dir / "compare-radar.png"))

    common = sorted(set(va) & set(vb))
    flips = [(pid, va[pid], vb[pid]) for pid in common
             if va[pid].verdict != vb[pid].verdict]

    lines = [f"# 运行对比 · {la} vs {lb}", ""]
    da = mta["overall_score"]
    db = mtb["overall_score"]
    lines.append(f"- 总体正确率：{da:.1%} → {db:.1%}"
                 f"（{db - da:+.1%}）　共同探测点 {len(common)}"
                 f"　判定翻转 {len(flips)}")
    lines.append("")
    lines.append("| 能力 | " + la + " | " + lb + " | Δ |")
    lines.append("|---|---|---|---|")
    for cap in _CAP_ORDER:
        s1 = mta["capability_scores"].get(cap)
        s2 = mtb["capability_scores"].get(cap)
        if s1 is None and s2 is None:
            continue
        s1 = s1 if s1 is not None else 0.0
        s2 = s2 if s2 is not None else 0.0
        lines.append(f"| {CAP_LABELS_ZH.get(cap, cap)} | {s1:.0%} | {s2:.0%} "
                     f"| {(s2 - s1):+.0%} |")
    lines.append("")
    lines.append(f"## 判定翻转（{len(flips)}/{len(common)}）")
    lines.append("")
    if flips:
        lines.append("| 探测点 | " + la + " | " + lb + " |")
        lines.append("|---|---|---|")
        for pid, x, y in flips:
            lines.append(f"| {pid} | {VERDICT_ZH.get(x.verdict.value, x.verdict.value)} "
                         f"| {VERDICT_ZH.get(y.verdict.value, y.verdict.value)} |")
    else:
        lines.append("两次运行判定完全一致（稳定性满分）。")
    lines.append("")
    # 统一模型对账：两次运行的 model_backend 不一致时，能力差不能全归因于智能体
    pa, pb = ma.get("model_backend"), mb.get("model_backend")
    parity = None
    if pa and pb:
        same = (pa.get("mode") == pb.get("mode")
                and pa.get("model") == pb.get("model"))
        parity = {"same": same, "a": pa, "b": pb}
        if not same:
            lines.insert(2, f"> ⚠️ 模型口径不一致：{la} 用 `{pa.get('model')}`"
                            f"（{pa.get('mode')}），{lb} 用 `{pb.get('model')}`"
                            f"（{pb.get('mode')}）——能力差含模型因素，"
                            "用统一模型网关（memhall gateway）后重跑。")
            lines.insert(3, "")
    (out_dir / "compare.md").write_text("\n".join(lines), encoding="utf-8")

    return {"label_a": la, "label_b": lb, "n_common": len(common),
            "n_flips": len(flips), "overall_a": da, "overall_b": db,
            "model_parity": parity,
            "radar": str(out_dir / "compare-radar.png"),
            "report": str(out_dir / "compare.md")}
