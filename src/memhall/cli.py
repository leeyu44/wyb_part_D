"""CLI 入口：memhall run / report。

run:    载入用例 → Runner 编排 → 评分引擎 → 指标/雷达图/报告，一次出齐
report: 对已有 run 目录重渲染报告（不重跑智能体）
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import yaml

from memhall.adapters.mock import MockAdapter
from memhall.runner.orchestrator import run_suite
from memhall.schema.models_case import MemoryCase
from memhall.scoring.engine import evaluate_case
from memhall.scoring.judge import OpenAICompatJudge
from memhall.report import compute_metrics, render_radar, render_report
from memhall.schema.evidence import Verdict


def load_cases(case_dir: Path) -> list[MemoryCase]:
    cases = []
    for path in sorted(case_dir.rglob("*.yaml")):
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        cases.append(MemoryCase.model_validate(raw))
    return cases


def _finish_run(run_dir: Path, run_id: str, manifest: dict,
                verdicts: list[Verdict], cases: dict[str, MemoryCase]) -> dict:
    metrics = compute_metrics(verdicts, cases)
    (run_dir / "metrics.json").write_text(
        json.dumps(metrics, ensure_ascii=False, indent=2), encoding="utf-8")
    with (run_dir / "verdicts.jsonl").open("w", encoding="utf-8") as f:
        for v in verdicts:
            f.write(v.model_dump_json() + "\n")
    render_radar({manifest.get("adapter", "agent"): metrics["capability_scores"]},
                 str(run_dir / "radar.png"))
    report = render_report(run_dir, run_id, manifest, verdicts, cases, metrics)
    (run_dir / "report.md").write_text(report, encoding="utf-8")
    return metrics


def cmd_run(args: argparse.Namespace) -> int:
    case_dir = Path(args.cases)
    cases = load_cases(case_dir)
    if not cases:
        print(f"未找到用例: {case_dir}", file=sys.stderr)
        return 1

    adapters: dict = {"mock": MockAdapter}
    if args.adapter == "hermes":
        from memhall.adapters.hermes import HermesAdapter
        adapters["hermes"] = HermesAdapter
    elif args.adapter == "kylinbot":
        from memhall.adapters.kylinbot import KylinBotAdapter
        adapters["kylinbot"] = KylinBotAdapter
    if args.adapter not in adapters:
        print(f"未知适配器: {args.adapter}（可选: {', '.join(adapters)}）", file=sys.stderr)
        return 1
    adapter = adapters[args.adapter]()

    judges = OpenAICompatJudge.pair_from_env() if args.judge == "dual" else None
    if args.judge == "dual" and judges is None:
        print("缺少 JUDGE_A_ 环境变量，回退脚本判卷", file=sys.stderr)

    run_id, stores = run_suite(adapter, cases, Path(args.out), args.adapter)
    run_dir = Path(args.out) / run_id
    manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))

    verdicts = []
    for case, store in zip(cases, stores):
        verdicts.extend(evaluate_case(case, store, run_id, judges))
    metrics = _finish_run(run_dir, run_id, manifest, verdicts, {c.case_id: c for c in cases})

    print(f"run_id: {run_id}")
    print(f"总体正确率: {metrics['overall_score']:.1%}"
          f"（有效 {metrics['n_valid']}/{metrics['n_probes_total']}，"
          f"规则判卷率 {metrics['rule_scoring_rate']:.0%}）")
    for cap, score in metrics["capability_scores"].items():
        print(f"  {cap:<14} {score:.0%}")
    print(f"产物: {run_dir}")
    return 0


def _load_verdicts(run_dir: Path, manifest: dict,
                   cases: dict[str, MemoryCase], judges) -> list[Verdict]:
    """优先读已落盘 verdicts；缺则从证据 JSONL 重放评分（评测贵、评分便宜）。"""
    vpath = run_dir / "verdicts.jsonl"
    if vpath.exists():
        return [Verdict.model_validate(json.loads(line))
                for line in vpath.read_text(encoding="utf-8").splitlines()]
    from memhall.schema.evidence import Evidence
    from memhall.scoring.engine import evaluate_case
    from memhall.scoring.rules import EvidenceStore
    verdicts: list[Verdict] = []
    for cid in manifest["cases"]:
        case = cases[cid]
        ev_path = run_dir / "cases" / cid / "evidence.jsonl"
        store = EvidenceStore([Evidence.model_validate(json.loads(line))
                               for line in ev_path.read_text(encoding="utf-8").splitlines()])
        verdicts.extend(evaluate_case(case, store, manifest["run_id"], judges))
    return verdicts


def cmd_report(args: argparse.Namespace) -> int:
    run_dir = Path(args.run_dir)
    manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    repo_root = Path(__file__).resolve().parents[2]
    cases = {c.case_id: c for c in load_cases(repo_root / "cases")}
    judges = OpenAICompatJudge.pair_from_env() if args.judge == "dual" else None
    verdicts = _load_verdicts(run_dir, manifest, cases, judges)
    metrics = _finish_run(run_dir, manifest["run_id"], manifest, verdicts, cases)
    print(f"报告已出: {run_dir / 'report.md'}（总体 {metrics['overall_score']:.1%}）")
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(prog="memhall",
                                     description="麟阁：智能体记忆能力评测基准")
    sub = parser.add_subparsers(dest="command", required=True)

    p_run = sub.add_parser("run", help="跑一轮评测并出报告")
    p_run.add_argument("-a", "--adapter", default="mock", help="适配器名（默认 mock）")
    p_run.add_argument("-c", "--cases", default="cases/full", help="用例目录")
    p_run.add_argument("-o", "--out", default="runs", help="输出根目录")
    p_run.add_argument("--judge", choices=["scripted", "dual"], default="scripted",
                       help="判卷方式（dual=LLM 判卷[单判或双判，按 JUDGE_B 是否配置]）")
    p_run.set_defaults(func=cmd_run)

    p_rep = sub.add_parser("report", help="出报告（缺 verdicts 时从证据重放评分）")
    p_rep.add_argument("run_dir", help="runs/ 下的 run 目录")
    p_rep.add_argument("--judge", choices=["scripted", "dual"], default="scripted",
                       help="重放评分时的判卷方式")
    p_rep.set_defaults(func=cmd_report)

    args = parser.parse_args()
    raise SystemExit(args.func(args))
