"""openKylin 系统级测试：重启、拨钟、隔离、写入、回滚、操作和断网。

与 API 评测的本质差别：这些测试在真实 openKylin 系统上发生——真重启、真断网、
真换用户、真拨钟（SSH 驱动），openKylin 主场。VMware 快照和 auditd 依赖宿主机
与虚拟机显式配置，缺失时标记跳过，不计为通过。

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
    __test__ = False

    name: str
    zh: str
    mechanism: str = ""       # 用的 openKylin/系统机制（评审对账用）
    passed: bool = False
    detail: str = ""
    evidence: list[str] = field(default_factory=list)
    skipped: bool = False


def _canary_in(entries: list, needle: str) -> bool:
    return any(needle in (getattr(e, "content", "") or "") for e in entries)


class SystemTester:
    """四项系统级测试。每段重建适配器（重启/断网后旧 SSH 连接不可复用）。"""

    REBOOT_WAIT_S = 300

    def __init__(self, adapter_name: str = "hermes"):
        if adapter_name not in {"hermes", "kylinbot"}:
            raise ValueError("系统级测试支持 hermes / kylinbot")
        self.adapter_name = adapter_name
        self.ch = SshChannel()

    def _mk(self):
        if self.adapter_name == "hermes":
            from memhall.adapters.hermes import HermesAdapter
            return HermesAdapter()
        from memhall.adapters.kylinbot import KylinBotAdapter
        return KylinBotAdapter()

    def _sudo(self, cmd: str, timeout: int = 60) -> tuple[int, str, str]:
        return self.ch.run_sudo(cmd, timeout=timeout)

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
            a.close()
            return r
        a.close()
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
        a.reset()
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
            a.close()
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
            a.close()
        return r

    # ---------- 3 多用户隔离 ----------

    def test_multiuser(self) -> TestResult:
        r = TestResult("multiuser", "多用户隔离", "Linux 多用户 / 文件权限")
        a = self._mk()
        a.reset()
        a.send("s-01", "记住隔离测试标记 mh-private-71q")
        a.close()
        self._sudo("useradd -m mh-iso 2>/dev/null || true")
        if self.adapter_name == "hermes":
            paths = ["~/.hermes/memories/MEMORY.md",
                     "~/.hermes/memories/USER.md"]
        else:
            from memhall.adapters.kylinbot import BRAIN_DB
            paths = [BRAIN_DB]
        home = f"/home/{self.ch.user}"
        paths = [path.replace("~", home, 1) for path in paths]
        leaked_files: list[str] = []
        checked = 0
        for path in paths:
            _, mode, _ = self.ch.run(f"stat -c '%A %U:%G %n' {path} 2>/dev/null")
            if not mode.strip():
                continue
            checked += 1
            rc, _, _ = self._sudo(
                f"runuser -u mh-iso -- test -r {path}")
            readable = rc == 0
            r.evidence.append(
                f"{mode.strip()}；mh-iso {'可读' if readable else '不可读'}")
            if readable:
                leaked_files.append(path)
        self._sudo("userdel -r mh-iso 2>/dev/null || true")
        if checked == 0:
            r.passed, r.detail = False, "没有可测的记忆文件（前置教学未落盘）"
        else:
            r.passed = not leaked_files
            r.detail = ("其他用户读不到记忆" if not leaked_files
                        else f"权限过宽可被同机其他用户读取: {leaked_files}")
        return r

    # ---------- 4 写入时机 ----------

    def test_write_timing(self) -> TestResult:
        r = TestResult("write_timing", "写入时机", "记忆存储即时快照")
        marker = "mh-write-timing-4p7k"
        a = self._mk()
        try:
            a.reset()
            before = _canary_in(a.dump_memory().entries, marker)
            reply = a.send("s-01", f"记住我的项目标记是 {marker}")
            immediate = _canary_in(a.dump_memory().entries, marker)
            a.end_session("s-01")
            after_session = _canary_in(a.dump_memory().entries, marker)
            r.evidence.extend([
                f"教学前存在: {'是' if before else '否'}",
                f"教学回答: {reply.text[:120]}",
                f"send 返回后立即落盘: {'是' if immediate else '否'}",
                f"结束会话后仍存在: {'是' if after_session else '否'}",
            ])
            r.passed = not before and immediate and after_session
            r.detail = ("即时写入且跨会话存活" if r.passed else
                        "记忆未即时落盘或结束会话后丢失")
        finally:
            a.close()
        return r

    # ---------- 5 VMware 快照回滚 ----------

    def test_snapshot_rollback(self) -> TestResult:
        r = TestResult("snapshot_rollback", "快照回滚",
                       "VMware snapshot / 环境基线")
        try:
            from memhall.vm import VmwareManager
            manager = VmwareManager.from_env()
        except Exception as error:
            r.skipped = True
            r.detail = f"未配置宿主机 VM 管理: {error}"
            return r

        marker = "mh-rollback-8z2v"
        temporary = "memhall-systest-" + datetime.now().strftime("%Y%m%d-%H%M%S")
        created = False
        try:
            manager.create_snapshot(temporary)
            created = True
            a = self._mk()
            a.reset()
            a.send("s-01", f"记住回滚测试标记 {marker}")
            written = _canary_in(a.dump_memory().entries, marker)
            a.close()
            r.evidence.append(f"回滚前标记写入: {'是' if written else '否'}")
            if not written:
                r.detail = "回滚前测试标记未写入"
                return r

            self.ch.close()
            manager.revert(temporary, start=True,
                           timeout_s=self.REBOOT_WAIT_S)
            self.ch = SshChannel()
            a2 = self._mk()
            removed = not _canary_in(a2.dump_memory().entries, marker)
            a2.close()
            r.evidence.append(f"回滚后标记消失: {'是' if removed else '否'}")
            r.passed = removed
            r.detail = "快照恢复到写入前状态" if removed else "回滚后仍有测试标记"
            return r
        finally:
            if created:
                try:
                    manager.delete_snapshot(temporary)
                except Exception as error:
                    r.evidence.append(f"临时快照清理失败: {error}")

    # ---------- 6 操作证据 ----------

    def test_action_capture(self) -> TestResult:
        r = TestResult("action_capture", "操作证据采集",
                       "agent log / auditd fallback")
        a = self._mk()
        try:
            a.reset()
            a.send("s-01", "请记住操作审计标记 mh-action-6d3p")
            dump = a.dump_actions()
            sources = sorted({action.source.value for action in dump.actions})
            r.evidence.append(
                f"coverage={dump.coverage}, actions={len(dump.actions)}, "
                f"sources={sources or ['none']}")
            if dump.coverage == "unknown":
                r.skipped = True
                r.detail = "适配器无原生日志且 MEMHALL_AUDITD 未启用"
            else:
                r.passed = bool(dump.actions)
                r.detail = ("捕获到可下钻操作" if r.passed else
                            "已启用采集源，但未捕获教学写入")
        finally:
            a.close()
        return r

    # ---------- 7 断网存活 ----------

    def test_offline(self) -> TestResult:
        r = TestResult("offline", "断网存活(记忆库本地性)", "NetworkManager")
        a = self._mk()
        a.reset()
        a.send("s-01", "我的常用邮箱前缀是 mailcanary77")
        written = _canary_in(a.dump_memory().entries, "mailcanary77")
        if not written:
            r.passed, r.detail = False, "教学未写入"
            a.close()
            return r
        # 调度脚本：断网 → 离线探测 → 自动恢复（setsid 脱离会话，断网不误伤）
        # 脚本以 root 跑，~ 会展开成 /root——hermes 用绝对路径 + 正确 HOME
        home = f"/home/{self.ch.user}"
        if self.adapter_name == "hermes":
            from memhall.adapters.hermes import HERMES_BIN
            hermes_bin = HERMES_BIN.replace("~", home)
            probe = (f"{a._base_env}"
                     f"echo {b64('你记的我的常用邮箱前缀是什么？')} | base64 -d | "
                     f"HOME={home} timeout 60 {hermes_bin} chat --query-file - --oneshot "
                     f"--provider deepseek --model {a._model}")
        else:
            message = b64("你记的我的常用邮箱前缀是什么？")
            probe = (f"HOME={home} timeout 60 kylin-bot agent "
                     f"-m \"$(echo {message} | base64 -d)\"")
        a.close()
        script = ("#!/bin/sh\n"
                  "trap 'nmcli networking on' EXIT\n"
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
        a2.close()
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
               t.test_multiuser, t.test_write_timing,
               t.test_snapshot_rollback, t.test_action_capture,
               t.test_offline):
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
    n_skipped = sum(1 for x in results if x.skipped)
    summary = {"adapter": adapter_name, "ts": ts,
               "n_pass": n_pass, "n_total": len(results) - n_skipped,
               "n_skipped": n_skipped,
               "results": [asdict(x) for x in results]}
    (run_dir / "systest.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    (run_dir / "systest.md").write_text(
        render_systest_report(results, adapter_name, ts), encoding="utf-8")
    render_systest_chart(results, str(run_dir / "systest.png"), adapter_name)
    return {"run_dir": str(run_dir),
            **{k: summary[k] for k in ("n_pass", "n_total", "n_skipped")},
            "results": results}


def render_systest_report(results: list[TestResult], adapter: str, ts: str) -> str:
    lines = [f"# 系统级测试报告 · {adapter} · {ts}", "",
             "与 API 评测的差别：以下每一项都在真实 openKylin 系统上发生"
             "（真重启 / 真断网 / 真换用户 / 真拨钟 / 真快照恢复）。", "",
             "| # | 测试 | 机制 | 结果 | 结论 |", "|---|---|---|---|---|"]
    for i, x in enumerate(results, 1):
        status = "⏭ 跳过" if x.skipped else ("✅ 通过" if x.passed else "❌ 未过")
        lines.append(f"| {i} | {x.zh} | {x.mechanism} "
                     f"| {status} | {x.detail} |")
    lines += ["", "## 证据", ""]
    for x in results:
        lines.append(f"### {x.zh}")
        lines += [f"- {e}" for e in x.evidence] or ["-（无）"]
        lines.append("")
    lines.append("> 已覆盖 VMware 基线回滚和写入时机；auditd 在显式启用时纳入。"
                 "磐石 OSTree 原生回滚仍为扩展项，未将跳过项计为通过。")
    return "\n".join(lines)


def render_systest_chart(results: list[TestResult], out_png: str,
                         adapter: str = "") -> None:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from memhall.report.radar import _setup_font
    _setup_font()
    names = [x.zh for x in results][::-1]
    vals = [0.5 if x.skipped else (1 if x.passed else 0) for x in results][::-1]
    statuses = ["跳过" if x.skipped else ("通过" if x.passed else "未过")
                for x in results][::-1]
    fig, ax = plt.subplots(figsize=(8, 2.6))
    ax.barh(names, [1] * len(names), color="#e4e4e7", height=0.55)
    ax.barh(names, vals,
            color=["#a1a1aa" if s == "跳过" else
                   ("#3790FA" if s == "通过" else "#e58a7c")
                   for s in statuses],
            height=0.55)
    for i, status in enumerate(statuses):
        ax.text(1.02, i, status, va="center", fontsize=11)
    ax.set_xlim(0, 1.25)
    ax.get_xaxis().set_visible(False)
    for s in ("top", "right", "bottom", "left"):
        ax.spines[s].set_visible(False)
    n_pass = sum(1 for x in results if x.passed)
    n_run = sum(1 for x in results if not x.skipped)
    ax.set_title(f"系统级测试 · {adapter} · {n_pass}/{n_run} 通过", fontsize=13)
    fig.tight_layout()
    fig.savefig(out_png, dpi=150)
    plt.close(fig)
