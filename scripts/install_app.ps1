# Jarvis Desktop installer: one-click, per-user, no administrator rights.
# Called by Install-Jarvis.cmd. Safe to re-run: upgrades in place and keeps settings,
# the stored API key (Windows Credential Manager), data and downloaded models.
[CmdletBinding()]
param(
    [string]$Destination = (Join-Path $env:LOCALAPPDATA 'Jarvis'),
    [switch]$NoUfo,
    [switch]$NoShortcuts,
    [switch]$AutoStart,
    [switch]$SkipModels,
    [switch]$NoSetup,
    [string]$UfoSource = '',
    [string]$UfoModel = ''
)
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
. (Join-Path $PSScriptRoot '..\installer\common.ps1')

$source = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..')).TrimEnd('\')
$dest = [IO.Path]::GetFullPath($Destination).TrimEnd('\')
$marker = Join-Path $dest '.jarvis-install.json'
$warnings = New-Object System.Collections.Generic.List[string]

# ---------------------------------------------------------------- 0. sanity checks
if ($dest -eq $source -or (Test-PathInside $dest $source) -or (Test-PathInside $source $dest)) {
    throw "Choose an install folder outside the extracted files ($source)."
}
if ($dest.Length -lt 4) { throw "Refusing to install into a drive root: $dest" }
foreach ($relative in @('pyproject.toml', 'uv.lock', 'jarvis\__init__.py', 'companion\ufo.py', 'installer\common.ps1', 'installer\Start-Jarvis.app.cmd', 'installer\Uninstall-Jarvis.app.cmd', 'installer\uninstall_app.ps1')) {
    if (-not (Test-Path -LiteralPath (Join-Path $source $relative) -PathType Leaf)) {
        throw "Installer file missing: $relative. Extract the entire ZIP first, then double-click Install-Jarvis.cmd."
    }
}
if ((Test-Path -LiteralPath $dest) -and -not (Test-Path -LiteralPath $marker)) {
    if (Get-ChildItem -LiteralPath $dest -Force | Where-Object { $_.Name -notin @('settings.json', 'models', 'data') }) {
        throw "$dest already contains files that were not installed by Jarvis. Choose an empty folder with -Destination."
    }
}
$upgrade = Test-Path -LiteralPath $marker
Write-Host "Jarvis Desktop installer"
Write-Host "Install folder: $dest$(if ($upgrade) { '  (upgrade: settings, key and data are kept)' })"
New-Item -ItemType Directory -Force -Path $dest | Out-Null
# Mark the folder as ours before anything is downloaded, so an interrupted or failed install
# can still be removed by the uninstaller (and rerunning resumes as an upgrade).
if (-not $upgrade) { Write-TextFile $marker (([ordered]@{ product = 'Jarvis Desktop'; status = 'installing'; started_at = (Get-Date).ToString('o') }) | ConvertTo-Json) }
# Copy (not hardlink) packages out of uv's cache: the install must not share file objects with other
# environments (a DLL loaded elsewhere would make uninstall/upgrade fail) or break if the cache is cleaned.
$env:UV_LINK_MODE = 'copy'
# Private cache: a uv on PATH may share a global cache whose lock another uv process holds,
# which can stall an install indefinitely. Bounded network waits so it can never hang forever.
$env:UV_CACHE_DIR = (Join-Path $dest 'tools\uv-cache')
$env:UV_HTTP_TIMEOUT = '120'
$env:UV_HTTP_RETRIES = '5'
foreach ($name in @('VIRTUAL_ENV', 'PYTHONPATH', 'PYTHONHOME', 'UV_PROJECT_ENVIRONMENT', 'CONDA_PREFIX')) {
    Remove-Item -LiteralPath "Env:$name" -ErrorAction SilentlyContinue
}

# ---------------------------------------------------------------- 1. uv
Write-Step 'Checking for uv (Python package manager)'
$uv = $null
$localUv = Join-Path $dest 'tools\uv\uv.exe'
$found = Get-Command uv.exe -ErrorAction SilentlyContinue | Select-Object -First 1
if ($found) { $uv = $found.Source }
elseif (Test-Path -LiteralPath $localUv) { $uv = $localUv }
else { $uv = Install-UvStandalone -ToolsDir (Join-Path $dest 'tools') }
Invoke-Native $uv @('--version') 'uv --version'

# ---------------------------------------------------------------- 2. app
Write-Step 'Installing the Jarvis app'
$app = Join-Path $dest 'app'
New-Item -ItemType Directory -Force -Path $app | Out-Null
foreach ($dir in $script:JarvisSourceDirs.Keys) {
    Remove-Item -LiteralPath (Join-Path $app $dir) -Recurse -Force -ErrorAction SilentlyContinue   # drop stale files on upgrade
}
$copied = 0
foreach ($relative in (Get-JarvisSourceFiles -Root $source)) {
    $to = Join-Path $app $relative
    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $to) | Out-Null
    Copy-Item -LiteralPath (Join-Path $source $relative) -Destination $to -Force
    $copied++
}
Write-Host "Copied $copied source files (no .env, keys, tests or runtime data)."
$pyproject = Get-Content -LiteralPath (Join-Path $app 'pyproject.toml') -Raw
$extras = @('desktop', 'voice')
if ($pyproject -match '(?m)^\s*app\s*=\s*\[') { $extras += 'app' }
else { $warnings.Add("pyproject.toml has no 'app' extra yet; installed without it.") }
$extraArgs = @(); foreach ($e in $extras) { $extraArgs += @('--extra', $e) }
Push-Location $app
try {
    try {
        Invoke-Native $uv (@('sync', '--frozen') + $extraArgs + @('--python', '3.12')) 'uv sync --frozen'
        Write-Host "Dependencies installed from the lock file (extras: $($extras -join ', '))."
    } catch {
        $warnings.Add('uv.lock did not match pyproject.toml; dependencies were re-resolved (uv sync without --frozen).')
        Write-Host 'Lock file is out of date for these extras; re-resolving dependencies...'
        Invoke-Native $uv (@('sync') + $extraArgs + @('--python', '3.12')) 'uv sync'
    }
} finally { Pop-Location }
$appPython = Join-Path $app '.venv\Scripts\python.exe'
Push-Location $app
try { Invoke-Native $appPython @('-c', 'import jarvis, companion') 'App import check' } finally { Pop-Location }

