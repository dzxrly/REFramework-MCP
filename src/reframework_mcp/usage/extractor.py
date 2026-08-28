"""A syntax-aware extractor for the REFramework calls used by Lua mods."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from itertools import pairwise
from pathlib import Path
from typing import Any, ClassVar

from reframework_mcp.storage import Database


@dataclass(frozen=True, slots=True)
class Token:
    kind: str
    value: str
    line: int
    column: int


@dataclass(frozen=True, slots=True)
class UsageSite:
    kind: str
    symbol: str
    line: int
    column: int
    receiver: str | None
    chain_id: str
    source_excerpt: str
    metadata: dict[str, object]


class LuaLexer:
    """Tokenize the Lua subset required to recover REFramework call expressions."""

    def tokenize(self, source: str) -> Iterator[Token]:
        index = 0
        line = 1
        column = 1
        length = len(source)
        while index < length:
            char = source[index]
            if char in " \t\r":
                index += 1
                column += 1
                continue
            if char == "\n":
                index += 1
                line += 1
                column = 1
                continue
            if source.startswith("--[[", index):
                index, line, column = self._consume_long_comment(source, index, line, column)
                continue
            if source.startswith("--", index):
                newline = source.find("\n", index)
                if newline < 0:
                    return
                column += newline - index
                index = newline
                continue
            if source.startswith("[[", index):
                value, index, line, column = self._consume_long_string(source, index, line, column)
                yield Token("string", value, line - value.count("\n"), column)
                continue
            if char in {"'", '"'}:
                start_line, start_column = line, column
                value, index, line, column = self._consume_string(source, index, line, column, char)
                yield Token("string", value, start_line, start_column)
                continue
            if char.isalpha() or char == "_":
                start = index
                start_column = column
                while index < length and (source[index].isalnum() or source[index] == "_"):
                    index += 1
                    column += 1
                yield Token("identifier", source[start:index], line, start_column)
                continue
            if char.isdigit():
                start = index
                start_column = column
                while index < length and (source[index].isalnum() or source[index] in ".xX_"):
                    index += 1
                    column += 1
                yield Token("number", source[start:index], line, start_column)
                continue
            yield Token("punct", char, line, column)
            index += 1
            column += 1

    @staticmethod
    def _consume_string(
        source: str,
        index: int,
        line: int,
        column: int,
        quote: str,
    ) -> tuple[str, int, int, int]:
        index += 1
        column += 1
        value: list[str] = []
        while index < len(source):
            char = source[index]
            if char == "\\" and index + 1 < len(source):
                escaped = source[index + 1]
                translations = {"n": "\n", "r": "\r", "t": "\t"}
                value.append(translations.get(escaped, escaped))
                index += 2
                column += 2
                continue
            if char == quote:
                return "".join(value), index + 1, line, column + 1
            value.append(char)
            index += 1
            if char == "\n":
                line += 1
                column = 1
            else:
                column += 1
        return "".join(value), index, line, column

    @staticmethod
    def _consume_long_string(
        source: str,
        index: int,
        line: int,
        column: int,
    ) -> tuple[str, int, int, int]:
        end = source.find("]]", index + 2)
        if end < 0:
            end = len(source)
            suffix = 0
        else:
            suffix = 2
        value = source[index + 2 : end]
        consumed = source[index : end + suffix]
        lines = consumed.splitlines(keepends=True)
        if len(lines) > 1:
            line += len(lines) - 1
            column = len(lines[-1]) + 1
        else:
            column += len(consumed)
        return value, end + suffix, line, column

    def _consume_long_comment(
        self,
        source: str,
        index: int,
        line: int,
        column: int,
    ) -> tuple[int, int, int]:
        _, index, line, column = self._consume_long_string(source, index + 2, line, column + 2)
        return index, line, column


class LuaUsageExtractor:
    """Build a small call-expression AST for the REFramework SDK surface."""

    SDK_CALLS: ClassVar[dict[str, str]] = {
        "find_type_definition": "type_lookup",
        "get_managed_singleton": "managed_singleton",
        "get_native_singleton": "native_singleton",
    }
    CHAIN_CALLS: ClassVar[dict[str, str]] = {
        "get_method": "method_lookup",
        "get_field": "field_read",
        "set_field": "field_write",
        "call": "method_call",
    }

    def __init__(self) -> None:
        self.lexer = LuaLexer()

    def extract(self, source: str, file_key: str) -> list[UsageSite]:
        tokens = list(self.lexer.tokenize(source))
        lines = source.splitlines()
        sites: list[UsageSite] = []
        bindings: dict[str, str] = {}
        chain_counter = 0
        current_chain = f"{file_key}:0"
        last_line = 0
        index = 0
        while index < len(tokens):
            token = tokens[index]
            if token.line != last_line and token.line > last_line:
                chain_counter += 1
                current_chain = f"{file_key}:{chain_counter}"
                last_line = token.line

            sdk_match = self._match_sdk_call(tokens, index)
            if sdk_match is not None:
                function_name, argument, consumed = sdk_match
                kind = self.SDK_CALLS[function_name]
                binding = self._binding_for_statement(tokens, index)
                sites.append(
                    self._site(
                        kind,
                        argument,
                        token,
                        current_chain,
                        lines,
                        receiver="sdk",
                        metadata={
                            "function": function_name,
                            "binding": binding,
                        },
                    )
                )
                if binding:
                    bindings[binding] = argument
                index += consumed
                continue

            chain_match = self._match_chain_call(tokens, index)
            if chain_match is not None:
                function_name, argument, receiver, consumed = chain_match
                kind = self.CHAIN_CALLS[function_name]
                binding = self._binding_for_statement(tokens, index)
                sites.append(
                    self._site(
                        kind,
                        argument,
                        token,
                        current_chain,
                        lines,
                        receiver=receiver,
                        metadata={
                            "function": function_name,
                            "binding": binding,
                            "receiver_binding": bindings.get(receiver or ""),
                        },
                    )
                )
                if binding:
                    bindings[binding] = argument
                index += consumed
                continue

            if self._matches(tokens, index, ["sdk", ".", "hook", "("]):
                hook_expression = self._hook_target(tokens, index + 4)
                hook_symbol = bindings.get(hook_expression, hook_expression) or "sdk.hook"
                sites.append(
                    self._site(
                        "hook_install",
                        hook_symbol,
                        token,
                        current_chain,
                        lines,
                        receiver="sdk",
                        metadata={
                            "target_expression": hook_expression,
                            "resolved_from_binding": hook_expression in bindings,
                        },
                    )
                )
            hook_argument = self._match_hook_argument(tokens, index)
            if hook_argument is not None:
                symbol, argument_index, consumed = hook_argument
                sites.append(
                    self._site(
                        "hook_argument",
                        symbol,
                        token,
                        current_chain,
                        lines,
                        receiver="args",
                        metadata={"argument_index": argument_index},
                    )
                )
                index += consumed
                continue
            collection = self._match_collection_iteration(tokens, index)
            if collection is not None:
                function_name, receiver, consumed = collection
                sites.append(
                    self._site(
                        "collection_iteration",
                        receiver,
                        token,
                        current_chain,
                        lines,
                        receiver=receiver,
                        metadata={"function": function_name},
                    )
                )
                index += consumed
                continue
            index += 1
        return sites

    def _match_sdk_call(
        self,
        tokens: list[Token],
        index: int,
    ) -> tuple[str, str, int] | None:
        if index + 5 >= len(tokens):
            return None
        if (
            tokens[index].value != "sdk"
            or tokens[index + 1].value != "."
            or tokens[index + 2].value not in self.SDK_CALLS
            or tokens[index + 3].value != "("
            or tokens[index + 4].kind != "string"
        ):
            return None
        return tokens[index + 2].value, tokens[index + 4].value, 5

    def _match_chain_call(
        self,
        tokens: list[Token],
        index: int,
    ) -> tuple[str, str, str | None, int] | None:
        if index + 4 >= len(tokens):
            return None
        if (
            tokens[index].value != ":"
            or tokens[index + 1].value not in self.CHAIN_CALLS
            or tokens[index + 2].value != "("
            or tokens[index + 3].kind != "string"
        ):
            return None
        return (
            tokens[index + 1].value,
            tokens[index + 3].value,
            self._receiver_before(tokens, index),
            4,
        )

    @staticmethod
    def _receiver_before(tokens: list[Token], index: int) -> str | None:
        if index <= 0 or tokens[index - 1].kind != "identifier":
            return None
        values = [tokens[index - 1].value]
        cursor = index - 2
        while cursor >= 1 and tokens[cursor].value == "." and tokens[cursor - 1].kind == "identifier":
            values[:0] = [tokens[cursor - 1].value, "."]
            cursor -= 2
        return "".join(values)

    @staticmethod
    def _binding_for_statement(tokens: list[Token], index: int) -> str | None:
        line = tokens[index].line
        start = index
        while start > 0 and tokens[start - 1].line == line:
            start -= 1
        values = tokens[start:index]
        equals = next(
            (position for position, token in enumerate(values) if token.value == "="),
            None,
        )
        if equals is None:
            return None
        left = [token.value for token in values[:equals] if token.value != "local"]
        if not left or any(
            value not in {".", "_"} and not value.replace("_", "").isalnum() for value in left
        ):
            return None
        return "".join(left)

    @staticmethod
    def _hook_target(tokens: list[Token], index: int) -> str:
        if index >= len(tokens):
            return ""
        depth = 0
        for cursor in range(index, min(len(tokens), index + 100)):
            value = tokens[cursor].value
            if value in {"(", "[", "{"}:
                depth += 1
            elif value in {")", "]", "}"}:
                if depth == 0:
                    break
                depth -= 1
            elif value == "," and depth == 0:
                break
            if (
                value == "get_method"
                and cursor + 2 < len(tokens)
                and tokens[cursor + 1].value == "("
                and tokens[cursor + 2].kind == "string"
            ):
                return tokens[cursor + 2].value
        if tokens[index].kind == "identifier":
            values = [tokens[index].value]
            cursor = index + 1
            while (
                cursor + 1 < len(tokens)
                and tokens[cursor].value == "."
                and tokens[cursor + 1].kind == "identifier"
            ):
                values.extend((".", tokens[cursor + 1].value))
                cursor += 2
            return "".join(values)
        return ""

    @staticmethod
    def _match_hook_argument(
        tokens: list[Token],
        index: int,
    ) -> tuple[str, int, int] | None:
        if index + 3 >= len(tokens):
            return None
        if (
            tokens[index].value != "args"
            or tokens[index + 1].value != "["
            or tokens[index + 2].kind != "number"
            or tokens[index + 3].value != "]"
        ):
            return None
        try:
            lua_index = int(float(tokens[index + 2].value))
        except ValueError:
            return None
        return f"args[{lua_index}]", lua_index, 4

    @staticmethod
    def _match_collection_iteration(
        tokens: list[Token],
        index: int,
    ) -> tuple[str, str, int] | None:
        if index + 2 >= len(tokens):
            return None
        if tokens[index].value not in {"pairs", "ipairs"} or tokens[index + 1].value != "(":
            return None
        if tokens[index + 2].kind != "identifier":
            return None
        values = [tokens[index + 2].value]
        cursor = index + 3
        while (
            cursor + 1 < len(tokens)
            and tokens[cursor].value == "."
            and tokens[cursor + 1].kind == "identifier"
        ):
            values.extend((".", tokens[cursor + 1].value))
            cursor += 2
        return tokens[index].value, "".join(values), cursor - index

    @staticmethod
    def _matches(tokens: list[Token], index: int, values: list[str]) -> bool:
        if index + len(values) > len(tokens):
            return False
        return all(tokens[index + offset].value == value for offset, value in enumerate(values))

    @staticmethod
    def _site(
        kind: str,
        symbol: str,
        token: Token,
        chain_id: str,
        lines: list[str],
        *,
        receiver: str | None,
        metadata: dict[str, object],
    ) -> UsageSite:
        excerpt = lines[token.line - 1].strip() if 0 < token.line <= len(lines) else ""
        return UsageSite(
            kind=kind,
            symbol=symbol,
            line=token.line,
            column=token.column,
            receiver=receiver,
            chain_id=chain_id,
            source_excerpt=excerpt,
            metadata=metadata,
        )


class UsageIndexer:
    def __init__(self, database: Database) -> None:
        self.database = database
        self.extractor = LuaUsageExtractor()

    def index_project(self, root: Path, *, game_id: str | None = None) -> dict[str, object]:
        resolved = root.resolve()
        files = sorted(path for path in resolved.rglob("*.lua") if path.is_file())
        digest = hashlib.sha256()
        extracted: list[tuple[Path, UsageSite]] = []
        for path in files:
            content = path.read_text(encoding="utf-8-sig")
            relative = path.relative_to(resolved).as_posix()
            digest.update(relative.encode("utf-8"))
            digest.update(content.encode("utf-8"))
            for site in self.extractor.extract(content, relative):
                extracted.append((path, site))

        source_hash = digest.hexdigest()
        project_id = f"usage-project:{hashlib.sha256(str(resolved).encode('utf-8')).hexdigest()}"
        indexed_at = datetime.now(UTC).isoformat()
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                INSERT INTO usage_projects(project_id, root_path, game_id, source_hash, indexed_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(project_id) DO UPDATE SET
                    root_path=excluded.root_path,
                    game_id=excluded.game_id,
                    source_hash=excluded.source_hash,
                    indexed_at=excluded.indexed_at
                """,
                (project_id, str(resolved), game_id, source_hash, indexed_at),
            )
            old_rows = connection.execute(
                "SELECT usage_pk FROM usage_sites WHERE project_id = ?",
                (project_id,),
            ).fetchall()
            for row in old_rows:
                connection.execute(
                    "DELETE FROM usage_fts WHERE usage_pk = ?",
                    (row["usage_pk"],),
                )
            connection.execute("DELETE FROM usage_sites WHERE project_id = ?", (project_id,))
            connection.execute("DELETE FROM usage_edges WHERE project_id = ?", (project_id,))
            connection.execute("DELETE FROM root_hints WHERE project_id = ?", (project_id,))

            indexed_sites: list[tuple[str, UsageSite]] = []
            for path, site in extracted:
                relative = path.relative_to(resolved).as_posix()
                connection.execute(
                    """
                    INSERT INTO usage_sites(
                        project_id, file_path, line, column_number, usage_kind,
                        symbol, receiver, chain_id, source_excerpt, metadata_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        project_id,
                        relative,
                        site.line,
                        site.column,
                        site.kind,
                        site.symbol,
                        site.receiver,
                        site.chain_id,
                        site.source_excerpt,
                        json.dumps(site.metadata, ensure_ascii=False),
                    ),
                )
                usage_pk = int(connection.execute("SELECT last_insert_rowid()").fetchone()[0])
                connection.execute(
                    """
                    INSERT INTO usage_fts(usage_pk, symbol, source_excerpt, file_path)
                    VALUES (?, ?, ?, ?)
                    """,
                    (usage_pk, site.symbol, site.source_excerpt, relative),
                )
                indexed_sites.append((relative, site))
                if site.kind in {"managed_singleton", "native_singleton"}:
                    connection.execute(
                        """
                        INSERT OR IGNORE INTO root_hints(
                            project_id, root_kind, type_name, evidence
                        ) VALUES (?, ?, ?, ?)
                        """,
                        (
                            project_id,
                            site.kind,
                            site.symbol,
                            f"{relative}:{site.line}",
                        ),
                    )
            self._insert_usage_edges(connection, project_id, indexed_sites)
            connection.commit()
        return {
            "project_id": project_id,
            "root_path": str(resolved),
            "source_hash": source_hash,
            "file_count": len(files),
            "usage_count": len(extracted),
        }

    @staticmethod
    def _insert_usage_edges(
        connection: Any,
        project_id: str,
        sites: list[tuple[str, UsageSite]],
    ) -> None:
        edge_for_kind = {
            "type_lookup": "CODE_USES_TYPE",
            "managed_singleton": "CODE_ACQUIRES_ROOT",
            "native_singleton": "CODE_ACQUIRES_ROOT",
            "method_lookup": "CODE_CALLS_MEMBER",
            "method_call": "CODE_CALLS_MEMBER",
            "field_read": "CODE_READS_FIELD",
            "field_write": "CODE_WRITES_FIELD",
            "hook_install": "CODE_HOOKS_MEMBER",
            "hook_argument": "CODE_USES_HOOK_ARGUMENT",
            "collection_iteration": "CODE_ITERATES_COLLECTION",
        }
        chains: dict[tuple[str, str], list[UsageSite]] = {}
        for file_path, site in sites:
            source = site.receiver or f"code:{file_path}:{site.line}"
            metadata = dict(site.metadata)
            connection.execute(
                """
                INSERT INTO usage_edges(
                    project_id, file_path, chain_id, source_symbol, target_symbol,
                    edge_kind, source_line, target_line, metadata_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    project_id,
                    file_path,
                    site.chain_id,
                    source,
                    site.symbol,
                    edge_for_kind.get(site.kind, "CODE_USES_SYMBOL"),
                    site.line,
                    site.line,
                    json.dumps(metadata, ensure_ascii=False),
                ),
            )
            binding = metadata.get("binding")
            if binding:
                connection.execute(
                    """
                    INSERT INTO usage_edges(
                        project_id, file_path, chain_id, source_symbol, target_symbol,
                        edge_kind, source_line, target_line, metadata_json
                    ) VALUES (?, ?, ?, ?, ?, 'CODE_BINDS_SYMBOL', ?, ?, ?)
                    """,
                    (
                        project_id,
                        file_path,
                        site.chain_id,
                        str(binding),
                        site.symbol,
                        site.line,
                        site.line,
                        "{}",
                    ),
                )
            chains.setdefault((file_path, site.chain_id), []).append(site)

        for (file_path, chain_id), chain_sites in chains.items():
            for source_site, target_site in pairwise(chain_sites):
                connection.execute(
                    """
                    INSERT INTO usage_edges(
                        project_id, file_path, chain_id, source_symbol, target_symbol,
                        edge_kind, source_line, target_line, metadata_json
                    ) VALUES (?, ?, ?, ?, ?, 'CODE_FOLLOWS_EDGE', ?, ?, ?)
                    """,
                    (
                        project_id,
                        file_path,
                        chain_id,
                        source_site.symbol,
                        target_site.symbol,
                        source_site.line,
                        target_site.line,
                        json.dumps(
                            {
                                "source_kind": source_site.kind,
                                "target_kind": target_site.kind,
                            },
                            ensure_ascii=False,
                        ),
                    ),
                )

    def search(
        self,
        query: str,
        *,
        game_id: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> dict[str, object]:
        clauses = ["(u.symbol LIKE ? COLLATE NOCASE OR u.source_excerpt LIKE ? COLLATE NOCASE)"]
        params: list[object] = [f"%{query}%", f"%{query}%"]
        if game_id:
            clauses.append("p.game_id = ?")
            params.append(game_id)
        bounded = max(1, min(limit, 200))
        params.extend((bounded + 1, max(0, offset)))
        with self.database.connect() as connection:
            rows = connection.execute(
                f"""
                SELECT u.*, p.root_path, p.game_id, p.source_hash
                FROM usage_sites u
                JOIN usage_projects p ON p.project_id = u.project_id
                WHERE {" AND ".join(clauses)}
                ORDER BY u.symbol, p.root_path, u.file_path, u.line
                LIMIT ? OFFSET ?
                """,
                params,
            ).fetchall()
        has_more = len(rows) > bounded
        items = []
        for row in rows[:bounded]:
            item = {str(key): row[index] for index, key in enumerate(row.keys())}
            item["metadata"] = json.loads(item.pop("metadata_json"))
            item["absolute_path"] = str(Path(item["root_path"]) / item["file_path"])
            items.append(item)
        return {
            "items": items,
            "next_offset": max(0, offset) + bounded if has_more else None,
        }
