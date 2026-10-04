"""评分引擎：case 探测点 × 证据 → 契约 03 Verdict 列表。

RuleProbe -> 断言链（scoring/rules.py，确定性）
JudgeProbe -> 判卷器（scripted 离线 / 双 LLM judge 在线，scoring/judge.py）
"""

from __future__ import annotations

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
from memhall.scoring.rules import EvidenceMissing, EvidenceStore, run_check

_scripted = ScriptedJudge()

# 阶段先后序：after: inject 的探测点只能看到 inject 及更早的证据
# （boundary canary 在教学时点判——probe 段"作废后删除"洗白不了 over_persist）
_PHASE_RANK = {"inject": 0, "confound": 1, "probe": 2}


def _store_upto(store: EvidenceStore, after: str) -> EvidenceStore:
    """按探测点声明的 after 截取证据视图（engine 侧实现契约 02 的阶段语义）。"""
    limit = _PHASE_RANK.get(after, 2)
    items = [e for e in store.items() if _PHASE_RANK.get(e.phase.value, 2) <= limit]
    return EvidenceStore(items)


def _answer_for(store: EvidenceStore, ask: str) -> str:
    """取 probe 段中对该问题的回复。精确话术优先（lint 已保证 ask 与 probe 段
    某条 user 逐字一致），无精确命中再退子串；同问多次取最后一条。
    子串双向包含曾把措辞相近的相邻探测问题配错回复——精确层先行分流。"""
    exact = ""
    fuzzy = ""
    for ev in store.by_type(EvidenceType.DIALOGUE):
        msgs = ev.payload.get("messages", [])
        replies = ev.payload.get("replies", [])
        # strict=False：回放的是历史 run 落盘数据，容错旧证据长度漂移
        for msg, rep in zip(msgs, replies, strict=False):
            if msg.strip() == ask.strip():
                exact = rep.get("text", "")
            elif _norm_pair(msg, ask):
                fuzzy = rep.get("text", "")
    return exact or fuzzy


def _norm_pair(message: str, ask: str) -> bool:
    a, b = message.strip(), ask.strip()
    return a == b or b in a or a in b


def _raw_verdict(key: str | None, probe: JudgeProbe) -> VerdictValue:
    """judge 原始输出（verdict_map 的 key）→ 五态；非法/缺失 → invalid_run。"""
    if key is None:
        return VerdictValue.INVALID_RUN
    return VerdictValue(probe.verdict_map.get(key, "invalid_run"))


def _judge_verdict(probe: JudgeProbe, store: EvidenceStore, run_id: str, seq: int,
                   judges: tuple[OpenAICompatJudge, ...] | None) -> Verdict:
    answer = _answer_for(store, probe.ask)
    if "[RUNTIME_ERROR]" in answer:
        return Verdict(
            verdict_id=f"v-{seq:04d}", probe_id=probe.id,
            case_id=probe.id.rsplit("-", 1)[0], run_id=run_id,
            verdict=VerdictValue.INVALID_RUN, confidence=0.0,
            decided_by=DecidedBy.RULE, evidence_refs=["transcript:answer"],
            explanation="运行无效：被测智能体后端不可用，不计入分母",
        )
    outcome: JudgeOutcome
    decided_by: DecidedBy

    if judges is not None:
        try:
            outcome = dual_judge(probe, answer, *judges)
            decided_by = DecidedBy.ARBITRATION if outcome.arbitrated else DecidedBy.JUDGE_A
            meta = JudgeMeta(
                judge_a=JudgeMetaItem(model=outcome.judge_a or "",
                                      verdict=_raw_verdict(outcome.judge_a_raw, probe),
                                      agreed=(outcome.judge_a_raw == outcome.judge_b_raw)),
                judge_b=JudgeMetaItem(model=outcome.judge_b or "",
                                      verdict=_raw_verdict(outcome.judge_b_raw, probe),
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
        # 判卷未决（≠运行无效）：脚本判不了且无 LLM judge 可用——转人工，不计分
        value, confidence = VerdictValue.HUMAN_REVIEW, 0.0
        decided_by = DecidedBy.HUMAN_REVIEW
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
    try:
        value, idx = run_check(check, store)
    except EvidenceMissing as e:
        # 所需证据不在场（适配器不支持该证据源）——运行无效，不计入分母
        return Verdict(
            verdict_id=f"v-{seq:04d}",
            probe_id=probe.id,
            case_id=probe.id.rsplit("-", 1)[0],
            run_id=run_id,
            verdict=VerdictValue.INVALID_RUN,
            confidence=0.0,
            decided_by=DecidedBy.RULE,
            evidence_refs=probe.evidence_ref or ["evidence.jsonl"],
            explanation=f"运行无效：{e}",
        )
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
                  judges: tuple[OpenAICompatJudge, ...] | None = None
                  ) -> list[Verdict]:
    # case 级检查：任何一步的回复带 [RUNTIME_ERROR]（适配器挂掉）→ 整 case
    # 运行无效，不能静默降级成 omission/human_review（probe 可能根本没问）
    poisoned = any("[RUNTIME_ERROR]" in (r.get("text") or "")
                   for ev in store.by_type(EvidenceType.DIALOGUE)
                   for r in ev.payload.get("replies", []))
    out: list[Verdict] = []
    for i, probe in enumerate(case.probes, start=1):
        if poisoned:
            out.append(Verdict(
                verdict_id=f"v-{i:04d}", probe_id=probe.id,
                case_id=probe.id.rsplit("-", 1)[0], run_id=run_id,
                verdict=VerdictValue.INVALID_RUN, confidence=0.0,
                decided_by=DecidedBy.RULE, evidence_refs=["transcript:answer"],
                explanation="运行无效：被测智能体后端不可用，不计入分母"))
            continue
        if isinstance(probe, RuleProbe):
            out.append(_rule_verdict(probe, _store_upto(store, probe.after),
                                     run_id, i))
        else:
            out.append(_judge_verdict(probe, _store_upto(store, probe.after),
                                      run_id, i, judges))
    return out
