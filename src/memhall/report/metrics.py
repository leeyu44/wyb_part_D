"""指标计算：Verdict 列表 → 六维能力分 + 错误分解 + 判卷方式统计。"""

from __future__ import annotations

from collections import Counter, defaultdict

from memhall.schema.evidence import DecidedBy, Verdict, VerdictValue
from memhall.schema.models_case import Capability, MemoryCase

CAP_ORDER = [
    Capability.PERSIST, Capability.RECALL, Capability.DYNAMIC_UPDATE,
    Capability.DISCRIMINATE, Capability.BOUNDARY, Capability.REUSE,
]
CAP_LABELS_ZH = {
    Capability.PERSIST: "长期保持",
    Capability.RECALL: "记忆调用",
    Capability.DYNAMIC_UPDATE: "动态更新",
    Capability.DISCRIMINATE: "相近区分",
    Capability.BOUNDARY: "边界识别",
    Capability.REUSE: "任务复用",
}


def compute_metrics(verdicts: list[Verdict], cases: dict[str, MemoryCase]) -> dict:
    """六维雷达 + 错误分解。invalid_run 不计入分母（运行无效单列）。"""
    by_cap: dict[Capability, list[Verdict]] = defaultdict(list)
    for v in verdicts:
        case = cases.get(v.case_id)
        if case is not None:
            by_cap[case.capability].append(v)

    capability_scores: dict[str, float] = {}
    detail: dict[str, dict] = {}
    for cap in CAP_ORDER:
        vs = by_cap.get(cap, [])
        valid = [v for v in vs if v.verdict not in (VerdictValue.INVALID_RUN,
                                                     VerdictValue.HUMAN_REVIEW)]
        correct = sum(1 for v in valid if v.verdict == VerdictValue.CORRECT)
        score = correct / len(valid) if valid else 0.0
        capability_scores[cap.value] = round(score, 4)
        errors = Counter(v.verdict.value for v in valid if v.verdict != VerdictValue.CORRECT)
        detail[cap.value] = {
            "label_zh": CAP_LABELS_ZH[cap],
            "n_probes": len(vs),
            "n_valid": len(valid),
            "n_correct": correct,
            "score": round(score, 4),
            "error_breakdown": dict(errors),
            "n_invalid_run": len(vs) - len(valid),
        }

    valid_all = [v for v in verdicts if v.verdict not in (VerdictValue.INVALID_RUN,
                                                              VerdictValue.HUMAN_REVIEW)]
    decided = Counter(v.decided_by.value for v in verdicts)
    # 写入卫生（design.md §8 边界维专属）：不该存的内容进记忆库的比例
    boundary = detail.get(Capability.BOUNDARY.value, {})
    bn = boundary.get("n_valid", 0)
    write_hygiene = (round(boundary.get("error_breakdown", {})
                           .get("over_persist", 0) / bn, 4)) if bn else None
    return {
        "n_probes_total": len(verdicts),
        "n_valid": len(valid_all),
        "overall_score": round(
            sum(1 for v in valid_all if v.verdict == VerdictValue.CORRECT) / len(valid_all), 4
        ) if valid_all else 0.0,
        "capability_scores": capability_scores,
        "capability_detail": detail,
        "decided_by": dict(decided),
        "rule_scoring_rate": round(
            decided.get("rule", 0) / len(verdicts), 4) if verdicts else 0.0,
        "write_hygiene": write_hygiene,
        # 判卷质检（design.md §8 评测质量指标）：仅 dual 判卷时有值
        "judge_agreement_rate": _agreement_rate(verdicts),
        "human_review_rate": round(
            sum(1 for v in verdicts
                if v.decided_by == DecidedBy.HUMAN_REVIEW) / len(verdicts), 4
        ) if verdicts else 0.0,
    }


def _agreement_rate(verdicts: list[Verdict]) -> float | None:
    """双判一致率：双评委原始票相同的比例（无 judge_meta/无双判时 None）。"""
    dual = [v for v in verdicts
            if v.judge_meta and v.judge_meta.judge_b is not None]
    if not dual:
        return None
    agreed = sum(1 for v in dual if v.judge_meta.judge_b.agreed)
    return round(agreed / len(dual), 4)
