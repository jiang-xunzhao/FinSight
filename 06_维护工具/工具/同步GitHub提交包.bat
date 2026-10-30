@echo off
rem 一键刷新 GitHub 提交包（内容=当前代码，.git 不会被动）
cd /d "%~dp0..\.."
if exist "C:\Users\35999\PyCharmMiscProject\.venv\Scripts\python.exe" (
  "C:\Users\35999\PyCharmMiscProject\.venv\Scripts\python.exe" -X utf8 "06_维护工具\工具\导出GitHub提交包.py"
) else (
  python -X utf8 "06_维护工具\工具\导出GitHub提交包.py"
)
pause
