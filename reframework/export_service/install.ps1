param(
    [Parameter(Mandatory = $true)]
    [string] $REFrameworkRoot,

    [string] $ReportPath
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest
$root = (Resolve-Path -LiteralPath $REFrameworkRoot).Path
$source = Split-Path -Parent $MyInvocation.MyCommand.Path
$actual = (& git -C $root rev-parse HEAD).Trim()
if ($LASTEXITCODE -ne 0) {
    throw "Unable to resolve the REFramework source commit at $root."
}

$adapter = & (Join-Path $source "apply_compatible_adapter.ps1") -REFrameworkRoot $root
$mode = [string] $adapter.mode
$modifiedFiles = [int] $adapter.modified_files

$report = [ordered]@{
    schema_version = 3
    source_commit = $actual
    adapter_mode = $mode
    modified_files = $modifiedFiles
    applied_at_utc = [DateTime]::UtcNow.ToString("o")
}
if ($ReportPath) {
    $resolvedReportPath = [System.IO.Path]::GetFullPath($ReportPath)
    $reportDirectory = Split-Path -Parent $resolvedReportPath
    New-Item -ItemType Directory -Force -Path $reportDirectory | Out-Null
    $report | ConvertTo-Json | Set-Content -LiteralPath $resolvedReportPath -Encoding utf8NoBOM
}

Write-Output "Applied REFramework-MCP adapter mode '$mode' to $actual."
