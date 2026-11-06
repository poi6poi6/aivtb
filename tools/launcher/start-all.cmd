@echo off
setlocal
cd /d "%~dp0..\.."
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0start-all.ps1" %*
set "LAUNCH_EXIT=%ERRORLEVEL%"
if not "%LAUNCH_EXIT%"=="0" if "%~1"=="" (
  echo.
  echo Startup failed. Press any key to close this window.
  pause >nul
)
exit /b %LAUNCH_EXIT%
