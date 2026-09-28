# -*- mode: python ; coding: utf-8 -*-
"""麟阁 MemHall Windows 打包配置（onedir；双击 exe = 原生窗口）。

构建：uv run pyinstaller --noconfirm memhall.spec（见 scripts/build_exe.sh）
产物：dist/MemHall/MemHall.exe + _internal/；cases/ 由构建脚本复制到 exe 同级。
"""
from pathlib import Path

ROOT = Path(SPECPATH).resolve()

a = Analysis(
    ["scripts/exe_entry.py"],
    pathex=[str(ROOT / "src")],
    binaries=[],
    datas=[
        (str(ROOT / "src" / "memhall" / "ui" / "static"), "memhall/ui/static"),
    ],
    hiddenimports=[
        # uvicorn 运行时按需导入的子模块，静态分析看不见
        "uvicorn.logging", "uvicorn.loops.auto",
        "uvicorn.protocols.http.auto", "uvicorn.protocols.websockets.auto",
        "uvicorn.lifespan.on",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["tkinter"],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="MemHall",
    debug=False,
    strip=False,
    upx=False,
    console=False,
    icon=str(ROOT / "src" / "memhall" / "ui" / "static" / "icon.ico"),
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="MemHall",
)
