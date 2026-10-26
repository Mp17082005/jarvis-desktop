@echo off
rem Jarvis Desktop one-click installer. Double-click; no administrator rights needed.
rem Optional switches: -Destination <folder> -NoUfo -NoShortcuts -SkipModels -NoSetup (see docs\INSTALL.md)
setlocal
cd /d "%~dp0"
if errorlevel 1 goto source_missing
if not exist "%~dp0scripts\install_app.ps1" goto source_missing
if not exist "%~dp0installer\common.ps1" goto source_missing
echo Installing Jarvis. This takes 5-15 minutes on first install; keep this window open.
"%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe" -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\install_app.ps1" %*
set "result=%ERRORLEVEL%"
if not "%result%"=="0" goto install_failed
echo.
echo Jarvis installation finished. Start it from the Start menu: Jarvis
pause
exit /b 0

:install_failed
echo.
echo Jarvis installation did not finish (code %result%). Read the error above.
echo You can safely double-click Install-Jarvis.cmd again; it continues where it stopped.
echo Help: docs\INSTALL.md (Troubleshooting).
pause
exit /b %result%

:source_missing
echo Jarvis installation files are missing. Extract the entire ZIP first.
echo Open the extracted folder and double-click Install-Jarvis.cmd there.
pause
exit /b 1
