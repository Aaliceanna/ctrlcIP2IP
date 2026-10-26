@echo off
setlocal
cd /d "%~dp0"
chcp 65001 >nul

if exist "clipshare-cli.exe" (
  clipshare-cli.exe install
  goto done
)

set "PYEXE="
for %%P in (py.exe python.exe python3.exe) do if not defined PYEXE (where %%P >nul 2>nul && set "PYEXE=%%P")
if not defined PYEXE (
  echo [ERROR] 没找到 clipshare-cli.exe，也没有可用的 Python。
  pause
  exit /b 1
)
%PYEXE% clipshare.py install

:done
echo.
echo 安装完成。双击 ClipShare.exe 即可从托盘管理（右键图标 → 打开设置面板）。
echo.
pause
