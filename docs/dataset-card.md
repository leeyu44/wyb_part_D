# 数据集说明卡（Dataset Card）· MemHall 用例库 v0.1

> owner：B · 更新：2026-10-04（评分口径 v2 + 大容量注入题）
> 说明卡随题库发布，数据设计的每个主张都能在此对账（design.md §4.6）。
> **2026-10-04 口径 v2**（docs/review-tasks.md）：探测点分 score/diagnostic 两层——存储态断言（memory.*）与 actions 断言只进故障定位不进六维，跨族 canary 不计入宿主族；canary 一律教学时点判（after:inject）。旧 run 可用 `memhall report` 按新口径重渲染（快照优先，role 按断言推断对旧快照同样适用）。

## 1. 数据集构成

| 项 | 值 |
|---|---|
| 种子用例（人工） | **45 道**（cases/full/），覆盖 6 能力 × 6 内容 = 36 格覆盖矩阵，无空格（lint 实测）；含 2 道大容量注入题（persist-008 / recall-008：一次教 16 条互不相关事实再考 5 条，检索竞争） |
| 团队生成用例（模板扩量） | **21 道**（cases/gen/，scripts/gen_cases.py，seed=20260928） |
| 任务链 | **4 条**（cases/chains/chain-001~003 每条 3 会话；chain-004 六会话长弧，对齐 LongMemEval/LoCoMo 的长程会话深度） |
| 冒烟集 | **6 道**（虚拟集，无独立目录：full 中六能力各 1 题按 ID 引用，`paths.QUICK_IDS` 单源），适配器接入验收口径（随 full 更新，不再维护副本） |
| **held-out 防背题池（不可见）** | **21 道**（评测时现场生成：`scripts/gen_cases.py --seed 4210 --out cases/heldout --prefix h`，题目文本不入公开仓库；公布 seed 保复现。智能体跑完公开集后换 held-out 复测，验证非背题） |
| 生成器备用池（B 本地） | 72 道（seed=42，B 的 generators/generate_cases.py），与 gen/heldout 功能重叠，未并入 PR |

## 2. 覆盖矩阵（cases/full/ 实测，lint 自动统计，43 道）

```
capability      | preference | path | template | fact | project_state | sensitive
persist         |     1      |  2   |    1     |  1   |       1       |    1
recall          |     1      |  2   |    1     |  2   |       1       |    1
dynamic_update  |     1      |  3   |    1     |  1   |       1       |    1
discriminate    |     1      |  1   |    1     |  1   |       1       |    1
boundary        |     1      |  1   |    1     |  1   |       1       |    3
reuse           |     1      |  1   |    1     |  1   |       1       |    1
```

重点格（评审最关注的动态更新、边界识别）每格 2–3 题；sensitive 是边界识别的主场，boundary×sensitive 3 题（含两档契约）。
合并前团队 14 道种子只覆盖 9/36 格（27 空格）——本 PR 的 29 道补充题正是补空格、把矩阵做满；canary 用例从 2 → **13**。

## 3. 构造方式与出题四规矩（design.md §4.5）

| 规矩 | 落实方式 |
|---|---|
| 可判定 | 每题 probes 全部可落为规则断言（fs/memory/actions）或 judge rubric+verdict_map；lint 强制：judge rubric 非空、verdict_map 值合法、锚定例 ≥2、rule 必须含 default 兜底 |
| 防污染 | sensitive/boundary 类必须含 `canary-[a-z0-9]{4}` 假信息串（lint 强制，full 集 13 个）；路径/代号/版本号全部虚构半随机（~/proj/fjord、calypso-x3、3.1.2）；canary 只在 inject 段出现，probe 段提问禁止泄底（lint 检查） |
| 像人话 | 注入一律用用户口吻（"我习惯…""帮我记住…""对了，改成…"），探测按日常对话问，不用"请记住路径 X"式指令 |
| 本土化 | 场景扎根 openKylin 桌面语境：UKUI 天气插件/夜间模式/应用商店、麒麟软件源、~/文稿/~/图片/~/模板 路径习惯、WPS/火狐 |

## 4. 题型分布（43 道种子，lint 实测）

- session_recall 5 / cross_session_recall 11 / info_update 7 / temporal 2 / similarity 6 / false_premise 6 / task_chain 6
- 任务链 6 道（design §4.3 要求 3–5 条，略超），每条 3 会话，判"用对了/用错了/没用上"
- 一致性检查变换（换说法/打乱顺序）作为生成器后续扩展项，W3 补

## 5. 难度分布与旋钮

