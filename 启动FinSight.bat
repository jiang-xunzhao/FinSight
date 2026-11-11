@echo off
setlocal enabledelayedexpansion
title FinSight
rem ============================================================
rem 解释器优先级（2026-11-11 修改）：
rem   旧写法按“目录在不在”挑解释器，结果先挑中了一个空 venv
rem   （FinSight\.venv，Python 3.14，只有 pip，13 个依赖全缺），
rem   每次双击都在现场 pip 下载安装，几分钟起不来。
rem   新写法按“依赖在不在”挑：谁能 import 下面三件套就用谁，
rem   都不行才退回项目内 .venv（第一次由 my_open.py 自动装依赖）。
rem ============================================================
set "PY="
for %%P in (
  "C:\Users\35999\PyCharmMiscProject\.venv\Scripts\python.exe"
  "%~dp0.venv\Scripts\python.exe"
  "%~dp0..\.venv\Scripts\python.exe"
) do (
  if not defined PY (
    if exist %%P (
      %%P -c "import pymysql, docx, fastembed" >nul 2>nul
      if not errorlevel 1 set "PY=%%~P"
    )
  )
)

rem 一个装齐的都没有：按老顺序退回（会由 my_open.py 自动补装）
if not defined PY if exist "%~dp0.venv\Scripts\python.exe" set "PY=%~dp0.venv\Scripts\python.exe"
if not defined PY if exist "%~dp0..\.venv\Scripts\python.exe" set "PY=%~dp0..\.venv\Scripts\python.exe"
if not defined PY (
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

echo [FinSight] 使用解释器: %PY%
"%PY%" "%~dp0finsight_launch.py"
if errorlevel 1 pause
endlocal
