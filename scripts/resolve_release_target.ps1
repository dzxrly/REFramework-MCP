param(
    [Parameter(Mandatory = $true)]
    [string] $OutputPath,

    [string] $Repository = $env:GITHUB_REPOSITORY,

    [string] $RequestedTag = $env:REQUESTED_TAG,

    [switch] $ForceBuild,

    [string] $GitHubToken = $env:GH_TOKEN,

    [string] $GitHubOutputPath = $env:GITHUB_OUTPUT,

    [string] $NightlyReleaseJsonPath,

    [string] $ExistingReleasesJsonPath
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

function Read-JsonFile {
    param([Parameter(Mandatory = $true)][string] $Path)

    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        throw "JSON fixture is missing: $Path"
    }
    return Get-Content -LiteralPath $Path -Raw | ConvertFrom-Json
}

function Get-OfficialNightlyRelease {
    if ($NightlyReleaseJsonPath) {
        return Read-JsonFile -Path $NightlyReleaseJsonPath
    }

    $headers = @{
        Accept = "application/vnd.github+json"
        "User-Agent" = "REFramework-MCP-release-resolver"
        "X-GitHub-Api-Version" = "2022-11-28"
    }
    if ($GitHubToken) {
        $headers.Authorization = "Bearer $GitHubToken"
    }
    if ([string]::IsNullOrWhiteSpace($RequestedTag)) {
        $uri = "https://api.github.com/repos/praydog/REFramework-nightly/releases/latest"
    } else {
        $encodedTag = [Uri]::EscapeDataString($RequestedTag)
        $uri = "https://api.github.com/repos/praydog/REFramework-nightly/releases/tags/$encodedTag"
    }
    return Invoke-RestMethod -Uri $uri -Headers $headers
}

function Get-ExistingReleases {
    if ($ExistingReleasesJsonPath) {
        return @(Read-JsonFile -Path $ExistingReleasesJsonPath)
    }
    if ([string]::IsNullOrWhiteSpace($Repository)) {
        throw "Repository is required when existing release fixtures are not provided."
    }

    $releaseJson = & gh release list --repo $Repository --limit 1000 --json tagName
    if ($LASTEXITCODE -ne 0) {
        throw "Reading existing releases failed."
    }
    return @($releaseJson | ConvertFrom-Json)
}

$projectRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot "..")).Path
$versionSourcePath = Join-Path $projectRoot "src\reframework_mcp\_version.py"
$versionSource = Get-Content -LiteralPath $versionSourcePath -Raw
$versionMatch = [regex]::Match(
    $versionSource,
    '(?m)^__version__\s*=\s*"(?<version>[0-9]+(?:\.[0-9]+){2})"\s*$'
)
if (-not $versionMatch.Success) {
    throw "Unable to resolve the canonical MCP version from $versionSourcePath."
}
$mcpVersion = $versionMatch.Groups["version"].Value

$nightly = Get-OfficialNightlyRelease
$nightlyTag = [string] $nightly.tag_name
$nightlyMatch = [regex]::Match(
    $nightlyTag,
    "^nightly-(?<number>[0-9]+)-(?<sha>[0-9a-f]{40})$"
)
if (-not $nightlyMatch.Success) {
    throw "Unexpected official REF Nightly tag: $nightlyTag"
}
if ($RequestedTag -and $RequestedTag -ne $nightlyTag) {
    throw "Requested Nightly tag mismatch: requested $RequestedTag, resolved $nightlyTag."
}

$releaseNumber = $nightlyMatch.Groups["number"].Value
$sourceCommit = $nightlyMatch.Groups["sha"].Value
$sourceShort = $sourceCommit.Substring(0, 8)
$releaseTag = "v$mcpVersion-ref-nightly-$releaseNumber-$sourceShort"
$legacyReleasePrefix = "$releaseTag-"
$assetStem = "reframework-mcp-$mcpVersion-ref-nightly-$releaseNumber-$sourceShort-windows-x64"
$releaseTitle = "REFramework-MCP $mcpVersion - REF Nightly $nightlyTag"

$existingReleases = Get-ExistingReleases
$alreadyReleased = @(
    $existingReleases |
        Where-Object {
            $tagName = [string] $_.tagName
            $tagName -eq $releaseTag -or $tagName.StartsWith(
                $legacyReleasePrefix,
                [System.StringComparison]::Ordinal
            )
        }
).Count -gt 0
$shouldBuild = $ForceBuild.IsPresent -or -not $alreadyReleased
$shouldPublish = -not $alreadyReleased

if ($alreadyReleased -and $ForceBuild.IsPresent) {
    $decision = "rebuild-without-duplicate-release"
} elseif ($shouldBuild) {
    $decision = "build-and-release"
} else {
    $decision = "skip-already-released"
}

$target = [ordered]@{
    schema_version = 1
    mcp_version = $mcpVersion
    nightly_tag = $nightlyTag
    release_number = $releaseNumber
    source_commit = $sourceCommit
    source_short = $sourceShort
    release_tag = $releaseTag
    release_title = $releaseTitle
    asset_stem = $assetStem
    archive_name = "$assetStem.zip"
    already_released = $alreadyReleased
    should_build = $shouldBuild
    should_publish = $shouldPublish
    decision = $decision
}

$resolvedOutputPath = [System.IO.Path]::GetFullPath($OutputPath)
$outputDirectory = Split-Path -Parent $resolvedOutputPath
New-Item -ItemType Directory -Force -Path $outputDirectory | Out-Null
$target | ConvertTo-Json |
    Set-Content -LiteralPath $resolvedOutputPath -Encoding utf8NoBOM

if ($GitHubOutputPath) {
    @(
        "should_build=$($shouldBuild.ToString().ToLowerInvariant())"
        "should_publish=$($shouldPublish.ToString().ToLowerInvariant())"
        "source_commit=$sourceCommit"
        "release_tag=$releaseTag"
    ) | Out-File -FilePath $GitHubOutputPath -Append
}

Write-Output $resolvedOutputPath
