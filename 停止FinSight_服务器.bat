@echo off
rem FinSight 服务器版停止：按命令行里有没有 my_open.py 来找进程（wmic 在新版 Windows 上已经没了，所以用 CIM）
chcp 65001 >nul
powershell -NoProfile -ExecutionPolicy Bypass -Command "Get-CimInstance Win32_Process | Where-Object { $_.Name -like 'python*' -and $_.CommandLine -match 'my_open' } | ForEach-Object { Write-Host ('正在停止 PID ' + $_.ProcessID); Stop-Process -Id $_.ProcessID -Force }; if (-not (Get-CimInstance Win32_Process | Where-Object { $_.Name -like 'python*' -and $_.CommandLine -match 'my_open' })) { Write-Host '已经没有 FinSight 在跑了' }"
pause
