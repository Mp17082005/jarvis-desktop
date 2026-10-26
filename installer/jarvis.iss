; Jarvis Desktop — Windows setup executable (Inno Setup 6/7).
; Build:  iscc /DAppVersion=0.1.0 /DSourceDir=<staged release folder> installer\jarvis.iss
; The .exe unpacks the small app source, then runs the same verified installer script
; (scripts\install_app.ps1) that downloads Python packages, Microsoft UFO and the wake model.

#ifndef AppVersion
  #define AppVersion "0.1.0"
#endif
#ifndef SourceDir
  #define SourceDir "..\dist\Jarvis-Setup"
#endif

[Setup]
AppId={{6C3F7A51-2D7E-4B0A-9E44-4A8F0B5D7E01}
AppName=Jarvis
AppVersion={#AppVersion}
AppVerName=Jarvis {#AppVersion}
AppPublisher=Jarvis Desktop
AppPublisherURL=https://github.com/Mp17082005/jarvis-desktop
AppSupportURL=https://github.com/Mp17082005/jarvis-desktop/issues
DefaultDirName={localappdata}\Jarvis
DisableDirPage=yes
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
OutputBaseFilename=Jarvis-Setup-{#AppVersion}
OutputDir=..\dist
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0
Uninstallable=yes
UninstallDisplayName=Jarvis
CreateUninstallRegKey=yes
; The real uninstall is done by the app's own uninstaller (it also removes the saved API key).
UninstallFilesDir={localappdata}\Jarvis-Setup\uninst

[Tasks]
Name: "autostart"; Description: "Start Jarvis when I sign in to Windows"; Flags: unchecked

[Files]
; Unpack the release source to a staging folder; install_app.ps1 copies what it needs into {app}.
Source: "{#SourceDir}\*"; DestDir: "{localappdata}\Jarvis-Setup\src"; Flags: recursesubdirs createallsubdirs ignoreversion

[Run]
; In silent mode (/VERYSILENT, used for automated installs) skip the interactive setup window;
; the user is asked for the key on first launch instead.
Filename: "{sys}\WindowsPowerShell\v1.0\powershell.exe"; \
  Parameters: "-NoProfile -ExecutionPolicy Bypass -File ""{localappdata}\Jarvis-Setup\src\scripts\install_app.ps1"" -Destination ""{app}"" {code:ExtraFlags}"; \
  StatusMsg: "Downloading and installing Jarvis (Python packages, Microsoft UFO, wake word). This takes 5-15 minutes..."; \
  Flags: waituntilterminated
Filename: "{app}\Start-Jarvis.cmd"; Description: "Start Jarvis now"; Flags: postinstall nowait skipifsilent shellexec unchecked

[UninstallRun]
; Run the uninstaller from the setup's own copy (outside {app}) so it can delete {app} entirely.
Filename: "{sys}\WindowsPowerShell\v1.0\powershell.exe"; \
  Parameters: "-NoProfile -ExecutionPolicy Bypass -File ""{localappdata}\Jarvis-Setup\src\installer\uninstall_app.ps1"" -Destination ""{app}"" -Yes"; \
  RunOnceId: "JarvisUninstall"; Flags: waituntilterminated runhidden

[UninstallDelete]
Type: filesandordirs; Name: "{localappdata}\Jarvis-Setup"

[Code]
function ExtraFlags(Param: String): String;
begin
  Result := '';
  if WizardIsTaskSelected('autostart') then Result := Result + ' -AutoStart';
  if WizardSilent then Result := Result + ' -NoSetup';
end;

function LoadMarker(Path: String): String;
var S: AnsiString;
begin
  Result := '';
  if LoadStringFromFile(Path, S) then Result := String(S);
end;

procedure CurStepChanged(CurStep: TSetupStep);
var ResultCode: Integer;
begin
  if CurStep = ssPostInstall then
  begin
    // install_app.ps1 marks the folder 'installed' only when every step finished successfully.
    if (not FileExists(ExpandConstant('{app}\.jarvis-install.json'))) or
       (Pos('"installed"', LoadMarker(ExpandConstant('{app}\.jarvis-install.json'))) = 0) then
      MsgBox('Jarvis did not finish installing (a download or package step failed).' + #13#10 +
             'Check your internet connection and run Jarvis-Setup again; it continues where it stopped.',
             mbError, MB_OK);
  end;
end;
