param(
    [Parameter(Mandatory = $true)]
    [string] $REFrameworkRoot,

    [Parameter(Mandatory = $true)]
    [string] $ReleaseTargetPath,

    [string] $CMakeGenerator = "Visual Studio 17 2022",

    [string] $VisualStudioVersionRange = "[17.0,18.0)"
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$projectRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot "..")).Path
Import-Module (
    Join-Path $projectRoot "scripts\ReleasePipeline.psm1"
) -Force
$target = Read-ReleaseTarget -Path $ReleaseTargetPath
$refRoot = (Resolve-Path -LiteralPath $REFrameworkRoot).Path
$temporaryRoot = Join-Path $projectRoot ".tmp"
New-Item -ItemType Directory -Force -Path $temporaryRoot | Out-Null
$workingRoot = Reset-TaskDirectory -Path (
    Join-Path $temporaryRoot "nightly-compatibility"
) -AllowedRoot $temporaryRoot
$runtimeRoot = Reset-TaskDirectory -Path (
    Join-Path $temporaryRoot "nightly-compatible-runtime"
) -AllowedRoot $temporaryRoot
$diagnosticsRoot = Reset-TaskDirectory -Path (
    Join-Path $temporaryRoot "nightly-diagnostics"
) -AllowedRoot $temporaryRoot
$projectBuild = Join-Path $workingRoot "project-build"
$refBuild = Join-Path $workingRoot "ref-build"
$adapterReportPath = Join-Path $diagnosticsRoot "adapter-report.json"
$compatibilityReportPath = Join-Path $diagnosticsRoot "compatibility-report.json"
$failureStage = "initialization"
$adapterMode = $null

