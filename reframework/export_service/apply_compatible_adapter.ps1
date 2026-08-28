param(
    [Parameter(Mandatory = $true)]
    [string] $REFrameworkRoot
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest
$lf = [string][char]10
$crlf = ([string][char]13) + ([string][char]10)

function Read-NormalizedText {
    param([Parameter(Mandatory = $true)][string] $Path)

    $raw = [System.IO.File]::ReadAllText($Path)
    return @{
        Text = $raw.Replace($script:crlf, $script:lf)
        UsesCrlf = $raw.Contains($script:crlf)
    }
}

function Write-NormalizedText {
    param(
        [Parameter(Mandatory = $true)][string] $Path,
        [Parameter(Mandatory = $true)][string] $Text,
        [Parameter(Mandatory = $true)][bool] $UsesCrlf
    )

    $output = if ($UsesCrlf) { $Text.Replace($script:lf, $script:crlf) } else { $Text }
    [System.IO.File]::WriteAllText(
        $Path,
        $output,
        [System.Text.UTF8Encoding]::new($false)
    )
}

function Assert-UniqueLiteral {
    param(
        [Parameter(Mandatory = $true)][string] $Text,
        [Parameter(Mandatory = $true)][string] $Needle,
        [Parameter(Mandatory = $true)][string] $Label
    )

    $count = [regex]::Matches($Text, [regex]::Escape($Needle)).Count
    if ($count -ne 1) {
        throw "Compatibility anchor '$Label' must occur exactly once; found $count."
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

function Insert-AfterUniqueLine {
    param(
        [Parameter(Mandatory = $true)][string] $Text,
        [Parameter(Mandatory = $true)][string] $Pattern,
        [Parameter(Mandatory = $true)][string[]] $Lines,
        [Parameter(Mandatory = $true)][string] $Label
    )

    $matches = [regex]::Matches(
        $Text,
        $Pattern,
        [System.Text.RegularExpressions.RegexOptions]::Multiline
    )
    if ($matches.Count -ne 1) {
        throw "Compatibility anchor '$Label' must occur exactly once; found $($matches.Count)."
    }

    $match = $matches[0]
    $indent = $match.Groups["indent"].Value
    $addition = ($Lines | ForEach-Object { "$indent$_" }) -join $script:lf
    return $Text.Insert($match.Index + $match.Length, "$($script:lf)$addition")
}

function Update-TextFile {
    param(
        [Parameter(Mandatory = $true)][string] $Path,
        [Parameter(Mandatory = $true)][scriptblock] $Transform
    )

    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        throw "Required REFramework file is missing: $Path"
    }
    $document = Read-NormalizedText -Path $Path
    $updated = & $Transform $document.Text
    if ($updated -eq $document.Text) {
        throw "Compatibility adapter made no change to required file: $Path"
    }
    Write-NormalizedText -Path $Path -Text $updated -UsesCrlf $document.UsesCrlf
}

$root = (Resolve-Path -LiteralPath $REFrameworkRoot).Path
$cmakePath = Join-Path $root "CMakeLists.txt"
$cmakeTomlPath = Join-Path $root "cmake.toml"
$explorerHeaderPath = Join-Path $root "src\mods\tools\ObjectExplorer.hpp"
$explorerSourcePath = Join-Path $root "src\mods\tools\ObjectExplorer.cpp"
$pluginLoaderPath = Join-Path $root "src\mods\PluginLoader.cpp"

$pluginLoader = (Read-NormalizedText -Path $pluginLoaderPath).Text
Assert-UniqueLiteral -Text $pluginLoader -Needle "lua_State* reframework_create_script_state()" -Label "script state creation ABI"
Assert-UniqueLiteral -Text $pluginLoader -Needle "void reframework_destroy_script_state(lua_State* lua_state)" -Label "script state destruction ABI"

Update-TextFile -Path $cmakePath -Transform {
    param([string] $text)
    $text = Insert-AfterUniqueLine -Text $text -Pattern '^(?<indent>[ \t]*)"src/mods/tools/ObjectExplorer\.hpp"[ \t]*$' -Lines @(
        '"src/ExportServiceV1.cpp"',
        '"src/ExportServiceV1.hpp"',
        '"src/ExportServiceHooks.hpp"',
        '"src/ProbeServiceV1.cpp"',
        '"src/ProbeServiceV1.hpp"',
        '"src/ProbeServiceHooks.hpp"'
    ) -Label "REFramework source list"
    return Insert-AfterUniqueLine -Text $text -Pattern '^(?<indent>[ \t]*)shlwapi[ \t]*$' -Lines @("bcrypt") -Label "REFramework link libraries"
}

Update-TextFile -Path $cmakeTomlPath -Transform {
    param([string] $text)
    return Insert-AfterUniqueLine -Text $text -Pattern '^(?<indent>[ \t]*)"shlwapi",[ \t]*$' -Lines @('"bcrypt",') -Label "cmake.toml REFramework link libraries"
}

Update-TextFile -Path $explorerHeaderPath -Transform {
    param([string] $text)
    $text = Replace-UniqueLiteral -Text $text -Old "    void generate_sdk(bool skip_sdkgenny);" -New "    void generate_sdk_impl(bool skip_sdkgenny);" -Label "private SDK generator declaration"

    $publicAnchor = "    void on_lua_state_created(sol::state& lua) override;"
    $publicMethods = @'
    void on_lua_state_created(sol::state& lua) override;

    bool generate_sdk(bool skip_sdkgenny);
    bool is_dumping_sdk() const noexcept { return m_dumping_sdk.load(); }
    float sdk_dump_progress() const noexcept { return m_sdk_dump_progress.load(); }
    int sdk_dump_stage() const noexcept { return static_cast<int>(m_sdk_dump_stage.load()); }
'@
    return Replace-UniqueLiteral -Text $text -Old $publicAnchor -New $publicMethods.TrimEnd() -Label "ObjectExplorer public API insertion"
}

Update-TextFile -Path $explorerSourcePath -Transform {
    param([string] $text)
    $includeBlock = @'
#include "ObjectExplorer.hpp"
#include "ExportServiceHooks.hpp"
'@
    $text = Replace-UniqueLiteral -Text $text -Old '#include "ObjectExplorer.hpp"' -New $includeBlock.TrimEnd() -Label "Export Service include"
    $text = Replace-UniqueLiteral -Text $text -Old ("    m_dumping_sdk = true;" + $script:lf) -New "" -Label "legacy SDK start flag"
    $completionBlock = "    g_imethoddb.clear();" + $script:lf + $script:lf + "    m_dumping_sdk = false;" + $script:lf
    $text = Replace-UniqueLiteral -Text $text -Old $completionBlock -New ("    g_imethoddb.clear();" + $script:lf) -Label "legacy SDK completion flag"

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
    return Replace-UniqueLiteral -Text $text -Old "void ObjectExplorer::generate_sdk(const bool skip_sdkgenny) {" -New $wrapper.TrimEnd() -Label "SDK generator definition"
}

Write-Output "Applied guarded semantic adapter to REFramework at $root."