# ---------------------------------------------------------------- 3. Microsoft UFO
$ufo = Join-Path $dest 'ufo'
if (-not $NoUfo) {
    Write-Step 'Installing Microsoft UFO (desktop automation)'
    $manifest = Get-Content -LiteralPath (Join-Path $source 'patches\ufo-manifest.json') -Raw | ConvertFrom-Json
    $commit = [string]$manifest.commit
    if ($commit -notmatch '^[0-9a-f]{40}$') { throw 'patches\ufo-manifest.json has no valid pinned commit.' }
    $ufoMarker = Join-Path $ufo '.jarvis-ufo-commit'
    $haveCommit = (Test-Path -LiteralPath $ufoMarker) -and ((Get-Content -LiteralPath $ufoMarker -Raw).Trim() -eq $commit) -and (Test-Path -LiteralPath (Join-Path $ufo 'ufo\__main__.py'))
    if (-not $haveCommit) {
        $keepVenv = Join-Path $dest '.ufo-venv-keep'
        if (Test-Path -LiteralPath (Join-Path $ufo '.venv')) { Move-Item -LiteralPath (Join-Path $ufo '.venv') -Destination $keepVenv -Force }
        Remove-Item -LiteralPath $ufo -Recurse -Force -ErrorAction SilentlyContinue
        $git = Get-Command git.exe -ErrorAction SilentlyContinue | Select-Object -First 1
        $fetched = $false
        if ($UfoSource) {
            $ufoSrc = [IO.Path]::GetFullPath($UfoSource)
            if ($git -and (Test-Path -LiteralPath (Join-Path $ufoSrc '.git'))) {
                Write-Host "Cloning UFO $commit from local checkout $ufoSrc"
                Invoke-Native $git.Source @('-c', 'advice.detachedHead=false', 'clone', '--quiet', '--no-checkout', '--no-hardlinks', $ufoSrc, $ufo) 'git clone (local UFO)'
                Invoke-Native $git.Source @('-C', $ufo, '-c', 'advice.detachedHead=false', 'checkout', '--quiet', $commit) 'git checkout'
                Invoke-Native $git.Source @('-C', $ufo, 'remote', 'set-url', 'origin', $manifest.repository) 'git remote set-url'
            } else {
                Write-Host "Copying UFO from $ufoSrc (no git: copying files as they are)"
                $null = robocopy $ufoSrc $ufo /E /NFL /NDL /NJH /NJS /NP /XD .git .venv logs __pycache__ /XF agents.yaml
                if ($LASTEXITCODE -ge 8) { throw "Copying UFO failed (robocopy $LASTEXITCODE)." }
            }
            $fetched = $true
        } elseif ($git) {
            Write-Host "Downloading UFO $commit with git"
            try {
                New-Item -ItemType Directory -Force -Path $ufo | Out-Null
                Invoke-Native $git.Source @('-C', $ufo, 'init', '--quiet') 'git init'
                Invoke-Native $git.Source @('-C', $ufo, 'remote', 'add', 'origin', $manifest.repository) 'git remote add'
                Invoke-Native $git.Source @('-C', $ufo, 'fetch', '--quiet', '--depth', '1', 'origin', $commit) 'git fetch'
                Invoke-Native $git.Source @('-C', $ufo, '-c', 'advice.detachedHead=false', 'checkout', '--quiet', 'FETCH_HEAD') 'git checkout'
                $fetched = $true
            } catch {
                Write-Host "git download failed ($($_.Exception.Message)); trying the ZIP download."
                Remove-Item -LiteralPath $ufo -Recurse -Force -ErrorAction SilentlyContinue
            }
        }
        if (-not $fetched) {
            $zipUrl = "https://codeload.github.com/microsoft/UFO/zip/$commit"
            $zipFile = Join-Path $dest "download\ufo-$commit.zip"
            Write-Host "Downloading $zipUrl"
            Invoke-Download -Url $zipUrl -OutFile $zipFile
            $staging = Join-Path $dest 'download\ufo-extract'
            Remove-Item -LiteralPath $staging -Recurse -Force -ErrorAction SilentlyContinue
            Expand-ZipSafely -ZipPath $zipFile -Destination $staging
            $top = Get-ChildItem -LiteralPath $staging -Directory | Select-Object -First 1
            if (-not $top -or -not (Test-Path -LiteralPath (Join-Path $top.FullName 'ufo\__main__.py'))) { throw 'Downloaded UFO archive has an unexpected layout.' }
            Move-Item -LiteralPath $top.FullName -Destination $ufo
            Remove-Item -LiteralPath (Join-Path $dest 'download') -Recurse -Force -ErrorAction SilentlyContinue
        }
        if (Test-Path -LiteralPath $keepVenv) { Move-Item -LiteralPath $keepVenv -Destination (Join-Path $ufo '.venv') -Force }
        Write-TextFile $ufoMarker $commit -Ascii
    } else { Write-Host "UFO $commit already present." }

    # Jarvis patches: verified full-file replacements (works without git); .patch files are for review.
    foreach ($prop in $manifest.files.PSObject.Properties) {
        $rel = $prop.Name; $info = $prop.Value
        $target = Join-Path $ufo ($rel -replace '/', '\')
        $patched = Join-Path $source ('patches\ufo-files\' + ($rel -replace '/', '\'))
        $bytes = [IO.File]::ReadAllBytes($target)
        $normalized = [Text.Encoding]::UTF8.GetString($bytes).Replace("`r`n", "`n")
        $sha = [BitConverter]::ToString([Security.Cryptography.SHA256]::Create().ComputeHash([Text.Encoding]::UTF8.GetBytes($normalized))).Replace('-', '').ToLowerInvariant()
        if ($sha -eq $info.patched_sha256) { Write-Host "Patch already applied: $rel"; continue }
        if ($sha -ne $info.base_sha256) { throw "UFO file $rel differs from pinned commit $commit; refusing to overwrite it." }
        if ((Get-Sha256 $patched) -ne $info.patched_sha256) { throw "Bundled patched file is corrupt: $rel" }
        Copy-Item -LiteralPath $patched -Destination $target -Force
        Write-Host "Patched: $rel"
    }

    # Requirements with the one unavailable pin relaxed (pandas 1.4.3 has no wheel for Python 3.11).
    $reqIn = Get-Content -LiteralPath (Join-Path $ufo 'requirements.txt')
    $reqOut = foreach ($line in $reqIn) { if ($line -match '^\s*pandas==1\.4\.3\s*$') { 'pandas>=2.0,<2.3' } else { $line } }
    $reqFile = Join-Path $ufo 'requirements.jarvis.txt'
    Write-TextFile $reqFile (($reqOut -join "`r`n") + "`r`n")
    $ufoPython = Join-Path $ufo '.venv\Scripts\python.exe'
    if (-not (Test-Path -LiteralPath $ufoPython)) {
        Invoke-Native $uv @('venv', '--no-project', '--python', '3.11', (Join-Path $ufo '.venv')) 'uv venv (UFO, Python 3.11)'
    }
    Write-Host 'Installing UFO dependencies (first install downloads about 1 GB)...'
    Invoke-Native $uv @('pip', 'install', '--python', $ufoPython, '-r', $reqFile) 'uv pip install (UFO requirements)'

    # agents.yaml: every agent talks to Jarvis's loopback proxy; no provider key is stored in UFO.
    if (-not $UfoModel) {
        $UfoModel = 'gpt-4o'
        $settingsFile = Join-Path $dest 'settings.json'
        if (Test-Path -LiteralPath $settingsFile) {
            try { $s = Get-Content -LiteralPath $settingsFile -Raw | ConvertFrom-Json; if ($s.PSObject.Properties['ufo_model'] -and $s.ufo_model) { $UfoModel = [string]$s.ufo_model } } catch { }
        }
    }
    if ($UfoModel -notmatch '^[A-Za-z0-9._:/@-]{1,100}$') { throw "Invalid UFO model name." }
    $values = @{ API_TYPE = 'openai'; API_BASE = $script:JarvisProxyBase; API_KEY = $script:JarvisProxyKey; API_MODEL = $UfoModel }
    $agents = @('HOST_AGENT', 'APP_AGENT', 'BACKUP_AGENT', 'EVALUATION_AGENT')
    $seen = @{}; $section = ''
    $out = foreach ($line in (Get-Content -LiteralPath (Join-Path $ufo 'config\ufo\agents.yaml.template'))) {
        if ($line -match '^([A-Za-z_][A-Za-z0-9_]*):') { $section = $Matches[1] }
        if (($agents -contains $section) -and $line -match '^(\s+)(API_TYPE|API_BASE|API_KEY|API_MODEL):') {
            $seen["$section.$($Matches[2])"] = $true
            "$($Matches[1])$($Matches[2]): `"$($values[$Matches[2]])`"  # set by Jarvis installer"
        } else { $line }
    }
    foreach ($a in $agents) { foreach ($k in $values.Keys) { if (-not $seen["$a.$k"]) { throw "agents.yaml template lacks $a.$k" } } }
    Write-TextFile (Join-Path $ufo 'config\ufo\agents.yaml') (($out -join "`r`n") + "`r`n")
    Write-Host "UFO agents use the Jarvis proxy $($script:JarvisProxyBase) (model $UfoModel)."
    Push-Location $ufo
    try { Invoke-Native $ufoPython @('-E', '-X', 'utf8', '-c', 'import ufo') 'UFO import check' } finally { Pop-Location }
} else { Write-Host ''; Write-Host 'Skipping Microsoft UFO (-NoUfo): general desktop tasks will be unavailable.' }

# ---------------------------------------------------------------- 4. wake-word model
$models = Join-Path $dest 'models'
$voskDir = Join-Path $models $script:JarvisVoskName
if (-not $SkipModels) {
    Write-Step 'Installing the offline wake-word model'
    if (Test-Path -LiteralPath (Join-Path $voskDir 'am\final.mdl')) { Write-Host 'Wake model already present.' }
    else {
        $zipFile = Join-Path $models "download\$($script:JarvisVoskName).zip"
        Invoke-Download -Url $script:JarvisVoskUrl -OutFile $zipFile
        $size = (Get-Item -LiteralPath $zipFile).Length
        if ($size -ne $script:JarvisVoskSize) { Remove-Item -LiteralPath $zipFile -Force; throw "Wake model download has size $size, expected $($script:JarvisVoskSize); refusing it." }
        Expand-ZipSafely -ZipPath $zipFile -Destination $models
        Remove-Item -LiteralPath (Join-Path $models 'download') -Recurse -Force
        if (-not (Test-Path -LiteralPath (Join-Path $voskDir 'am\final.mdl'))) { throw 'Wake model archive did not contain the expected files.' }
        Write-Host "Wake model installed: $voskDir"
    }
} else { Write-Host ''; Write-Host 'Skipping wake model download (-SkipModels).' }

# ---------------------------------------------------------------- 5/6. launchers, uninstaller, shortcuts
Write-Step 'Creating Start and Uninstall files'
Copy-Item -LiteralPath (Join-Path $source 'installer\Start-Jarvis.app.cmd') -Destination (Join-Path $dest 'Start-Jarvis.cmd') -Force
Copy-Item -LiteralPath (Join-Path $source 'installer\Uninstall-Jarvis.app.cmd') -Destination (Join-Path $dest 'Uninstall-Jarvis.cmd') -Force
New-Item -ItemType Directory -Force -Path (Join-Path $dest 'installer') | Out-Null
foreach ($f in @('common.ps1', 'uninstall_app.ps1')) { Copy-Item -LiteralPath (Join-Path $source "installer\$f") -Destination (Join-Path $dest "installer\$f") -Force }
$info = [ordered]@{
    product = 'Jarvis Desktop'; status = 'installed'; installed_at = (Get-Date).ToString('o'); source = $source
    ufo = (-not $NoUfo) -or (Test-Path -LiteralPath (Join-Path $ufo 'ufo\__main__.py'))
    wake_model = (Test-Path -LiteralPath (Join-Path $voskDir 'am\final.mdl')); extras = $extras
}
Write-TextFile $marker ($info | ConvertTo-Json)
if (-not $NoShortcuts) {
    $shell = New-Object -ComObject WScript.Shell
    $folders = @([Environment]::GetFolderPath('Programs'))
    if ($AutoStart) { $folders += [Environment]::GetFolderPath('Startup') }
    foreach ($folder in $folders) {
        $path = Join-Path $folder 'Jarvis.lnk'
        $lnk = $shell.CreateShortcut($path)
        $lnk.TargetPath = Join-Path $dest 'Start-Jarvis.cmd'
        $lnk.WorkingDirectory = $dest
        $lnk.WindowStyle = 7
        $lnk.Description = 'Jarvis voice assistant'
        $lnk.Save()
        if (-not (Test-Path -LiteralPath $path)) { throw "Could not create shortcut $path" }
        Write-Host "Shortcut: $path"
    }
} else { Write-Host 'No shortcuts created (-NoShortcuts).' }

# ---------------------------------------------------------------- 7. first-run setup
$settingsPath = Join-Path $dest 'settings.json'
if (Test-Path -LiteralPath $settingsPath) { Write-Host ''; Write-Host 'Existing settings kept. Run Start-Jarvis.cmd --setup to change them.' }
elseif ($NoSetup) { Write-Host ''; Write-Host 'First-run setup skipped (-NoSetup). Run Start-Jarvis.cmd --setup later.' }
elseif (-not (Test-Path -LiteralPath (Join-Path $app 'jarvis_desktop\__main__.py'))) {
    $warnings.Add('This build has no jarvis_desktop app yet; first-run setup was skipped.')
} else {
    Write-Step 'First-run setup: enter your AI provider and API key in the window that opens'
    $env:JARVIS_DESKTOP_HOME = $dest
    $env:PYTHONPATH = ''
    Push-Location $app
    try { Invoke-Native $appPython @('-m', 'jarvis_desktop', '--setup') 'First-run setup' } finally { Pop-Location }
}

Write-Host ''
foreach ($w in $warnings) { Write-Host "WARNING: $w" }
Write-Host "Jarvis is installed in $dest"
Write-Host "Start it from the Start menu (Jarvis) or double-click $(Join-Path $dest 'Start-Jarvis.cmd')."
Write-Host 'Then say "Jarvis" and talk.'