难度 1:13 / 2:24 / 3:6（lint 实测）。三旋钮落实：
- **干扰信息多少**：confound 段 filler 轮数 1–3 递增（d1=1，d2=2，d3=3）
- **间隔多久**：end_session + 拨钟（d3 题 system_events.clock_shift_days=3，如 update-006/007/008、discriminate-005）
- **相似程度**：d2=两相似项，d3=三相似项/更接近的干扰（api vs api-v2、3.1.2 vs 3.2.1、8080 vs 8081）

## 6. 判定口径（对齐 C 实测，W1/W2 结论）

- **update 族（info_update 题型）答旧值 → wrong_reuse**（C 实测 §8：KylinBot 更新不走版本链，答旧值即 false_reuse；**推荐口径**，lint 提示不拦截。注意：团队审计版 update-001 仍用 confusion，两版并存，待 C/A 统一后再收敛为单一映射；temporal 题型答错版本判 confusion——是时间理解失败，不是旧值复用）
- **boundary 两档契约**（boundary-003，对齐实测）：严格=无时效标注入库即 over_persist（rule 判）；宽松=回答带时效限定可接受降级（judge rubric 识别）
- **该记的敏感信息 vs 不该记的**分开判：persist-006（收货地址该记 + canary 假地址别记）双 probe
- 五态判定值：correct / omission / confusion / fabrication / over_persist / wrong_reuse（契约 02 §6）

## 7. 校准数据（初步，W3 扩）

- C 实测首跑（KylinBot 0.7.5）：retention 100 / recall 0 / update 0 / boundary 0 —— 佐证 recall/update/boundary 类题区分度天然高
- 待 W3：在 KylinBot + Hermes 上跑全量 43 题（合并后），统计每题通过率，全对/全错题调难度或标注为上下限参照题（design §4.4 实测校准）
- 变换保难度（一致性检查）用试点数据验证前后通过率无系统差异（W3）

## 8. 已知局限（诚实边界）

- 语言：仅中文；场景：桌面办公/开发场景，未覆盖多语言与专业领域
- **recall/persist 的机制边界**：真智能体适配器每条消息独立进程（无会话上下文），"会话内提问"实测等价于"写库后立刻检索"；两维按可测口径收窄（recall=写后即取、persist=跨干扰保持，见 design §4.2 注记），接真会话型适配器后语义恢复
- **held-out 威胁模型**：seed 公开 + 生成器公开 = 题目可完整重构——防训练污染有效、防定向作弊无效；held-out 与 gen 同模板同槽位池，防"背题库"不防"背题型"。后续方向：held-out 加 paraphrase 变换 + 换槽位词表
- **verdict_map 键名词表未统一**（lint advisory 统计）：correct 类现有 33 种写法——judge 只做分类不受影响，但锚例/诱饵维护成本随词表膨胀；新题按契约 02 §6 标准词表（mixed_up/dont_know/made_up/leaked…）出
- 生成扩量以团队 scripts/gen_cases.py 为准（gen=公开集 seed=20260928，heldout=不可见集 seed=4210，`--prefix h` 隔离 ID）；B 的 generators/generate_cases.py（pool，seed=42）为备用素材，未并入 PR
- **三难度旋钮**（design §4.4，2026-10-02 参数化）：`--distract N` 干扰密度（confound 闲聊条数）、`--gap-days N` 拨钟间隔天数、`--similar high|mid|low` 诱饵相似度；同 seed 改旋钮 = 仅难度不同的对照变体；默认参数与历史存档逐字一致（核心闲聊池 5 条不动，扩展池仅在 N≥2 时并入）
- **方差口径**：正式全量跑每智能体 ≥2 轮，报告六维与总分的 mean±std（样本标准差，`memhall aggregate`）+ 逐探测点 bootstrap 95% CI；n=2 时 mean±std 支撑不了排名叙事，跨智能体比较看 CI 重叠与 compare 的符号检验 p 值，单轮裸分数不作对外口径
- **mock 基线（口径 v2，2026-10-04 实测）**：full 66.1%（62/66 计分探测有效）、chains 22.2%；口径 v1 分别为 62.2%/41.7%——差异来自诊断探测出分母与 canary 教学时点判，属口径变更非行为变更
- 任务链的"步数/耗时对比"（记忆效率指标）需 runner 支持，probe 侧已预留 actions 计数断言（actions 证据面 coverage=full 前 role=diagnostic 不进分）
- 难度标定数据量有限，结论标注"初步标定"
- 待统一项：update-001 的旧值判定（confusion vs wrong_reuse），由 C/A 裁决后收敛
