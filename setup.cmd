@echo off
setlocal
cd /d "%~dp0"
set "TEMP=%CD%\.tmp"
set "TMP=%CD%\.tmp"
if not exist "%TEMP%" mkdir "%TEMP%"
if not exist ".venv" python -m venv .venv
".venv\Scripts\python.exe" -m ensurepip --upgrade --default-pip
if errorlevel 1 exit /b %errorlevel%
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 exit /b %errorlevel%
".venv\Scripts\python.exe" -m pip install --no-deps -e .
if errorlevel 1 exit /b %errorlevel%
echo Cai dat hoan tat. Chay: .venv\Scripts\python.exe -m pytest

