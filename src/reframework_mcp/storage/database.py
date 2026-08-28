"""SQLite schema and connection management."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

SCHEMA_VERSION = "1.0"

SCHEMA_SQL = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS schema_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS snapshots (
    snapshot_id TEXT PRIMARY KEY,
    schema_version TEXT NOT NULL,
    game_id TEXT,
    game_version TEXT,
    tdb_version INTEGER,
    tdb_fingerprint TEXT,
    reframework_version TEXT,
    provider TEXT NOT NULL,
    provider_version TEXT,
    mode TEXT,
    source_path TEXT NOT NULL,
    artifact_sha256 TEXT NOT NULL,
    created_at TEXT NOT NULL,
    runtime_epoch TEXT,
    active INTEGER NOT NULL DEFAULT 0,
    import_state TEXT NOT NULL,
    type_count INTEGER NOT NULL DEFAULT 0,
    member_count INTEGER NOT NULL DEFAULT 0,
    error TEXT
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_snapshots_active
ON snapshots(active) WHERE active = 1;

CREATE TABLE IF NOT EXISTS export_jobs (
    job_ref TEXT PRIMARY KEY,
    runtime_epoch TEXT,
    mode TEXT NOT NULL,
    request_policy TEXT NOT NULL,
    state TEXT NOT NULL,
    activate_snapshot INTEGER NOT NULL,
    index_after_export INTEGER NOT NULL,
    status_json TEXT NOT NULL DEFAULT '{}',
    artifact_path TEXT,
    snapshot_id TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    error TEXT
);

CREATE INDEX IF NOT EXISTS idx_export_jobs_state
ON export_jobs(state, updated_at);

CREATE TABLE IF NOT EXISTS snapshot_coverage (
    snapshot_id TEXT NOT NULL REFERENCES snapshots(snapshot_id) ON DELETE CASCADE,
    section TEXT NOT NULL,
    status TEXT NOT NULL,
    details_json TEXT NOT NULL DEFAULT '{}',
    PRIMARY KEY (snapshot_id, section)
);

CREATE TABLE IF NOT EXISTS types (
    type_pk INTEGER PRIMARY KEY AUTOINCREMENT,
    snapshot_id TEXT NOT NULL REFERENCES snapshots(snapshot_id) ON DELETE CASCADE,
    source_type_id INTEGER,
    full_name TEXT NOT NULL,
    namespace TEXT NOT NULL,
    name TEXT NOT NULL,
    parent_name TEXT,
    declaring_type_name TEXT,
    native_typename TEXT,
    size_text TEXT,
    flags TEXT,
    is_generic_type INTEGER,
    is_generic_definition INTEGER,
    source TEXT NOT NULL,
    raw_json TEXT NOT NULL DEFAULT '{}',
    UNIQUE (snapshot_id, full_name)
);

CREATE INDEX IF NOT EXISTS idx_types_snapshot_name
ON types(snapshot_id, full_name);

CREATE TABLE IF NOT EXISTS members (
    member_pk INTEGER PRIMARY KEY AUTOINCREMENT,
    snapshot_id TEXT NOT NULL REFERENCES snapshots(snapshot_id) ON DELETE CASCADE,
    source_member_id INTEGER,
    stable_key TEXT NOT NULL,
    kind TEXT NOT NULL,
    declaring_type TEXT NOT NULL,
    name TEXT NOT NULL,
    canonical_signature TEXT NOT NULL,
    value_type TEXT,
    return_type TEXT,
    flags TEXT,
    address TEXT,
    offset_from_base TEXT,
    getter_name TEXT,
    setter_name TEXT,
    source TEXT NOT NULL,
    raw_json TEXT NOT NULL DEFAULT '{}',
    UNIQUE (snapshot_id, stable_key)
);

CREATE INDEX IF NOT EXISTS idx_members_snapshot_declaring
ON members(snapshot_id, declaring_type);

CREATE INDEX IF NOT EXISTS idx_members_snapshot_name
ON members(snapshot_id, name);

CREATE TABLE IF NOT EXISTS method_params (
    member_pk INTEGER NOT NULL REFERENCES members(member_pk) ON DELETE CASCADE,
    position INTEGER NOT NULL,
    name TEXT,
    type_name TEXT NOT NULL,
    by_ref INTEGER NOT NULL DEFAULT 0,
    by_ptr INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (member_pk, position)
);

CREATE TABLE IF NOT EXISTS type_edges (
    edge_pk INTEGER PRIMARY KEY AUTOINCREMENT,
    snapshot_id TEXT NOT NULL REFERENCES snapshots(snapshot_id) ON DELETE CASCADE,
    source_type TEXT NOT NULL,
    target_type TEXT NOT NULL,
    edge_kind TEXT NOT NULL,
    member_pk INTEGER REFERENCES members(member_pk) ON DELETE CASCADE,
    metadata_json TEXT NOT NULL DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_type_edges_source
ON type_edges(snapshot_id, source_type);

CREATE INDEX IF NOT EXISTS idx_type_edges_target
ON type_edges(snapshot_id, target_type);

CREATE INDEX IF NOT EXISTS idx_type_edges_member
ON type_edges(member_pk);

CREATE TABLE IF NOT EXISTS usage_projects (
    project_id TEXT PRIMARY KEY,
    root_path TEXT NOT NULL,
    game_id TEXT,
    source_hash TEXT NOT NULL,
    indexed_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS usage_sites (
    usage_pk INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id TEXT NOT NULL REFERENCES usage_projects(project_id) ON DELETE CASCADE,
    file_path TEXT NOT NULL,
    line INTEGER NOT NULL,
    column_number INTEGER NOT NULL DEFAULT 1,
    usage_kind TEXT NOT NULL,
    symbol TEXT NOT NULL,
    receiver TEXT,
    chain_id TEXT,
    source_excerpt TEXT NOT NULL,
    metadata_json TEXT NOT NULL DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_usage_symbol
ON usage_sites(symbol);

CREATE TABLE IF NOT EXISTS usage_edges (
    usage_edge_pk INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id TEXT NOT NULL REFERENCES usage_projects(project_id) ON DELETE CASCADE,
    file_path TEXT NOT NULL,
    chain_id TEXT,
    source_symbol TEXT NOT NULL,
    target_symbol TEXT NOT NULL,
    edge_kind TEXT NOT NULL,
    source_line INTEGER,
    target_line INTEGER,
    metadata_json TEXT NOT NULL DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_usage_edges_symbols
ON usage_edges(source_symbol, target_symbol, edge_kind);

CREATE TABLE IF NOT EXISTS root_hints (
    root_pk INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id TEXT REFERENCES usage_projects(project_id) ON DELETE CASCADE,
    root_kind TEXT NOT NULL,
    type_name TEXT NOT NULL,
    evidence TEXT,
    UNIQUE(project_id, root_kind, type_name, evidence)
);

CREATE TABLE IF NOT EXISTS runtime_nodes (
    node_ref TEXT PRIMARY KEY,
    runtime_epoch TEXT NOT NULL,
    scene_epoch TEXT,
    save_epoch TEXT,
    type_name TEXT,
    node_kind TEXT NOT NULL,
    value_json TEXT NOT NULL DEFAULT '{}',
    expires_at TEXT
);

CREATE TABLE IF NOT EXISTS runtime_edges (
    edge_pk INTEGER PRIMARY KEY AUTOINCREMENT,
    runtime_epoch TEXT NOT NULL,
    source_ref TEXT NOT NULL,
    target_ref TEXT NOT NULL,
    edge_kind TEXT NOT NULL,
    member_signature TEXT,
    metadata_json TEXT NOT NULL DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_runtime_nodes_epoch_type
ON runtime_nodes(runtime_epoch, type_name, node_kind);

CREATE INDEX IF NOT EXISTS idx_runtime_edges_epoch_source
ON runtime_edges(runtime_epoch, source_ref, edge_kind);

CREATE INDEX IF NOT EXISTS idx_runtime_edges_epoch_member
ON runtime_edges(runtime_epoch, member_signature, edge_kind);

CREATE TABLE IF NOT EXISTS hook_sessions (
    hook_ref TEXT PRIMARY KEY,
    runtime_epoch TEXT NOT NULL,
    member_signature TEXT NOT NULL,
    state TEXT NOT NULL,
    argument_layout_json TEXT NOT NULL DEFAULT '[]',
    installed_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    stats_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS hook_events (
    event_pk INTEGER PRIMARY KEY AUTOINCREMENT,
    hook_ref TEXT NOT NULL,
    runtime_epoch TEXT NOT NULL,
    timestamp TEXT NOT NULL,
    phase TEXT NOT NULL,
    member_signature TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    event_hash TEXT
);

CREATE INDEX IF NOT EXISTS idx_hook_events_member_epoch
ON hook_events(member_signature, runtime_epoch, timestamp);

CREATE TABLE IF NOT EXISTS probe_runs (
    probe_ref TEXT PRIMARY KEY,
    runtime_epoch TEXT,
    validation_ref TEXT,
    mode TEXT NOT NULL,
    state TEXT NOT NULL,
    status_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS probe_events (
    event_pk INTEGER PRIMARY KEY AUTOINCREMENT,
    probe_ref TEXT NOT NULL,
    runtime_epoch TEXT,
    timestamp TEXT NOT NULL,
    event_json TEXT NOT NULL,
    event_hash TEXT NOT NULL,
    UNIQUE(probe_ref, event_hash)
);

CREATE TABLE IF NOT EXISTS access_plans (
    plan_ref TEXT PRIMARY KEY,
    snapshot_id TEXT NOT NULL,
    game_id TEXT,
    goal TEXT NOT NULL,
    plan_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS plan_validations (
    validation_ref TEXT PRIMARY KEY,
    plan_ref TEXT NOT NULL REFERENCES access_plans(plan_ref) ON DELETE CASCADE,
    runtime_epoch TEXT,
    status TEXT NOT NULL,
    validation_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT
);

CREATE TABLE IF NOT EXISTS lua_validations (
    validation_ref TEXT PRIMARY KEY,
    code_sha256 TEXT NOT NULL,
    plan_ref TEXT,
    snapshot_id TEXT,
    runtime_epoch TEXT,
    mode TEXT NOT NULL,
    validation_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT
);

CREATE TABLE IF NOT EXISTS audit_events (
    event_pk INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    tool_name TEXT NOT NULL,
    runtime_epoch TEXT,
    snapshot_id TEXT,
    request_hash TEXT NOT NULL,
    outcome TEXT NOT NULL,
    details_json TEXT NOT NULL DEFAULT '{}'
);

CREATE VIRTUAL TABLE IF NOT EXISTS types_fts USING fts5(
    snapshot_id UNINDEXED,
    type_pk UNINDEXED,
    full_name,
    namespace,
    name,
    tokenize = 'unicode61'
);

CREATE VIRTUAL TABLE IF NOT EXISTS members_fts USING fts5(
    snapshot_id UNINDEXED,
    member_pk UNINDEXED,
    name,
    canonical_signature,
    declaring_type,
    value_type,
    return_type,
    tokenize = 'unicode61'
);

CREATE VIRTUAL TABLE IF NOT EXISTS usage_fts USING fts5(
    usage_pk UNINDEXED,
    symbol,
    source_excerpt,
    file_path,
    tokenize = 'unicode61'
);
"""


class Database:
    """Small connection factory; each operation gets its own SQLite connection."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as connection:
            connection.executescript(SCHEMA_SQL)
            self._migrate_1_0(connection)
            connection.execute(
                """
                INSERT INTO schema_meta(key, value) VALUES('schema_version', ?)
                ON CONFLICT(key) DO UPDATE SET value=excluded.value
                """,
                (SCHEMA_VERSION,),
            )
            connection.commit()

    @staticmethod
    def _migrate_1_0(connection: sqlite3.Connection) -> None:
        """Apply additive v1 migrations to databases created by early previews."""

        columns = {
            str(row["name"]) for row in connection.execute("PRAGMA table_info(hook_events)").fetchall()
        }
        if "event_hash" not in columns:
            connection.execute("ALTER TABLE hook_events ADD COLUMN event_hash TEXT")
        connection.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS idx_hook_events_dedupe
            ON hook_events(hook_ref, event_hash) WHERE event_hash IS NOT NULL
            """
        )

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=30.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA synchronous = NORMAL")
        connection.execute("PRAGMA busy_timeout = 30000")
        try:
            yield connection
        finally:
            connection.close()
