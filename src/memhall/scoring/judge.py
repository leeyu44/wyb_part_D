"""判卷器：脚本判卷（离线确定性）+ 双 LLM judge（在线，跨家族交叉仲裁）。

双 judge 框架移植自 okim-bench/scoring/judges.py（C 角色 W1 交付），
适配主线契约：judge 输出 verdict_map 的 key（如 new_path/old_path），
引擎再把 key 映射回主线五态判定值。
"""

from __future__ import annotations

import json
import os
import re
import string
from dataclasses import dataclass
from typing import Optional

from memhall.schema.models_case import JudgeProbe


@dataclass
class JudgeOutcome:
    """判卷结果：key 为 verdict_map 的 key；None 表示无法判定。"""

    key: Optional[str]
    confidence: float
    evidence_refs: list[str]
    reason: str
    judge_a: Optional[str] = None
    judge_b: Optional[str] = None
    judge_a_raw: Optional[str] = None
    judge_b_raw: Optional[str] = None
    arbitrated: bool = False


# ---------- 归一化 ----------

_STRIP = set(string.punctuation + "。，、；：？！“”‘’（） \t\n\r")


def _norm(s: str) -> str:
    return "".join(ch.lower() for ch in s if ch not in _STRIP)


_ABSTAIN = re.compile(r"不记得|不知道|没提过|没有记录|没听说过|无法确认|不清楚")


class ScriptedJudge:
    """离线脚本判卷：expect 匹配 + 锚例匹配 + 拒答关键词，全程确定性。

    无 API key / CI / 冒烟的默认判卷器；判不了显式返回 None（不瞎猜）。
    """

    name = "scripted"

    def judge(self, probe: JudgeProbe, answer: str) -> JudgeOutcome:
        ans_n, exp_n = _norm(answer), _norm(probe.expect)
        correct_keys = [k for k, v in probe.verdict_map.items() if v == "correct"]
        refs = ["transcript:answer"]

        for anchor in probe.anchors:
            a_n = _norm(anchor.reply)
            if not a_n:
                continue
            overlap = sum(1 for tok in _tokens(a_n) if tok in _tokens(ans_n))
            toks = _tokens(a_n)
            if toks and overlap / len(toks) >= 0.8:
                return JudgeOutcome(anchor.expect_verdict, 0.9, refs,
                                    f"锚例命中（重叠率 {overlap}/{len(toks)}）")
        if exp_n and exp_n in ans_n:
            return JudgeOutcome(correct_keys[0] if correct_keys else "correct",
                                0.95, refs, f"回答包含期望答案 {probe.expect}")
        if _ABSTAIN.search(answer):
            abstain_keys = [k for k, v in probe.verdict_map.items()
                            if v == "omission"]
            if not abstain_keys and not probe.expect:
                # 拒答题（expect 为空）：拒答本身就是正确行为
                abstain_keys = [k for k, v in probe.verdict_map.items()
                                if v == "correct"]
            if abstain_keys:
                return JudgeOutcome(abstain_keys[0], 0.8, refs, "回答为拒答话术")
        return JudgeOutcome(None, 0.0, refs, "脚本判卷无法判定，需 LLM judge 或人工复核")


def _tokens(s: str) -> set[str]:
    """锚例重叠度用的粗粒度 token 集：连续字母数字串 + 单个 CJK 字符。"""
    out: set[str] = set()
    buf = ""
    for ch in s:
        if ch.isascii() and ch.isalnum():
            buf += ch
        else:
            if buf:
                out.add(buf)
                buf = ""
            if "一" <= ch <= "鿿":
                out.add(ch)
    if buf:
        out.add(buf)
    return out


# ---------- 双 LLM judge（移植自 okim-bench，适配 verdict_map）----------

JUDGE_PROMPT = """你是记忆评测评委。根据探测项契约与被测智能体的回答，从给定类别中选出判定。

规则：
1. 只能依据回答原文，禁止臆测；评分必须引用具体证据（evidence_refs），无证据不得分。
2. 回答与期望答案语义等价即选 correct 对应类别；命中 rubric 描述的其他情形选对应类别。
3. "语义等价"指关键识别信息一致（实体、路径、取值），不要求逐字重复。
4. 输出严格 JSON：{{"verdict": "<类别key>", "confidence": 0-1, "evidence_refs": ["..."], "reason": "..."}}

类别（verdict_map，key=judge输出，value=五态判定）：
{verdict_map}

探测项契约：
问题：{ask}
期望答案：{expect}
判卷标准：
{rubric}

被测智能体的回答：
{answer}

请输出 JSON。"""

ARBITER_PROMPT = """你是仲裁评委。两位评委对同一回答给出不同结论，请复议并给出最终结论。

类别（verdict_map）：
{verdict_map}

问题：{ask}
期望答案：{expect}
判卷标准：
{rubric}

被测智能体的回答：
{answer}

评委A结论：{verdict_a}
评委B结论：{verdict_b}

规则同上：只依据回答原文、必须给出 evidence_refs、verdict 必须是类别 key 之一。
输出严格 JSON：{{"verdict": "...", "confidence": 0-1, "evidence_refs": ["..."], "reason": "..."}}"""


