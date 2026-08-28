param(
    [Parameter(Mandatory = $true)]
    [string] $REFrameworkRoot,

    [Parameter(Mandatory = $true)]
    [string] $NightlyTag,

    [Parameter(Mandatory = $true)]
    [string] $SourceCommit,

    [Parameter(Mandatory = $true)]
    [string] $ReleaseNumber,

    [Parameter(Mandatory = $true)]
    [string] $OutputDirectory,

    [Parameter(Mandatory = $true)]
    [string] $ReleaseId,

    [string] $PythonExecutable = "python"
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

function Invoke-Checked {
    param(
        [Parameter(Mandatory = $true)][string] $FilePath,
        [Parameter(Mandatory = $true)][string[]] $Arguments,
        [Parameter(Mandatory = $true)][string] $FailureMessage
    )

    & $FilePath @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "$FailureMessage Exit code: $LASTEXITCODE."
    }
}

$projectRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot "..")).Path
$refRoot = (Resolve-Path -LiteralPath $REFrameworkRoot).Path
$outputRoot = [System.IO.Path]::GetFullPath($OutputDirectory)
if ($SourceCommit -notmatch "^[0-9a-f]{40}$") {
    throw "SourceCommit must be a full lowercase Git SHA."
}
if ($ReleaseId -notmatch "^[0-9a-f]{8}$") {
    throw "ReleaseId must contain exactly eight lowercase hexadecimal characters."
}
$sourceShort = $SourceCommit.Substring(0, 8)
$temporaryRoot = Join-Path $projectRoot ".tmp"
$diagnosticsRoot = Join-Path $temporaryRoot "nightly-diagnostics"
$adapterReportPath = Join-Path $diagnosticsRoot "adapter-report.json"
$compatibilityReportPath = Join-Path $diagnosticsRoot "compatibility-report.json"
$hostDist = Join-Path $temporaryRoot "nightly-host-dist"
$hostWork = Join-Path $temporaryRoot "nightly-pyinstaller-build"
$refBuild = Join-Path $temporaryRoot "nightly-ref-build"
$bridgeBuild = Join-Path $temporaryRoot "nightly-bridge-build"
$stage = Join-Path $temporaryRoot "nightly-package-stage"
$baseline = Get-Content -LiteralPath (Join-Path $projectRoot "reframework\nightly-baseline.json") -Raw |
    ConvertFrom-Json

New-Item -ItemType Directory -Force -Path $temporaryRoot, $diagnosticsRoot, $outputRoot | Out-Null

$adapterMode = $null
$dinputPath = $null
$bridgePath = $null
$hostPath = Join-Path $hostDist "REFramework-MCP.exe"
$archivePath = Join-Path $outputRoot (
    "reframework-mcp-$($baseline.mcp_version)-ref-nightly-$ReleaseNumber-$sourceShort-$ReleaseId-windows-x64.zip"
)

