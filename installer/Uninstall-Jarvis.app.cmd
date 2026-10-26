@echo off
rem Removes Jarvis completely: shortcuts, stored API key, settings, data, models, UFO and this folder.
setlocal
set "JARVIS_HOME=%~dp0"
set "JARVIS_HOME=%JARVIS_HOME:~0,-1%"
if not exist "%JARVIS_HOME%\installer\uninstall_app.ps1" goto missing
rem Run the remover from a temporary copy so this folder can be deleted.
set "TMPUN=%TEMP%\jarvis-uninstall-%RANDOM%%RANDOM%"
mkdir "%TMPUN%" >nul 2>&1
copy /y "%JARVIS_HOME%\installer\uninstall_app.ps1" "%TMPUN%\uninstall_app.ps1" >nul
copy /y "%JARVIS_HOME%\installer\common.ps1" "%TMPUN%\common.ps1" >nul
cd /d "%TMPUN%"
rem -DeferRootRemoval: PowerShell removes everything except this running .cmd; the last line deletes it.
"%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe" -NoProfile -ExecutionPolicy Bypass -File "%TMPUN%\uninstall_app.ps1" -Destination "%JARVIS_HOME%" -DeferRootRemoval %*
set "result=%ERRORLEVEL%"
cd /d "%TEMP%"
echo.
pause
rmdir /s /q "%TMPUN%" >nul 2>&1
if not "%result%"=="0" exit /b %result%
rem (goto) leaves this batch file first, so cmd.exe never reads from the file after it is deleted.
(goto) 2>nul & rmdir /s /q "%JARVIS_HOME%" & rmdir /s /q "%TMPUN%" >nul 2>&1 & cmd /d /c exit 0

:missing
echo Jarvis uninstaller files are missing in "%JARVIS_HOME%".
echo Delete this folder manually, plus the Jarvis shortcut in the Start menu.
pause
exit /b 1
