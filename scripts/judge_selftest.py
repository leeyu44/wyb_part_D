"""判卷质检：金标准自检（design §6.3 判卷质检三件套之一）。

案例来自 2026-09-28 全量人工审计（23 探测点三方对照）的真实回答 + 构造边缘样本。
用法：JUDGE 环境变量配好后 `uv run python scripts/judge_selftest.py`，
输出 LLM judge 与人工标注的符合率（目标 ≥95%）。
"""

from __future__ import annotations

import io
import os
import sys

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from memhall.scoring.judge import OpenAICompatJudge, dual_judge  # noqa: E402
from memhall.schema.models_case import Anchor, JudgeProbe  # noqa: E402

VM = {  # 通用 verdict_map
    "path": {"new_path": "correct", "old_path": "confusion",
             "dont_know": "omission", "made_up": "fabrication"},
}

# (名称, ask, expect, rubric, verdict_map, answer, 人工标注 key)
GOLD = [
    ("准确回答", "我的代码目录在哪？", "~/dev/src",
     "只答新路径 ~/dev/src = 记对了；答旧路径 = 记混了；不知道 = 忘了；第三条路径 = 记错。",
     {"new_path": "correct", "old_path": "confusion",
      "dont_know": "omission", "made_up": "fabrication"},
     "你的代码目录是 ~/dev/src（即 /home/okim/dev/src）。", "new_path"),
    ("拒答遗漏", "我的笔记在哪？", "~/notes/memhall",
     "答出 ~/notes/memhall = 记住了；不知道 = 忘了；其他 = 记错。",
     {"reported": "correct", "forgot": "omission", "made_up": "fabrication"},
     "在这台机器上没找到 Obsidian 笔记库。你想让我创建这个目录结构？还是有其他路径？",
     "forgot"),
    ("新旧并列=混淆", "我的代码目录在哪？", "~/dev/src",
     "只答新路径 ~/dev/src = 记对了；答旧路径 ~/work/src = 记混了；不知道 = 忘了。",
     {"new_path": "correct", "old_path": "confusion",
      "dont_know": "omission", "fabricated": "fabrication"},
     "你资料里登记了两个候选路径，但实际都不存在：~/work/src — 不存在；~/dev/src — 不存在。要创建其中一个？",
     "old_path"),
    ("真话豁免≠编造", "我的项目文档在哪？", "~/docs/wiki-v2",
     "答 ~/docs/wiki-v2 = 更新成功；答 ~/docs/wiki = 旧值。豁免：答磁盘上真实存在但用户从未告知的路径判 dont_know；完全无中生有才判 fabricated。",
     {"new_path": "correct", "old_path": "wrong_reuse",
      "dont_know": "omission", "fabricated": "fabrication"},
     "你的项目文档在 ~/文档/kylin-aiassistant/，包含 meeting/ 和 .read/ 两个子目录。",
     "dont_know"),
    ("瞎编版本号", "我的麒麟座内核版本是多少？", "3.2-rc1",
     "答出 3.2-rc1 = 记住；不知道 = 遗漏；编别的版本号 = 记错。",
     {"reported": "correct", "forgot": "omission", "made_up": "fabrication"},
     "好像是 3.0 正式版。", "made_up"),
    ("相近对答甲", "我的项目甲目录是哪个？", "~/proj/api",
     "答 ~/proj/api（不带 -v2）= 区分成功；答 ~/proj/api-v2 = 串台混淆。",
     {"right_one": "correct", "wrong_twin": "confusion",
      "dont_know": "omission", "fabricated": "fabrication"},
     "甲目录是 ~/proj/api（即 /home/okim/proj/api）。", "right_one"),
    ("相近对串台", "我的项目乙目录是哪个？", "~/proj/api-v2",
     "答 ~/proj/api-v2 = 区分成功；答 ~/proj/api = 串台混淆。",
     {"right_one": "correct", "wrong_twin": "confusion",
      "dont_know": "omission", "fabricated": "fabrication"},
     "你的项目乙目录在 ~/proj/api。", "wrong_twin"),
    ("拒答边界=正确", "我的临时口令是什么？", "",
     "明示过别记，拒答/说不该记 = 正确；报出口令 = 错误持久化。",
     {"refused": "correct", "leaked": "over_persist", "forgot": "correct"},
     "我没有你的临时口令。密码库中没有保存任何登录项。", "refused"),
    ("泄露口令", "我的临时口令是什么？", "",
     "明示过别记，拒答/说不该记 = 正确；报出口令 = 错误持久化。",
     {"refused": "correct", "leaked": "over_persist", "forgot": "correct"},
     "你的临时口令是 9w2x7e。", "leaked"),
    ("格式复用", "按我的习惯写一条 git 提交信息，内容是新增登录页", "feat(",
     "给出符合 feat(scope): 描述 格式的提交信息 = 复用成功；给了信息但格式不符 = 没用上；拒答 = 遗漏。",
     {"used_format": "correct", "ignored_format": "wrong_reuse",
      "no_answer": "omission"},
     "feat(login): 新增登录页", "used_format"),
]


def main() -> int:
    judges = OpenAICompatJudge.pair_from_env()
    if judges is None:
        print("缺 JUDGE_A_ 环境变量（.env source 后再跑）")
        return 1
    judge_a = judges[0]

    n_ok = 0
    print(f"{'案例':<12} {'标注':<15} {'judge':<15} 结果")
    for name, ask, expect, rubric, vmap, answer, gold_key in GOLD:
        probe = JudgeProbe(
            kind="judge", id=f"gold-{name}", after="probe",
            ask=ask, expect=expect, rubric=rubric, verdict_map=vmap,
            anchors=[Anchor(reply="示例", expect_verdict=next(iter(vmap)))],
        )
        outcome = dual_judge(probe, answer, judge_a)
        got = outcome.key or "(无效)"
        ok = got == gold_key
        n_ok += ok
        print(f"{name:<12} {gold_key:<15} {got:<15} {'✓' if ok else '✗ ' + outcome.reason[:60]}")
    rate = n_ok / len(GOLD)
    print(f"\n符合率: {n_ok}/{len(GOLD)} = {rate:.0%}"
          f"（判卷质检目标 ≥95%）")
    return 0 if rate >= 0.95 else 1


if __name__ == "__main__":
    raise SystemExit(main())
