from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from reframework_mcp import __version__

ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / ".github" / "workflows" / "release.yml"
RESOLVER = ROOT / "scripts" / "resolve_release_target.ps1"
PIPELINE_MODULE = ROOT / "scripts" / "ReleasePipeline.psm1"
ADAPTER = ROOT / "reframework" / "export_service" / "apply_compatible_adapter.ps1"
VERSION_SOURCE = ROOT / "src" / "reframework_mcp" / "_version.py"
SOURCE_SHA = "88ece6b7f91d36976a124d4a5f886dac2675b0b8"
NIGHTLY_TAG = f"nightly-01398-{SOURCE_SHA}"
MCP_VERSION = __version__
RELEASE_TAG = f"v{MCP_VERSION}-ref-nightly-01398-88ece6b7"
ASSET_STEM = f"reframework-mcp-{MCP_VERSION}-ref-nightly-01398-88ece6b7-windows-x64"


def _pwsh() -> str:
    executable = shutil.which("pwsh")
    if executable is None:
        if os.name == "nt":
            pytest.fail("pwsh is required for Windows release automation tests")
        pytest.skip("PowerShell release automation is Windows-specific")
    return executable


def _workflow_pwsh_scripts(workflow: str) -> list[str]:
    lines = workflow.splitlines()
    scripts: list[str] = []
    for shell_index, shell_line in enumerate(lines):
        if shell_line.strip() != "shell: pwsh":
            continue

        property_indent = len(shell_line) - len(shell_line.lstrip())
        run_index: int | None = None
        for candidate_index in range(shell_index + 1, len(lines)):
            candidate = lines[candidate_index]
            if not candidate.strip():
                continue
            candidate_indent = len(candidate) - len(candidate.lstrip())
            if candidate_indent < property_indent:
                break
            if candidate_indent == property_indent and candidate.strip() == "run: |":
                run_index = candidate_index
                break

        assert run_index is not None, f"pwsh step at line {shell_index + 1} has no run block"
        body: list[str] = []
        for source_line in lines[run_index + 1 :]:
            if not source_line.strip():
                body.append("")
                continue
            source_indent = len(source_line) - len(source_line.lstrip())
            if source_indent <= property_indent:
                break
            body.append(source_line[property_indent + 2 :])
        scripts.append("\n".join(body))
    return scripts


def _run_resolver(
    temporary: Path,
    release_tags: list[str],
    *,
    force_build: bool = False,
    nightly_tag: str = NIGHTLY_TAG,
) -> subprocess.CompletedProcess[str]:
    nightly_path = temporary / "nightly.json"
    releases_path = temporary / "releases.json"
    output_path = temporary / "release-target.json"
    nightly_path.write_text(json.dumps({"tag_name": nightly_tag}), encoding="utf-8")
    releases_path.write_text(
        json.dumps([{"tagName": tag} for tag in release_tags]),
        encoding="utf-8",
    )
    command = [
        _pwsh(),
        "-NoProfile",
        "-File",
        str(RESOLVER),
        "-OutputPath",
        str(output_path),
        "-Repository",
        "example/repository",
        "-NightlyReleaseJsonPath",
        str(nightly_path),
        "-ExistingReleasesJsonPath",
        str(releases_path),
    ]
    if force_build:
        command.append("-ForceBuild")
    return subprocess.run(
        command,
        cwd=ROOT,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        check=False,
    )


