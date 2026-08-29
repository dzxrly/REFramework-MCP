param(
    [Parameter(Mandatory = $true)]
    [string] $ReleaseTargetPath,

    [Parameter(Mandatory = $true)]
    [string] $RuntimeDirectory,

    [Parameter(Mandatory = $true)]
    [string] $OutputDirectory,

    [string] $PythonExecutable = "python"
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$projectRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot "..")).Path
Import-Module (
    Join-Path $projectRoot "scripts\ReleasePipeline.psm1"
) -Force
$target = Read-ReleaseTarget -Path $ReleaseTargetPath
$runtimeRoot = (Resolve-Path -LiteralPath $RuntimeDirectory).Path
$outputRoot = [System.IO.Path]::GetFullPath($OutputDirectory)
$temporaryRoot = Join-Path $projectRoot ".tmp"
New-Item -ItemType Directory -Force -Path $temporaryRoot, $outputRoot | Out-Null
$workingRoot = Reset-TaskDirectory -Path (
    Join-Path $temporaryRoot "nightly-package"
) -AllowedRoot $temporaryRoot
$hostDist = Join-Path $workingRoot "host-dist"
$hostWork = Join-Path $workingRoot "pyinstaller-build"
$pythonDist = Join-Path $workingRoot "python-dist"
$pytestTemp = Join-Path $workingRoot "pytest"
$stage = Join-Path $workingRoot "stage"
$diagnosticsRoot = Reset-TaskDirectory -Path (
    Join-Path $temporaryRoot "nightly-package-diagnostics"
) -AllowedRoot $temporaryRoot
$packageReportPath = Join-Path $diagnosticsRoot "package-report.json"
$archivePath = Join-Path $outputRoot ([string] $target.archive_name)
$failureStage = "initialization"

