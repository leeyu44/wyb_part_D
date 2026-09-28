# 麟阁 MemHall

**面向 openKylin 生态的智能体记忆能力评测基准**

A Memory Benchmark for Agents on the openKylin Ecosystem

> 名字取自麒麟阁——汉代评定功臣、画像记名的殿堂：给记忆能力优秀的智能体立榜。

给跑在 openKylin 上的智能体测记性，全自动、可复现：用「教 → 隔 → 考」三阶段剧本驱动任意智能体，把对话日志、记忆快照、操作记录、文件变化统一为证据，自动评分并输出六维能力雷达图。

## 文档

| 文档 | 内容 |
|---|---|
| [design.md](design.md) | 总体技术设计（交付物 a 底稿） |
| [team-plan.md](team-plan.md) | 五人分工、4 周排期、协作规约 |
| [environment.md](environment.md) | 环境基线与搭建步骤 |

## 目录结构

```
memhall/
├── design.md / team-plan.md / environment.md   # 三份基准文档
├── docs/            # 方案文档（A 总稿）+ contracts/（接口契约）
├── schema/          # case/evidence/verdict schema
├── cases/           # 种子用例库
├── generators/      # 用例生成器
├── adapters/        # 智能体适配器（三档接入）
├── runner/          # 三阶段编排 + 系统级测试
├── evidence/        # 证据采集与存储
├── scoring/         # 评分引擎
├── report/          # 指标与雷达图
├── cli/             # memhall 命令行入口
├── packaging/       # .deb 打包配置
└── tests/           # 端到端测试
```

## 使用（开发中）

## 使用

```bash
uv sync --group dev          # 装依赖（uv，Python ≥ 3.11）

# 一轮评测（Mock 适配器，离线零成本，全链路出报告）
uv run memhall run -a mock -c cases/full -o runs
# 产物：runs/<run_id>/{manifest.json, verdicts.jsonl, metrics.json, radar.png, report.md}
#       runs/<run_id>/cases/<case_id>/evidence.jsonl（每条判定可下钻证据哈希）

uv run memhall report runs/<run_id>    # 对已有 run 重渲染报告

# 双 LLM judge 判卷（可选，替代默认的离线脚本判卷；两 judge 需跨厂商）
export JUDGE_A_BASE_URL=... JUDGE_A_MODEL=... JUDGE_A_KEY=...
export JUDGE_B_BASE_URL=... JUDGE_B_MODEL=... JUDGE_B_KEY=...
uv run memhall run -a mock --judge dual

uv run pytest tests/ -q                # 测试（含端到端冒烟）
```

判定五态：正确 / 遗漏 / 混淆 / 错误持久化 / 错误复用；规则判不了的才升级语义判卷（脚本判卷 → 双 LLM judge 交叉仲裁）。

## 安装（openKylin / Debian 系）

```bash
sudo dpkg -i memhall_0.1.0_all.deb     # 内置全部依赖 wheel，安装不联网
memhall run -a mock -c /usr/share/memhall/cases/full -o /tmp/mh-demo
dpkg -r memhall                         # 卸载干净（prerm 清 /usr/lib/memhall）
```

包构建在 openKylin 目标机上原生完成（`scripts/build_deb_vm.sh`：清华源拉依赖 wheel → 组装离线安装树 → dpkg-deb），保证 wheel ABI 与目标机 Python 精确匹配、可复现。

## 许可

Apache-2.0（见 [LICENSE](LICENSE)）

## 状态

W2（2026-09-28）：双真智能体对比达成——Hermes 81.6% vs KylinBot 61.5%（39 探测点全有效），判卷质检金标准 10/10，用例库 38 条（种子 17 + 生成 21），.deb v0.1 装机验收通过，win 分支提供 Windows 原生适配。里程碑见 team-plan.md。
