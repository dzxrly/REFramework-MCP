from __future__ import annotations

from pathlib import Path

import reframework_mcp.console as console
from reframework_mcp.console import adjacent_config, prepare_arguments


def test_double_click_defaults_to_local_streamable_http(tmp_path: Path) -> None:
    prepared, double_click = prepare_arguments([])

    assert double_click is True
    assert prepared == ["serve", "--transport", "streamable-http"]
    assert adjacent_config(tmp_path) is None


def test_adjacent_config_is_inserted_before_subcommand(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text("[server]\nport = 9876\n", encoding="utf-8")

    prepared, double_click = prepare_arguments([], config_path=adjacent_config(tmp_path))

    assert double_click is True
    assert prepared == [
        "--config",
        str(config_path),
        "serve",
        "--transport",
        "streamable-http",
    ]


def test_explicit_cli_arguments_keep_stdio_semantics(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    prepared, double_click = prepare_arguments(
        ["serve"],
        config_path=config_path,
    )

    assert double_click is False
    assert prepared == ["--config", str(config_path), "serve"]


def test_explicit_config_is_not_duplicated(tmp_path: Path) -> None:
    requested = tmp_path / "requested.toml"
    adjacent = tmp_path / "config.toml"

    prepared, double_click = prepare_arguments(
        ["--config", str(requested), "doctor"],
        config_path=adjacent,
    )

    assert double_click is False
    assert prepared == ["--config", str(requested), "doctor"]


def test_console_treats_ctrl_c_as_a_clean_shutdown(
    monkeypatch,
    capsys,
) -> None:
    def interrupted(_arguments: list[str]) -> int:
        raise KeyboardInterrupt

    monkeypatch.setattr(console, "adjacent_config", lambda: None)
    monkeypatch.setattr(console, "cli_main", interrupted)

    assert console.main(["serve"]) == 0
    assert "REFramework-MCP stopped." in capsys.readouterr().out
