from __future__ import annotations

from pathlib import Path

import reframework_mcp.cli as cli
import reframework_mcp.console as console
from reframework_mcp.config import ServerSettings, load_settings
from reframework_mcp.console import adjacent_config, prepare_arguments


def test_default_http_port_is_9966(monkeypatch) -> None:
    monkeypatch.delenv("REFMCP_PORT", raising=False)
    assert ServerSettings().port == 9966
    assert load_settings().server.port == 9966


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


def test_serve_overrides_are_applied_before_service_initialization(
    monkeypatch,
) -> None:
    captured: dict[str, object] = {}

    class FakeServices:
        def __init__(self, settings) -> None:
            captured["settings"] = settings

    class FakeServer:
        def run(self, **arguments) -> None:
            captured["run"] = arguments

    monkeypatch.setattr(cli, "ApplicationServices", FakeServices)
    monkeypatch.setattr(cli, "create_server", lambda _services: FakeServer())

    result = cli.main(
        [
            "serve",
            "--transport",
            "streamable-http",
            "--host",
            "0.0.0.0",
            "--port",
            "9988",
        ]
    )

    settings = captured["settings"]
    assert result == 0
    assert settings.server.transport == "streamable-http"
    assert settings.server.host == "0.0.0.0"
    assert settings.server.port == 9988
    assert captured["run"] == {
        "transport": "streamable-http",
        "host": "0.0.0.0",
        "port": 9988,
    }
