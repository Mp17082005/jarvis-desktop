@echo off
cd /d "%~dp0"
if not exist "tools\cloudflared.exe" (
  echo Install cloudflared from https://developers.cloudflare.com/tunnel/downloads/
  exit /b 1
)
"tools\cloudflared.exe" tunnel --url http://127.0.0.1:8765 --protocol http2