Push-Location $projectRoot
try {
    $failureStage = "compatibility-handoff"
    $compatibilityReportPath = Join-Path $runtimeRoot "compatibility-report.json"
    $runtimeDinput = Join-Path $runtimeRoot "dinput8.dll"
    $runtimeBridge = Join-Path $runtimeRoot "reframework_mcp.dll"
    $runtimeLicense = Join-Path $runtimeRoot "REFramework-LICENSE"
    foreach ($path in @(
        $compatibilityReportPath,
        $runtimeDinput,
        $runtimeBridge,
        $runtimeLicense
    )) {
        if (-not (Test-Path -LiteralPath $path -PathType Leaf)) {
            throw "Compatibility handoff file is missing: $path"
        }
    }
    $compatibility = Get-Content -LiteralPath $compatibilityReportPath -Raw |
        ConvertFrom-Json
    if ([string] $compatibility.status -ne "passed") {
        throw "Compatibility handoff did not pass."
    }
    if ([string] $compatibility.release_tag -ne [string] $target.release_tag) {
        throw "Compatibility handoff target does not match ReleaseTarget."
    }
    $dinputHash = (
        Get-FileHash -LiteralPath $runtimeDinput -Algorithm SHA256
    ).Hash.ToLowerInvariant()
    $bridgeHash = (
        Get-FileHash -LiteralPath $runtimeBridge -Algorithm SHA256
    ).Hash.ToLowerInvariant()
    if ($dinputHash -ne [string] $compatibility.artifacts.dinput8_sha256 -or
        $bridgeHash -ne [string] $compatibility.artifacts.bridge_sha256) {
        throw "Compatibility handoff artifact hashes do not match the report."
    }

    $failureStage = "dependencies"
    Invoke-Checked -FilePath $PythonExecutable -Arguments @(
        "-m", "pip", "install", "--upgrade", "pip"
    ) -FailureMessage "Upgrading pip failed."
    Invoke-Checked -FilePath $PythonExecutable -Arguments @(
        "-m", "pip", "install", "--upgrade", "-e", ".[dev,bundle]"
    ) -FailureMessage "Installing Python dependencies failed."
    $installedVersion = (& $PythonExecutable -c (
        "from reframework_mcp import __version__; print(__version__)"
    )).Trim()
    if ($LASTEXITCODE -ne 0 -or
        $installedVersion -ne [string] $target.mcp_version) {
        throw (
            "MCP version mismatch: target=$($target.mcp_version), " +
            "package=$installedVersion."
        )
    }

    $failureStage = "project-quality"
    Invoke-Checked -FilePath $PythonExecutable -Arguments @(
        "-m", "pytest", "-q", "--basetemp", $pytestTemp
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
        "--output", (Join-Path $workingRoot "tool-contracts-v1.json"),
        "--check-digest", "schemas\tool-contracts-v1.sha256"
    ) -FailureMessage "Frozen schema verification failed."
    Invoke-Checked -FilePath $PythonExecutable -Arguments @(
        "-m", "build", "--outdir", $pythonDist
    ) -FailureMessage "Building Python distributions failed."

    $failureStage = "host-bundle"
    $hostPath = Join-Path $hostDist "REFramework-MCP.exe"
    Invoke-Checked -FilePath $PythonExecutable -Arguments @(
        "-m", "PyInstaller",
        "--noconfirm",
        "--clean",
        "--onefile",
        "--console",
        "--name", "REFramework-MCP",
        "--copy-metadata", "mcp",
        "--distpath", $hostDist,
        "--workpath", $hostWork,
        "--specpath", $workingRoot,
        (Join-Path $projectRoot "scripts\reframework_mcp_console.py")
    ) -FailureMessage "PyInstaller failed."
    Invoke-Checked -FilePath $hostPath -Arguments @(
        "--version"
    ) -FailureMessage "Bundled console smoke test failed."

    $failureStage = "package"
    if (Test-Path -LiteralPath $archivePath) {
        throw "Release archive already exists; refusing to overwrite it: $archivePath"
    }
    New-Item -ItemType Directory -Path $stage | Out-Null
    $gameRoot = Join-Path $stage "game"
    $pluginRoot = Join-Path $gameRoot "reframework\plugins"
    New-Item -ItemType Directory -Path $pluginRoot -Force | Out-Null

    Copy-Item -LiteralPath $hostPath -Destination $stage
    Copy-Item -LiteralPath $runtimeDinput -Destination (
        Join-Path $gameRoot "dinput8.dll"
    )
    Copy-Item -LiteralPath $runtimeBridge -Destination $pluginRoot
    Copy-Item -LiteralPath (
        Join-Path $projectRoot "README.md"
    ), (
        Join-Path $projectRoot "LICENSE"
    ), (
        Join-Path $projectRoot "config.example.toml"
    ) -Destination $stage
    Copy-Item -LiteralPath $runtimeLicense -Destination $stage
    Copy-Item -LiteralPath (Join-Path $projectRoot "docs") -Destination $stage -Recurse
    Copy-Item -LiteralPath $ReleaseTargetPath -Destination (
        Join-Path $stage "release-target.json"
    )
    Copy-Item -LiteralPath $compatibilityReportPath -Destination $stage

    $packageReport = [ordered]@{
        schema_version = 1
        status = "passed"
        failure_stage = $null
        release_tag = [string] $target.release_tag
        archive_name = [string] $target.archive_name
        mcp_version = [string] $target.mcp_version
        artifacts = [ordered]@{
            host_sha256 = (
                Get-FileHash -LiteralPath $hostPath -Algorithm SHA256
            ).Hash.ToLowerInvariant()
            dinput8_sha256 = $dinputHash
            bridge_sha256 = $bridgeHash
        }
        completed_at_utc = [DateTime]::UtcNow.ToString("o")
    }
    $packageReport | ConvertTo-Json -Depth 4 |
        Set-Content -LiteralPath $packageReportPath -Encoding utf8NoBOM
    Copy-Item -LiteralPath $packageReportPath -Destination $stage

    Compress-Archive -Path (Join-Path $stage "*") -DestinationPath $archivePath -CompressionLevel Optimal
    $digest = (
        Get-FileHash -LiteralPath $archivePath -Algorithm SHA256
    ).Hash.ToLowerInvariant()
    "$digest  $($target.archive_name)" |
        Set-Content -LiteralPath "$archivePath.sha256" -Encoding utf8NoBOM
    $failureStage = "complete"
    Write-Output $archivePath
} catch {
    $failure = [ordered]@{
        schema_version = 1
        status = "failed"
        failure_stage = $failureStage
        release_tag = [string] $target.release_tag
        archive_name = [string] $target.archive_name
        mcp_version = [string] $target.mcp_version
        message = $_.Exception.Message
        completed_at_utc = [DateTime]::UtcNow.ToString("o")
    }
    $failure | ConvertTo-Json -Depth 4 |
        Set-Content -LiteralPath $packageReportPath -Encoding utf8NoBOM
    throw
} finally {
    Pop-Location
}
