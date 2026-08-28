"""Configuration loading with TOML and environment overrides."""

from __future__ import annotations

import os
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any


def _default_data_dir() -> Path:
    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        return Path(local_app_data) / "REFramework-MCP"
    return Path.home() / ".local" / "share" / "reframework-mcp"


def _section(data: Mapping[str, Any], name: str) -> Mapping[str, Any]:
    value = data.get(name, {})
    return value if isinstance(value, Mapping) else {}


def _as_path(value: object, default: Path) -> Path:
    if not value:
        return default
    return Path(os.path.expandvars(os.path.expanduser(str(value)))).resolve()


@dataclass(frozen=True, slots=True)
class ServerSettings:
    transport: str = "stdio"
    host: str = "127.0.0.1"
    port: int = 9966


@dataclass(frozen=True, slots=True)
class BridgeSettings:
    pipe_name: str = r"\\.\pipe\reframework-mcp-v1"
    connect_timeout_seconds: float = 2.0
    request_timeout_seconds: float = 30.0


@dataclass(frozen=True, slots=True)
class PolicySettings:
    allow_generate_sdk: bool = True
    prompt_force_generate_sdk: bool = True
    allow_runtime_read: bool = True
    require_mutation_approval: bool = True
    allow_observe_hooks: bool = True


@dataclass(frozen=True, slots=True)
class Settings:
    server: ServerSettings = field(default_factory=ServerSettings)
    bridge: BridgeSettings = field(default_factory=BridgeSettings)
    policy: PolicySettings = field(default_factory=PolicySettings)
    data_dir: Path = field(default_factory=_default_data_dir)
    database_path: Path = field(default_factory=lambda: _default_data_dir() / "metadata.db")
    mod_roots: tuple[Path, ...] = ()
    config_path: Path | None = None

    @property
    def snapshots_dir(self) -> Path:
        return self.data_dir / "snapshots"

    @property
    def audit_dir(self) -> Path:
        return self.data_dir / "audit"

    def ensure_directories(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.snapshots_dir.mkdir(parents=True, exist_ok=True)
        self.audit_dir.mkdir(parents=True, exist_ok=True)
        self.database_path.parent.mkdir(parents=True, exist_ok=True)


def load_settings(path: Path | None = None) -> Settings:
    """Load settings from TOML, then apply REFMCP_* environment overrides."""

    data: Mapping[str, Any] = {}
    if path is not None:
        with path.open("rb") as stream:
            loaded = tomllib.load(stream)
        data = loaded if isinstance(loaded, Mapping) else {}

    server_data = _section(data, "server")
    bridge_data = _section(data, "bridge")
    storage_data = _section(data, "storage")
    policy_data = _section(data, "policy")
    usage_data = _section(data, "usage")

    data_dir = _as_path(storage_data.get("data_dir"), _default_data_dir())
    database_path = _as_path(storage_data.get("database_path"), data_dir / "metadata.db")

    roots_value = usage_data.get("mod_roots", [])
    roots: tuple[Path, ...] = ()
    if isinstance(roots_value, list):
        roots = tuple(_as_path(value, Path.cwd()) for value in roots_value if value)

    settings = Settings(
        server=ServerSettings(
            transport=str(server_data.get("transport", "stdio")),
            host=str(server_data.get("host", "127.0.0.1")),
            port=int(server_data.get("port", 9966)),
        ),
        bridge=BridgeSettings(
            pipe_name=str(bridge_data.get("pipe_name", BridgeSettings().pipe_name)),
            connect_timeout_seconds=float(bridge_data.get("connect_timeout_seconds", 2.0)),
            request_timeout_seconds=float(bridge_data.get("request_timeout_seconds", 30.0)),
        ),
        policy=PolicySettings(
            allow_generate_sdk=bool(policy_data.get("allow_generate_sdk", True)),
            prompt_force_generate_sdk=bool(policy_data.get("prompt_force_generate_sdk", True)),
            allow_runtime_read=bool(policy_data.get("allow_runtime_read", True)),
            require_mutation_approval=bool(policy_data.get("require_mutation_approval", True)),
            allow_observe_hooks=bool(policy_data.get("allow_observe_hooks", True)),
        ),
        data_dir=data_dir,
        database_path=database_path,
        mod_roots=roots,
        config_path=path,
    )

    transport = os.environ.get("REFMCP_TRANSPORT")
    host = os.environ.get("REFMCP_HOST")
    port = os.environ.get("REFMCP_PORT")
    pipe_name = os.environ.get("REFMCP_PIPE_NAME")
    env_data_dir = os.environ.get("REFMCP_DATA_DIR")
    env_database = os.environ.get("REFMCP_DATABASE_PATH")

    if transport or host or port:
        settings = replace(
            settings,
            server=replace(
                settings.server,
                transport=transport or settings.server.transport,
                host=host or settings.server.host,
                port=int(port) if port else settings.server.port,
            ),
        )
    if pipe_name:
        settings = replace(settings, bridge=replace(settings.bridge, pipe_name=pipe_name))
    if env_data_dir:
        new_data_dir = _as_path(env_data_dir, settings.data_dir)
        settings = replace(
            settings,
            data_dir=new_data_dir,
            database_path=_as_path(env_database, new_data_dir / "metadata.db"),
        )
    elif env_database:
        settings = replace(
            settings,
            database_path=_as_path(env_database, settings.database_path),
        )

    return settings
