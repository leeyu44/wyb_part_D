# 评测设计缺陷整改队列（benchmark review）

> 2026-10-04 基准设计专项排查产出，24 项，编号 R01–R24。
> 与 `engineering-tasks.md`（工程化）互补：这里全是"分数能不能代表它声称的东西"。
> 排查证据：design/契约/评分链代码/五个适配器/full+chains 全部 47 题 + 官方口径 89 探测点统计。

## 轮次划分

- **R-A 评分正确性**（R01/R17/R07/R09/R18/R03/R10/R08）：影响六维数字语义，先改先测。
- **R-B 判卷与鲁棒**（R04/R14/R22/R20/R23/R02）。
- **R-C 统计与证据覆盖**（R11/R12/R24）。
- **R-D 数据与文档**（R06/R05/R13/R15/R16/R21/R19）。

## 任务表

| # | 问题 | 修复方案 | 轮次 | 状态 |
|---|---|---|---|---|
| R01 | 4 个 actions 断言探测点对真智能体恒 omission（reuse 维 17% 结構性地板）；ActionDump.coverage 字段无人消费 | `_action_items`：coverage != "full" 或无 actions 证据 → `EvidenceMissing` → invalid_run（证据不足≠答错） | A | ☐ |
| R02 | 同问异答 × reset 彻底性是适配器私有知识（hermes 只删 2 个 md，会话转录保留；openclaw 整目录重建）→ 跨用例泄漏放大 | ① adapter 加 `verify_reset()`（reset 后 dump_memory 必须为空，违者 fail fast）；② hermes reset 补清 sessions 目录 | B | ☐ |
| R03 | 正式跑用脚本判卷 + HUMAN_REVIEW 不计分 → 分母随答案风格漂移 | metrics 补 `overall_score_floor`（未决按错计的区间下界）+ n_human_review 单列；报告展示分数区间；正式口径建议 dual judge 收尾 | A | ☐ |
| R04 | anchors 没进 LLM 判卷提示词——few-shot 防漂移是纸面能力 | JUDGE_PROMPT 加 anchors 槽位，JUDGE_PROMPT_VERSION 升版 | B | ☐ |
| R05 | recall 与 persist 在无会话适配器下不可区分（每消息独立进程，end_session 空操作） | 文档重定义：recall=写后即取（即时调用）、persist=跨干扰保持；dataset-card 注明当前机制差异 | D | ☐ |
| R06 | "长期"没被拉长：inject 1-2 句、confound 1-3 句，检索竞争不存在 | 新增 2 道大容量注入题（persist-008 / recall-008：一次教 16 条再考 5 条），制造检索竞争 | D | ☐ |
| R07 | 故障定位探测点混进能力分：update-001-p3 等存储断言计入 dynamic_update；一个"没写库"故障跨三维重复扣分 | probe 加 role（score/diagnostic），**按断言类型推断**（boundary 族 rule 探测 + fs 探测 = score；actions/memory 存储探测 + 跨族 canary = diagnostic），diagnostic 只进故障定位表不进六维；旧 run 快照无 role 字段同样可推断重放 | A | ☐ |
| R08 | design §6.3 故障定位四态表无报告产物；§8"过期信息调用率"没名字 | metrics+report 落地：没存/存了没用上/存了但内容错/该删没删 聚合表 + stale_info_rate（update 族 confusion+wrong_reuse 占比） | A | ☐ |
| R09 | canary 用最新快照判 →"作废后删除"可洗白 over_persist；ever_contained 含最终快照 → 写入时机不可分 | 12 个 canary 探测（boundary-001..008-p1、persist-007-p1、recall-007-p1、discriminate-006-p1、reuse-006-p1）改 `after: inject`——教学时点快照判，删除洗白失效 | A | ☐ |
| R10 | 探测点为单位 + 族题量悬殊（reuse 23/discriminate 10）→ overall 隐含权重 2.3 倍差 | metrics 补 `overall_score_case_weighted`（每 case 先聚合再等权平均），报告并列展示 | A | ☐ |
| R11 | n=2 的 mean±std 支撑不了排名叙事 | aggregate 加逐探测点 bootstrap 95% CI（固定 seed 可复现）；compare 加判定翻转符号检验 p 值 | C | ☐ |
| R12 | 每轮分母不同（invalid/human_review 剔除）还做轮间平均 | metrics 顶层补 n_invalid_run/n_human_review；aggregate runs[] 记录每轮分母 | C | ☐ |
| R13 | design §8 写"每次都过才算过"，实现是池化通过率 | 文档改齐（池化口径 + mean±std），消除对账矛盾 | D | ☐ |
| R14 | judge_a 自己仲裁自己的分歧（自偏好）；一票无效也进仲裁 | 一票无效→直接采信对侧有效票（省一次调用）；双票不一致→仲裁评委 A/B 轮值；design §6.1 同步改口径 | B | ☐ |
| R15 | heldout"不可见"与"seed 公布保复现"矛盾；heldout 防背题库不防背题型 | README/dataset-card 写明威胁模型：防训练污染有效、防定向重构无效；背题型风险列为已知局限 | D | ☐ |
| R16 | §8 承诺的"重复运行一致率"无数字、"记忆效率/难度边界"未做 | compare 输出 verdict_agreement_rate；design §8 指标表加状态列（落地/预留） | D | ☐ |
| R17 | probe.after 死旋钮（引擎不看，一律跑完判） | engine 实现阶段过滤（after:inject 只喂 inject 及以前证据）；配合 R09 | A | ☐ |
| R18 | 缺失能力族记 0 分（像不及格）；detail.n_invalid_run 把 human_review 混计 | 无有效探测的维输出 None（雷达/报告跳轴）；n_invalid_run 与 n_human_review 分开 | A | ☐ |
| R19 | ScriptedJudge 锚例三层匹配重度措辞耦合（mock v1 批评的病住进了判卷器） | 暂缓：需先落 dual judge 运营化，否则简化只会放大 human_review；方向=脚本只做高置信子集（expect 子串+拒答正则），其余全交 LLM | — | ☐ 暂缓 |
| R20 | persist/recall 族 verdict_map 无 confusion 出口 → 跨题泄漏只能判 fabrication，错误构成失真 | 无 confusion/wrong_reuse 出口的非拒答 judge 探测补 `mixed_up: confusion` + rubric 行 | B | ☐ |
| R21 | verdict_map 键名各题自造（reported/right_one/correct_path…） | lint 加词表统计提示（advisory 不拦截）+ 契约文档给标准键名词表 | D | ☐ |
| R22 | _answer_for 同问取最后一条、跨全 case 搜——ask 复现两次会静默取后者 | 优先 probe 段对话、精确匹配、取最后；无命中再全库回退 | B | ☐ |
| R23 | runner 无 case 级异常隔离：一个未预期异常=整轮无 manifest 报废 | run_suite 每 case try/except 续跑 + manifest 在 finally 落盘 + failed_cases 记录 | B | ☐ |
| R24 | fs 证据覆盖不对等：openclaw 剪枝自家 workspace（chain 写沙箱内看不见）；claude-local 相对路径永不匹配 ~/ 断言 | openclaw 快照并入 workspace 子树；claude-local 路径加 ~/ 前缀归一 | C | ☐ |

## 验收口径

- 每轮改完：`PYTHONIOENCODING=utf-8 uv run pytest tests/ -q` 全绿 + `uv run memhall run -a mock -c cases/full -o runs`（与 chains）冒烟。
- R-A 完成后 mock 基线会变（diagnostic 出分母）——重测并把新基线写进 dataset-card（口径 v2）。
- 旧马拉松 run 可用新口径重渲染（report 快照优先 + role 推断对旧快照同样适用），无需重跑真机。
