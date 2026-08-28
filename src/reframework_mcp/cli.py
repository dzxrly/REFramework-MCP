"""Command-line entry point."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections.abc import Sequence
from pathlib import Path

from reframework_mcp import __version__
from reframework_mcp.config import load_settings
from reframework_mcp.server import create_server
from reframework_mcp.services import ApplicationServices


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="reframework-mcp",
        description="Explore REFramework metadata and live game objects through MCP.",
    )
    parser.add_argument("--config", type=Path, help="Path to a TOML configuration file")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    subparsers = parser.add_subparsers(dest="command")

    serve = subparsers.add_parser("serve", help="Start the MCP server")
    serve.add_argument(
        "--transport",
        choices=("stdio", "streamable-http"),
        help="Override configured transport",
    )
    serve.add_argument("--host", help="Streamable HTTP bind host")
    serve.add_argument("--port", type=int, help="Streamable HTTP bind port")
    serve.add_argument(
        "--index-configured-mods",
        action="store_true",
        help="Index usage roots from the configuration before serving",
    )

    import_dump = subparsers.add_parser(
        "import-dump",
        help="Import an existing il2cpp_dump.json without starting MCP",
    )
    import_dump.add_argument("path", type=Path)
    import_dump.add_argument("--manifest", type=Path)
    import_dump.add_argument("--no-activate", action="store_true")

    index_mod = subparsers.add_parser(
        "index-mod",
        help="Index REFramework Lua usage from one MOD project",
    )
    index_mod.add_argument("path", type=Path)
    index_mod.add_argument("--game-id")

    subparsers.add_parser("doctor", help="Print local server and bridge status")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    command = args.command or "serve"
    settings = load_settings(args.config)
    services = ApplicationServices(settings)

    if command == "import-dump":
        result = services.import_dump(
            args.path,
            activate=not args.no_activate,
            manifest_path=args.manifest,
        )
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result.get("ok") else 1

    if command == "index-mod":
        result = services.index_usage_project(args.path, game_id=args.game_id)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result.get("ok") else 1

    if command == "doctor":
        result = asyncio.run(services.runtime_status())
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result.get("ok") else 1

    if command != "serve":
        parser.error(f"Unknown command: {command}")

    if getattr(args, "index_configured_mods", False):
        for root in settings.mod_roots:
            result = services.index_usage_project(root)
            if not result.get("ok"):
                print(json.dumps(result, ensure_ascii=False), file=sys.stderr)

    mcp = create_server(services)
    transport = getattr(args, "transport", None) or settings.server.transport
    if transport == "stdio":
        mcp.run(transport="stdio")
    else:
        mcp.run(
            transport="streamable-http",
            host=getattr(args, "host", None) or settings.server.host,
            port=getattr(args, "port", None) or settings.server.port,
        )
    return 0
