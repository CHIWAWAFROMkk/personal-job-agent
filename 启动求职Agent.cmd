@echo off
setlocal
chcp 65001 >nul
title Personal Job Agent
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo 首次运行，正在创建本地环境。这个过程只需要完成一次。
    powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\setup.ps1"
    if errorlevel 1 (
        echo.
        echo 安装未完成。请确认已安装 64 位 Python 3.12 或 3.13。
        pause
        exit /b 1
    )
)

echo 正在启动个人求职 Agent...
".venv\Scripts\python.exe" -m job_agent.desktop
if errorlevel 1 (
    echo.
    echo 启动失败。请保留此窗口中的提示以便排查。
    pause
)
endlocal
