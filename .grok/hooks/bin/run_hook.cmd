@echo off
setlocal
set "ROOT=%GROK_WORKSPACE_ROOT%"
if "%ROOT%"=="" set "ROOT=%CD%"
set "PY=%ROOT%\.venv\Scripts\python.exe"
if not exist "%PY%" set "PY=python"
"%PY%" "%~dp0%1" %2 %3 %4 %5
exit /b %ERRORLEVEL%
