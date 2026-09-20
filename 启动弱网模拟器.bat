@echo off
chcp 65001 >nul
cd /d "%~dp0"
set "PY=%~dp0.venv\Scripts\python.exe"
if not exist "%PY%" set "PY=python"

net session >nul 2>&1
if %errorlevel% neq 0 (
    echo 正在请求管理员权限...
    powershell -NoProfile -Command "Start-Process -FilePath '%PY%' -ArgumentList '\"%~dp0weak_net_simulator.py\"' -WorkingDirectory '%~dp0' -Verb RunAs"
    exit /b
)
"%PY%" "%~dp0weak_net_simulator.py"
if errorlevel 1 pause
