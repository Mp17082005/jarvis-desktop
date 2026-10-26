@echo off
rem Jarvis Desktop launcher (installed copy). Double-click or use the Start menu shortcut.
rem Arguments are passed to the app, e.g.  Start-Jarvis.cmd --setup   or   --doctor
setlocal
set "JARVIS_HOME=%~dp0"
set "JARVIS_HOME=%JARVIS_HOME:~0,-1%"
set "APP_PY=%JARVIS_HOME%\app\.venv\Scripts\python.exe"
if not exist "%APP_PY%" goto missing_install
if "%~1"=="" if not "%JARVIS_LAUNCH_MIN%"=="1" (
    set "JARVIS_LAUNCH_MIN=1"
    start "Jarvis" /min "%ComSpec%" /d /c ""%~f0""
    exit /b 0
)
set "PYTHONPATH="
set "PYTHONHOME="
set "VIRTUAL_ENV="
set "PYTHONUTF8=1"
set "JARVIS_DESKTOP_HOME=%JARVIS_HOME%"
if exist "%JARVIS_HOME%\ufo\ufo\__main__.py" (
    set "JARVIS_UFO_HOME=%JARVIS_HOME%\ufo"
    set "JARVIS_UFO_ENABLED=true"
)
if exist "%JARVIS_HOME%\models\vosk-model-small-en-us-0.15\am\final.mdl" set "JARVIS_WAKE_MODEL_PATH=%JARVIS_HOME%\models\vosk-model-small-en-us-0.15"
cd /d "%JARVIS_HOME%\app"
"%APP_PY%" -m jarvis_desktop %*
set "result=%ERRORLEVEL%"
if "%result%"=="0" exit /b 0
echo.
echo Jarvis stopped with an error (code %result%). Read the message above.
echo To check your setup run:  "%~f0" --doctor
pause
exit /b %result%

:missing_install
echo Jarvis is not fully installed in "%JARVIS_HOME%".
echo Extract Jarvis-Setup.zip again and double-click Install-Jarvis.cmd.
pause
exit /b 1
