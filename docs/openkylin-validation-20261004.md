# openKylin 3.0 真机验收记录（2026-10-04）

## 目标环境

- 系统：openKylin 3.0，kernel 7.0.0-2-generic，x86_64
- Python：3.12.2；KylinBot：0.7.5
- SSH 主机密钥 SHA-256：`tfhjGVNoVaz+WXihEVVDDx+5E/l9fawI3Y6rk1aIb2A`
- 构建工具：隔离 venv 内 uv 0.12.16
- 凭据只通过进程环境使用，未写入仓库或报告

## 结果

| 项目 | 结果 | 证据摘要 |
|---|---|---|
| 源码传输 | 通过 | 本地与 guest 归档 SHA-256 均为 `fd77717f17fcb6275b79169b76399bd4e9f64e2499c3cbf20f3d4de14c9d7a3a` |
| 目标机双构建 | 通过 | 两份 `.deb` 与 sidecar 均 `cmp` 相同；33 wheels |
| 最终包 | 通过 | SHA-256 `c5d0b99b5d1ff92f2914b5adb6c1275a66bddd28caf327b719a6ff92f0c3f882` |
| 安装验收 | 通过 | dpkg 状态正常；两轮 quick 各 81 条证据；一致率 100%；`pass^2` 71.4% |
| 普通用户运行 | 通过 | `kylin` 用户运行 CLI 和 quick；安装目录 `0755 root:root` |
| 重启持久性 | 通过 | boot ID `23417952-...` 变为 `7bc7e4ce-...`；重启后包仍可用 |
| 拨钟恢复 | 通过 | +1 天期间 NTP 关闭；恢复后 epoch 仅补偿实耗，NTP 重新同步 |
| 多用户隔离 | 通过 | 临时用户无法读取 KylinBot `brain.db` |
| 断网恢复 | 通过 | NetworkManager 离线 15 秒后恢复 `full`，SSH 自动恢复 |
| auditd Action | 通过 | canary 产生 6 条 Action，覆盖 create/rename/delete/write，规则已清理 |
| 文件快照 | 通过 | 144 条路径指纹，canary 内容 SHA-256 精确匹配 |

真机联调修复了四个只在目标环境暴露的问题：远端 Python 探针首字符错误、重启过早
判定、openKylin `ausearch` 需要显式日志路径，以及 KylinBot stderr 被丢弃。

## 未完成边界

- 宿主未提供 `vmrun`、`.vmx` 路径和快照名，VMware 快照回滚未执行。
- KylinBot 配置指向 OpenRouter，但没有 API key；真实对话与记忆能力系统测试未执行。
- Hermes 未安装。

最终安装日志见 `dist/memhall_0.2.1_amd64.acceptance-openkylin.txt`。
