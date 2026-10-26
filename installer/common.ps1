# Shared helpers for the Jarvis Windows installer (dot-sourced; defines functions only).
# No remote script is ever executed. Downloads are verified (SHA-256 or exact size) and
# ZIP archives are validated entry-by-entry before anything is written to disk.

Set-StrictMode -Version 2.0

$script:JarvisUvZipUrl = 'https://github.com/astral-sh/uv/releases/latest/download/uv-x86_64-pc-windows-msvc.zip'
$script:JarvisVoskUrl = 'https://alphacephei.com/vosk/models/vosk-model-small-en-us-0.15.zip'
$script:JarvisVoskName = 'vosk-model-small-en-us-0.15'
$script:JarvisVoskSize = 41205931
$script:JarvisProxyBase = 'http://127.0.0.1:8780/v1'
$script:JarvisProxyKey = 'local-proxy'

# Files/folders copied into <Destination>\app and into the release ZIP (positive allowlist).
$script:JarvisRootFiles = @('pyproject.toml', 'uv.lock', 'jarvis_tools.py')
$script:JarvisSourceDirs = [ordered]@{
    'jarvis'         = @('.py')
    'companion'      = @('.py')
    'jarvis_desktop' = @('.py', '.json', '.txt', '.wav', '.png', '.ico')
    'scripts'        = @('.ps1', '.cmd', '.py')
    'docs'           = @('.md')
    'static'         = @('.html', '.js', '.css', '.svg', '.png', '.ico', '.woff2')
}
# Never copied or packaged, even if a future allowlist entry would match.
$script:JarvisForbiddenNames = @('.env', 'ACCESS.txt', 'settings.json', 'agents.yaml')

function Write-Step([string]$Text) { Write-Host ''; Write-Host "==> $Text" }

function Invoke-Native {
    # Run a native program; stderr progress must not become a terminating PowerShell error.
    param([Parameter(Mandatory)][string]$FilePath, [string[]]$Arguments = @(), [string]$What = $FilePath)
    $previous = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try { & $FilePath @Arguments; $code = $LASTEXITCODE } finally { $ErrorActionPreference = $previous }
    if ($code -ne 0) { throw "$What failed (exit code $code)." }
}

function Invoke-Download {
    param([Parameter(Mandatory)][string]$Url, [Parameter(Mandatory)][string]$OutFile)
    [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $OutFile) | Out-Null
    $client = New-Object Net.WebClient
    $client.Headers.Add('User-Agent', 'Jarvis-Installer')
    try { $client.DownloadFile($Url, $OutFile) } finally { $client.Dispose() }
}

function Get-Sha256([string]$Path) { (Get-FileHash -Algorithm SHA256 -LiteralPath $Path).Hash.ToLowerInvariant() }

