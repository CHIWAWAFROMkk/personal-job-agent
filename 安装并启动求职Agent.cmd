@echo off
setlocal
chcp 65001 >nul
title 安装个人求职 Agent
cd /d "%~dp0"

echo 正在准备个人求职 Agent 的本地运行环境...
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\setup.ps1"
if errorlevel 1 (
    echo.
    echo 安装未完成。请确认已安装 64 位 Python 3.12 或 3.13，并勾选 Add python.exe to PATH。
    pause
    exit /b 1
)

echo.
echo 安装完成，正在打开桌面程序...
".venv\Scripts\python.exe" -m job_agent.desktop
if errorlevel 1 (
    echo.
    echo 启动失败。请保留此窗口中的提示以便排查。
    pause
)
endlocal
