@echo off
setlocal
title FinSight
rem 解释器优先级：项目内 .venv → 上级目录 .venv → 本机固定 venv → PATH 里的 python
set "PY=%~dp0.venv\Scripts\python.exe"
if not exist "%PY%" set "PY=%~dp0..\.venv\Scripts\python.exe"
if not exist "%PY%" set "PY=C:\Users\35999\PyCharmMiscProject\.venv\Scripts\python.exe"
if not exist "%PY%" (
  where python >nul 2>nul
  if errorlevel 1 (
    echo [FinSight] Python not found. Install deps or fix the venv path in this .bat
    pause
    exit /b 1
  )
  set "PY=python"
)
if not exist "%~dp0finsight_launch.py" (
  echo [FinSight] finsight_launch.py is missing next to this .bat
  echo [FinSight] Run deploy_launchers.py from the FinSight tools folder to re-create it.
  pause
  exit /b 1
)
"%PY%" "%~dp0finsight_launch.py" --stop
if errorlevel 1 pause
endlocal
