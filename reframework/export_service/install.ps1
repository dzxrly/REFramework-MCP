param(
    [Parameter(Mandatory = $true)]
    [string] $REFrameworkRoot,

    [switch] $AllowNightlyCommit,

    [string] $ReportPath
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest
$root = (Resolve-Path -LiteralPath $REFrameworkRoot).Path
$source = Split-Path -Parent $MyInvocation.MyCommand.Path
$baselinePath = Join-Path (Split-Path -Parent $source) "nightly-baseline.json"
$baseline = Get-Content -LiteralPath $baselinePath -Raw | ConvertFrom-Json
$expected = [string] $baseline.commit
$actual = (& git -C $root rev-parse HEAD).Trim()
if ($LASTEXITCODE -ne 0) {
    throw "Unable to resolve the REFramework source commit at $root."
}

$pluginLoaderPath = Join-Path $root "src\mods\PluginLoader.cpp"
if (-not (Test-Path -LiteralPath $pluginLoaderPath -PathType Leaf)) {
    throw "Required REF Nightly file is missing: $pluginLoaderPath"
}
$pluginLoader = [System.IO.File]::ReadAllText($pluginLoaderPath)
foreach ($requiredAnchor in @(
    "lua_State* reframework_create_script_state()",
    "void reframework_destroy_script_state(lua_State* lua_state)"
)) {
    $count = [regex]::Matches($pluginLoader, [regex]::Escape($requiredAnchor)).Count
    if ($count -ne 1) {
        throw (
            "Required REF Nightly Probe ABI anchor must occur exactly once: " +
            "'$requiredAnchor'; found $count."
        )
    }
}

$patchPath = Join-Path $source "reframework-684ca773-export-service.patch"
$mode = ""
if ($actual -eq $expected) {
    & git -C $root apply --check $patchPath
    if ($LASTEXITCODE -ne 0) {
        throw "The adapter patch does not apply to the verified REF Nightly baseline."
    }
    & git -C $root apply $patchPath
    if ($LASTEXITCODE -ne 0) {
        throw "Applying the verified REF Nightly adapter patch failed."
    }
    $mode = "verified-nightly-patch"
} else {
    if (-not $AllowNightlyCommit) {
        throw (
            "The verified REF Nightly baseline is $($baseline.tag) ($expected); " +
            "current checkout is $actual. Use -AllowNightlyCommit only in the " +
            "automated full compatibility build."
        )
    }

    & git -C $root apply --check $patchPath 2>$null
    if ($LASTEXITCODE -eq 0) {
        & git -C $root apply $patchPath
        if ($LASTEXITCODE -ne 0) {
            throw "Applying the context-compatible REF Nightly patch failed."
        }
        $mode = "nightly-context-patch"
    } else {
        & (Join-Path $source "apply_compatible_adapter.ps1") -REFrameworkRoot $root
        $mode = "nightly-semantic-adapter"
    }
}

Copy-Item -LiteralPath (Join-Path $source "ExportServiceV1.cpp") -Destination (Join-Path $root "src\ExportServiceV1.cpp")
Copy-Item -LiteralPath (Join-Path $source "ExportServiceV1.hpp") -Destination (Join-Path $root "src\ExportServiceV1.hpp")
Copy-Item -LiteralPath (Join-Path $source "ExportServiceHooks.hpp") -Destination (Join-Path $root "src\ExportServiceHooks.hpp")
$probeSource = Join-Path (Split-Path -Parent $source) "probe_service"
Copy-Item -LiteralPath (Join-Path $probeSource "ProbeServiceV1.cpp") -Destination (Join-Path $root "src\ProbeServiceV1.cpp")
Copy-Item -LiteralPath (Join-Path $probeSource "ProbeServiceV1.hpp") -Destination (Join-Path $root "src\ProbeServiceV1.hpp")
Copy-Item -LiteralPath (Join-Path $probeSource "ProbeServiceHooks.hpp") -Destination (Join-Path $root "src\ProbeServiceHooks.hpp")

$report = [ordered]@{
    schema_version = 1
    source_commit = $actual
    baseline_tag = [string] $baseline.tag
    baseline_commit = $expected
    adapter_mode = $mode
    applied_at_utc = [DateTime]::UtcNow.ToString("o")
}
if ($ReportPath) {
    $resolvedReportPath = [System.IO.Path]::GetFullPath($ReportPath)
    $reportDirectory = Split-Path -Parent $resolvedReportPath
    New-Item -ItemType Directory -Force -Path $reportDirectory | Out-Null
    $report | ConvertTo-Json | Set-Content -LiteralPath $resolvedReportPath -Encoding utf8NoBOM
}

Write-Output "Applied REFramework-MCP adapter mode '$mode' to $actual."
