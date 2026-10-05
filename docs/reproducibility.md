# MemHall 稳定性与可复现运行手册

本文档对应评分项“稳定性与可复现（15%）”，覆盖评测前环境归一、运行测量
契约、证据完整性、重复运行和 `.deb` 干净机验收。

## 1. 固定 openKylin 基线

宿主机安装 VMware Workstation，虚拟机安装 openKylin 3.0、OpenSSH Server 和
被测智能体。完成智能体配置后创建 `agents-warm` 快照。将 `.env.example` 复制为
`.env` 并填写：

```dotenv
VMX_PATH=D:\VMs\openkylin30\openkylin30.vmx
VM_SNAPSHOT=agents-warm
VM_HOST=192.168.61.133
VM_USER=okim
VM_PASS=...
VM_SUDO_PASS=...
```

单轮正式评测前执行：

```bash
memhall vm status
memhall vm prepare
```

`prepare` 固定执行“回滚 `VM_SNAPSHOT` → 无界面启动 → 等待 SSH → 采集系统、
内核、Python、主机密钥和快照指纹”。任一步失败都会返回非零状态，不会带病开跑。
重复评测应让 runner 在每一轮前执行同一流程：

```bash
memhall run -a hermes -c cases/full -o runs --seed 42 --repeat 2 --prepare-vm
```

这样第 2 轮不会继承第 1 轮产生的智能体状态、文件或系统变化。

## 2. 固定测量契约

```bash
memhall run -a hermes -c cases/full -o runs --seed 42 --repeat 2 --prepare-vm
```

每轮独立执行全部 case。`manifest.json` 在开始运行前创建，并在每个 case 完成后
原子更新；进程意外退出时仍能区分 `running`、`aborted` 和 `completed`。清单固定
记录：

- 工具、代码和依赖版本；
- 用例顺序、逐题 SHA-256、题库总指纹和本次用例完整副本；
- 宿主与 openKylin 目标环境指纹、VM 快照名、SSH 主机密钥和实测智能体版本；
- 源码树 SHA-256（安装包内没有 `.git` 时仍可锁定实际运行代码）；
- seed、重复组、轮次和 `repeat_of`；
- 智能体及各 judge 端点的 token 汇总与 usage 上报覆盖率；
- 每个 case 的状态、错误、证据条数和文件哈希；
- 整个证据包的 SHA-256；
- `verdicts.jsonl`、`metrics.json`、`report.md`、`radar.png` 的输出哈希。

运行 ID 含 UTC 微秒，同一秒内启动的重复轮次也不会覆盖。

## 3. 证据持久化与校验

runner 在每个 `inject/confound/probe` 阶段结束时采集：

1. 对话和逐轮延迟；
2. 全量记忆快照；
3. 智能体操作记录及 coverage；
4. 带内容哈希的文件差异，可区分创建、修改和删除。

`inject` 每一步发送后还会立即采一份记忆快照，用于判断写入时机。每条证据生成后
立即追加到 JSONL 并 `fsync`，所以后续步骤超时或崩溃不会抹掉已经获得的证据。
异常 case 的未执行阶段会落显式的 skipped/invalid 记录，评分统一判
`invalid_run`，不混入能力分。

```bash
memhall verify runs/<run_id>
```

校验器会重新解析 schema、计算每条 payload 哈希、检查全局证据 ID 和判定 ID、
拒绝跨 case 的证据引用，并核对每阶段四类证据、case 文件哈希、整包封印及评分
产物哈希。任何人工改写都会返回非零状态。

操作日志优先使用智能体原生日志。需要 auditd 兜底时，在 VM 安装并启用
`auditd`，设置 `MEMHALL_AUDITD=1`；采集器以本轮唯一 key 建立用户目录写入审计，
将 create/write/rename/delete 映射为 `Action`，结束后移除规则。拿不到日志时
coverage 保持 `unknown`，报告不会宣称操作证据完整。

## 4. 重复稳定性

`--repeat 2` 会生成两份互相链接的独立 run，并自动输出
`<repeat_group>-stability-<adapter>/`：

