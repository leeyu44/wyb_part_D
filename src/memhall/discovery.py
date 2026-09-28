"""一键发现本机/评测机智能体（doctor 档接入，仿 brew doctor / opencode provider 探测）。

三路探测：
1. 本机：PATH 可执行文件 + 知名配置目录 + --version（shutil.which，平台无关）
2. 评测机（可选）：单次 SSH 复合命令探测远端二进制与记忆库状态
3. 评测环境就绪度：.env 密钥、SSH 可达、LLM 网关可达

设计要点（沿用成熟方案惯例）：
- 信号矩阵而非单一判据：可执行 + 配置目录任一命中即"发现"
- 优雅降级：发现但无适配器的智能体照常列出，给接入指引而不是报错
- 退出码：0 = 至少一个适配器可用；1 = 什么都不能跑
"""

from __future__ import annotations

import os
import shutil
import socket
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse


@dataclass
class Finding:
    name: str
    where: str                 # local / vm
    found: bool
    version: str = ""
    detail: str = ""
    adapter: str = ""          # 对应 memhall run -a <name>；空 = 无适配器
    hint: str = ""


@dataclass
class EnvCheck:
    name: str
    ok: bool
    detail: str = ""


@dataclass
class DoctorReport:
    local: list[Finding] = field(default_factory=list)
    vm: list[Finding] = field(default_factory=list)
    env: list[EnvCheck] = field(default_factory=list)
    vm_error: str = ""

    def usable_adapters(self) -> list[str]:
        names = {f.adapter for f in self.local + self.vm
                 if f.found and f.adapter}
        names.add("mock")
        return sorted(names)


# (名字, 可执行候选, 配置目录候选, 适配器名) —— 信号矩阵：可执行或配置目录任一命中
LOCAL_AGENTS: list[tuple[str, list[str], list[str], str]] = [
    ("claude-code", ["claude"], ["~/.claude"], ""),
    ("codex", ["codex"], ["~/.codex"], ""),
    ("gemini-cli", ["gemini"], ["~/.gemini"], ""),
    ("opencode", ["opencode"], ["~/.config/opencode"], ""),
    ("aider", ["aider"], ["~/.aider.conf.yml"], ""),
    ("hermes", ["hermes"], ["~/.hermes"], "hermes"),
]

VM_AGENTS: list[tuple[str, list[str], str]] = [
    ("hermes", ["~/.hermes/bin/hermes", "/usr/local/bin/hermes"], "hermes"),
    ("kylin-bot", ["/usr/bin/kylin-bot", "/usr/local/bin/kylin-bot"], "kylinbot"),
]


def scan_local(timeout_s: int = 3) -> list[Finding]:
    import subprocess
    from concurrent.futures import ThreadPoolExecutor
    out: list[Finding] = []
    hits: list[tuple[Finding, str]] = []   # (finding, exe)
    for name, bins, cfgs, adapter in LOCAL_AGENTS:
        exe = next((b for b in bins if shutil.which(b)), "")
        hit_cfg = next((c for c in cfgs
                        if Path(c).expanduser().exists()), "")
        if exe or hit_cfg:
            hits.append((Finding(name, "local", True,
                                 detail=exe or hit_cfg, adapter=adapter), exe))
        else:
            out.append(Finding(name, "local", False))

    # 版本是锦上添花：并行探测 + 短超时，慢 CLI（如本地 hermes --version 实测 11s）不等
    def _ver(exe: str) -> str:
        if not exe:
            return ""
        try:
            r = subprocess.run([exe, "--version"], capture_output=True,
                               text=True, timeout=timeout_s)
            return ((r.stdout or r.stderr).strip().splitlines()[0][:40]
                    if (r.stdout or r.stderr).strip() else "")
        except (OSError, subprocess.TimeoutExpired, IndexError):
            return ""

    if hits:
        with ThreadPoolExecutor(max_workers=6) as ex:
            versions = list(ex.map(_ver, [h[1] for h in hits]))
        for (f, _), ver in zip(hits, versions):
            f.version = ver
            out.append(f)
    return out


# 一次 SSH 打包全部探测（省往返），输出 name|version|path 供解析
_VM_PROBE = (
    "for spec in "
    "'hermes|~/.hermes/bin/hermes' "
    "'hermes|/usr/local/bin/hermes' "
    "'kylin-bot|/usr/bin/kylin-bot' "
    "'kylin-bot|/usr/local/bin/kylin-bot'; do "
    "n=${spec%%|*}; p=${spec##*|}; "
    "[ -x $(eval echo $p) ] && "
    "echo \"$n|$($(eval echo $p) --version 2>/dev/null | head -1 | cut -c1-40)|$p\"; "
    "done; "
    "ls ~/.kylinbot/workspace/memory/brain.db "
    "~/hermes/memories/MEMORY.md 2>/dev/null | head -2"
)