Push-Location $projectRoot
try {
    $failureStage = "source-checkout"
    $actualSourceCommit = (& git -C $refRoot rev-parse HEAD).Trim()
    if ($LASTEXITCODE -ne 0) {
        throw "Resolving the checked-out REF Nightly commit failed."
    }
    if ($actualSourceCommit -ne [string] $target.source_commit) {
        throw (
            "REF Nightly checkout mismatch: expected $($target.source_commit), " +
            "found $actualSourceCommit."
        )
    }

    $failureStage = "toolchain"
    if ($null -eq (Get-Command cmake -ErrorAction SilentlyContinue)) {
        throw "cmake was not found."
    }
    $programFilesX86 = [Environment]::GetFolderPath(
        [Environment+SpecialFolder]::ProgramFilesX86
    )
    $vswhere = Join-Path $programFilesX86 "Microsoft Visual Studio\Installer\vswhere.exe"
    if (-not (Test-Path -LiteralPath $vswhere -PathType Leaf)) {
        throw "vswhere.exe was not found."
    }
    $vsRootOutput = & $vswhere -latest -version $VisualStudioVersionRange -products * -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -property installationPath
    if ($LASTEXITCODE -ne 0) {
        throw "Querying Visual Studio C++ tools failed."
    }
    $vsRoot = ([string] $vsRootOutput).Trim()
    if (-not $vsRoot) {
        throw "Visual Studio C++ tools in range $VisualStudioVersionRange were not found."
    }
    $dumpbin = Get-ChildItem -LiteralPath (Join-Path $vsRoot "VC\Tools\MSVC") -Filter "dumpbin.exe" -Recurse |
        Sort-Object FullName -Descending |
        Select-Object -First 1
    if ($null -eq $dumpbin) {
        throw "dumpbin.exe was not found."
    }

    $failureStage = "hostile-host-configure"
    Invoke-Checked -FilePath "cmake" -Arguments @(
        "-S", $projectRoot,
        "-B", $projectBuild,
        "-G", $CMakeGenerator,
        "-A", "x64",
        "-DREFRAMEWORK_ROOT=$refRoot"
    ) -FailureMessage "Configuring the hostile-host compile target failed."

    $failureStage = "hostile-host-compile"
    Invoke-Checked -FilePath "cmake" -Arguments @(
        "--build", $projectBuild,
        "--config", "Release",
        "--parallel",
        "--target", "reframework_export_service_hostile_host_syntax"
    ) -FailureMessage "The hostile Windows host compile failed."

    $failureStage = "adapt"
    $installParameters = @{
        REFrameworkRoot = $refRoot
        ReportPath = $adapterReportPath
    }
    & (Join-Path $projectRoot "reframework\export_service\install.ps1") @installParameters
    $adapter = Get-Content -LiteralPath $adapterReportPath -Raw | ConvertFrom-Json
    $adapterMode = [string] $adapter.adapter_mode

    $failureStage = "ref-configure"
    $injectionModule = Join-Path $projectRoot "reframework\cmake\InjectServices.cmake"
    Invoke-Checked -FilePath "cmake" -Arguments @(
        "-S", $refRoot,
        "-B", $refBuild,
        "-G", $CMakeGenerator,
        "-A", "x64",
        "-DREF_BUILD_FRAMEWORK=ON",
        "-DREFMCP_PROJECT_ROOT=$projectRoot",
        "-DCMAKE_PROJECT_INCLUDE=$injectionModule"
    ) -FailureMessage "Configuring REF Nightly with MCP service injection failed."

    $failureStage = "ref-build"
    Invoke-Checked -FilePath "cmake" -Arguments @(
        "--build", $refBuild,
        "--config", "Release",
        "--parallel",
        "--target", "REFramework"
    ) -FailureMessage "Building REF Nightly failed."

    $failureStage = "bridge-build"
    Invoke-Checked -FilePath "cmake" -Arguments @(
        "--build", $projectBuild,
        "--config", "Release",
        "--parallel",
        "--target",
        "reframework_mcp",
        "reframework_export_service_syntax",
        "reframework_probe_service_syntax"
    ) -FailureMessage "Building the bridge and adapter syntax targets failed."

    $failureStage = "abi-check"
    $dinputPath = Join-Path $refBuild "bin\REFramework\dinput8.dll"
    if (-not (Test-Path -LiteralPath $dinputPath -PathType Leaf)) {
        throw "Injected REF Nightly output is missing: $dinputPath"
    }
    $bridgePath = Join-Path $projectBuild "bridge\Release\reframework_mcp.dll"
    if (-not (Test-Path -LiteralPath $bridgePath -PathType Leaf)) {
        throw "REFramework-MCP bridge output is missing: $bridgePath"
    }
    $exports = & $dumpbin.FullName /exports $dinputPath | Out-String
    if ($LASTEXITCODE -ne 0) {
        throw "dumpbin failed for dinput8.dll."
    }
    foreach ($required in @(
        "reframework_get_export_service_v1",
        "reframework_get_probe_service_v1"
    )) {
        if (-not $exports.Contains($required)) {
            throw "Required service ABI export is missing: $required"
        }
    }
    $exports | Set-Content -LiteralPath (
        Join-Path $diagnosticsRoot "dinput8-exports.txt"
    ) -Encoding utf8NoBOM

    $failureStage = "runtime-handoff"
    $runtimeDinput = Join-Path $runtimeRoot "dinput8.dll"
    $runtimeBridge = Join-Path $runtimeRoot "reframework_mcp.dll"
    Copy-Item -LiteralPath $dinputPath -Destination $runtimeDinput
    Copy-Item -LiteralPath $bridgePath -Destination $runtimeBridge
    Copy-Item -LiteralPath (Join-Path $refRoot "LICENSE") -Destination (
        Join-Path $runtimeRoot "REFramework-LICENSE"
    )

    $report = [ordered]@{
        schema_version = 3
        status = "passed"
        failure_stage = $null
        official_nightly_tag = [string] $target.nightly_tag
        source_commit = [string] $target.source_commit
        release_tag = [string] $target.release_tag
        adapter_mode = $adapterMode
        mcp_version = [string] $target.mcp_version
        toolchain = [ordered]@{
            cmake_generator = $CMakeGenerator
            visual_studio_root = $vsRoot
        }
        artifacts = [ordered]@{
            dinput8_sha256 = (
                Get-FileHash -LiteralPath $runtimeDinput -Algorithm SHA256
            ).Hash.ToLowerInvariant()
            bridge_sha256 = (
                Get-FileHash -LiteralPath $runtimeBridge -Algorithm SHA256
            ).Hash.ToLowerInvariant()
        }
        completed_at_utc = [DateTime]::UtcNow.ToString("o")
    }
    $report | ConvertTo-Json -Depth 5 |
        Set-Content -LiteralPath $compatibilityReportPath -Encoding utf8NoBOM
    Copy-Item -LiteralPath $compatibilityReportPath -Destination $runtimeRoot
    $failureStage = "complete"
    Write-Output $runtimeRoot
} catch {
    $failure = [ordered]@{
        schema_version = 3
        status = "failed"
        failure_stage = $failureStage
        official_nightly_tag = [string] $target.nightly_tag
        source_commit = [string] $target.source_commit
        release_tag = [string] $target.release_tag
        adapter_mode = $adapterMode
        mcp_version = [string] $target.mcp_version
        message = $_.Exception.Message
        completed_at_utc = [DateTime]::UtcNow.ToString("o")
    }
    $failure | ConvertTo-Json -Depth 4 |
        Set-Content -LiteralPath $compatibilityReportPath -Encoding utf8NoBOM
    throw
} finally {
    Pop-Location
}
