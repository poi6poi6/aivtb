@echo off
setlocal
set "OBS_DIR=%~dp0data\obs-studio"
if not exist "%OBS_DIR%\bin\64bit\obs64.exe" (
  echo OBS Studio is missing from data\obs-studio.
  pause
  exit /b 1
)
start "" /D "%OBS_DIR%\bin\64bit" "%OBS_DIR%\bin\64bit\obs64.exe" --portable
exit /b 0
