# -*- mode: python ; coding: utf-8 -*-
"""麟阁 MemHall 单文件版（onefile）：一个 exe 直发即用，用例打进包内。

启动比 onedir 慢几秒（每次解包到临时目录），分发场景（微信群/网盘）专用；
runs/.env 落在 exe 所在目录，重跑不丢。
"""
from pathlib import Path

ROOT = Path(SPECPATH).resolve()

a = Analysis(
    ["scripts/exe_entry.py"],
    pathex=[str(ROOT / "src")],
    binaries=[],
    datas=[
        (str(ROOT / "src" / "memhall" / "ui" / "static"), "memhall/ui/static"),
        (str(ROOT / "cases"), "cases"),
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
    a.binaries,
    a.datas,
    [],
    name="MemHall",
    debug=False,
    strip=False,
    upx=False,
    console=False,
    icon=str(ROOT / "src" / "memhall" / "ui" / "static" / "icon.ico"),
)