- `stability.json`：机器可读指标；
- `stability.md`：总体分均值、标准差、极差、判定一致率、逐维 `pass^k` 和翻转明细。

也可以汇总已有运行：

```bash
memhall stability runs/<run-a> runs/<run-b> -o runs/_stability
```

分析器会检查 adapter、题库指纹、代码版本和 seed 是否一致。不一致时仍可查看交集，
但报告明确标记不可直接比较。

## 5. openKylin 系统体检

```bash
memhall systest -a hermes -o runs
memhall systest -a kylinbot -o runs
```

套件实际执行重启存活、拨钟、Linux 多用户隔离、即时写入、VMware 临时快照回滚、
操作证据和断网存活。快照测试先创建临时恢复点，写入 canary 后回滚该恢复点，最后
删除临时快照，因此不会把 VM 留在测试污染状态。未配置 `VMX_PATH` 或 auditd 时，
对应项标记“跳过”，不计为通过。

拨钟测试先记录 guest epoch 和 NTP 状态；若 NTP 已启用则暂时关闭，测试结束时按
实际耗时补偿恢复 epoch，并恢复原 NTP 状态。时间或 NTP 任一恢复失败都会使 case
无效并写入错误，避免污染后续题目。

## 6. 可复现 .deb

在 openKylin 构建机安装 `uv 0.12.16`、`python3-pip` 和 `dpkg-dev`，然后执行：

```bash
bash scripts/build_deb_vm.sh
```

构建脚本只接受冻结的 `uv.lock`，使用 `pip --require-hashes` 下载目标机原生 wheel，
固定 `SOURCE_DATE_EPOCH`，规范化包内时间戳，并生成：

```text
dist/memhall_<version>_<debian-arch>.deb
dist/memhall_<version>_<debian-arch>.build.json
```

例如 x86_64 openKylin 产出 `memhall_0.2.1_amd64.deb`。包包含目标机构建时解析的
原生 wheel，因此不能标记为 `Architecture: all`，也不能拿另一种 CPU 或 Python ABI
的构建产物替代。sidecar 记录架构、Python/uv 版本、lock、wheel 集和 `.deb` 的
SHA-256。安装过程完全离线，依赖先装入临时目录，通过核心模块导入自检后再原子
替换正式目录。

相同源码做两次独立构建并比对：

```bash
DIST=/tmp/memhall-build-a bash scripts/build_deb_vm.sh
DIST=/tmp/memhall-build-b bash scripts/build_deb_vm.sh
sha256sum /tmp/memhall-build-{a,b}/memhall_0.2.1_amd64.deb
```

两行 SHA-256 必须相同；`.build.json` 中的 `wheelset_sha256` 也必须相同。

在一台从干净快照启动的 openKylin VM 中执行：

```bash
bash scripts/test_deb_vm.sh dist/memhall_0.2.1_amd64.deb
```

验收脚本要求 `.deb` 与同名 `.build.json` 同时存在，先核对 sidecar 中的包哈希和
目标机 Debian 架构，再安装包、检查版本、离线运行 Mock quick 集两轮、逐轮执行
证据校验并确认稳定性报告存在。完整日志默认写入
`/tmp/memhall-deb-acceptance.txt`。

## 7. 验收边界

仓库 CI/开发机可以验证 runner、哈希、重复分析、VM 命令边界及打包脚本语法。
重启、断网、拨钟、VMware 回滚和 .deb ABI 只能在配置好的真实 openKylin VM 上
验收；提交材料应附系统体检报告和干净机安装日志，不能用 Mock 结果替代。

2026-10-04 已通过 SSH 在 openKylin 3.0 x86_64 真机完成目标机双构建、安装、普通
用户运行、重启、拨钟、多用户隔离、断网恢复、auditd 和文件哈希验收。当前宿主仍
未提供 `vmrun`、`VMX_PATH` 与快照名，因此 VMware 快照回滚尚未执行；KylinBot
真实记忆能力测试还需要配置模型 key。证据见
[openkylin-validation-20261004.md](openkylin-validation-20261004.md)。
