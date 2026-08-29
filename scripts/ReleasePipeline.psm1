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

function Reset-TaskDirectory {
    param(
        [Parameter(Mandatory = $true)][string] $Path,
        [Parameter(Mandatory = $true)][string] $AllowedRoot
    )

    $resolvedPath = [System.IO.Path]::GetFullPath($Path)
    $resolvedRoot = [System.IO.Path]::GetFullPath($AllowedRoot).TrimEnd(
        [System.IO.Path]::DirectorySeparatorChar,
        [System.IO.Path]::AltDirectorySeparatorChar
    )
    $rootPrefix = $resolvedRoot + [System.IO.Path]::DirectorySeparatorChar
    if (-not $resolvedPath.StartsWith(
        $rootPrefix,
        [System.StringComparison]::OrdinalIgnoreCase
    )) {
        throw "Refusing to reset a path outside the task temporary root: $resolvedPath"
    }
    if (Test-Path -LiteralPath $resolvedPath) {
        Remove-Item -LiteralPath $resolvedPath -Recurse -Force
    }
    New-Item -ItemType Directory -Path $resolvedPath | Out-Null
    return $resolvedPath
}

function Read-ReleaseTarget {
    param([Parameter(Mandatory = $true)][string] $Path)

    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        throw "ReleaseTarget JSON is missing: $Path"
    }
    $target = Get-Content -LiteralPath $Path -Raw | ConvertFrom-Json
    $required = @(
        "schema_version",
        "mcp_version",
        "nightly_tag",
        "release_number",
        "source_commit",
        "source_short",
        "release_tag",
        "release_title",
        "asset_stem",
        "archive_name",
        "already_released",
        "should_build",
        "should_publish",
        "decision"
    )
    foreach ($name in $required) {
        if ($target.PSObject.Properties.Name -notcontains $name) {
            throw "ReleaseTarget is missing required field '$name'."
        }
    }
    if ([int] $target.schema_version -ne 1) {
        throw "Unsupported ReleaseTarget schema: $($target.schema_version)"
    }

    $mcpVersion = [string] $target.mcp_version
    if ($mcpVersion -notmatch "^[0-9]+(?:\.[0-9]+){2}$") {
        throw "ReleaseTarget contains an invalid MCP version."
    }
    $nightlyTag = [string] $target.nightly_tag
    $nightlyMatch = [regex]::Match(
        $nightlyTag,
        "^nightly-(?<number>[0-9]+)-(?<sha>[0-9a-f]{40})$"
    )
    if (-not $nightlyMatch.Success) {
        throw "ReleaseTarget contains an invalid Nightly tag."
    }

    $releaseNumber = [string] $target.release_number
    $sourceCommit = [string] $target.source_commit
    $sourceShort = [string] $target.source_short
    if ($releaseNumber -ne $nightlyMatch.Groups["number"].Value) {
        throw "ReleaseTarget release_number does not match nightly_tag."
    }
    if ($sourceCommit -ne $nightlyMatch.Groups["sha"].Value) {
        throw "ReleaseTarget source_commit does not match nightly_tag."
    }
    if ($sourceShort -ne $sourceCommit.Substring(0, 8)) {
        throw "ReleaseTarget source_short does not match source_commit."
    }

    $releaseTag = "v$mcpVersion-ref-nightly-$releaseNumber-$sourceShort"
    $releaseTitle = "REFramework-MCP $mcpVersion - REF Nightly $nightlyTag"
    $assetStem = (
        "reframework-mcp-$mcpVersion-ref-nightly-" +
        "$releaseNumber-$sourceShort-windows-x64"
    )
    $derivedFields = [ordered]@{
        release_tag = $releaseTag
        release_title = $releaseTitle
        asset_stem = $assetStem
        archive_name = "$assetStem.zip"
    }
    foreach ($entry in $derivedFields.GetEnumerator()) {
        if ([string] $target.($entry.Key) -ne [string] $entry.Value) {
            throw "ReleaseTarget $($entry.Key) does not match its canonical identity."
        }
    }

    foreach ($name in @("already_released", "should_build", "should_publish")) {
        if ($target.($name) -isnot [bool]) {
            throw "ReleaseTarget field '$name' must be a boolean."
        }
    }
    $alreadyReleased = [bool] $target.already_released
    $shouldBuild = [bool] $target.should_build
    $shouldPublish = [bool] $target.should_publish
    if ($shouldPublish -ne (-not $alreadyReleased)) {
        throw "ReleaseTarget publication state is inconsistent."
    }
    if (-not $alreadyReleased -and -not $shouldBuild) {
        throw "An unpublished ReleaseTarget must be built before publication."
    }
    $expectedDecision = if (-not $alreadyReleased) {
        "build-and-release"
    } elseif ($shouldBuild) {
        "rebuild-without-duplicate-release"
    } else {
        "skip-already-released"
    }
    if ([string] $target.decision -ne $expectedDecision) {
        throw "ReleaseTarget decision does not match its build state."
    }
    return $target
}

Export-ModuleMember -Function Invoke-Checked, Reset-TaskDirectory, Read-ReleaseTarget
