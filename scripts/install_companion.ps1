# Install from reviewed source. No administrator rights or boot changes required.
[CmdletBinding()]
param(
    [string]$Destination = (Join-Path $env:LOCALAPPDATA 'JarvisCompanion'),
    [switch]$SourceOnly,
    [switch]$StartMenu,
    [switch]$AutoStart
)
$ErrorActionPreference = 'Stop'
$source = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$target = [IO.Path]::GetFullPath($Destination)
if ($source.TrimEnd('\') -eq $target.TrimEnd('\')) { throw 'Choose a destination different from the source checkout.' }
if (($SourceOnly) -and ($StartMenu -or $AutoStart)) { throw 'SourceOnly does not create shortcuts.' }
$required = @('pyproject.toml', 'uv.lock', 'jarvis_tools.py', '.env.example', 'companion\launcher.py', 'scripts\start_companion.cmd')
foreach ($relative in $required) {
    if (-not (Test-Path -LiteralPath (Join-Path $source $relative) -PathType Leaf)) { throw "Missing source file: $relative" }
}
if (-not $SourceOnly) {
    $uv = Get-Command uv -ErrorAction SilentlyContinue
    if (-not $uv) {
        Write-Host 'Jarvis needs uv to install its Python dependencies.'
        Write-Host 'Open the official installation page in your browser:'
        Write-Host 'https://docs.astral.sh/uv/getting-started/installation/'
        Write-Host 'After installing uv, close this window and double-click Install-Jarvis.cmd again.'
        Write-Host 'You can use Jarvis in your browser instead, without local desktop control.'
        throw 'uv was not found. No remote installer was executed and no files were installed.'
    }
}
$files = @('Install-Jarvis.cmd', 'Start-Jarvis.cmd', 'README-FIRST.txt', 'pyproject.toml', 'uv.lock', 'jarvis_tools.py', '.env.example', 'scripts\install_companion.ps1', 'scripts\start_companion.cmd', 'docs\DESKTOP-COMPANION.md', 'docs\DEVICE-PAIRING.md')
foreach ($folder in @('jarvis', 'companion', 'static')) {
    $extensions = if ($folder -eq 'static') { @('.html','.js','.css','.svg','.png','.ico','.woff2') } else { @('.py') }
    foreach ($file in Get-ChildItem -LiteralPath (Join-Path $source $folder) -Recurse -File) {
        $relative = $file.FullName.Substring($source.Length + 1)
        if (($extensions -contains $file.Extension.ToLower()) -and ($relative -notmatch '(^|[\\/])(\.[^\\/]+|__pycache__)([\\/]|$)') -and (-not ($file.Attributes -band [IO.FileAttributes]::ReparsePoint))) {
            $files += $relative
        }
    }
}
New-Item -ItemType Directory -Force -Path $target | Out-Null
foreach ($relative in ($files | Sort-Object -Unique)) {
    $from = Join-Path $source $relative
    if (Test-Path -LiteralPath $from -PathType Leaf) {
        $to = Join-Path $target $relative
        New-Item -ItemType Directory -Force -Path (Split-Path -Parent $to) | Out-Null
        Copy-Item -LiteralPath $from -Destination $to -Force
    }
}
Write-Host "Source installed: $target"
Write-Host 'No .env, ACCESS.txt, models, browser sessions, tests or runtime data were copied.'
if ($SourceOnly) { Write-Host 'SourceOnly: dependencies not installed. No shortcuts created.'; exit 0 }
$env:PYTHONPATH = ''
$env:PYTHONHOME = ''
Push-Location $target
try {
    & $uv.Source sync --frozen --extra desktop --python 3.12
    if ($LASTEXITCODE -ne 0) { throw 'Dependency install failed. Fix the reported uv error, then rerun.' }
} finally { Pop-Location }
if ($StartMenu -or $AutoStart) {
    $shell = New-Object -ComObject WScript.Shell
    $folders = @()
    if ($StartMenu) { $folders += [Environment]::GetFolderPath('Programs') }
    if ($AutoStart) { $folders += [Environment]::GetFolderPath('Startup') }
    foreach ($folder in $folders) {
        $shortcut = $shell.CreateShortcut((Join-Path $folder 'Jarvis Companion.lnk'))
        $shortcut.TargetPath = Join-Path $target 'scripts\start_companion.cmd'
        $shortcut.WorkingDirectory = $target
        $shortcut.Description = 'Jarvis local Windows companion (microphone remains opt-in)'
        $shortcut.Save()
        if (-not (Test-Path -LiteralPath (Join-Path $folder 'Jarvis Companion.lnk'))) { throw 'Shortcut verification failed.' }
    }
} else { Write-Host 'No shortcuts created. Use -StartMenu and/or -AutoStart explicitly if wanted.' }
Write-Host 'No automatic shortcuts are needed; use the double-click files in the installation folder.'
Write-Host "Open this folder in File Explorer: $target"
Write-Host 'Read README-FIRST.txt. Copy .env.example to .env and configure your own settings.'
Write-Host 'Keep an existing .env unchanged unless you want to edit your own configuration.'
Write-Host "Double-click Start-Jarvis.cmd in: $target"
Write-Host 'Keep the companion console open while using local Jarvis.'
