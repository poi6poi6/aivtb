@echo off
setlocal
title Antigravity - Google Pro sign in
"%~dp0..\.venv\Scripts\python.exe" -X utf8 "%~dp0login_argon.py"
set "argon_login_result=%errorlevel%"
exit /b %argon_login_result%