@pytest.mark.parametrize(
    ("release_tags", "force_build", "should_build", "should_publish", "decision"),
    [
        ([], False, True, True, "build-and-release"),
        ([RELEASE_TAG], False, False, False, "skip-already-released"),
        (
            [f"{RELEASE_TAG}-deadbeef"],
            False,
            False,
            False,
            "skip-already-released",
        ),
        (
            [RELEASE_TAG],
            True,
            True,
            False,
            "rebuild-without-duplicate-release",
        ),
    ],
)
def test_release_target_decisions_are_deterministic(
    tmp_path: Path,
    release_tags: list[str],
    force_build: bool,
    should_build: bool,
    should_publish: bool,
    decision: str,
) -> None:
    result = _run_resolver(tmp_path, release_tags, force_build=force_build)

    assert result.returncode == 0, result.stderr
    target = json.loads((tmp_path / "release-target.json").read_text(encoding="utf-8"))
    assert target == {
        "schema_version": 1,
        "mcp_version": MCP_VERSION,
        "nightly_tag": NIGHTLY_TAG,
        "release_number": "01398",
        "source_commit": SOURCE_SHA,
        "source_short": "88ece6b7",
        "release_tag": RELEASE_TAG,
        "release_title": f"REFramework-MCP {MCP_VERSION} - REF Nightly {NIGHTLY_TAG}",
        "asset_stem": ASSET_STEM,
        "archive_name": f"{ASSET_STEM}.zip",
        "already_released": not should_publish,
        "should_build": should_build,
        "should_publish": should_publish,
        "decision": decision,
    }


def test_release_target_rejects_malformed_official_tag(tmp_path: Path) -> None:
    result = _run_resolver(tmp_path, [], nightly_tag="nightly-latest")

    assert result.returncode != 0
    assert "Unexpected official REF Nightly tag" in result.stderr
    assert not (tmp_path / "release-target.json").exists()


def test_release_target_reader_rejects_derived_identity_tampering(tmp_path: Path) -> None:
    resolved = _run_resolver(tmp_path, [])
    assert resolved.returncode == 0, resolved.stderr

    target_path = tmp_path / "release-target.json"
    target = json.loads(target_path.read_text(encoding="utf-8"))
    target["source_short"] = "deadbeef"
    target_path.write_text(json.dumps(target), encoding="utf-8")
    environment = {
        **os.environ,
        "REFMCP_TEST_MODULE": str(PIPELINE_MODULE),
        "REFMCP_TEST_TARGET": str(target_path),
    }
    result = subprocess.run(
        [
            _pwsh(),
            "-NoProfile",
            "-Command",
            (
                "Import-Module -Name $env:REFMCP_TEST_MODULE -Force; "
                "Read-ReleaseTarget -Path $env:REFMCP_TEST_TARGET"
            ),
        ],
        cwd=ROOT,
        env=environment,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        check=False,
    )

    assert result.returncode != 0
    assert "source_short does not match source_commit" in result.stderr


def _fixture_source(root: Path, *, break_late_contract: bool = False) -> tuple[Path, Path]:
    tools = root / "src" / "mods" / "tools"
    tools.mkdir(parents=True)
    header = tools / "ObjectExplorer.hpp"
    source = tools / "ObjectExplorer.cpp"
    header.write_text(
        """class ObjectExplorer {
public:
    void on_lua_state_created(sol::state& lua) override;
private:
    void generate_sdk(bool skip_sdkgenny);
};
""",
        encoding="utf-8",
    )
    completion = (
        "    g_imethoddb.clear();\n"
        if break_late_contract
        else ("    g_imethoddb.clear();\n\n    m_dumping_sdk = false;\n")
    )
    source.write_text(
        """#include "ObjectExplorer.hpp"

void ObjectExplorer::generate_sdk(const bool skip_sdkgenny) {
    m_dumping_sdk = true;
"""
        + completion
        + "}\n",
        encoding="utf-8",
    )
    (root / "CMakeLists.txt").write_text("project(Fixture)\n", encoding="utf-8")
    (root / "cmake.toml").write_text('[project]\nname = "Fixture"\n', encoding="utf-8")
    return header, source


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _run_adapter(root: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            _pwsh(),
            "-NoProfile",
            "-File",
            str(ADAPTER),
            "-REFrameworkRoot",
            str(root),
        ],
        cwd=ROOT,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        check=False,
    )


