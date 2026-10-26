#!/usr/bin/env bash
# 在 Linux 上构建单文件可执行程序：dist/clipshare
set -euo pipefail
cd "$(dirname "$0")"

PY=${PYTHON:-python3}
command -v "$PY" >/dev/null || { echo "未找到 $PY，请先安装 Python 3.9+"; exit 1; }

if [ ! -x .venv/bin/python ]; then
  echo "[1/3] 创建虚拟环境..."
  "$PY" -m venv .venv
fi

echo "[2/3] 安装依赖..."
.venv/bin/python -m pip install -q --upgrade pip pyinstaller pystray pillow

echo "[3/3] 编译..."
.venv/bin/python -m PyInstaller --onefile --noconfirm --clean --name clipshare \
  --collect-submodules pystray --collect-submodules PIL clipshare.py
chmod +x dist/clipshare

echo
ls -lh dist/clipshare
echo "完成：dist/clipshare"
echo "运行需要 xclip（X11）或 wl-clipboard（Wayland），例如：sudo apt install xclip"