function Test-PathInside([string]$Child, [string]$Parent) {
    $p = [IO.Path]::GetFullPath($Parent).TrimEnd('\') + '\'
    $c = [IO.Path]::GetFullPath($Child)
    return $c.StartsWith($p, [StringComparison]::OrdinalIgnoreCase)
}

function Assert-ZipSafe {
    # Reject absolute paths, drive letters, '..' segments and device names before extraction.
    param([Parameter(Mandatory)][string]$ZipPath)
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    $zip = [IO.Compression.ZipFile]::OpenRead($ZipPath)
    try {
        if ($zip.Entries.Count -eq 0) { throw "Archive is empty: $ZipPath" }
        foreach ($entry in $zip.Entries) {
            $name = $entry.FullName
            $parts = $name -split '[\\/]'
            if ($name.StartsWith('/') -or $name.StartsWith('\') -or $name -match '^[A-Za-z]:' -or $name.Contains(':') -or ($parts -contains '..')) {
                throw "Unsafe path inside archive, refusing to extract: $name"
            }
        }
        return $zip.Entries.Count
    } finally { $zip.Dispose() }
}

function Expand-ZipSafely {
    param([Parameter(Mandatory)][string]$ZipPath, [Parameter(Mandatory)][string]$Destination)
    Assert-ZipSafe -ZipPath $ZipPath | Out-Null
    $root = [IO.Path]::GetFullPath($Destination)
    New-Item -ItemType Directory -Force -Path $root | Out-Null
    $zip = [IO.Compression.ZipFile]::OpenRead($ZipPath)
    try {
        foreach ($entry in $zip.Entries) {
            $target = [IO.Path]::GetFullPath((Join-Path $root $entry.FullName))
            if (-not (Test-PathInside $target $root)) { throw "Archive entry escapes destination: $($entry.FullName)" }
            if ($entry.FullName.EndsWith('/') -or $entry.FullName.EndsWith('\')) {
                New-Item -ItemType Directory -Force -Path $target | Out-Null; continue
            }
            New-Item -ItemType Directory -Force -Path (Split-Path -Parent $target) | Out-Null
            [IO.Compression.ZipFileExtensions]::ExtractToFile($entry, $target, $true)
        }
    } finally { $zip.Dispose() }
}

function Install-UvStandalone {
    # Official standalone uv release ZIP + its published .sha256 sibling; hash checked before extraction.
    param([Parameter(Mandatory)][string]$ToolsDir, [string]$ZipUrl = $script:JarvisUvZipUrl, [string]$ShaUrl = '')
    if (-not $ShaUrl) { $ShaUrl = "$ZipUrl.sha256" }
    $work = Join-Path $ToolsDir 'download'
    $zipFile = Join-Path $work 'uv.zip'
    $shaFile = Join-Path $work 'uv.zip.sha256'
    Remove-Item -LiteralPath $work -Recurse -Force -ErrorAction SilentlyContinue
    Write-Host "Downloading uv: $ZipUrl"
    Invoke-Download -Url $ZipUrl -OutFile $zipFile
    Invoke-Download -Url $ShaUrl -OutFile $shaFile
    $expected = ((Get-Content -LiteralPath $shaFile -Raw).Trim() -split '\s+')[0].ToLowerInvariant()
    if ($expected -notmatch '^[0-9a-f]{64}$') { throw 'The published uv checksum file is malformed; refusing to install uv.' }
    $actual = Get-Sha256 $zipFile
    if ($actual -ne $expected) {
        Remove-Item -LiteralPath $work -Recurse -Force -ErrorAction SilentlyContinue
        throw "uv download SHA-256 mismatch (expected $expected, got $actual). Nothing was extracted."
    }
    Write-Host "uv SHA-256 verified: $actual"
    $uvDir = Join-Path $ToolsDir 'uv'
    $staging = Join-Path $work 'extract'
    Expand-ZipSafely -ZipPath $zipFile -Destination $staging
    $exe = Get-ChildItem -LiteralPath $staging -Recurse -File -Filter 'uv.exe' | Select-Object -First 1
    if (-not $exe) { throw 'uv.exe not found in the verified uv archive.' }
    Remove-Item -LiteralPath $uvDir -Recurse -Force -ErrorAction SilentlyContinue
    New-Item -ItemType Directory -Force -Path $uvDir | Out-Null
    Get-ChildItem -LiteralPath $exe.DirectoryName -File | Copy-Item -Destination $uvDir -Force
    Remove-Item -LiteralPath $work -Recurse -Force -ErrorAction SilentlyContinue
    return (Join-Path $uvDir 'uv.exe')
}

function Get-JarvisSourceFiles {
    # Relative paths (backslash) of every allowlisted source file under $Root.
    param([Parameter(Mandatory)][string]$Root)
    $root = [IO.Path]::GetFullPath($Root).TrimEnd('\')
    $files = New-Object System.Collections.Generic.List[string]
    foreach ($name in $script:JarvisRootFiles) {
        if (Test-Path -LiteralPath (Join-Path $root $name) -PathType Leaf) { $files.Add($name) }
    }
    foreach ($dir in $script:JarvisSourceDirs.Keys) {
        $full = Join-Path $root $dir
        if (-not (Test-Path -LiteralPath $full -PathType Container)) { continue }
        foreach ($file in Get-ChildItem -LiteralPath $full -Recurse -File -Force) {
            $relative = $file.FullName.Substring($root.Length + 1)
            if ($relative -match '(^|\\)(\.[^\\]+|__pycache__)(\\|$)') { continue }
            if ($file.Attributes -band [IO.FileAttributes]::ReparsePoint) { continue }
            if ($script:JarvisForbiddenNames -contains $file.Name) { continue }
            if ($script:JarvisSourceDirs[$dir] -contains $file.Extension.ToLowerInvariant()) { $files.Add($relative) }
        }
    }
    return $files | Sort-Object -Unique
}

function Write-TextFile([string]$Path, [string]$Content, [switch]$Ascii) {
    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $Path) | Out-Null
    $encoding = if ($Ascii) { New-Object Text.ASCIIEncoding } else { New-Object Text.UTF8Encoding($false) }
    [IO.File]::WriteAllText($Path, $Content, $encoding)
}
