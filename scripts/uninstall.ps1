[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'

function Assert-SafeChildPath {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$Parent,
        [Parameter(Mandatory = $true)][string]$ExpectedLeaf
    )
    $fullPath = [IO.Path]::GetFullPath($Path)
    $fullParent = [IO.Path]::GetFullPath($Parent).TrimEnd('\') + '\'
    if (-not $fullPath.StartsWith($fullParent, [StringComparison]::OrdinalIgnoreCase) -or
        [IO.Path]::GetFileName($fullPath) -ne $ExpectedLeaf) {
        throw "Unsafe uninstall target: $fullPath"
    }
    return $fullPath
}

$installParent = $env:LOCALAPPDATA
$installRoot = Assert-SafeChildPath (Join-Path $installParent 'WorkCall Transcriber') $installParent 'WorkCall Transcriber'
$startMenuParent = Join-Path ([Environment]::GetFolderPath('StartMenu')) 'Programs'
$startMenuRoot = Assert-SafeChildPath (Join-Path $startMenuParent 'WorkCall Transcriber') $startMenuParent 'WorkCall Transcriber'
$runKey = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Run'

if (Test-Path -LiteralPath $installRoot) {
    Remove-Item -LiteralPath $installRoot -Recurse -Force
}
if (Test-Path -LiteralPath $startMenuRoot) {
    Remove-Item -LiteralPath $startMenuRoot -Recurse -Force
}
Remove-ItemProperty -Path $runKey -Name 'WorkCallTranscriber' -ErrorAction SilentlyContinue

$removeState = Read-Host 'Remove non-recording WorkCalls settings, logs, and temp files? Archives and Inbox recordings are never removed. [y/N]'
if ($removeState -match '^(y|yes)$') {
    foreach ($name in @('State', 'Logs', 'Temp')) {
        $target = "D:\WorkCalls\$name"
        if (Test-Path -LiteralPath $target) {
            Remove-Item -LiteralPath $target -Recurse -Force
        }
    }
}

Write-Output 'WorkCall Transcriber was uninstalled. D:\WorkCalls\Archive and D:\WorkCalls\Inbox were preserved.'