Push-Location $projectRoot
try {
    Invoke-Checked -FilePath $PythonExecutable -Arguments @(
        "-m", "pip", "install", "--upgrade", "pip"
    ) -FailureMessage "Upgrading pip failed."
    Invoke-Checked -FilePath $PythonExecutable -Arguments @(
        "-m", "pip", "install", "--upgrade", "-e", ".[dev,bundle]"
    ) -FailureMessage "Installing Python dependencies failed."
    Invoke-Checked -FilePath $PythonExecutable -Arguments @(
        "-m", "pytest", "-q"
    ) -FailureMessage "Python tests failed."
    Invoke-Checked -FilePath $PythonExecutable -Arguments @(
        "-m", "ruff", "check", "src", "tests", "scripts"
    ) -FailureMessage "Ruff checks failed."
    Invoke-Checked -FilePath $PythonExecutable -Arguments @(
        "-m", "ruff", "format", "--check", "src", "tests", "scripts"
    ) -FailureMessage "Ruff formatting check failed."
    Invoke-Checked -FilePath $PythonExecutable -Arguments @(
        "-m", "mypy", "src"
    ) -FailureMessage "Mypy checks failed."
    Invoke-Checked -FilePath $PythonExecutable -Arguments @(
        "scripts\export_schemas.py",
        "--output", ".tmp\tool-contracts-v1.json",
        "--check-digest", "schemas\tool-contracts-v1.sha256"
    ) -FailureMessage "Frozen schema verification failed."

    $pyInstallerArguments = @(
        "-m", "PyInstaller",
        "--noconfirm",
        "--clean",
        "--onefile",
        "--console",
        "--name", "REFramework-MCP",
        "--copy-metadata", "mcp",
        "--distpath", $hostDist,
        "--workpath", $hostWork,
        "--specpath", $temporaryRoot,
        (Join-Path $projectRoot "scripts\reframework_mcp_console.py")
    )
    Invoke-Checked -FilePath $PythonExecutable -Arguments $pyInstallerArguments -FailureMessage "PyInstaller failed."
    Invoke-Checked -FilePath $hostPath -Arguments @("--version") -FailureMessage "Bundled console smoke test failed."

    $installParameters = @{
        REFrameworkRoot = $refRoot
        AllowNightlyCommit = $true
        ReportPath = $adapterReportPath
    }
    & (Join-Path $projectRoot "reframework\export_service\install.ps1") @installParameters
    $adapter = Get-Content -LiteralPath $adapterReportPath -Raw | ConvertFrom-Json
    $adapterMode = [string] $adapter.adapter_mode

    Invoke-Checked -FilePath "cmake" -Arguments @(
        "-S", $refRoot,
        "-B", $refBuild,
        "-G", "Visual Studio 17 2022",
        "-A", "x64",
        "-DREF_BUILD_FRAMEWORK=ON"
    ) -FailureMessage "Configuring REF Nightly failed."
    Invoke-Checked -FilePath "cmake" -Arguments @(
        "--build", $refBuild,
        "--config", "Release",
        "--parallel",
        "--target", "REFramework"
    ) -FailureMessage "Building REF Nightly failed."

    Invoke-Checked -FilePath "cmake" -Arguments @(
        "-S", $projectRoot,
        "-B", $bridgeBuild,
        "-G", "Visual Studio 17 2022",
        "-A", "x64",
        "-DREFRAMEWORK_ROOT=$refRoot"
    ) -FailureMessage "Configuring the bridge failed."
    Invoke-Checked -FilePath "cmake" -Arguments @(
        "--build", $bridgeBuild,
        "--config", "Release",
        "--parallel",
        "--target",
        "reframework_mcp",
        "reframework_export_service_syntax",
        "reframework_probe_service_syntax"
    ) -FailureMessage "Building the bridge and adapter checks failed."

    $dinput = Get-ChildItem -LiteralPath $refBuild -Filter "dinput8.dll" -Recurse |
        Select-Object -First 1
    if ($null -eq $dinput) {
        throw "Patched REF Nightly dinput8.dll was not produced."
    }
    $dinputPath = $dinput.FullName
    $bridgePath = (Resolve-Path -LiteralPath (
        Join-Path $bridgeBuild "bridge\Release\reframework_mcp.dll"
    )).Path

    $programFilesX86 = [Environment]::GetFolderPath(
        [Environment+SpecialFolder]::ProgramFilesX86
    )
    $vswhere = Join-Path $programFilesX86 "Microsoft Visual Studio\Installer\vswhere.exe"
    $vsRoot = (& $vswhere -latest -products * -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -property installationPath).Trim()
    if (-not $vsRoot) {
        throw "Visual Studio C++ tools were not found."
    }
    $dumpbin = Get-ChildItem -LiteralPath (Join-Path $vsRoot "VC\Tools\MSVC") -Filter "dumpbin.exe" -Recurse |
        Sort-Object FullName -Descending |
        Select-Object -First 1
    if ($null -eq $dumpbin) {
        throw "dumpbin.exe was not found."
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
    $exports | Set-Content -LiteralPath (Join-Path $diagnosticsRoot "dinput8-exports.txt") -Encoding utf8NoBOM

    if (Test-Path -LiteralPath $stage) {
        throw "Package stage already exists; refusing to overwrite it: $stage"
    }
    $gameRoot = Join-Path $stage "game"
    $pluginRoot = Join-Path $gameRoot "reframework\plugins"
    New-Item -ItemType Directory -Force -Path $pluginRoot | Out-Null

    Copy-Item -LiteralPath $hostPath -Destination $stage
    Copy-Item -LiteralPath $dinputPath -Destination (Join-Path $gameRoot "dinput8.dll")
    Copy-Item -LiteralPath $bridgePath -Destination $pluginRoot
    Copy-Item -LiteralPath (
        Join-Path $projectRoot "README.md"
    ), (
        Join-Path $projectRoot "LICENSE"
    ), (
        Join-Path $projectRoot "config.example.toml"
    ) -Destination $stage
    Copy-Item -LiteralPath (Join-Path $refRoot "LICENSE") -Destination (Join-Path $stage "REFramework-LICENSE")
    Copy-Item -LiteralPath (Join-Path $projectRoot "docs") -Destination $stage -Recurse
    Copy-Item -LiteralPath (Join-Path $projectRoot "reframework\nightly-baseline.json") -Destination (Join-Path $stage "source-baseline.json")

    $report = [ordered]@{
        schema_version = 1
        status = "passed"
        upstream_channel = "praydog/REFramework-nightly"
        official_nightly_tag = $NightlyTag
        source_commit = $SourceCommit
        release_id = $ReleaseId
        baseline_tag = [string] $baseline.tag
        adapter_mode = $adapterMode
        mcp_version = [string] $baseline.mcp_version
        artifacts = [ordered]@{
            host_sha256 = (Get-FileHash -LiteralPath $hostPath -Algorithm SHA256).Hash.ToLowerInvariant()
            dinput8_sha256 = (Get-FileHash -LiteralPath $dinputPath -Algorithm SHA256).Hash.ToLowerInvariant()
            bridge_sha256 = (Get-FileHash -LiteralPath $bridgePath -Algorithm SHA256).Hash.ToLowerInvariant()
        }
        completed_at_utc = [DateTime]::UtcNow.ToString("o")
    }
    $report | ConvertTo-Json -Depth 5 |
        Set-Content -LiteralPath $compatibilityReportPath -Encoding utf8NoBOM
    Copy-Item -LiteralPath $compatibilityReportPath -Destination $stage

    Compress-Archive -Path (Join-Path $stage "*") -DestinationPath $archivePath -CompressionLevel Optimal
    $archiveName = Split-Path -Leaf $archivePath
    $digest = (Get-FileHash -LiteralPath $archivePath -Algorithm SHA256).Hash.ToLowerInvariant()
    "$digest  $archiveName" |
        Set-Content -LiteralPath "$archivePath.sha256" -Encoding utf8NoBOM
    Write-Output "Aligned runtime package: $archivePath"
} catch {
    $failure = [ordered]@{
        schema_version = 1
        status = "failed"
        upstream_channel = "praydog/REFramework-nightly"
        official_nightly_tag = $NightlyTag
        source_commit = $SourceCommit
        release_id = $ReleaseId
        baseline_tag = [string] $baseline.tag
        adapter_mode = $adapterMode
        message = $_.Exception.Message
        completed_at_utc = [DateTime]::UtcNow.ToString("o")
    }
    $failure | ConvertTo-Json -Depth 4 |
        Set-Content -LiteralPath $compatibilityReportPath -Encoding utf8NoBOM
    throw
} finally {
    Pop-Location
}
