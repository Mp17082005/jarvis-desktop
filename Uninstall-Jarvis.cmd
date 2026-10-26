@echo off
rem Removes Jarvis (default install folder %LOCALAPPDATA%\Jarvis), its shortcuts and saved API key.
setlocal
set "TARGET=%LOCALAPPDATA%\Jarvis"
if exist "%TARGET%\Uninstall-Jarvis.cmd" (
    call "%TARGET%\Uninstall-Jarvis.cmd" %*
    exit /b %ERRORLEVEL%
)
cd /d "%~dp0"
if not exist "%~dp0installer\uninstall_app.ps1" goto missing
"%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe" -NoProfile -ExecutionPolicy Bypass -File "%~dp0installer\uninstall_app.ps1" %*
set "result=%ERRORLEVEL%"
pause
exit /b %result%

:missing
echo Jarvis does not appear to be installed in "%TARGET%".
pause
exit /b 1