class OpenAICompatJudge:
    """OpenAI 兼容端点评委，key 从环境变量读取（JUDGE_A_/JUDGE_B_ 前缀）。"""

    def __init__(self, name: str, base_url: str, model: str, api_key: str,
                 temperature: float | None = None):
        self.name, self.base_url, self.model, self.api_key = name, base_url, model, api_key
        self.temperature = temperature

    @classmethod
    def from_env(cls, which: str) -> "OpenAICompatJudge":
        prefix = f"JUDGE_{which}"
        return cls(
            name=os.environ.get(f"{prefix}_MODEL", "unknown"),
            base_url=os.environ[f"{prefix}_BASE_URL"],
            model=os.environ.get(f"{prefix}_MODEL", "unknown"),
            api_key=os.environ[f"{prefix}_KEY"],
            temperature=float(os.environ[f"{prefix}_TEMP"])
            if os.environ.get(f"{prefix}_TEMP") else None,
        )

    @classmethod
    def pair_from_env(cls) -> Optional[tuple["OpenAICompatJudge", "OpenAICompatJudge"]]:
        """环境变量齐（双 judge 跨家族）才返回，否则 None。"""
        try:
            return cls.from_env("A"), cls.from_env("B")
        except KeyError:
            return None

    def complete(self, prompt: str) -> str:
        import time
        import urllib.request
        payload = {"model": self.model, "messages": [{"role": "user", "content": prompt}]}
        if self.temperature is not None:
            payload["temperature"] = self.temperature
        last_err: Exception | None = None
        for attempt in range(5):   # 网关间歇性 TLS 干扰/限流，指数退避
            try:
                req = urllib.request.Request(
                    f"{self.base_url.rstrip('/')}/chat/completions",
                    data=json.dumps(payload).encode("utf-8"),
                    headers={"Content-Type": "application/json",
                             "Authorization": f"Bearer {self.api_key}"},
                )
                with urllib.request.urlopen(req, timeout=120) as resp:
                    return json.loads(resp.read().decode("utf-8"))["choices"][0]["message"]["content"]
            except Exception as e:   # noqa: BLE001 TLS 断流/429/503 一律重试
                last_err = e
                time.sleep(2 ** attempt)
        raise RuntimeError(f"judge {self.name} 重试 5 次仍失败: {last_err}")


@dataclass
class _RawVerdict:
    key: str
    confidence: float
    evidence_refs: list[str]
    reason: str


def _parse(raw: str, valid_keys: set[str]) -> Optional[_RawVerdict]:
    try:
        start, end = raw.index("{"), raw.rindex("}") + 1
        data = json.loads(raw[start:end])
        key = str(data["verdict"]).strip()
        if key not in valid_keys:
            return None
        refs = [str(r) for r in data.get("evidence_refs", [])]
        if not refs:   # 无证据不得分
            return None
        return _RawVerdict(key, float(data.get("confidence", 0.5)), refs,
                           str(data.get("reason", "")))
    except (ValueError, KeyError, json.JSONDecodeError):
        return None


def dual_judge(probe: JudgeProbe, answer: str,
               judge_a: OpenAICompatJudge, judge_b: OpenAICompatJudge) -> JudgeOutcome:
    """双 judge 独立判 → 一致即出；不一致（或票无效）→ 仲裁；仲裁无效 → key=None。"""
    valid = set(probe.verdict_map.keys())
    ctx = dict(verdict_map=json.dumps(probe.verdict_map, ensure_ascii=False),
               ask=probe.ask, expect=probe.expect, rubric=probe.rubric, answer=answer)
    va = _parse(judge_a.complete(JUDGE_PROMPT.format(**ctx)), valid)
    vb = _parse(judge_b.complete(JUDGE_PROMPT.format(**ctx)), valid)

    if va and vb and va.key == vb.key:
        return JudgeOutcome(va.key, (va.confidence + vb.confidence) / 2,
                            sorted(set(va.evidence_refs + vb.evidence_refs)),
                            f"双评委一致: {va.reason} | {vb.reason}",
                            judge_a=judge_a.name, judge_b=judge_b.name,
                            judge_a_raw=va.key, judge_b_raw=vb.key)
    raw = judge_a.complete(ARBITER_PROMPT.format(
        **ctx,
        verdict_a=va.key if va else "无效（未按格式输出或无证据引用）",
        verdict_b=vb.key if vb else "无效（未按格式输出或无证据引用）"))
    arb = _parse(raw, valid)
    if arb:
        return JudgeOutcome(arb.key, arb.confidence, arb.evidence_refs,
                            f"仲裁结论: {arb.reason}",
                            judge_a=judge_a.name, judge_b=judge_b.name,
                            judge_a_raw=va.key if va else None,
                            judge_b_raw=vb.key if vb else None, arbitrated=True)
    return JudgeOutcome(None, 0.0, [], "仲裁输出无效，转人工复核",
                        judge_a=judge_a.name, judge_b=judge_b.name,
                        judge_a_raw=va.key if va else None,
                        judge_b_raw=vb.key if vb else None, arbitrated=True)
