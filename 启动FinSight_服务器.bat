@echo off
rem FinSight 服务器版启动：不带本机注册表密钥、不挑 venv，全部依赖 .env（config.py 会把 .env 灌进环境变量）
chcp 65001 >nul
cd /d "%~dp0"

where python >nul 2>&1
if errorlevel 1 (
    echo 没找到 python。请先装 Python 3 并勾选 "Add python.exe to PATH"。
    pause
    exit /b 1
)
if not exist ".env" (
    echo 请先在同目录把 .env.example 复制成 .env，至少填 DEEPSEEK_API_KEY 和 MYSQL_PASSWORD 两行。
    pause
    exit /b 1
)

title FinSight Server 8501
echo 正在启动 FinSight（首次会自动 pip 装缺失依赖，可能要等几分钟）...
python -u 99_启动入口\my_open.py
echo.
echo 服务已退出。如果是报错退出，往上翻看最后一条错误。
pause
