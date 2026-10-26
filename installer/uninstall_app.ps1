# Jarvis Desktop uninstaller: removes shortcuts, the stored API key, and the whole install folder.
[CmdletBinding()]
param(
    [string]$Destination = (Join-Path $env:LOCALAPPDATA 'Jarvis'),
    [switch]$Yes,
    [switch]$KeepShortcuts,
    [switch]$KeepCredential,   # tests/scratch installs: the credential is per Windows user, not per folder
    [switch]$DeferRootRemoval  # used by Uninstall-Jarvis.cmd, which lives in the folder and deletes it last
)
$ErrorActionPreference = 'Stop'
$dest = [IO.Path]::GetFullPath($Destination).TrimEnd('\')
$marker = Join-Path $dest '.jarvis-install.json'
if (-not (Test-Path -LiteralPath $marker)) {
    Write-Host "No Jarvis installation found in $dest (missing .jarvis-install.json). Nothing removed."
    exit 1
}
Write-Host "This removes Jarvis completely from $dest :"
Write-Host ' - Start menu and startup shortcuts'
Write-Host ' - your saved API key in Windows Credential Manager'
Write-Host ' - settings, conversation data, models, Microsoft UFO and the app itself'
if (-not $Yes) {
    $answer = Read-Host 'Remove Jarvis now? (Y/N)'
    if ($answer -notmatch '^(y|yes)$') { Write-Host 'Cancelled. Nothing was removed.'; exit 2 }
}
# Stop running Jarvis processes that live inside the install folder (and only those).
Get-Process -ErrorAction SilentlyContinue | Where-Object {
    try { $_.Path -and $_.Path.StartsWith($dest + '\', [StringComparison]::OrdinalIgnoreCase) } catch { $false }
} | ForEach-Object { Write-Host "Stopping $($_.ProcessName) ($($_.Id))"; Stop-Process -Id $_.Id -Force -ErrorAction SilentlyContinue }
Start-Sleep -Milliseconds 500

if (-not $KeepShortcuts) {
    $target = Join-Path $dest 'Start-Jarvis.cmd'
    $shell = New-Object -ComObject WScript.Shell
    foreach ($folder in @([Environment]::GetFolderPath('Programs'), [Environment]::GetFolderPath('Startup'))) {
        $path = Join-Path $folder 'Jarvis.lnk'
        if (Test-Path -LiteralPath $path) {
            if ($shell.CreateShortcut($path).TargetPath -ieq $target) { Remove-Item -LiteralPath $path -Force; Write-Host "Removed shortcut $path" }
            else { Write-Host "Left $path alone (it points somewhere else)." }
        }
    }
}

# Delete the credential with the app's own Python before its venv disappears.
$py = Join-Path $dest 'app\.venv\Scripts\python.exe'
if ($KeepCredential) { Write-Host 'Saved API key left in Windows Credential Manager (-KeepCredential).' }
elseif (Test-Path -LiteralPath $py) {
    $code = "import keyring`ntry:`n    keyring.delete_password('Jarvis', 'provider_api_key'); print('Removed saved API key.')`nexcept Exception:`n    print('No saved API key found.')"
    $env:PYTHONPATH = ''
    & $py -E -c $code
} else { Write-Host 'App Python missing; remove "Jarvis" from Windows Credential Manager manually if present.' }

# Antivirus can hold freshly written .pyd files for a few seconds; retry for up to ~60 s.
$keep = if ($DeferRootRemoval) { @('Uninstall-Jarvis.cmd') } else { @() }
function Get-Remaining { if (-not (Test-Path -LiteralPath $dest)) { return @() }; @(Get-ChildItem -LiteralPath $dest -Force | Where-Object { $keep -notcontains $_.Name }) }
for ($i = 0; $i -lt 30; $i++) {
    if ($DeferRootRemoval) { Get-Remaining | ForEach-Object { Remove-Item -LiteralPath $_.FullName -Recurse -Force -ErrorAction SilentlyContinue } }
    else { Remove-Item -LiteralPath $dest -Recurse -Force -ErrorAction SilentlyContinue }
    if ((Get-Remaining).Count -eq 0 -and ($DeferRootRemoval -or -not (Test-Path -LiteralPath $dest))) { break }
    if ($i -eq 0) { Write-Host 'Waiting for files in use to be released...' }
    Start-Sleep -Seconds 2
}
if ((Get-Remaining).Count -gt 0 -or (-not $DeferRootRemoval -and (Test-Path -LiteralPath $dest))) {
    Write-Host "Some files in $dest are in use. Close Jarvis and delete the folder manually."; exit 1
}
Write-Host 'Jarvis has been removed.'
exit 0
