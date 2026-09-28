"""doctor 一键发现：本机扫描信号矩阵 + 环境体检降级路径。"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import pytest

from memhall.discovery import (
    DoctorReport,
    Finding,
    check_env,
    render_doctor,
    scan_local,
)


def test_scan_local_signal_matrix(monkeypatch, tmp_path):
    import memhall.discovery as disc
    monkeypatch.setattr(disc, "_which", lambda b: f"/fake/bin/{b}" if b == "claude" else "")
    monkeypatch.setattr(disc, "CACHE_PATH", tmp_path / "cache.json")
    monkeypatch.setattr(
        "memhall.discovery.Path.exists", lambda self: "codex" in str(self))
    monkeypatch.setattr("subprocess.run", lambda *a, **k: type(
        "R", (), {"stdout": "1.0.0-cli\n", "stderr": ""})())
    out = scan_local()
    by_name = {f.name: f for f in out}
    assert by_name["claude-code"].found and by_name["claude-code"].version == "1.0.0-cli"
    assert by_name["codex"].found and by_name["codex"].detail  # 仅配置目录命中也算发现
    assert not by_name["aider"].found


def test_check_env_degrades(monkeypatch):
    for k in ("AGENT_LLM_KEY", "AGENT_LLM_BASE_URL", "AGENT_LLM_MODEL", "VM_PASS"):
        monkeypatch.delenv(k, raising=False)
    checks = {c.name: c for c in check_env()}
    assert checks["LLM 网关配置"].ok is False
    assert "缺" in checks["LLM 网关配置"].detail


def test_usable_adapters_always_has_mock():
    rep = DoctorReport(local=[], vm=[], env=[])
    assert "mock" in rep.usable_adapters()


def test_render_contains_sections():
    rep = DoctorReport(local=[Finding("claude-code", "local", True, "1.0", "x")],
                       vm=[], env=[])
    text = render_doctor(rep)
    for section in ("本机智能体", "评测机智能体", "评测环境就绪度", "可跑"):
        assert section in text
