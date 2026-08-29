param(
    [Parameter(Mandatory = $true)]
    [string] $REFrameworkRoot
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest
$lf = [string][char]10
$crlf = ([string][char]13) + ([string][char]10)

function Read-SourceDocument {
    param([Parameter(Mandatory = $true)][string] $Path)

    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        throw "Required REFramework file is missing: $Path"
    }
    $raw = [System.IO.File]::ReadAllText($Path)
    return [pscustomobject]@{
        Path = $Path
        Text = $raw.Replace($script:crlf, $script:lf)
        UsesCrlf = $raw.Contains($script:crlf)
    }
}

function Assert-UniqueLiteral {
    param(
        [Parameter(Mandatory = $true)][string] $Text,
        [Parameter(Mandatory = $true)][string] $Needle,
        [Parameter(Mandatory = $true)][string] $Label
    )

    $count = [regex]::Matches($Text, [regex]::Escape($Needle)).Count
    if ($count -ne 1) {
        throw "Required source contract '$Label' must occur exactly once; found $count."
    }
}

function Replace-UniqueLiteral {
    param(
        [Parameter(Mandatory = $true)][string] $Text,
        [Parameter(Mandatory = $true)][string] $Old,
        [Parameter(Mandatory = $true)][AllowEmptyString()][string] $New,
        [Parameter(Mandatory = $true)][string] $Label
    )

    Assert-UniqueLiteral -Text $Text -Needle $Old -Label $Label
    return $Text.Replace($Old, $New)
}

function Test-FullyAdaptedHeader {
    param([Parameter(Mandatory = $true)][string] $Text)

    return $Text.Contains("    void generate_sdk_impl(bool skip_sdkgenny);") -and
        $Text.Contains("    bool generate_sdk(bool skip_sdkgenny);") -and
        $Text.Contains("    float sdk_dump_progress() const noexcept") -and
        -not $Text.Contains("    void generate_sdk(bool skip_sdkgenny);")
}

function Test-FullyAdaptedSource {
    param([Parameter(Mandatory = $true)][string] $Text)

    return $Text.Contains('#include "ExportServiceHooks.hpp"') -and
        $Text.Contains("bool ObjectExplorer::generate_sdk(const bool skip_sdkgenny) {") -and
        $Text.Contains("void ObjectExplorer::generate_sdk_impl(const bool skip_sdkgenny) {") -and
        -not $Text.Contains("void ObjectExplorer::generate_sdk(const bool skip_sdkgenny) {")
}

function Convert-ObjectExplorerHeader {
    param([Parameter(Mandatory = $true)][string] $Text)

    $updated = Replace-UniqueLiteral -Text $Text -Old (
        "    void generate_sdk(bool skip_sdkgenny);"
    ) -New (
        "    void generate_sdk_impl(bool skip_sdkgenny);"
    ) -Label "private SDK generator declaration"

    $publicDeclaration = "    void on_lua_state_created(sol::state& lua) override;"
    $publicMethods = @'
    void on_lua_state_created(sol::state& lua) override;

    bool generate_sdk(bool skip_sdkgenny);
    bool is_dumping_sdk() const noexcept { return m_dumping_sdk.load(); }
    float sdk_dump_progress() const noexcept { return m_sdk_dump_progress.load(); }
    int sdk_dump_stage() const noexcept { return static_cast<int>(m_sdk_dump_stage.load()); }
'@
    return Replace-UniqueLiteral -Text $updated -Old $publicDeclaration -New (
        $publicMethods.TrimEnd()
    ) -Label "ObjectExplorer public API insertion"
}

