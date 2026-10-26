@echo off
cd /d "%~dp0"
set "PYTHONPATH="
if not exist ".venv\Scripts\python.exe" (
  echo Run: uv sync --extra voice --extra dev
  exit /b 1
)
".venv\Scripts\python.exe" -m uvicorn jarvis.main:app --host 127.0.0.1 --port 8765 --proxy-headers --forwarded-allow-ips 127.0.0.1
