@echo off
setlocal
cd /d "%~dp0"
".venv\Scripts\python.exe" -m tiktok_zalo_bot --config config.yaml status

