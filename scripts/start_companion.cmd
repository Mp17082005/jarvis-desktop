@echo off
setlocal
set "PYTHONPATH="
set "PYTHONHOME="
cd /d "%~dp0.."
if not exist ".venv\Scripts\python.exe" (
  echo Companion Python environment is missing. Run scripts\install_companion.ps1 first.
  exit /b 1
)
rem Optional Microsoft UFO: start its local LLM proxy if UFO is configured beside this install.
if defined JARVIS_UFO_PROXY_CMD if exist "%JARVIS_UFO_PROXY_CMD%" (
  powershell.exe -NoProfile -Command "try { Invoke-WebRequest -UseBasicParsing -TimeoutSec 2 http://127.0.0.1:8780/health ^| Out-Null } catch { Start-Process -WindowStyle Hidden -FilePath $env:JARVIS_UFO_PROXY_CMD }"
)
".venv\Scripts\python.exe" -m companion.launcher %*
exit /b %ERRORLEVEL%
