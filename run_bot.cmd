@echo off
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo [ERROR] Chua co .venv. Hay chay setup.cmd truoc.
  exit /b 1
)
".venv\Scripts\python.exe" -m tiktok_zalo_bot --config config.yaml run

