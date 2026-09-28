"""评分引擎：case 探测点 × 证据 → 契约 03 Verdict 列表。

RuleProbe -> 断言链（scoring/rules.py，确定性）
JudgeProbe -> 判卷器（scripted 离线 / 双 LLM judge 在线，scoring/judge.py）
"""

from __future__ import annotations

from typing import Optional

from memhall.schema.evidence import (
    DecidedBy,
    EvidenceType,
    JudgeMeta,
    JudgeMetaItem,
    Verdict,
    VerdictValue,
)
from memhall.schema.models_case import JudgeProbe, MemoryCase, RuleProbe
from memhall.scoring.judge import (
    JudgeOutcome,
    OpenAICompatJudge,
    ScriptedJudge,
    dual_judge,
)
from memhall.scoring.rules import EvidenceStore, run_check

_scripted = ScriptedJudge()


def _answer_for(store: EvidenceStore, ask: str) -> str:
    """取 probe 段中对该问题的回复（按 message 匹配，取最后一条）。"""
    best = ""
    for ev in store.by_type(EvidenceType.DIALOGUE):
        msgs = ev.payload.get("messages", [])
        replies = ev.payload.get("replies", [])
        for msg, rep in zip(msgs, replies):
            if _norm_pair(msg, ask):
                best = rep.get("text", "")
    return best


def _norm_pair(message: str, ask: str) -> bool:
    a, b = message.strip(), ask.strip()
    return a == b or b in a or a in b


def _judge_verdict(probe: JudgeProbe, store: EvidenceStore, run_id: str, seq: int,
                   dual: Optional[tuple[OpenAICompatJudge, OpenAICompatJudge]]) -> Verdict:
    answer = _answer_for(store, probe.ask)
    outcome: JudgeOutcome
    decided_by: DecidedBy

    if dual is not None:
        try:
            outcome = dual_judge(probe, answer, *dual)
            decided_by = DecidedBy.ARBITRATION if outcome.arbitrated else DecidedBy.JUDGE_A
            meta = JudgeMeta(
                judge_a=JudgeMetaItem(model=outcome.judge_a or "",
                                      verdict=VerdictValue(outcome.judge_a_raw)
                                      if outcome.judge_a_raw else VerdictValue.INVALID_RUN,
                                      agreed=(outcome.judge_a_raw == outcome.judge_b_raw)),
                judge_b=JudgeMetaItem(model=outcome.judge_b or "",
                                      verdict=VerdictValue(outcome.judge_b_raw)
                                      if outcome.judge_b_raw else VerdictValue.INVALID_RUN,
                                      agreed=(outcome.judge_a_raw == outcome.judge_b_raw)),
                prompt_version="judge-prompt-v1",
                arbiter="judge_a-arbiter" if outcome.arbitrated else None,
            )
        except RuntimeError as e:
            # judge 端点彻底不可用：降级脚本判卷，评测不因 judge 挂而报废
            outcome = _scripted.judge(probe, answer)
            decided_by = DecidedBy.HUMAN_REVIEW
            meta = None
            reason = f"[judge 降级] {e}: {outcome.reason}"
            outcome = JudgeOutcome(outcome.key, 0.0, outcome.evidence_refs, reason)
    else:
        outcome = _scripted.judge(probe, answer)
        decided_by = DecidedBy.JUDGE_A
        meta = None

    if outcome.key is None:
        value, confidence = VerdictValue.INVALID_RUN, 0.0
    else:
        value = VerdictValue(probe.verdict_map.get(outcome.key, "invalid_run"))
        confidence = outcome.confidence

    return Verdict(
        verdict_id=f"v-{seq:04d}",
        probe_id=probe.id,
        case_id=probe.id.rsplit("-", 1)[0],
        run_id=run_id,
        verdict=value,
        confidence=confidence,
        decided_by=decided_by,
        evidence_refs=outcome.evidence_refs,
        explanation=outcome.reason,
        judge_meta=meta,
    )


def _rule_verdict(probe: RuleProbe, store: EvidenceStore, run_id: str, seq: int) -> Verdict:
    check = [b.model_dump(by_alias=True) for b in probe.check]
    value, idx = run_check(check, store)
    branch = probe.check[idx]
    return Verdict(
        verdict_id=f"v-{seq:04d}",
        probe_id=probe.id,
        case_id=probe.id.rsplit("-", 1)[0],
        run_id=run_id,
        verdict=VerdictValue(value),
        confidence=1.0,
        decided_by=DecidedBy.RULE,
        evidence_refs=probe.evidence_ref or ["evidence.jsonl"],
        explanation=f"断言命中: {branch.assert_name}（第 {idx + 1} 分支）",
    )


def evaluate_case(case: MemoryCase, store: EvidenceStore, run_id: str,
                  dual: Optional[tuple[OpenAICompatJudge, OpenAICompatJudge]] = None
                  ) -> list[Verdict]:
    out: list[Verdict] = []
    for i, probe in enumerate(case.probes, start=1):
        if isinstance(probe, RuleProbe):
            out.append(_rule_verdict(probe, store, run_id, i))
        else:
            out.append(_judge_verdict(probe, store, run_id, i, dual))
    return out
