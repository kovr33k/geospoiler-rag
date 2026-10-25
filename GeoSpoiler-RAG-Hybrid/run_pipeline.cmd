@echo off
setlocal
powershell.exe -ExecutionPolicy Bypass -File "%~dp0run_pipeline.ps1" %*
exit /b %ERRORLEVEL%
