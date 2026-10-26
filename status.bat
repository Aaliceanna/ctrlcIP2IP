@echo off
setlocal
cd /d "%~dp0"
chcp 65001 >nul

if exist "clipshare-cli.exe" (
  clipshare-cli.exe status
  goto done
)

set "PYEXE="
for %%P in (py.exe python.exe python3.exe) do if not defined PYEXE (where %%P >nul 2>nul && set "PYEXE=%%P")
if not defined PYEXE (
  echo [ERROR] 没找到 clipshare-cli.exe，也没有可用的 Python。
  pause
  exit /b 1
)
%PYEXE% clipshare.py status

:done
echo.
pause
