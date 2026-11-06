@echo off
setlocal
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0tools\launcher\launch.ps1" %*
set "LAUNCH_EXIT=%ERRORLEVEL%"
if not "%LAUNCH_EXIT%"=="0" if "%~1"=="" (
  echo.
  echo Launcher failed. Press any key to close this window.
  pause >nul
)
exit /b %LAUNCH_EXIT%
