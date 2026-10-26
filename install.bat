@echo off
setlocal
cd /d "%~dp0"
set "PYEXE="
for %%P in (py.exe python.exe python3.exe) do if not defined PYEXE (where %%P >nul 2>nul && set "PYEXE=%%P")
if not defined PYEXE (
  echo [ERROR] Python not found in PATH. Install Python 3.9+ first.
  pause
  exit /b 1
)
echo Using: %PYEXE%
%PYEXE% clipshare.py install
echo.
echo Next: %PYEXE% clipshare.py link   (then bind on the other PC)
echo.
pause
