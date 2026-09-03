[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$python = Join-Path $projectRoot '.venv\Scripts\python.exe'
$entryPoint = Join-Path $projectRoot 'run.py'
$versionInfo = Join-Path $PSScriptRoot 'version_info.txt'

if (-not (Test-Path -LiteralPath $python)) {
    throw 'Project build environment is missing. Run the documented development setup first.'
}

& $python -m PyInstaller `
    --noconfirm `
    --clean `
    --windowed `
    --name 'WorkCallTranscriber' `
    --paths (Join-Path $projectRoot 'src') `
    --add-data "$(Join-Path $projectRoot 'src\workcall_transcriber');worker_lib\workcall_transcriber" `
    --version-file $versionInfo `
    $entryPoint

if ($LASTEXITCODE -ne 0) {
    throw "PyInstaller failed with exit code $LASTEXITCODE."
}

$executable = Join-Path $projectRoot 'dist\WorkCallTranscriber\WorkCallTranscriber.exe'
if (-not (Test-Path -LiteralPath $executable)) {
    throw 'Build completed without the expected executable.'
}

Write-Output $executable
