@echo off
setlocal
chcp 65001 >nul
title RivenSniper QQ BOT Launcher
cd /d "%~dp0"

where pwsh.exe >nul 2>&1
if %errorlevel% equ 0 (
    pwsh.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\windows\start_bot.ps1" %*
) else (
    powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\windows\start_bot.ps1" %*
)

set "RIVENSNIPER_EXIT=%errorlevel%"
echo.
if not "%RIVENSNIPER_EXIT%"=="0" echo Startup did not complete. See the error above.
pause
exit /b %RIVENSNIPER_EXIT%
