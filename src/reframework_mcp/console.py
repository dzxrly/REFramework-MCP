"""Double-click friendly console launcher for the bundled Windows executable."""

from __future__ import annotations

import sys
from collections.abc import Sequence
from pathlib import Path

from reframework_mcp import __version__
from reframework_mcp.cli import main as cli_main
from reframework_mcp.config import load_settings

_DOUBLE_CLICK_ARGS = ("serve", "--transport", "streamable-http")


def executable_directory() -> Path:
    """Return the directory that owns the frozen executable or source checkout."""

    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path.cwd()


def adjacent_config(directory: Path | None = None) -> Path | None:
    """Return an optional config.toml placed next to the executable."""

    candidate = (directory or executable_directory()) / "config.toml"
    return candidate if candidate.is_file() else None


def prepare_arguments(
    argv: Sequence[str],
    *,
    config_path: Path | None = None,
) -> tuple[list[str], bool]:
    """Normalize console arguments and report whether this was a double-click launch."""

    prepared = list(argv)
    double_click = not prepared
    if double_click:
        prepared.extend(_DOUBLE_CLICK_ARGS)

    if config_path is not None and "--config" not in prepared:
        prepared[0:0] = ["--config", str(config_path)]
    return prepared, double_click


def _print_banner(config_path: Path | None) -> None:
    settings = load_settings(config_path)
    endpoint = f"http://{settings.server.host}:{settings.server.port}/mcp"
    print(f"REFramework-MCP {__version__}")
    print(f"MCP endpoint: {endpoint}")
    print(f"Configuration: {config_path if config_path is not None else 'built-in defaults'}")
    print("Close this window or press Ctrl+C to stop the MCP server.")
    print()


def main(argv: Sequence[str] | None = None) -> int:
    """Run the normal CLI, selecting local HTTP only when launched without arguments."""

    supplied = list(sys.argv[1:] if argv is None else argv)
    config_path = adjacent_config()
    prepared, double_click = prepare_arguments(supplied, config_path=config_path)
    if double_click:
        _print_banner(config_path)
    try:
        return cli_main(prepared)
    except KeyboardInterrupt:
        print()
        print("REFramework-MCP stopped.")
        return 0
