[CmdletBinding()]
param(
    [string]$PackageDirectory = ''
)

$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
if (-not $PackageDirectory) {
    $PackageDirectory = Join-Path $projectRoot 'dist\WorkCallTranscriber'
}
$packagePath = [IO.Path]::GetFullPath($PackageDirectory)
$sourceExecutable = Join-Path $packagePath 'WorkCallTranscriber.exe'
if (-not (Test-Path -LiteralPath $sourceExecutable)) {
    throw "Built package was not found at $sourceExecutable. Run scripts\build.ps1 first."
}

$installRoot = Join-Path $env:LOCALAPPDATA 'WorkCall Transcriber'
$startMenuRoot = Join-Path ([Environment]::GetFolderPath('StartMenu')) 'Programs\WorkCall Transcriber'
$runtimeRoot = 'D:\WorkCalls'

New-Item -ItemType Directory -Force -Path $installRoot | Out-Null
Get-ChildItem -LiteralPath $packagePath -Force |
    Copy-Item -Destination $installRoot -Recurse -Force
foreach ($directory in @('Inbox', 'Archive', 'Temp', 'Logs', 'State')) {
    New-Item -ItemType Directory -Force -Path (Join-Path $runtimeRoot $directory) | Out-Null
}

New-Item -ItemType Directory -Force -Path $startMenuRoot | Out-Null
$installedExecutable = Join-Path $installRoot 'WorkCallTranscriber.exe'
$shell = New-Object -ComObject WScript.Shell
$shortcut = $shell.CreateShortcut((Join-Path $startMenuRoot 'WorkCall Transcriber.lnk'))
$shortcut.TargetPath = $installedExecutable
$shortcut.WorkingDirectory = $installRoot
$shortcut.IconLocation = "$installedExecutable,0"
$shortcut.Description = 'Local OBS work-call transcription utility'
$shortcut.Save()

$runKey = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Run'
New-Item -Path $runKey -Force | Out-Null
New-ItemProperty -Path $runKey -Name 'WorkCallTranscriber' -Value "`"$installedExecutable`" --minimized" -PropertyType String -Force | Out-Null

Start-Process -FilePath $installedExecutable
Write-Output $installedExecutable
