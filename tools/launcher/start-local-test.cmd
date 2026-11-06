@echo off
setlocal
cd /d "%~dp0..\.."
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0start-local-test.ps1" %*
set "TEST_EXIT=%ERRORLEVEL%"
if not "%TEST_EXIT%"=="0" if "%~1"=="" (
  echo.
  echo Offline test failed to start. Press any key to close this window.
  pause >nul
)
exit /b %TEST_EXIT%