def test_adapter_is_atomic_idempotent_and_does_not_patch_build_files(tmp_path: Path) -> None:
    header, source = _fixture_source(tmp_path)
    cmake_hash = _sha256(tmp_path / "CMakeLists.txt")
    cmake_toml_hash = _sha256(tmp_path / "cmake.toml")

    first = _run_adapter(tmp_path)
    assert first.returncode == 0, first.stderr
    assert "atomic-semantic-adapter" in first.stdout
    assert "generate_sdk_impl" in header.read_text(encoding="utf-8")
    assert "ExportServiceHooks.hpp" in source.read_text(encoding="utf-8")
    adapted_hashes = (_sha256(header), _sha256(source))

    second = _run_adapter(tmp_path)
    assert second.returncode == 0, second.stderr
    assert "already-adapted" in second.stdout
    assert (_sha256(header), _sha256(source)) == adapted_hashes
    assert _sha256(tmp_path / "CMakeLists.txt") == cmake_hash
    assert _sha256(tmp_path / "cmake.toml") == cmake_toml_hash


def test_late_adapter_failure_leaves_every_source_hash_unchanged(tmp_path: Path) -> None:
    header, source = _fixture_source(tmp_path, break_late_contract=True)
    original_hashes = (_sha256(header), _sha256(source))

    result = _run_adapter(tmp_path)

    assert result.returncode != 0
    assert "legacy SDK completion flag" in result.stderr
    assert (_sha256(header), _sha256(source)) == original_hashes


def test_workflow_separates_compatibility_from_python_packaging() -> None:
    workflow = WORKFLOW.read_text(encoding="utf-8")
    cron_lines = [line.strip() for line in workflow.splitlines() if line.strip().startswith("- cron:")]

    assert cron_lines == ['- cron: "30 0 * * *"']
    assert "resolve:" in workflow
    assert "compatibility:" in workflow
    assert "package-release:" in workflow
    assert workflow.index("test_ref_nightly_compatibility.ps1") < workflow.index("setup-python")
    assert "resolve_release_target.ps1" in workflow
    assert "package_ref_nightly_release.ps1" in workflow
    assert "--notes-file" not in workflow
    assert '"--notes="' in workflow


def test_workflow_inline_pwsh_scripts_parse() -> None:
    workflow = WORKFLOW.read_text(encoding="utf-8")
    scripts = _workflow_pwsh_scripts(workflow)
    parser = (
        "$source = [Console]::In.ReadToEnd(); "
        "$tokens = $null; $errors = $null; "
        "[System.Management.Automation.Language.Parser]::ParseInput("
        "$source, [ref]$tokens, [ref]$errors) | Out-Null; "
        "if ($errors.Count -gt 0) { "
        "$errors | ForEach-Object { [Console]::Error.WriteLine($_.Message) }; exit 1 }"
    )

    assert len(scripts) == 6
    for index, script in enumerate(scripts, start=1):
        result = subprocess.run(
            [_pwsh(), "-NoProfile", "-NonInteractive", "-Command", parser],
            cwd=ROOT,
            input=script,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            check=False,
        )
        assert result.returncode == 0, f"inline pwsh block {index}: {result.stderr}"


def test_project_version_has_one_literal_source() -> None:
    assert re.fullmatch(r"[0-9]+(?:\.[0-9]+){2}", __version__)
    assert VERSION_SOURCE.read_text(encoding="utf-8").count(f'"{__version__}"') == 1
    consumers = [
        ROOT / "pyproject.toml",
        ROOT / "CMakeLists.txt",
        ROOT / "bridge" / "CMakeLists.txt",
        ROOT / "bridge" / "include" / "reframework_mcp" / "protocol.hpp",
        ROOT / "bridge" / "src" / "plugin.cpp",
        ROOT / "reframework" / "export_service" / "ExportServiceV1.cpp",
        ROOT / "reframework" / "probe_service" / "ProbeServiceV1.cpp",
        ROOT / "src" / "reframework_mcp" / "lua" / "draft.py",
        ROOT / "scripts" / "export_schemas.py",
    ]
    for path in consumers:
        assert __version__ not in path.read_text(encoding="utf-8"), path

    assert not (ROOT / "reframework" / "nightly-baseline.json").exists()
    assert not list((ROOT / "docs").glob("RELEASE_NOTES*"))
