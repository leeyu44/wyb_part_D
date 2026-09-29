"""系统级测试（design.md §7）最小版：重启存活 / 拨钟隔天 / 多用户隔离 / 断网存活。

与 API 评测的本质差别：这些测试在真实 openKylin 系统上发生——真重启、真断网、
真换用户、真拨钟（SSH 驱动），openKylin 主场。磐石回滚 / auditd / 写入监控为
扩展项，本版未含（design.md ⚠️ 项，验证前不夸大）。

用法：memhall systest -a hermes（VM 内适配器；凭据 VM_* 环境变量）
产物：runs/<ts>-systest-<adapter>/{systest.md, systest.png, systest.json}

注意：本套件会重启虚拟机、短暂断开虚拟机网络（约 1 分钟，自动恢复）。
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from memhall.adapters.remote import SshChannel, b64


@dataclass
class TestResult:
    name: str
    zh: str
    mechanism: str = ""       # 用的 openKylin/系统机制（评审对账用）
    passed: bool = False
    detail: str = ""
    evidence: list[str] = field(default_factory=list)


def _canary_in(entries: list, needle: str) -> bool:
    return any(needle in (getattr(e, "content", "") or "") for e in entries)


class SystemTester:
    """四项系统级测试。每段重建适配器（重启/断网后旧 SSH 连接不可复用）。"""

    REBOOT_WAIT_S = 300

    def __init__(self, adapter_name: str = "hermes"):
        if adapter_name != "hermes":
            raise ValueError("最小版先支持 hermes；kylinbot 按同契约扩展")
        self.adapter_name = adapter_name
        self.ch = SshChannel()

    def _mk(self):
        from memhall.adapters.hermes import HermesAdapter
        return HermesAdapter()

    def _sudo(self, cmd: str, timeout: int = 60) -> tuple[int, str, str]:
        return self.ch.run(f"echo '{self.ch.password}' | sudo -S {cmd}",
                           timeout=timeout)

    # ---------- 1 重启存活 ----------

    def test_reboot_survival(self) -> TestResult:
        r = TestResult("reboot_survival", "重启存活", "systemd / 持久存储")
        a = self._mk()
        a.reset()
        a.send("s-01", "我的代码目录是 ~/work/src")
        taught = _canary_in(a.dump_memory().entries, "~/work/src")
        r.evidence.append(f"教学后记忆写入: {'✓' if taught else '✗'}")
        if not taught:
            r.passed, r.detail = False, "教学阶段就没写入记忆，无法测重启"
            return r
        try:
            self._sudo("shutdown -r now", timeout=8)
        except Exception:
            pass  # 连接随重启断开，属预期
        self.ch.close()
        up = self._wait_ssh(self.REBOOT_WAIT_S)
        if not up:
            r.passed, r.detail = False, f"{self.REBOOT_WAIT_S}s 内 SSH 未恢复"
            return r
        a2 = self._mk()
        reply = a2.send("s-01", "你记的我的代码目录是哪个？")
        remembered = "~/work/src" in reply.text
        survived = _canary_in(a2.dump_memory().entries, "~/work/src")
        r.evidence.append(f"重启后回答: {reply.text[:120]}")
        r.evidence.append(f"重启后记忆文件仍含 canary: {'✓' if survived else '✗'}")
        r.passed, r.detail = remembered and survived, \
            f"召回{'✓' if remembered else '✗'} 记忆存活{'✓' if survived else '✗'}"
        return r

    def _wait_ssh(self, timeout_s: int) -> bool:
        t0 = time.time()
        while time.time() - t0 < timeout_s:
            time.sleep(10)
            try:
                ch = SshChannel()
                rc, out, _ = ch.run("echo ok")
                ch.close()
                if rc == 0:
                    return True
            except Exception:
                continue
        return False

    # ---------- 2 拨钟隔天 ----------

    def test_clock_day(self) -> TestResult:
        r = TestResult("clock_day", "拨钟隔天(时间理解前提)", "系统时钟")
        a = self._mk()
        reply = a.send("s-01", "临时口令 canary-9x8q 今天有效，明天作废")
        written = _canary_in(a.dump_memory().entries, "canary-9x8q")
        if not written:
            # hermes 已知概率性丢写（嘴上说记住实际没写）——重教一次再判
            r.evidence.append(f"第 1 次教学回答: {reply.text[:120]}（未写入，重试）")
            a.send("s-01", "记住：我的临时口令是 canary-9x8q，今天有效明天作废")
            written = _canary_in(a.dump_memory().entries, "canary-9x8q")
        if not written:
            entries = [e.content[:60] for e in a.dump_memory().entries]
            r.evidence.append(f"两次教学均未写入；当前记忆: {entries}")
            r.passed, r.detail = False, "口令两次教学均未写入（被测体写入可靠性缺陷实录）"
            return r
        try:
            a.clock_shift(3)
            reply = a.send("s-01", "你记的我的临时口令是什么？")
            r.evidence.append(f"+3 天后回答: {reply.text[:200]}")
            remembered = "canary-9x8q" in reply.text
            r.passed = remembered
            r.detail = (f"时钟真拨 +3 天 ✓，召回口令 {'✓' if remembered else '✗'}；"
                        "过期语义由判卷层评，此处记录行为")
        finally:
            a.clock_restore()
        return r

    # ---------- 3 多用户隔离 ----------

    def test_multiuser(self) -> TestResult:
        from memhall.adapters.hermes import MEM_DIR
        r = TestResult("multiuser", "多用户隔离", "Linux 多用户 / 文件权限")
        self._sudo("useradd -m mh-iso 2>/dev/null || true")
        # 教学内容可能落在 MEMORY.md 或 USER.md——逐个查真实存在的
        _, lsout, _ = self.ch.run(f"ls -l {MEM_DIR}/ 2>/dev/null")
        r.evidence.append(f"记忆目录: {(lsout.strip().splitlines() or ['(空)'])}")
        leaked_files: list[str] = []
        checked = 0
        for fname in ("MEMORY.md", "USER.md"):
            rc, out, err = self._sudo(f"-u mh-iso cat {MEM_DIR}/{fname}")
            exists = "No such file" not in (out + err)
            if not exists:
                continue
            checked += 1
            content = out.strip()
            r.evidence.append(f"mh-iso 读 {fname}: "
                              f"{'泄漏!' if content else '拒绝/空'}"
                              f"{(' | ' + err.strip()[-60:]) if err.strip() else ''}")
            if content:
                leaked_files.append(fname)
        self._sudo("userdel -r mh-iso 2>/dev/null || true")
        if checked == 0:
            r.passed, r.detail = False, "没有可测的记忆文件（前置教学未落盘）"
        else:
            r.passed = not leaked_files
            r.detail = ("其他用户读不到记忆" if not leaked_files
                        else f"权限过宽可被同机其他用户读取: {leaked_files}")
        return r

    # ---------- 4 断网存活 ----------

    def test_offline(self) -> TestResult:
        from memhall.adapters.hermes import HERMES_BIN
        r = TestResult("offline", "断网存活(记忆库本地性)", "NetworkManager")
        a = self._mk()
        a.send("s-01", "我的常用邮箱前缀是 mailcanary77")
        written = _canary_in(a.dump_memory().entries, "mailcanary77")
        if not written:
            r.passed, r.detail = False, "教学未写入"
            return r
        # 调度脚本：断网 → 离线探测 → 自动恢复（setsid 脱离会话，断网不误伤）
        # 脚本以 root 跑，~ 会展开成 /root——hermes 用绝对路径 + 正确 HOME
        home = f"/home/{self.ch.user}"
        hermes_bin = HERMES_BIN.replace("~", home)
        probe = (f"{a._base_env}"  # 与评测 send 同构：网关凭据走环境（root 下无用户配置）
                 f"echo {b64('你记的我的常用邮箱前缀是什么？')} | base64 -d | "
                 f"HOME={home} timeout 60 {hermes_bin} chat --query-file - --oneshot "
                 f"--provider deepseek --model {a._model}")
        script = ("#!/bin/sh\n"
                  "nmcli networking off\n"
                  "sleep 20\n"
                  f"{probe} > /tmp/mh-offline.txt 2>&1; echo probe_rc=$? >> /tmp/mh-offline.txt\n"
                  "sleep 5\n"
                  "nmcli networking on\n")
        self.ch.run(f"cat > /tmp/mh-offline.sh <<'SEOF'\n{script}SEOF\n"
                    "chmod 700 /tmp/mh-offline.sh")
        self.ch.run("rm -f /tmp/mh-offline.txt")
        self._sudo("sh -c 'setsid /tmp/mh-offline.sh >/dev/null 2>&1 < /dev/null &'")
        self.ch.close()
        up = self._wait_ssh(180)
        if not up:
            r.passed, r.detail = False, "断网后 180s 内网络未自动恢复（需手动 nmcli on）"
            return r
        _, out, _ = self.ch.run("cat /tmp/mh-offline.txt 2>/dev/null | head -6")
        a2 = self._mk()
        survived = _canary_in(a2.dump_memory().entries, "mailcanary77")
        r.evidence.append(f"离线期间探测输出: {out.strip()[:200] or '(空)'}")
        r.evidence.append(f"断网窗口后记忆仍含 canary: {'✓' if survived else '✗'}")
        replied = "mailcanary77" in out
        r.passed, r.detail = survived, \
            (f"记忆库本地存活{'✓' if survived else '✗'}；"
             f"断网中智能体{'仍可应答' if replied else '不可应答（云端模型依赖）'}——"
             "前者测存储本地性，后者如实记录")
        return r


def run_systest(adapter_name: str, out_root: Path) -> dict:
    t = SystemTester(adapter_name)
    results: list[TestResult] = []
    for fn in (t.test_reboot_survival, t.test_clock_day,
               t.test_multiuser, t.test_offline):
        try:
            results.append(fn())
        except Exception as e:  # 单项失败不能打崩整轮系统级测试
            results.append(TestResult(fn.__name__.removeprefix("test_"),
                                      fn.__doc__ or "", "", False,
                                      f"运行错误: {type(e).__name__}: {e}"))
    t.ch.close()

    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    run_dir = out_root / f"{ts}-systest-{adapter_name}"
    run_dir.mkdir(parents=True, exist_ok=True)
    n_pass = sum(1 for x in results if x.passed)
    summary = {"adapter": adapter_name, "ts": ts,
               "n_pass": n_pass, "n_total": len(results),
               "results": [asdict(x) for x in results]}
    (run_dir / "systest.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    (run_dir / "systest.md").write_text(
        render_systest_report(results, adapter_name, ts), encoding="utf-8")
    render_systest_chart(results, str(run_dir / "systest.png"), adapter_name)
    return {"run_dir": str(run_dir), **{k: summary[k] for k in ("n_pass", "n_total")},
            "results": results}


def render_systest_report(results: list[TestResult], adapter: str, ts: str) -> str:
    lines = [f"# 系统级测试报告 · {adapter} · {ts}", "",
             "与 API 评测的差别：以下每一项都在真实 openKylin 系统上发生"
             "（真重启 / 真断网 / 真换用户 / 真拨钟）。", "",
             "| # | 测试 | 机制 | 结果 | 结论 |", "|---|---|---|---|---|"]
    for i, x in enumerate(results, 1):
        lines.append(f"| {i} | {x.zh} | {x.mechanism} "
                     f"| {'✅ 通过' if x.passed else '❌ 未过'} | {x.detail} |")
    lines += ["", "## 证据", ""]
    for x in results:
        lines.append(f"### {x.zh}")
        lines += [f"- {e}" for e in x.evidence] or ["-（无）"]
        lines.append("")
    lines.append("> 磐石回滚 / auditd 审计 / 写入监控为扩展项（design.md §7 ⚠️），本版未含。")
    return "\n".join(lines)


def render_systest_chart(results: list[TestResult], out_png: str,
                         adapter: str = "") -> None:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from memhall.report.radar import _setup_font
    _setup_font()
    names = [x.zh for x in results][::-1]
    vals = [1 if x.passed else 0 for x in results][::-1]
    fig, ax = plt.subplots(figsize=(8, 2.6))
    ax.barh(names, [1] * len(names), color="#e4e4e7", height=0.55)
    ax.barh(names, vals, color=["#3790FA" if v else "#e58a7c" for v in vals],
            height=0.55)
    for i, v in enumerate(vals):
        ax.text(1.02, i, "通过" if v else "未过", va="center", fontsize=11)
    ax.set_xlim(0, 1.25)
    ax.get_xaxis().set_visible(False)
    for s in ("top", "right", "bottom", "left"):
        ax.spines[s].set_visible(False)
    ax.set_title(f"系统级测试 · {adapter} · {sum(vals)}/{len(results)} 通过", fontsize=13)
    fig.tight_layout()
    fig.savefig(out_png, dpi=150)
    plt.close(fig)
