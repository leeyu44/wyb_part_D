#!/usr/bin/env bash
# 麟阁 MemHall Windows 打包：
#   dist/MemHall/                onedir（本机用，启动快）
#   dist/麟阁MemHall-单文件版.exe  onefile（单文件直发微信群/网盘）
set -euo pipefail
cd "$(dirname "$0")/.."

rm -rf build dist/MemHall dist/MemHall.exe "dist/麟阁MemHall-单文件版.exe"
uv run pyinstaller --noconfirm memhall.spec
cp -r cases dist/MemHall/cases
uv run pyinstaller --noconfirm memhall-onefile.spec
mv dist/MemHall.exe "dist/麟阁MemHall-单文件版.exe"
du -sh dist/MemHall "dist/麟阁MemHall-单文件版.exe"
echo "完成：onedir=dist/MemHall/ · 单文件=dist/麟阁MemHall-单文件版.exe"
