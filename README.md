# 麟阁 MemHall

**面向 openKylin 生态的智能体记忆能力评测基准**

A Memory Benchmark for Agents on the openKylin Ecosystem

[![CI](https://github.com/shangjian2023/MemHall/actions/workflows/ci.yml/badge.svg)](https://github.com/shangjian2023/MemHall/actions/workflows/ci.yml)

> 名字取自麒麟阁——汉代评定功臣、画像记名的殿堂：给记忆能力优秀的智能体立榜。

给跑在 openKylin 上的智能体测记性，全自动、可复现：用「教 → 隔 → 考」三阶段剧本驱动任意智能体，把对话日志、记忆快照、操作记录、文件变化统一为证据，自动评分并输出六维能力雷达图。

## 文档

| 文档 | 内容 |
|---|---|
| [design.md](design.md) | 总体技术设计（交付物 a 底稿） |
| [team-plan.md](team-plan.md) | 五人分工、4 周排期、协作规约 |
| [environment.md](environment.md) | 环境基线与搭建步骤 |
| [okim-bench/README.md](okim-bench/README.md) | 评分子系统（C 角色，L1→L3 判卷流水线） |

## 目录结构

```
memhall/
├── design.md / team-plan.md / environment.md   # 三份基准文档
├── docs/            # 方案文档（A 总稿）+ contracts/（接口契约）
├── okim-bench/      # 评分子系统：L1 确定性检查 → L2 语义规则 → L3 双 LLM judge
├── src/memhall/     # 源码包
│   ├── adapters/    # 智能体适配器（mock/hermes/kylinbot/claude/qwen/opencode…）
│   ├── runner/      # 三阶段编排
│   ├── schema/      # case/evidence/verdict 数据模型
│   ├── scoring/     # 规则判卷 + 六维指标
│   ├── report/      # 报告与雷达图
│   ├── ui/          # Web UI（FastAPI + SSE 评测直播）
│   └── cli.py / discovery.py / notify.py / systests.py
├── cases/           # 用例库（full 43 / gen 21 / chains 4 / heldout 21※；quick=full 的虚拟冒烟子集，按 ID 引用不落盘）
├── scripts/         # deb/exe 打包、VM 通道、判卷自检等脚本
└── tests/           # 端到端与适配器测试
```

> ※ 正式口径 = **full + chains**；**heldout 为不可见防背题池**（`scripts/gen_cases.py --seed 4210 --out cases/heldout --prefix h` 评测时现场生成，题目文本不入公开仓库，seed 公布保复现）；全量跑 ≥2 轮报 mean±std（`memhall aggregate`）。

## 使用

```bash
uv sync --group dev          # 装依赖（uv，Python ≥ 3.11）

uv run memhall doctor        # 一键发现本机/评测机智能体 + 评测环境体检
# 三路探测：本机（PATH+配置目录+版本）、openKylin VM（SSH 单往返复合探测，
# 含 brain.db 记忆库在位）、环境就绪度（密钥/SSH/网关可达），仿 brew doctor

uv run memhall ui            # Web UI（本地 127.0.0.1:8300，自动开浏览器）
# 浏览器里选适配器和用例库发起评测，问答与记忆快照逐条直播（SSE）；
# exe 双击默认走 pywebview 原生窗口；URL hash 可直达标签页

# 一轮评测（命令行）
uv run memhall run -a mock -c cases/full -o runs      # Mock 适配器，离线零成本
uv run memhall run -a hermes -c cases/full -o runs    # 真智能体（SSH 驱动 VM）
# 适配器：mock / hermes / kylinbot（VM 内）/
#         hermes-local / claude-local / qwen-local / opencode（本机）
# 产物：runs/<run_id>/{manifest.json, verdicts.jsonl, metrics.json, radar.png, report.md}
#       runs/<run_id>/cases/<case_id>/evidence.jsonl（每条判定可下钻证据哈希）

uv run memhall report runs/<run_id>     # 对已有 run 重渲染报告（缺 verdicts 时从证据重放）
uv run memhall compare runs/A runs/B    # 对比雷达 + 判定翻转明细（两智能体/两次运行）

uv run memhall systest -a hermes        # 系统级测试：重启/拨钟/多用户/断网（真机真做）

# 双 LLM judge 判卷（可选，替代默认的离线脚本判卷；两 judge 需跨厂商）
export JUDGE_A_BASE_URL=... JUDGE_A_MODEL=... JUDGE_A_KEY=...
export JUDGE_B_BASE_URL=... JUDGE_B_MODEL=... JUDGE_B_KEY=...
uv run memhall run -a mock --judge dual

uv run pytest tests/ -q                # 测试（含端到端冒烟）
```

判定五态：正确 / 遗漏 / 混淆 / 错误持久化 / 错误复用；规则判不了的才升级语义判卷（脚本判卷 → 双 LLM judge 交叉仲裁），未决判定单列 HUMAN_REVIEW 待人工复核，不计入运行无效。

评测收尾可选 UKUI 桌面通知（notify-send）并自动弹出雷达图（xdg-open）。

## 安装（openKylin / Debian 系）

```bash
sudo dpkg -i memhall_*_all.deb     # 内置全部依赖 wheel，安装不联网（版本号随发行）
memhall run -a mock -c /usr/share/memhall/cases/full -o ~/memhall-runs
dpkg -r memhall                         # 卸载干净（prerm 清 /usr/lib/memhall）
```

系统目录只读：run 产物写 `~/memhall-runs`，配置读 `~/memhall.env`。

包构建在 openKylin 目标机上原生完成（`scripts/build_deb_vm.sh`：清华源拉依赖 wheel → 组装离线安装树 → dpkg-deb），保证 wheel ABI 与目标机 Python 精确匹配、可复现。

## 安装（Windows）

`scripts/build_exe.sh` 打包两种发行物：`dist/MemHall/`（onedir，启动快）与 `dist/麟阁MemHall-单文件版.exe`（单文件，可直发）。双击即进 Web UI 原生窗口。

## 平台支持（分级）

主力线是 openKylin：级别越高，验证深度越深——Tier 1 的真机系统级测试（重启/拨钟/多用户/断网）只能在 openKylin 真机上做，是其他平台复制不了的验证深度。

| 级别 | 平台 | 验证深度 | 证据 |
|---|---|---|---|
| **Tier 1 旗舰** | openKylin 3.0 | 全链路验收：全量测试 + 真机系统级测试（重启存活/拨钟隔天/多用户隔离/断网存活）+ 目标机原生构建 .deb + UKUI 桌面通知 | [CI](https://github.com/shangjian2023/MemHall/actions/workflows/ci.yml) · [build_deb_vm.sh](scripts/build_deb_vm.sh) · 上文安装节 |
| Tier 2 | 主流 Linux · Windows 10/11 | 核心功能等价：CI 矩阵（ubuntu/windows × Python 3.11/3.12）每提交跑题库门禁 + 全量测试；Windows 另有 exe 发行 | [CI](https://github.com/shangjian2023/MemHall/actions/workflows/ci.yml) · 上文安装节 |
| Tier 3 | macOS / 其他 Linux | 尽力而为：纯 Python 源码安装（uv/pip），未持续验证 | — |

注：WSL 能跑但无增益——评测目标在 VM 里，多一层反而慢，不作为支持目标。

功能影响：六维评测、双智能体判卷、雷达图、报告全链路平台无关；平台差异仅在安装方式（uv/pip vs .deb/exe）与控制台编码。

## 许可

Apache-2.0（见 [LICENSE](LICENSE)）

## 状态

v0.2.1（2026-09-29）：Web UI 评测直播、`compare` 对比 CLI、系统级测试（重启/拨钟/多用户/断网，hermes 真机 4/4）、claude/qwen 本机适配器（沙箱隔离配置目录）、UKUI 桌面通知；Mock v2 缺陷注入基线（措辞解耦后总分 62%，六维显式缺陷模式表）。里程碑见 team-plan.md。

2026-10-02：评测口径对齐主流基准——不可见 held-out 防背题池（seed 4210 现场生成）、N 轮方差口径（`memhall aggregate` 出六维 mean±std）、六会话长链 chain-004（对齐 LongMemEval/LoCoMo 的长程会话深度）；CI 平台矩阵（ubuntu/windows × py3.11/3.12）+ 平台分级表；deb 补 UKUI 菜单项。

双智能体对比（W2，09-28）：Hermes 81.6% vs KylinBot 61.5%（39 探测点全有效），判卷质检金标准 10/10。