function Convert-ObjectExplorerSource {
    param([Parameter(Mandatory = $true)][string] $Text)

    $includeBlock = @'
#include "ObjectExplorer.hpp"
#include "ExportServiceHooks.hpp"
'@
    $updated = Replace-UniqueLiteral -Text $Text -Old (
        '#include "ObjectExplorer.hpp"'
    ) -New $includeBlock.TrimEnd() -Label "Export Service include"
    $updated = Replace-UniqueLiteral -Text $updated -Old (
        "    m_dumping_sdk = true;" + $script:lf
    ) -New "" -Label "legacy SDK start flag"
    $completionBlock = (
        "    g_imethoddb.clear();" + $script:lf + $script:lf +
        "    m_dumping_sdk = false;" + $script:lf
    )
    $updated = Replace-UniqueLiteral -Text $updated -Old $completionBlock -New (
        "    g_imethoddb.clear();" + $script:lf
    ) -Label "legacy SDK completion flag"

    $wrapper = @'
bool ObjectExplorer::generate_sdk(const bool skip_sdkgenny) {
    bool expected = false;
    if (!m_dumping_sdk.compare_exchange_strong(expected, true)) {
        return false;
    }
    m_sdk_dump_progress = 0.0f;
    m_sdk_dump_stage = SdkDumpStage::DUMP_INITIALIZATION;
    try {
        generate_sdk_impl(skip_sdkgenny);
    } catch (...) {
        m_dumping_sdk = false;
        m_sdk_dump_stage = SdkDumpStage::NONE;
        throw;
    }
    m_dumping_sdk = false;
    m_sdk_dump_stage = SdkDumpStage::NONE;
    return true;
}

bool reframework_generate_sdk(const bool skip_sdkgenny) {
    return ObjectExplorer::get()->generate_sdk(skip_sdkgenny);
}

float reframework_sdk_dump_progress() noexcept {
    return ObjectExplorer::get()->sdk_dump_progress();
}

int reframework_sdk_dump_stage() noexcept {
    return ObjectExplorer::get()->sdk_dump_stage();
}

const char* reframework_export_game_name() noexcept {
    return REFramework::get_game_name();
}

std::wstring reframework_export_persistent_file(const char* name) {
    return REFramework::get_persistent_dir(name).wstring();
}

REFMCPExportTDBInfo reframework_export_tdb_info() noexcept {
    const auto* tdb = sdk::RETypeDB::get();
    if (tdb == nullptr) {
        return {};
    }
    return {
        tdb->get_version(),
        tdb->get_num_types(),
        tdb->get_num_methods(),
        tdb->get_num_fields(),
        tdb->get_num_properties(),
    };
}

void ObjectExplorer::generate_sdk_impl(const bool skip_sdkgenny) {
'@
    return Replace-UniqueLiteral -Text $updated -Old (
        "void ObjectExplorer::generate_sdk(const bool skip_sdkgenny) {"
    ) -New $wrapper.TrimEnd() -Label "SDK generator definition"
}

function Convert-ToOriginalNewlines {
    param(
        [Parameter(Mandatory = $true)][string] $Text,
        [Parameter(Mandatory = $true)][bool] $UsesCrlf
    )

    if ($UsesCrlf) {
        return $Text.Replace($script:lf, $script:crlf)
    }
    return $Text
}

function Commit-SourceUpdates {
    param([Parameter(Mandatory = $true)][object[]] $Updates)

    $encoding = [System.Text.UTF8Encoding]::new($false)
    $transactionId = [Guid]::NewGuid().ToString("N")
    $prepared = @()
    $replaced = @()
    try {
        foreach ($update in $Updates) {
            $temporaryPath = "$($update.Path).refmcp-$transactionId.tmp"
            $backupPath = "$($update.Path).refmcp-$transactionId.bak"
            $output = Convert-ToOriginalNewlines -Text $update.Text -UsesCrlf $update.UsesCrlf
            [System.IO.File]::WriteAllText($temporaryPath, $output, $encoding)
            if ([System.IO.File]::ReadAllText($temporaryPath) -ne $output) {
                throw "Prepared adapter output verification failed: $($update.Path)"
            }
            $prepared += [pscustomobject]@{
                Path = $update.Path
                TemporaryPath = $temporaryPath
                BackupPath = $backupPath
            }
        }

        foreach ($item in $prepared) {
            [System.IO.File]::Replace(
                $item.TemporaryPath,
                $item.Path,
                $item.BackupPath,
                $true
            )
            $replaced += $item
        }
    } catch {
        for ($index = $replaced.Count - 1; $index -ge 0; $index--) {
            $item = $replaced[$index]
            if (Test-Path -LiteralPath $item.BackupPath -PathType Leaf) {
                [System.IO.File]::Replace(
                    $item.BackupPath,
                    $item.Path,
                    $null,
                    $true
                )
            }
        }
        throw
    } finally {
        foreach ($item in $prepared) {
            foreach ($path in @($item.TemporaryPath, $item.BackupPath)) {
                if (Test-Path -LiteralPath $path -PathType Leaf) {
                    Remove-Item -LiteralPath $path -Force
                }
            }
        }
    }
}

$root = (Resolve-Path -LiteralPath $REFrameworkRoot).Path
$header = Read-SourceDocument -Path (
    Join-Path $root "src\mods\tools\ObjectExplorer.hpp"
)
$source = Read-SourceDocument -Path (
    Join-Path $root "src\mods\tools\ObjectExplorer.cpp"
)

$headerAdapted = Test-FullyAdaptedHeader -Text $header.Text
$sourceAdapted = Test-FullyAdaptedSource -Text $source.Text
if ($headerAdapted -or $sourceAdapted) {
    if (-not ($headerAdapted -and $sourceAdapted)) {
        throw "REFramework contains a partially applied MCP adapter; no files were changed."
    }
    [pscustomobject]@{
        mode = "already-adapted"
        modified_files = 0
    }
    return
}

$updatedHeader = Convert-ObjectExplorerHeader -Text $header.Text
$updatedSource = Convert-ObjectExplorerSource -Text $source.Text
if ($updatedHeader -eq $header.Text -or $updatedSource -eq $source.Text) {
    throw "The compatibility adapter produced an incomplete update; no files were changed."
}
if (-not (Test-FullyAdaptedHeader -Text $updatedHeader) -or
    -not (Test-FullyAdaptedSource -Text $updatedSource)) {
    throw "The compatibility adapter output failed validation; no files were changed."
}

Commit-SourceUpdates -Updates @(
    [pscustomobject]@{
        Path = $header.Path
        Text = $updatedHeader
        UsesCrlf = $header.UsesCrlf
    }
    [pscustomobject]@{
        Path = $source.Path
        Text = $updatedSource
        UsesCrlf = $source.UsesCrlf
    }
)

[pscustomobject]@{
    mode = "atomic-semantic-adapter"
    modified_files = 2
}
