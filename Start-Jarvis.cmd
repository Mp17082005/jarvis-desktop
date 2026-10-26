@echo off
rem Starts the installed Jarvis Desktop app (installed by Install-Jarvis.cmd into %LOCALAPPDATA%\Jarvis).
rem Falls back to the older browser companion (JarvisCompanion) when the desktop app is not installed.
setlocal
set "TARGET=%LOCALAPPDATA%\Jarvis"
if exist "%TARGET%\Start-Jarvis.cmd" if exist "%TARGET%\app\.venv\Scripts\python.exe" (
    call "%TARGET%\Start-Jarvis.cmd" %*
    exit /b %ERRORLEVEL%
)
set "root=%~dp0"
if exist "%LOCALAPPDATA%\JarvisCompanion\scripts\start_companion.cmd" set "root=%LOCALAPPDATA%\JarvisCompanion\"
cd /d "%root%"
if errorlevel 1 goto missing_install
if not exist "%root%scripts\start_companion.cmd" goto missing_install
if not exist "%root%.env" goto missing_config
if not exist "%root%.venv\Scripts\python.exe" goto missing_install
call "%root%scripts\start_companion.cmd" %*
set "result=%ERRORLEVEL%"
if "%result%"=="0" goto finished
echo.
echo Jarvis could not start or stopped with an error. Read the message above.
:finished
pause
exit /b %result%

:missing_config
echo Jarvis browser companion needs its local configuration: "%root%.env"
echo Copy .env.example in that folder to .env and fill in your own settings.
echo Or install the voice app instead: double-click Install-Jarvis.cmd.
pause
exit /b 1

:missing_install
echo Jarvis is not installed yet.
echo Double-click Install-Jarvis.cmd in this folder first, then use the Start menu entry "Jarvis".
echo Extract the entire ZIP first if you have not. See README-FIRST.txt.
pause
exit /b 1
