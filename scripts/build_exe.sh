#!/usr/bin/env bash
# 麟阁 MemHall Windows 打包（onedir）：产物 dist/MemHall/MemHall.exe
set -euo pipefail
cd "$(dirname "$0")/.."

rm -rf build dist/MemHall
uv run pyinstaller --noconfirm memhall.spec
cp -r cases dist/MemHall/cases
du -sh dist/MemHall
echo "产物: dist/MemHall/MemHall.exe（双击即开原生窗口）"
