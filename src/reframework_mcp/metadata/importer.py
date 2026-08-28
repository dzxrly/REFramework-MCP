"""Streaming import for REFramework il2cpp_dump.json files."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import ijson

from reframework_mcp.storage import Database

_TRAILING_METHOD_ID = re.compile(r"^(.*?)(\d+)$")


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _split_type_name(full_name: str) -> tuple[str, str]:
    namespace, separator, name = full_name.rpartition(".")
    if not separator:
        return "", full_name
    return namespace, name


def _type_from_param(value: object) -> str:
    if isinstance(value, Mapping):
        type_name = value.get("type")
        return str(type_name) if type_name is not None else ""
    return str(value) if value is not None else ""


def _method_name(raw_key: str, entry: Mapping[str, Any]) -> str:
    source_id = entry.get("id")
    if isinstance(source_id, int):
        suffix = str(source_id)
        if raw_key.endswith(suffix):
            candidate = raw_key[: -len(suffix)]
            if candidate:
                return candidate
    match = _TRAILING_METHOD_ID.match(raw_key)
    return match.group(1) if match else raw_key


def _canonical_method(
    declaring_type: str,
    name: str,
    params: Iterable[Mapping[str, Any]],
    return_type: str,
) -> str:
    types = ", ".join(_type_from_param(item) for item in params)
    return f"{declaring_type}::{name}({types}) -> {return_type or 'System.Void'}"


def _source_member_key(
    kind: str,
    canonical_signature: str,
    source_member_id: object,
    fallback_identity: object,
) -> str:
    """Keep a searchable signature while preserving distinct source member slots."""

    identity = (
        f"id:{source_member_id}" if isinstance(source_member_id, int) else f"source:{fallback_identity}"
    )
    return f"{kind}:{canonical_signature}#{identity}"


@dataclass(frozen=True, slots=True)
class ImportResult:
    snapshot_id: str
    artifact_sha256: str
    type_count: int
    member_count: int
    activated: bool
    coverage: dict[str, str]


class Il2CppDumpImporter:
    """Import one top-level type entry at a time with ijson."""

    def __init__(self, database: Database) -> None:
        self.database = database

    def import_file(
        self,
        path: Path,
        *,
        provider: str = "il2cpp_dump_import",
        provider_version: str = "1.0",
        mode: str = "json_only",
        manifest: Mapping[str, Any] | None = None,
        activate: bool = True,
    ) -> ImportResult:
        resolved = path.resolve()
        digest = _sha256_file(resolved)
        manifest_data = dict(manifest or {})
        snapshot_id = str(manifest_data.get("snapshot_id") or f"snapshot:{digest}")
        coverage = self._initial_coverage()

        with self.database.connect() as connection:
            existing = connection.execute(
                "SELECT import_state FROM snapshots WHERE snapshot_id = ?",
                (snapshot_id,),
            ).fetchone()
            if existing and existing["import_state"] == "complete":
                if activate:
                    self._activate(connection, snapshot_id)
                    connection.commit()
                counts = connection.execute(
                    "SELECT type_count, member_count FROM snapshots WHERE snapshot_id = ?",
                    (snapshot_id,),
                ).fetchone()
                return ImportResult(
                    snapshot_id=snapshot_id,
                    artifact_sha256=digest,
                    type_count=int(counts["type_count"]),
                    member_count=int(counts["member_count"]),
                    activated=activate,
                    coverage=self._read_coverage(connection, snapshot_id),
                )

            connection.execute("BEGIN IMMEDIATE")
            if existing:
                connection.execute("DELETE FROM snapshots WHERE snapshot_id = ?", (snapshot_id,))
            connection.execute(
                """
                INSERT INTO snapshots(
                    snapshot_id, schema_version, game_id, game_version, tdb_version,
                    tdb_fingerprint, reframework_version, provider, provider_version,
                    mode, source_path, artifact_sha256, created_at, runtime_epoch,
                    active, import_state
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 'importing')
                """,
                (
                    snapshot_id,
                    str(manifest_data.get("snapshot_schema", "1.0")),
                    manifest_data.get("game_id"),
                    manifest_data.get("game_version"),
                    manifest_data.get("tdb_version"),
                    manifest_data.get("tdb_fingerprint"),
                    manifest_data.get("reframework_version"),
                    provider,
                    provider_version,
                    mode,
                    str(resolved),
                    digest,
                    str(manifest_data.get("created_at") or _utc_now()),
                    manifest_data.get("runtime_epoch"),
                ),
            )

            type_count = 0
            member_count = 0
            try:
                with resolved.open("rb") as stream:
                    for full_name, raw_entry in ijson.kvitems(stream, ""):
                        if not isinstance(raw_entry, Mapping):
                            continue
                        added = self._import_type(
                            connection,
                            snapshot_id,
                            str(full_name),
                            raw_entry,
                            provider,
                        )
                        type_count += 1
                        member_count += added

                for section, status in coverage.items():
                    connection.execute(
                        """
                        INSERT INTO snapshot_coverage(snapshot_id, section, status)
                        VALUES (?, ?, ?)
                        """,
                        (snapshot_id, section, status),
                    )
                connection.execute(
                    """
                    UPDATE snapshots
                    SET import_state='complete', type_count=?, member_count=?
                    WHERE snapshot_id=?
                    """,
                    (type_count, member_count, snapshot_id),
                )
                if activate:
                    self._activate(connection, snapshot_id)
                connection.commit()
            except Exception as error:
                connection.rollback()
                with self.database.connect() as failure_connection:
                    failure_connection.execute(
                        """
                        INSERT OR REPLACE INTO snapshots(
                            snapshot_id, schema_version, provider, source_path,
                            artifact_sha256, created_at, active, import_state, error
                        ) VALUES (?, '1.0', ?, ?, ?, ?, 0, 'failed', ?)
                        """,
                        (
                            snapshot_id,
                            provider,
                            str(resolved),
                            digest,
                            _utc_now(),
                            str(error),
                        ),
                    )
                    failure_connection.commit()
                raise

        return ImportResult(
            snapshot_id=snapshot_id,
            artifact_sha256=digest,
            type_count=type_count,
            member_count=member_count,
            activated=activate,
            coverage=coverage,
        )

    @staticmethod
    def _initial_coverage() -> dict[str, str]:
        return {
            "types": "complete",
            "methods": "complete",
            "method_parameters": "complete",
            "method_returns": "complete",
            "fields": "complete",
            "properties": "present_if_exported",
            "generics": "present_if_exported",
            "rsz": "present_if_exported",
            "reflection_methods": "partial_version_dependent",
            "reflection_properties": "partial_version_dependent",
            "deserializer_chain": "present_if_exported",
        }

    @staticmethod
    def _activate(connection: Any, snapshot_id: str) -> None:
        connection.execute("UPDATE snapshots SET active = 0 WHERE active = 1")
        connection.execute(
            "UPDATE snapshots SET active = 1 WHERE snapshot_id = ? AND import_state = 'complete'",
            (snapshot_id,),
        )

    @staticmethod
    def _read_coverage(connection: Any, snapshot_id: str) -> dict[str, str]:
        rows = connection.execute(
            "SELECT section, status FROM snapshot_coverage WHERE snapshot_id = ?",
            (snapshot_id,),
        ).fetchall()
        return {str(row["section"]): str(row["status"]) for row in rows}

    def _import_type(
        self,
        connection: Any,
        snapshot_id: str,
        full_name: str,
        entry: Mapping[str, Any],
        provider: str,
    ) -> int:
        namespace, name = _split_type_name(full_name)
        source_type_id = entry.get("id") if isinstance(entry.get("id"), int) else None
        connection.execute(
            """
            INSERT INTO types(
                snapshot_id, source_type_id, full_name, namespace, name,
                parent_name, declaring_type_name, native_typename, size_text,
                flags, is_generic_type, is_generic_definition, source, raw_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                snapshot_id,
                source_type_id,
                full_name,
                namespace,
                name,
                entry.get("parent"),
                entry.get("declaring_type"),
                entry.get("native_typename"),
                entry.get("size"),
                entry.get("flags"),
                self._optional_bool(entry.get("is_generic_type")),
                self._optional_bool(entry.get("is_generic_type_definition")),
                provider,
                json.dumps(
                    {
                        key: entry.get(key)
                        for key in (
                            "address",
                            "fqn",
                            "crc",
                            "name_hierarchy",
                            "generic_arguments",
                            "generic_type_arguments",
                        )
                        if key in entry
                    },
                    ensure_ascii=False,
                ),
            ),
        )
        type_pk = int(connection.execute("SELECT last_insert_rowid()").fetchone()[0])
        connection.execute(
            "INSERT INTO types_fts(snapshot_id, type_pk, full_name, namespace, name) VALUES (?, ?, ?, ?, ?)",
            (snapshot_id, type_pk, full_name, namespace, name),
        )

        if parent := entry.get("parent"):
            self._insert_edge(connection, snapshot_id, full_name, str(parent), "INHERITS")
        if owner := entry.get("declaring_type"):
            self._insert_edge(connection, snapshot_id, str(owner), full_name, "DECLARES_TYPE")

        member_count = 0
        method_names: dict[str, tuple[int, str]] = {}
        method_records: list[dict[str, Any]] = []
        raw_methods = entry.get("methods", {})
        if isinstance(raw_methods, Mapping):
            for raw_key, raw_method in raw_methods.items():
                if not isinstance(raw_method, Mapping):
                    continue
                params_value = raw_method.get("params", [])
                params = (
                    [item for item in params_value if isinstance(item, Mapping)]
                    if isinstance(params_value, list)
                    else []
                )
                return_type = _type_from_param(raw_method.get("returns"))
                method_name = _method_name(str(raw_key), raw_method)
                canonical = _canonical_method(full_name, method_name, params, return_type)
                member_pk = self._insert_member(
                    connection,
                    snapshot_id=snapshot_id,
                    source_member_id=raw_method.get("id"),
                    stable_key=_source_member_key(
                        "method",
                        canonical,
                        raw_method.get("id"),
                        raw_key,
                    ),
                    kind="tdb_method",
                    declaring_type=full_name,
                    name=method_name,
                    canonical_signature=canonical,
                    return_type=return_type,
                    flags=raw_method.get("flags"),
                    address=raw_method.get("function"),
                    source=provider,
                    raw=raw_method,
                )
                method_names[method_name] = (member_pk, return_type)
                self._insert_params(connection, member_pk, params)
                method_records.append(
                    {
                        "member_pk": member_pk,
                        "name": method_name,
                        "return_type": return_type,
                        "params": params,
                        "source_member_id": raw_method.get("id"),
                    }
                )
                if return_type:
                    self._insert_edge(
                        connection,
                        snapshot_id,
                        full_name,
                        return_type,
                        "RETURN_TYPE",
                        member_pk,
                    )
                for position, param in enumerate(params):
                    param_type = _type_from_param(param)
                    if param_type:
                        self._insert_edge(
                            connection,
                            snapshot_id,
                            full_name,
                            param_type,
                            "PARAMETER_TYPE",
                            member_pk,
                            {"position": position},
                        )
                member_count += 1

        raw_fields = entry.get("fields", {})
        if isinstance(raw_fields, Mapping):
            for field_name, raw_field in raw_fields.items():
                if not isinstance(raw_field, Mapping):
                    continue
                value_type = str(raw_field.get("type") or "")
                canonical = f"{full_name}::{field_name}: {value_type or 'unknown'}"
                member_pk = self._insert_member(
                    connection,
                    snapshot_id=snapshot_id,
                    source_member_id=raw_field.get("id"),
                    stable_key=f"field:{canonical}",
                    kind="tdb_field",
                    declaring_type=full_name,
                    name=str(field_name),
                    canonical_signature=canonical,
                    value_type=value_type,
                    flags=raw_field.get("flags"),
                    offset_from_base=raw_field.get("offset_from_base"),
                    source=provider,
                    raw=raw_field,
                )
                if value_type:
                    self._insert_edge(
                        connection,
                        snapshot_id,
                        full_name,
                        value_type,
                        "FIELD_TYPE",
                        member_pk,
                    )
                member_count += 1

        raw_properties = entry.get("properties", {})
        real_property_names: set[str] = set()
        if isinstance(raw_properties, Mapping):
            for property_name, raw_property in raw_properties.items():
                if not isinstance(raw_property, Mapping):
                    continue
                real_property_names.add(str(property_name))
                getter_name = str(raw_property.get("getter") or "")
                setter_name = str(raw_property.get("setter") or "")
                value_type = method_names.get(getter_name, (0, ""))[1]
                canonical = f"{full_name}::{property_name} {{ {getter_name}; {setter_name}; }}"
                member_pk = self._insert_member(
                    connection,
                    snapshot_id=snapshot_id,
                    source_member_id=raw_property.get("id"),
                    stable_key=f"property:{canonical}",
                    kind="tdb_property",
                    declaring_type=full_name,
                    name=str(property_name),
                    canonical_signature=canonical,
                    value_type=value_type,
                    getter_name=getter_name,
                    setter_name=setter_name,
                    source=provider,
                    raw=raw_property,
                )
                if value_type:
                    self._insert_edge(
                        connection,
                        snapshot_id,
                        full_name,
                        value_type,
                        "PROPERTY_TYPE",
                        member_pk,
                    )
                member_count += 1

        member_count += self._import_synthetic_properties(
            connection,
            snapshot_id,
            full_name,
            method_records,
            real_property_names,
            provider,
        )

        member_count += self._import_reflection(connection, snapshot_id, full_name, entry, provider)
        member_count += self._import_rsz(
            connection,
            snapshot_id,
            full_name,
            entry,
            provider,
        )
        self._import_generic_edges(connection, snapshot_id, full_name, entry)
        self._import_deserializer_edges(connection, snapshot_id, full_name, entry)
        return member_count

    def _import_synthetic_properties(
        self,
        connection: Any,
        snapshot_id: str,
        full_name: str,
        methods: list[dict[str, Any]],
        real_property_names: set[str],
        provider: str,
    ) -> int:
        """Infer logical properties from get_X/set_X methods without hiding methods."""

        groups: dict[tuple[str, tuple[str, ...]], dict[str, Any]] = {}
        for method in methods:
            name = str(method["name"])
            params = list(method["params"])
            if name.startswith("get_") and len(name) > 4:
                property_name = name[4:]
                index_types = tuple(_type_from_param(item) for item in params)
                group = groups.setdefault(
                    (property_name, index_types),
                    {"getter": None, "setter": None, "value_type": ""},
                )
                group["getter"] = method
                group["value_type"] = str(method.get("return_type") or "")
            elif name.startswith("set_") and len(name) > 4 and params:
                property_name = name[4:]
                index_types = tuple(_type_from_param(item) for item in params[:-1])
                group = groups.setdefault(
                    (property_name, index_types),
                    {"getter": None, "setter": None, "value_type": ""},
                )
                group["setter"] = method
                group["value_type"] = str(group["value_type"] or _type_from_param(params[-1]))

        count = 0
        for (property_name, index_types), group in sorted(groups.items()):
            if property_name in real_property_names and not index_types:
                continue
            getter = group["getter"]
            setter = group["setter"]
            value_type = str(group["value_type"] or "unknown")
            getter_name = str(getter["name"]) if getter else ""
            setter_name = str(setter["name"]) if setter else ""
            index_text = f"({', '.join(index_types)})" if index_types else ""
            canonical = (
                f"{full_name}::{property_name}{index_text}: {value_type} {{ {getter_name}; {setter_name}; }}"
            )
            raw = {
                "inference_rule": "get_X/set_X",
                "index_parameter_types": list(index_types),
                "getter_member_pk": getter["member_pk"] if getter else None,
                "setter_member_pk": setter["member_pk"] if setter else None,
            }
            member_pk = self._insert_member(
                connection,
                snapshot_id=snapshot_id,
                source_member_id=None,
                stable_key=f"synthetic_property:{canonical}",
                kind="synthetic_property",
                declaring_type=full_name,
                name=property_name,
                canonical_signature=canonical,
                value_type=value_type,
                getter_name=getter_name,
                setter_name=setter_name,
                source=provider,
                raw=raw,
            )
            if value_type and value_type != "unknown":
                self._insert_edge(
                    connection,
                    snapshot_id,
                    full_name,
                    value_type,
                    "PROPERTY_TYPE",
                    member_pk,
                    raw,
                )
            count += 1
        return count

    def _import_reflection(
        self,
        connection: Any,
        snapshot_id: str,
        full_name: str,
        entry: Mapping[str, Any],
        provider: str,
    ) -> int:
        count = 0
        methods = entry.get("reflection_methods", {})
        if isinstance(methods, Mapping):
            for name, raw in methods.items():
                if not isinstance(raw, Mapping):
                    continue
                params_value = raw.get("params", [])
                params = (
                    [item for item in params_value if isinstance(item, Mapping)]
                    if isinstance(params_value, list)
                    else []
                )
                return_type = str(raw.get("returns") or "")
                canonical = _canonical_method(full_name, str(name), params, return_type)
                member_pk = self._insert_member(
                    connection,
                    snapshot_id=snapshot_id,
                    source_member_id=None,
                    stable_key=f"reflection_method:{canonical}",
                    kind="reflection_method",
                    declaring_type=full_name,
                    name=str(name),
                    canonical_signature=canonical,
                    return_type=return_type,
                    address=raw.get("function"),
                    source=provider,
                    raw=raw,
                )
                self._insert_params(connection, member_pk, params)
                if return_type:
                    self._insert_edge(
                        connection,
                        snapshot_id,
                        full_name,
                        return_type,
                        "REFLECTION_RETURN_TYPE",
                        member_pk,
                    )
                count += 1

        properties = entry.get("reflection_properties", {})
        if isinstance(properties, Mapping):
            for name, raw in properties.items():
                if not isinstance(raw, Mapping):
                    continue
                value_type = str(raw.get("type") or "")
                canonical = f"{full_name}::{name}: {value_type or 'unknown'}"
                member_pk = self._insert_member(
                    connection,
                    snapshot_id=snapshot_id,
                    source_member_id=None,
                    stable_key=f"reflection_property:{canonical}",
                    kind="reflection_property",
                    declaring_type=full_name,
                    name=str(name),
                    canonical_signature=canonical,
                    value_type=value_type,
                    address=raw.get("getter"),
                    source=provider,
                    raw=raw,
                )
                if value_type:
                    self._insert_edge(
                        connection,
                        snapshot_id,
                        full_name,
                        value_type,
                        "REFLECTION_PROPERTY_TYPE",
                        member_pk,
                    )
                count += 1
        return count

    def _import_rsz(
        self,
        connection: Any,
        snapshot_id: str,
        full_name: str,
        entry: Mapping[str, Any],
        provider: str,
    ) -> int:
        values = entry.get("RSZ", [])
        if not isinstance(values, list):
            return 0
        count = 0
        for index, value in enumerate(values):
            if not isinstance(value, Mapping):
                continue
            target_type = str(value.get("type") or "")
            if target_type:
                name = str(value.get("name") or f"rsz_{index}")
                canonical = f"{full_name}::{name}: {target_type}"
                member_pk = self._insert_member(
                    connection,
                    snapshot_id=snapshot_id,
                    source_member_id=value.get("id"),
                    stable_key=_source_member_key(
                        "rsz_field",
                        canonical,
                        value.get("id"),
                        index,
                    ),
                    kind="rsz_field",
                    declaring_type=full_name,
                    name=name,
                    canonical_signature=canonical,
                    value_type=target_type,
                    offset_from_base=value.get("offset_from_base") or value.get("offset"),
                    source=provider,
                    raw=value,
                )
                self._insert_edge(
                    connection,
                    snapshot_id,
                    full_name,
                    target_type,
                    "RSZ_CONTAINS",
                    member_pk,
                    metadata={"index": index, **dict(value)},
                )
                count += 1
        return count

    def _import_generic_edges(
        self,
        connection: Any,
        snapshot_id: str,
        full_name: str,
        entry: Mapping[str, Any],
    ) -> None:
        for key in ("generic_arguments", "generic_type_arguments", "generic_args"):
            values = entry.get(key)
            if not isinstance(values, list):
                continue
            for position, value in enumerate(values):
                target = value.get("type") or value.get("name") if isinstance(value, Mapping) else value
                if target:
                    self._insert_edge(
                        connection,
                        snapshot_id,
                        full_name,
                        str(target),
                        "GENERIC_ARGUMENT",
                        metadata={"position": position},
                    )

    def _import_deserializer_edges(
        self,
        connection: Any,
        snapshot_id: str,
        full_name: str,
        entry: Mapping[str, Any],
    ) -> None:
        values = entry.get("deserializer_chain", [])
        if not isinstance(values, list):
            return
        for position, value in enumerate(values):
            if not isinstance(value, Mapping):
                continue
            target = value.get("name") or value.get("type")
            if target:
                self._insert_edge(
                    connection,
                    snapshot_id,
                    full_name,
                    str(target),
                    "DESERIALIZER_USES",
                    metadata={"position": position, **dict(value)},
                )

    @staticmethod
    def _optional_bool(value: object) -> int | None:
        if value is None:
            return None
        return 1 if bool(value) else 0

    @staticmethod
    def _insert_member(
        connection: Any,
        *,
        snapshot_id: str,
        source_member_id: object,
        stable_key: str,
        kind: str,
        declaring_type: str,
        name: str,
        canonical_signature: str,
        source: str,
        raw: Mapping[str, Any],
        value_type: str | None = None,
        return_type: str | None = None,
        flags: object = None,
        address: object = None,
        offset_from_base: object = None,
        getter_name: str | None = None,
        setter_name: str | None = None,
    ) -> int:
        numeric_id = source_member_id if isinstance(source_member_id, int) else None
        connection.execute(
            """
            INSERT INTO members(
                snapshot_id, source_member_id, stable_key, kind, declaring_type,
                name, canonical_signature, value_type, return_type, flags,
                address, offset_from_base, getter_name, setter_name, source, raw_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                snapshot_id,
                numeric_id,
                stable_key,
                kind,
                declaring_type,
                name,
                canonical_signature,
                value_type,
                return_type,
                str(flags) if flags is not None else None,
                str(address) if address is not None else None,
                str(offset_from_base) if offset_from_base is not None else None,
                getter_name,
                setter_name,
                source,
                json.dumps(dict(raw), ensure_ascii=False, default=str),
            ),
        )
        member_pk = int(connection.execute("SELECT last_insert_rowid()").fetchone()[0])
        connection.execute(
            """
            INSERT INTO members_fts(
                snapshot_id, member_pk, name, canonical_signature,
                declaring_type, value_type, return_type
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                snapshot_id,
                member_pk,
                name,
                canonical_signature,
                declaring_type,
                value_type or "",
                return_type or "",
            ),
        )
        return member_pk

    @staticmethod
    def _insert_params(connection: Any, member_pk: int, params: list[Mapping[str, Any]]) -> None:
        for position, param in enumerate(params):
            connection.execute(
                """
                INSERT INTO method_params(member_pk, position, name, type_name, by_ref, by_ptr)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    member_pk,
                    position,
                    param.get("name"),
                    _type_from_param(param),
                    1 if param.get("by_ref") else 0,
                    1 if param.get("by_ptr") else 0,
                ),
            )

    @staticmethod
    def _insert_edge(
        connection: Any,
        snapshot_id: str,
        source_type: str,
        target_type: str,
        edge_kind: str,
        member_pk: int | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> None:
        connection.execute(
            """
            INSERT INTO type_edges(
                snapshot_id, source_type, target_type, edge_kind, member_pk, metadata_json
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                snapshot_id,
                source_type,
                target_type,
                edge_kind,
                member_pk,
                json.dumps(dict(metadata or {}), ensure_ascii=False, default=str),
            ),
        )
