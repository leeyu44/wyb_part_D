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
| [docs/reproducibility.md](docs/reproducibility.md) | runner、VM、证据校验、重复运行与 `.deb` 验收 |

## 目录结构

```
memhall/
├── cases/                    # full / quick / gen / chains 用例
├── docs/contracts/           # adapter / case / evidence 契约
├── scripts/                  # VM 辅助、可复现 .deb 构建与验收
├── src/memhall/
│   ├── adapters/             # 本机与 openKylin SSH 适配器
│   ├── runner/               # 三阶段编排、增量证据、完整性校验
│   ├── schema/               # case / evidence / verdict 模型
│   ├── scoring/              # 规则与语义判卷
│   ├── report/               # 指标、雷达、对比、稳定性
│   ├── systests.py           # openKylin 系统级体检
│   ├── vm.py                 # VMware 生命周期管理
│   └── ui/                   # FastAPI 本地 UI
└── tests/
```

## 使用

```bash
uv sync --group dev          # 装依赖（uv，Python ≥ 3.11）

uv run memhall doctor        # 一键发现本机/评测机智能体 + 评测环境体检
# 三路探测：本机（PATH+配置目录+版本）、openKylin VM（SSH 单往返复合探测，
# 含 brain.db 记忆库在位）、环境就绪度（密钥/SSH/网关可达），仿 brew doctor

# 一轮评测（Mock 适配器，离线零成本，全链路出报告）
uv run memhall run -a mock -c cases/full -o runs
# 产物：runs/<run_id>/{manifest.json, cases.json, verdicts.jsonl, metrics.json, radar.png, report.md}
#       runs/<run_id>/cases/<case_id>/evidence.jsonl（每条判定可下钻证据哈希）

uv run memhall verify runs/<run_id>     # 重算证据/文件/整包 SHA-256

# 固定 seed 重复两轮，自动输出标准差、判定翻转与 pass^2
uv run memhall run -a mock -c cases/quick --seed 42 --repeat 2

# openKylin：每一轮都回滚同一快照，再启动、等待 SSH 并采集环境指纹
uv run memhall run -a hermes -c cases/full --seed 42 --repeat 2 --prepare-vm

# 单独管理 VM 或运行破坏性系统体检
uv run memhall vm prepare
uv run memhall systest -a hermes

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
sudo apt install ./memhall_0.2.1_amd64.deb  # x86_64 openKylin；自动解析系统依赖，内置锁定 wheel
memhall run -a mock -c cases/quick -o /tmp/mh-demo --repeat 2
memhall verify /tmp/mh-demo/<run_id>
sudo dpkg -r memhall                    # postrm 清理 /usr/lib/memhall
```

包在 openKylin 目标机上原生构建：`scripts/build_deb_vm.sh` 从冻结的
`uv.lock` 按哈希取得依赖、自动写入 Debian 架构、固定构建时间并输出 .deb SHA-256；
`scripts/test_deb_vm.sh` 负责干净机安装、两轮 quick 评测和证据完整性验收。

## 平台支持

| 平台 | 支持度 | 说明 |
|---|---|---|
| Windows | ✅ 原生 | 开发与评测主战场：被测智能体经 SSH 驱动 VM，宿主 OS 无关；CLI 已做 UTF-8 控制台适配 |
| WSL | ✅ | 能跑（纯 Python + pip 依赖），但无增益——评测目标在 VM，多一层反而慢 |
| openKylin / Debian 系 | ✅ .deb | 见上节，离线安装 |

功能影响：六维评测、双智能体判卷、雷达图、报告全链路平台无关；平台差异仅在安装方式（uv/pip vs .deb）与控制台编码。

## 许可

Apache-2.0（见 [LICENSE](LICENSE)）

## 状态

当前主线包含 43 条人工用例、21 条固定 seed 生成用例和 3 条任务链；支持
Hermes/KylinBot openKylin 评测、本机 CLI 智能体、证据包离线重评分、重复稳定性
分析和离线 .deb。历史实测结果及里程碑见 team-plan.md。
