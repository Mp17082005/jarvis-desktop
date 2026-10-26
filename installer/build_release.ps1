# Builds dist\Jarvis-Setup.zip: installer + app source + UFO patches. Positive allowlist only;
# secrets (.env, ACCESS.txt, settings, agents.yaml), data, workspace, tests, venvs and caches are never included.
[CmdletBinding()]
param([string]$Output = '')
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'common.ps1')
$root = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..')).TrimEnd('\')
if (-not $Output) { $Output = Join-Path $root 'dist\Jarvis-Setup.zip' }
$Output = [IO.Path]::GetFullPath($Output)
$prefix = 'Jarvis-Setup/'

$files = New-Object System.Collections.Generic.List[string]
foreach ($f in @('Install-Jarvis.cmd', 'Uninstall-Jarvis.cmd', 'Start-Jarvis.cmd', 'README-FIRST.txt', 'scripts\install_app.ps1')) {
    if (-not (Test-Path -LiteralPath (Join-Path $root $f) -PathType Leaf)) { throw "Missing release file: $f" }
    $files.Add($f)
}
foreach ($f in (Get-ChildItem -LiteralPath (Join-Path $root 'installer') -File)) {
    if ($f.Extension -in @('.ps1', '.cmd')) { $files.Add("installer\$($f.Name)") }
}
foreach ($f in (Get-ChildItem -LiteralPath (Join-Path $root 'patches') -Recurse -File)) {
    if ($f.Extension -in @('.patch', '.py', '.json')) { $files.Add($f.FullName.Substring($root.Length + 1)) }
}
foreach ($f in (Get-JarvisSourceFiles -Root $root)) { $files.Add($f) }
$files = $files | Sort-Object -Unique

$forbidden = '(^|\\)(\.env(\..*)?|ACCESS\.txt|settings\.json|agents\.yaml)$|(^|\\)(data|workspace|tests|\.venv|__pycache__|logs|tools|dist|\.git)(\\|$)'
foreach ($f in $files) { if ($f -match $forbidden -and $f -notmatch '^\.env\.example$') { throw "Refusing to package forbidden file: $f" } }

New-Item -ItemType Directory -Force -Path (Split-Path -Parent $Output) | Out-Null
Remove-Item -LiteralPath $Output -Force -ErrorAction SilentlyContinue
Add-Type -AssemblyName System.IO.Compression, System.IO.Compression.FileSystem
$zip = [IO.Compression.ZipFile]::Open($Output, [IO.Compression.ZipArchiveMode]::Create)
try {
    foreach ($f in $files) {
        [IO.Compression.ZipFileExtensions]::CreateEntryFromFile($zip, (Join-Path $root $f), $prefix + ($f -replace '\\', '/'), [IO.Compression.CompressionLevel]::Optimal) | Out-Null
    }
} finally { $zip.Dispose() }

# Verify the archive: safe paths, no secrets, required entry points present.
Assert-ZipSafe -ZipPath $Output | Out-Null
$check = [IO.Compression.ZipFile]::OpenRead($Output)
try {
    $names = @($check.Entries | ForEach-Object { $_.FullName })
    foreach ($n in $names) {
        $rel = $n.Substring($prefix.Length) -replace '/', '\'
        if ($rel -match $forbidden) { throw "Secret or runtime file leaked into release: $n" }
    }
    foreach ($req in @('Install-Jarvis.cmd', 'README-FIRST.txt', 'scripts/install_app.ps1', 'installer/common.ps1', 'patches/ufo-manifest.json', 'pyproject.toml', 'uv.lock')) {
        if ($names -notcontains ($prefix + $req)) { throw "Release is missing $req" }
    }
} finally { $check.Dispose() }
$size = (Get-Item -LiteralPath $Output).Length
Write-Host "Built $Output ($($names.Count) files, $size bytes); verified no secrets or runtime data."
