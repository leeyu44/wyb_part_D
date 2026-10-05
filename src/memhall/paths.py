"""装机布局路径的单一来源。

四种运行形态共用同一套解析：源码树 / onedir exe / onefile 解包目录 / deb 安装。
此前 CLI 与 UI 各写一套（cmd_run 的 /usr/share/memhall 回退、app.py 的
_case_roots），行为已在 report 安装态路径上分叉过一次——收敛于此。
"""

from __future__ import annotations

import sys
from pathlib import Path


def repo_root() -> Path:
    """运行根：源码=仓库根；onedir exe=exe 同级。"""
    return (Path(sys.executable).resolve().parent
            if getattr(sys, "frozen", False)
            else Path(__file__).resolve().parents[2])


def case_roots() -> list[Path]:
    """用例目录候选根（先到先用）：运行根 → onefile 解包目录 → deb 安装位。"""
    roots = [repo_root()]
    meipass = getattr(sys, "_MEIPASS", "")
    if meipass:
        roots.append(Path(meipass))
    deb_share = Path("/usr/share/memhall")
    if (deb_share / "cases").is_dir():
        roots.append(deb_share)
    return roots


def resolve_case_dir(spec: str) -> Path | None:
    """把 'cases/full' 这类相对路径解析到实际存在的目录（cwd 相对/绝对优先）。"""
    d = Path(spec)
    if d.is_dir():
        return d
    for root in case_roots():
        p = root / spec
        if p.is_dir():
            return p
    return None


# 冒烟集（虚拟集，不入库）：full 中六能力各 1 题。
# 曾以 cases/quick/ 目录手工复制 full 文件维护，副本与行尾都已漂移——
# 改为按 ID 引用，full 改题自动跟随。
QUICK_IDS = [
    "persist-002", "recall-002", "update-001",
    "discriminate-001", "boundary-001", "reuse-001",
]
