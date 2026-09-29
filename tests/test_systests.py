"""系统级测试纯函数部分：报告渲染 + 招牌图（VM 交互不进 CI）。"""

from pathlib import Path

from memhall.systests import TestResult, render_systest_chart, render_systest_report


def _rs():
    return [
        TestResult("reboot_survival", "重启存活", "systemd / 持久存储", True,
                   "召回✓ 记忆存活✓", ["重启后回答: 代码目录是 ~/work/src"]),
        TestResult("multiuser", "多用户隔离", "Linux 多用户 / 文件权限", False,
                   "记忆文件权限过宽", ["权限: -rw-r--r--"]),
    ]


def test_systest_report_renders():
    md = render_systest_report(_rs(), "hermes", "20260929-000000")
    assert "系统级测试报告 · hermes" in md
    assert "✅ 通过" in md and "❌ 未过" in md
    assert "扩展项" in md          # 诚实边界声明
    assert "-rw-r--r--" in md      # 证据行


def test_systest_chart(tmp_path: Path):
    png = tmp_path / "systest.png"
    render_systest_chart(_rs(), str(png), "hermes")
    assert png.exists() and png.stat().st_size > 4000
