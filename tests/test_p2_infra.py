"""工程化整改 P2（engineering-tasks.md T17-T20）：

- T17 logging：CLI --verbose 全链路日志（runner 逐 case/SSH 命令/判卷重试）
- T19 manifest 复现元数据（memhall 版本 + Python 版本，安装态不再只有 unknown）
- T20 dotenv 单源解析（值含 " #" 不再被拦腰截断）
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from memhall import __version__
from memhall.env import _parse_line

REPO_ROOT = Path(__file__).resolve().parents[1]


def test_dotenv_parse(tmp_path, monkeypatch):
    # 裸值：剥 " #" 起的行内注释
    assert _parse_line("K=abc  # 注释") == ("K", "abc")
    # 值本身含 " #"：裸值仍会被截（与主流 dotenv 行为一致），引号包裹可保真
    assert _parse_line('K="pa#ss word"') == ("K", "pa#ss word")
    assert _parse_line("K='a # b'") == ("K", "a # b")
    # 注释行 / 空行 / 无等号行 / 空键
    assert _parse_line("# 注释") is None
    assert _parse_line("") is None
    assert _parse_line("no-equals") is None
    assert _parse_line("=value") is None


def test_load_dotenv_precedence(tmp_path, monkeypatch):
    from memhall import env as env_mod
    a, b = tmp_path / "a.env", tmp_path / "b.env"
    a.write_text("MH_P2_K=first\n", encoding="utf-8")
    b.write_text("MH_P2_K=second\nMH_P2_ONLY=b\n", encoding="utf-8")
    monkeypatch.setattr(env_mod, "env_candidates", lambda: [a, b])
    monkeypatch.delenv("MH_P2_K", raising=False)
    monkeypatch.delenv("MH_P2_ONLY", raising=False)
    loaded = env_mod.load_dotenv()
    assert loaded == [a, b]
    # 先读到的文件优先（setdefault），后文件只补缺
    assert os.environ["MH_P2_K"] == "first"
    assert os.environ["MH_P2_ONLY"] == "b"
    # 手工 export 优先于 .env
    os.environ["MH_P2_K"] = "manual"
    env_mod.load_dotenv()
    assert os.environ["MH_P2_K"] == "manual"


def test_cli_verbose_logs_and_manifest_meta(tmp_path):
    """--verbose 走通全链路：stderr 有 runner 日志；manifest 记复现元数据。"""
    exe = Path(sys.executable).parent / ("memhall.exe" if os.name == "nt" else "memhall")
    out = tmp_path / "runs"
    r = subprocess.run(
        [str(exe), "run", "-a", "mock", "-c", "cases/quick", "-o", str(out), "-v"],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        cwd=REPO_ROOT, timeout=180,
        env={**os.environ, "PYTHONIOENCODING": "utf-8"},
    )
    assert r.returncode == 0, r.stderr[-2000:]
    assert "评测开始" in r.stderr
    assert "开跑" in r.stderr and "完成" in r.stderr
    run_dir = next(out.iterdir())
    manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["tool_version"] == __version__ != "dev"
    assert manifest["python"] == sys.version.split()[0]
