@echo off
setlocal
chcp 65001 >nul
title Personal Job Agent - Browser Component
cd /d "%~dp0"

set "PW_NODE=%~dp0_internal\playwright\driver\node.exe"
set "PW_CLI=%~dp0_internal\playwright\driver\package\cli.js"

if not exist "%PW_NODE%" (
  echo Missing the bundled browser installer. Please download the complete desktop package again.
  pause
  exit /b 1
)

if not exist "%PW_CLI%" (
  echo Missing the browser component manifest. Please download the complete desktop package again.
  pause
  exit /b 1
)

echo Downloading Chromium from the official Playwright distribution service...
"%PW_NODE%" "%PW_CLI%" install chromium
if errorlevel 1 (
  echo.
  echo Installation failed. Check your network and retry. Core Job Agent features remain available.
  pause
  exit /b 1
)

echo.
echo The optional application-assist browser is ready.
pause
endlocal
