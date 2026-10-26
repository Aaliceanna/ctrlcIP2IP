@echo off
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
  echo [1/3] 创建虚拟环境...
  py -3 -m venv .venv 2>nul || python -m venv .venv
)
if not exist ".venv\Scripts\python.exe" (
  echo [ERROR] 未找到 Python，请先安装 Python 3.9+
  pause
  exit /b 1
)

echo [2/3] 安装依赖...
".venv\Scripts\python.exe" -m pip install -q --upgrade pip pyinstaller pystray pillow

echo [3/3] 编译...
".venv\Scripts\python.exe" -m PyInstaller --onefile --noconfirm --clean --name clipshare-cli ^
  --collect-submodules pystray --collect-submodules PIL clipshare.py
".venv\Scripts\python.exe" -m PyInstaller --onefile --noconsole --noconfirm --clean --name ClipShare ^
  --collect-submodules pystray --collect-submodules PIL clipshare.py

echo.
echo 产物:
dir /b dist
echo.
echo   ClipShare.exe      双击运行，托盘常驻（推荐）
echo   clipshare-cli.exe  带控制台，命令行操作用
echo.
pause
