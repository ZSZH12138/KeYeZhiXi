[CmdletBinding()]
param(
    [Parameter()]
    [string]$PythonExecutable = "python",

    [Parameter()]
    [switch]$ValidateOnly
)

$ErrorActionPreference = "Stop"
$projectRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot "..")).Path
$manifestPath = Join-Path $projectRoot "packaging\desktop_manifest.json"
$specPath = Join-Path $projectRoot "packaging\KeYeZhiXi.spec"
$manifest = Get-Content -LiteralPath $manifestPath -Raw | ConvertFrom-Json

if ($manifest.formatVersion -ne 1) {
    throw "Unsupported desktop manifest format: $($manifest.formatVersion)"
}
if ($manifest.mode -ne "onedir") {
    throw "The Windows desktop application must use onedir mode."
}
if (-not (Test-Path -LiteralPath $specPath -PathType Leaf)) {
    throw "PyInstaller specification is missing: $specPath"
}

$resourcePaths = @()
foreach ($resource in $manifest.resources) {
    $sourcePath = Join-Path $projectRoot $resource.source
    if (-not (Test-Path -LiteralPath $sourcePath -PathType Leaf)) {
        throw "Desktop resource is missing: $($resource.source)"
    }
    $resourcePaths += $resource.source.Replace("\", "/")
}

$contract = [ordered]@{
    appName = $manifest.appName
    contentsDirectory = $manifest.contentsDirectory
    entrypoint = $manifest.entrypoint.Replace("\", "/")
    executable = "$($manifest.appName).exe"
    mode = $manifest.mode
    mutableDataIncluded = $false
    resources = $resourcePaths
    toolDirectory = ".build-tools/pyinstaller"
}

if ($ValidateOnly) {
    $contract | ConvertTo-Json -Compress
    exit 0
}

$toolRoot = Join-Path $projectRoot ".build-tools\pyinstaller"
$pyInstallerModule = Join-Path $toolRoot "PyInstaller\__init__.py"
if (-not (Test-Path -LiteralPath $pyInstallerModule -PathType Leaf)) {
    New-Item -ItemType Directory -Path $toolRoot -Force | Out-Null
    & $PythonExecutable -m pip install `
        --disable-pip-version-check `
        --target $toolRoot `
        "PyInstaller>=6.22,<7"
    if ($LASTEXITCODE -ne 0) {
        throw "Unable to install the isolated PyInstaller build tool."
    }
}

$previousPythonPath = $env:PYTHONPATH
$pathSeparator = [IO.Path]::PathSeparator
if ([string]::IsNullOrEmpty($previousPythonPath)) {
    $env:PYTHONPATH = $toolRoot
} else {
    $env:PYTHONPATH = "$toolRoot$pathSeparator$previousPythonPath"
}

$distRoot = Join-Path $projectRoot "dist"
$workRoot = Join-Path $projectRoot "build"
try {
    & $PythonExecutable -m PyInstaller `
        --noconfirm `
        --clean `
        --distpath $distRoot `
        --workpath $workRoot `
        $specPath
    if ($LASTEXITCODE -ne 0) {
        throw "PyInstaller failed with exit code $LASTEXITCODE."
    }
} finally {
    if ($null -eq $previousPythonPath) {
        Remove-Item Env:PYTHONPATH -ErrorAction SilentlyContinue
    } else {
        $env:PYTHONPATH = $previousPythonPath
    }
}

$appDirectory = Join-Path $distRoot $manifest.appName
$executablePath = Join-Path $appDirectory "$($manifest.appName).exe"
if (-not (Test-Path -LiteralPath $executablePath -PathType Leaf)) {
    throw "The expected desktop executable was not produced: $executablePath"
}

foreach ($forbiddenPath in $manifest.forbiddenPaths) {
    $relativePath = $forbiddenPath.Replace("/", "\")
    $rootCandidate = Join-Path $appDirectory $relativePath
    $internalCandidate = Join-Path `
        (Join-Path $appDirectory $manifest.contentsDirectory) `
        $relativePath
    if ((Test-Path -LiteralPath $rootCandidate) -or
        (Test-Path -LiteralPath $internalCandidate)) {
        throw "Mutable data was included in the build: $forbiddenPath"
    }
}

$archivePath = Join-Path $distRoot "$($manifest.appName)-windows-x64.zip"
if (Test-Path -LiteralPath $archivePath -PathType Leaf) {
    Remove-Item -LiteralPath $archivePath -Force
}
Compress-Archive -LiteralPath $appDirectory -DestinationPath $archivePath -CompressionLevel Optimal

Write-Host "Windows desktop application: $executablePath"
Write-Host "Distribution archive: $archivePath"