def scan_vm() -> tuple[list[Finding], str]:
    if not os.environ.get("VM_PASS"):
        return [], "缺 VM_PASS（.env 未加载），跳过评测机扫描"
    try:
        from memhall.adapters.remote import SshChannel
        ch = SshChannel()
        rc, out, _ = ch.run(_VM_PROBE, timeout=30)
    except Exception as e:  # SSH 不通是"警告"不是崩溃
        return [], f"评测机不可达: {str(e)[:120]}"

    findings: dict[str, Finding] = {}
    for line in out.splitlines():
        line = line.strip()
        if "|" in line and not line.startswith(("/", "~")):
            name, version, path = (line.split("|", 2) + ["", ""])[:3]
            adapter = next((a for n, _, a in VM_AGENTS if n == name), "")
            findings[name] = Finding(name, "vm", True, version.strip(),
                                     detail=path.strip(), adapter=adapter)
    extra = [l for l in out.splitlines()
             if l.strip().startswith(("/", "~"))]
    if "brain.db" in "\n".join(extra):
        f = findings.setdefault("kylin-bot", Finding("kylin-bot", "vm", True,
                                                     adapter="kylinbot"))
        f.hint = "brain.db 记忆库在位"
    return list(findings.values()), ""


def check_env() -> list[EnvCheck]:
    checks: list[EnvCheck] = []
    need = ["AGENT_LLM_KEY", "AGENT_LLM_BASE_URL", "AGENT_LLM_MODEL"]
    missing = [k for k in need if not os.environ.get(k)]
    checks.append(EnvCheck(
        "LLM 网关配置", not missing,
        "齐全" if not missing else f"缺 {', '.join(missing)}（source .env）"))
    if not missing:
        u = urlparse(os.environ["AGENT_LLM_BASE_URL"])
        host, port = u.hostname, u.port or 443
        try:
            with socket.create_connection((host, port), timeout=4):
                checks.append(EnvCheck("LLM 网关可达", True, f"{host}:{port}"))
        except OSError as e:
            checks.append(EnvCheck("LLM 网关可达", False,
                                   f"{host}:{port} {type(e).__name__}"))
    host = os.environ.get("VM_HOST", "192.168.61.133")
    if os.environ.get("VM_PASS"):
        try:
            with socket.create_connection((host, 22), timeout=4):
                checks.append(EnvCheck("评测机 SSH", True, f"{host}:22"))
        except OSError as e:
            checks.append(EnvCheck("评测机 SSH", False,
                                   f"{host}:22 {type(e).__name__}"))
    else:
        checks.append(EnvCheck("评测机 SSH", False, "缺 VM_PASS，跳过"))
    return checks


def run_doctor(scan_remote: bool = True) -> DoctorReport:
    """三路并行体检（local/vm/env 互不阻塞，墙钟≈最慢一路）。"""
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=3) as ex:
        fut_local = ex.submit(scan_local)
        fut_vm = ex.submit(scan_vm) if scan_remote else None
        fut_env = ex.submit(check_env)
        rep = DoctorReport(local=fut_local.result())
        if fut_vm is not None:
            rep.vm, rep.vm_error = fut_vm.result()
        rep.env = fut_env.result()
    return rep


def render_doctor(rep: DoctorReport) -> str:
    lines = ["麟阁 MemHall · 环境体检", ""]

    lines.append("── 本机智能体 ──")
    for f in rep.local:
        mark = "✓" if f.found else "·"
        ver = f"  {f.version}" if f.version else ""
        where = f"  ({f.detail})" if f.detail else ""
        ad = f"  [适配器: -a {f.adapter}]" if f.adapter else \
            "  [无适配器，可按契约 01 定制]"
        if f.found:
            lines.append(f"  {mark} {f.name:<12}{ver}{where}{ad}")
        else:
            lines.append(f"  {mark} {f.name:<12}（未检出）")
    lines.append("")

    lines.append("── 评测机智能体（openKylin VM）──")
    if rep.vm_error:
        lines.append(f"  ⚠ {rep.vm_error}")
    elif rep.vm:
        for f in rep.vm:
            ver = f"  {f.version}" if f.version else ""
            hint = f"  · {f.hint}" if f.hint else ""
            lines.append(f"  ✓ {f.name:<12}{ver}  ({f.detail})"
                         f"  [适配器: -a {f.adapter}]{hint}")
    else:
        lines.append("  （未发现评测机智能体）")
    lines.append("")

    lines.append("── 评测环境就绪度 ──")
    for c in rep.env:
        lines.append(f"  {'✓' if c.ok else '✗'} {c.name:<12} {c.detail}")
    lines.append("")

    usable = rep.usable_adapters()
    if usable:
        lines.append(f"可跑: memhall run -a {usable[0]} -c cases/full -o runs"
                     f"（可用适配器: {', '.join(usable)}）")
    else:
        lines.append("无可用适配器；mock 始终可用: memhall run -a mock ...")
    return "\n".join(lines)
