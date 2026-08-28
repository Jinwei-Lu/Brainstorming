#!/usr/bin/env python3
"""Dependency-free MCP server for a durable, MCTS-inspired idea tree."""

from __future__ import annotations

import json
import math
import sqlite3
import sys
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator


SERVER_NAME = "brainstorming-tree"
SERVER_VERSION = "0.1.0"
DATABASE_DIR = ".idea-tree"
DATABASE_FILE = "ideas.sqlite3"
SCHEMA_VERSION = 1
SUPPORTED_PROTOCOLS = (
    "2026-07-28",
    "2025-11-25",
    "2025-06-18",
    "2025-03-26",
    "2024-11-05",
)
NODE_KINDS = ("root", "branch", "idea", "synthesis")
NODE_STATUSES = ("open", "survived", "rejected", "blocked", "finalist", "deleted")
LIVE_STATUSES = ("open", "survived")
TREE_STATUSES = ("active", "completed", "archived")


SCHEMA_SQL = """
CREATE TABLE trees (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    id TEXT NOT NULL UNIQUE,
    title TEXT NOT NULL,
    goal TEXT NOT NULL,
    root_node_id TEXT NOT NULL UNIQUE,
    exploration_constant REAL NOT NULL CHECK(exploration_constant > 0),
    status TEXT NOT NULL CHECK(status IN ('active', 'completed', 'archived')),
    version INTEGER NOT NULL DEFAULT 1 CHECK(version > 0),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE nodes (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    id TEXT NOT NULL UNIQUE,
    tree_id TEXT NOT NULL REFERENCES trees(id) ON DELETE CASCADE,
    parent_id TEXT REFERENCES nodes(id),
    kind TEXT NOT NULL CHECK(kind IN ('root', 'branch', 'idea', 'synthesis')),
    title TEXT NOT NULL,
    content TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('open', 'survived', 'rejected', 'blocked', 'finalist', 'deleted')),
    prior REAL NOT NULL DEFAULT 1.0 CHECK(prior >= 0 AND prior <= 1),
    visits INTEGER NOT NULL DEFAULT 0 CHECK(visits >= 0),
    value_sum REAL NOT NULL DEFAULT 0,
    version INTEGER NOT NULL DEFAULT 1 CHECK(version > 0),
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    deleted_at TEXT
);

CREATE UNIQUE INDEX one_root_per_tree
ON nodes(tree_id)
WHERE parent_id IS NULL;

CREATE INDEX nodes_by_parent ON nodes(tree_id, parent_id, seq);
CREATE INDEX nodes_by_status ON nodes(tree_id, status, seq);

CREATE TABLE evaluations (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    id TEXT NOT NULL UNIQUE,
    tree_id TEXT NOT NULL REFERENCES trees(id) ON DELETE CASCADE,
    node_id TEXT NOT NULL REFERENCES nodes(id),
    value REAL NOT NULL CHECK(value >= -1 AND value <= 1),
    rationale TEXT NOT NULL,
    evidence_json TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0, 1)),
    created_at TEXT NOT NULL,
    invalidated_at TEXT,
    invalidation_reason TEXT
);

CREATE INDEX evaluations_by_node ON evaluations(tree_id, node_id, seq);
CREATE INDEX active_evaluations ON evaluations(tree_id, active, seq);

CREATE TABLE events (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    tree_id TEXT NOT NULL REFERENCES trees(id) ON DELETE CASCADE,
    node_id TEXT REFERENCES nodes(id),
    operation TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX events_by_tree ON events(tree_id, seq);
CREATE INDEX events_by_node ON events(tree_id, node_id, seq);

PRAGMA user_version = 1;
"""


class ToolFailure(Exception):
    """A user-correctable tool error."""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


def compact_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def require_text(args: dict[str, Any], key: str) -> str:
    value = args.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ToolFailure(f"`{key}` must be a non-empty string")
    return value.strip()


def optional_text(args: dict[str, Any], key: str) -> str | None:
    if key not in args:
        return None
    return require_text(args, key)


def require_integer(args: dict[str, Any], key: str, minimum: int | None = None) -> int:
    value = args.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ToolFailure(f"`{key}` must be an integer")
    if minimum is not None and value < minimum:
        raise ToolFailure(f"`{key}` must be at least {minimum}")
    return value


def optional_integer(
    args: dict[str, Any], key: str, default: int, minimum: int, maximum: int
) -> int:
    if key not in args:
        return default
    value = require_integer(args, key, minimum)
    if value > maximum:
        raise ToolFailure(f"`{key}` must be at most {maximum}")
    return value


def optional_boolean(args: dict[str, Any], key: str, default: bool) -> bool:
    if key not in args:
        return default
    value = args[key]
    if not isinstance(value, bool):
        raise ToolFailure(f"`{key}` must be a boolean")
    return value


def require_number(args: dict[str, Any], key: str) -> float:
    value = args.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ToolFailure(f"`{key}` must be a number")
    result = float(value)
    if not math.isfinite(result):
        raise ToolFailure(f"`{key}` must be finite")
    return result


def optional_number(args: dict[str, Any], key: str, default: float) -> float:
    if key not in args:
        return default
    return require_number(args, key)


def optional_object(args: dict[str, Any], key: str, default: dict[str, Any]) -> dict[str, Any]:
    if key not in args:
        return dict(default)
    value = args[key]
    if not isinstance(value, dict):
        raise ToolFailure(f"`{key}` must be an object")
    return value


def require_string_list(args: dict[str, Any], key: str) -> list[str]:
    value = args.get(key)
    if not isinstance(value, list) or not value:
        raise ToolFailure(f"`{key}` must be a non-empty array of strings")
    result: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise ToolFailure(f"every `{key}` item must be a non-empty string")
        result.append(item.strip())
    return result


def enum_value(args: dict[str, Any], key: str, allowed: tuple[str, ...]) -> str:
    value = require_text(args, key)
    if value not in allowed:
        raise ToolFailure(f"`{key}` must be one of: {', '.join(allowed)}")
    return value


class Store:
    def __init__(self, workspace: str, allow_create: bool = False):
        raw_path = Path(workspace).expanduser()
        if not raw_path.is_absolute():
            raise ToolFailure("`workspace` must be an absolute directory path")
        try:
            self.workspace = raw_path.resolve(strict=True)
        except OSError as exc:
            raise ToolFailure(f"workspace does not exist: {raw_path}") from exc
        if not self.workspace.is_dir():
            raise ToolFailure(f"workspace is not a directory: {self.workspace}")

        self.database_dir = self.workspace / DATABASE_DIR
        self.database_path = self.database_dir / DATABASE_FILE
        self.allow_create = allow_create
        if allow_create:
            self.database_dir.mkdir(parents=False, exist_ok=True)
        elif not self.database_path.is_file():
            raise ToolFailure(
                f"no idea-tree database exists at {self.database_path}; create a tree first"
            )

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(
            self.database_path,
            timeout=10,
            isolation_level=None,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 10000")
        connection.execute("PRAGMA journal_mode = WAL")
        version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        if version == 0:
            if not self.allow_create:
                connection.close()
                raise ToolFailure(f"database at {self.database_path} is not initialized")
            connection.executescript(SCHEMA_SQL)
        elif version != SCHEMA_VERSION:
            connection.close()
            raise ToolFailure(
                f"database schema version {version} is unsupported; expected {SCHEMA_VERSION}"
            )
        try:
            yield connection
        finally:
            connection.close()


@contextmanager
def write_transaction(connection: sqlite3.Connection) -> Iterator[None]:
    connection.execute("BEGIN IMMEDIATE")
    try:
        yield
    except Exception:
        connection.rollback()
        raise
    else:
        connection.commit()


def open_store(args: dict[str, Any], allow_create: bool = False) -> Store:
    return Store(require_text(args, "workspace"), allow_create=allow_create)


def get_tree_row(connection: sqlite3.Connection, tree_id: str) -> sqlite3.Row:
    row = connection.execute("SELECT * FROM trees WHERE id = ?", (tree_id,)).fetchone()
    if row is None:
        raise ToolFailure(f"tree not found: {tree_id}")
    return row


def get_node_row(
    connection: sqlite3.Connection, tree_id: str, node_id: str
) -> sqlite3.Row:
    row = connection.execute(
        "SELECT * FROM nodes WHERE tree_id = ? AND id = ?",
        (tree_id, node_id),
    ).fetchone()
    if row is None:
        raise ToolFailure(f"node not found in tree {tree_id}: {node_id}")
    return row


def ensure_tree_writable(tree: sqlite3.Row) -> None:
    if tree["status"] != "active":
        raise ToolFailure(
            f"tree {tree['id']} is {tree['status']}; set it to `active` before mutating nodes"
        )


def decode_json(value: str, fallback: Any) -> Any:
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return fallback


def tree_dict(row: sqlite3.Row) -> dict[str, Any]:
    return dict(row)


def node_dict(row: sqlite3.Row) -> dict[str, Any]:
    result = dict(row)
    result["metadata"] = decode_json(result.pop("metadata_json"), {})
    visits = result["visits"]
    result["mean_value"] = result["value_sum"] / visits if visits else None
    return result


def evaluation_dict(row: sqlite3.Row) -> dict[str, Any]:
    result = dict(row)
    result["active"] = bool(result["active"])
    result["evidence"] = decode_json(result.pop("evidence_json"), [])
    return result


def event_dict(row: sqlite3.Row) -> dict[str, Any]:
    result = dict(row)
    result["payload"] = decode_json(result.pop("payload_json"), {})
    return result


def append_event(
    connection: sqlite3.Connection,
    tree_id: str,
    node_id: str | None,
    operation: str,
    payload: dict[str, Any],
    created_at: str,
) -> int:
    cursor = connection.execute(
        "INSERT INTO events(tree_id, node_id, operation, payload_json, created_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (tree_id, node_id, operation, compact_json(payload), created_at),
    )
    return int(cursor.lastrowid)


def ancestor_path(
    connection: sqlite3.Connection, tree_id: str, node_id: str
) -> list[str]:
    rows = connection.execute(
        "SELECT id, parent_id FROM nodes WHERE tree_id = ?",
        (tree_id,),
    ).fetchall()
    parent_by_id = {row["id"]: row["parent_id"] for row in rows}
    if node_id not in parent_by_id:
        raise ToolFailure(f"node not found in tree {tree_id}: {node_id}")
    result: list[str] = []
    current: str | None = node_id
    seen: set[str] = set()
    while current is not None:
        if current in seen:
            raise ToolFailure("corrupt idea tree: cycle detected")
        seen.add(current)
        result.append(current)
        current = parent_by_id.get(current)
    return result


def rebuild_stats(connection: sqlite3.Connection, tree_id: str, timestamp: str) -> list[str]:
    nodes = connection.execute(
        "SELECT id, parent_id, visits, value_sum FROM nodes WHERE tree_id = ?",
        (tree_id,),
    ).fetchall()
    parent_by_id = {row["id"]: row["parent_id"] for row in nodes}
    computed = {row["id"]: [0, 0.0] for row in nodes}
    evaluations = connection.execute(
        "SELECT e.node_id, e.value FROM evaluations e "
        "JOIN nodes n ON n.id = e.node_id "
        "WHERE e.tree_id = ? AND e.active = 1 AND n.deleted_at IS NULL",
        (tree_id,),
    ).fetchall()
    for evaluation in evaluations:
        current: str | None = evaluation["node_id"]
        seen: set[str] = set()
        while current is not None and current in computed:
            if current in seen:
                raise ToolFailure("corrupt idea tree: cycle detected while rebuilding statistics")
            seen.add(current)
            computed[current][0] += 1
            computed[current][1] += float(evaluation["value"])
            current = parent_by_id[current]

    changed: list[str] = []
    for row in nodes:
        visits, value_sum = computed[row["id"]]
        if row["visits"] != visits or not math.isclose(
            float(row["value_sum"]), value_sum, rel_tol=1e-12, abs_tol=1e-12
        ):
            connection.execute(
                "UPDATE nodes SET visits = ?, value_sum = ?, version = version + 1, "
                "updated_at = ? WHERE id = ?",
                (visits, value_sum, timestamp, row["id"]),
            )
            changed.append(row["id"])
    return changed


def handle_create_tree(args: dict[str, Any]) -> dict[str, Any]:
    title = require_text(args, "title")
    goal = require_text(args, "goal")
    exploration_constant = optional_number(args, "exploration_constant", math.sqrt(2.0))
    if exploration_constant <= 0 or exploration_constant > 10:
        raise ToolFailure("`exploration_constant` must be greater than 0 and at most 10")

    store = open_store(args, allow_create=True)
    tree_id = new_id("tree")
    root_id = new_id("node")
    timestamp = utc_now()
    with store.connect() as connection, write_transaction(connection):
        connection.execute(
            "INSERT INTO trees(id, title, goal, root_node_id, exploration_constant, status, "
            "version, created_at, updated_at) VALUES (?, ?, ?, ?, ?, 'active', 1, ?, ?)",
            (tree_id, title, goal, root_id, exploration_constant, timestamp, timestamp),
        )
        connection.execute(
            "INSERT INTO nodes(id, tree_id, parent_id, kind, title, content, status, prior, "
            "visits, value_sum, version, metadata_json, created_at, updated_at) "
            "VALUES (?, ?, NULL, 'root', ?, ?, 'open', 1.0, 0, 0, 1, ?, ?, ?)",
            (
                root_id,
                tree_id,
                title,
                goal,
                compact_json({"role": "frozen_goal"}),
                timestamp,
                timestamp,
            ),
        )
        event_seq = append_event(
            connection,
            tree_id,
            root_id,
            "tree.created",
            {
                "title": title,
                "goal": goal,
                "root_node_id": root_id,
                "exploration_constant": exploration_constant,
            },
            timestamp,
        )
        tree = tree_dict(get_tree_row(connection, tree_id))
        root = node_dict(get_node_row(connection, tree_id, root_id))
    return {
        "database_path": str(store.database_path),
        "tree": tree,
        "root": root,
        "event_seq": event_seq,
    }


def handle_list_trees(args: dict[str, Any]) -> dict[str, Any]:
    store = open_store(args)
    include_archived = optional_boolean(args, "include_archived", True)
    with store.connect() as connection:
        where = "" if include_archived else "WHERE t.status != 'archived'"
        rows = connection.execute(
            "SELECT t.*, "
            "SUM(CASE WHEN n.deleted_at IS NULL THEN 1 ELSE 0 END) AS active_node_count, "
            "SUM(CASE WHEN n.deleted_at IS NOT NULL THEN 1 ELSE 0 END) AS deleted_node_count, "
            "(SELECT COUNT(*) FROM evaluations e WHERE e.tree_id = t.id AND e.active = 1) "
            "AS active_evaluation_count "
            "FROM trees t LEFT JOIN nodes n ON n.tree_id = t.id "
            f"{where} GROUP BY t.seq ORDER BY t.seq"
        ).fetchall()
    return {
        "database_path": str(store.database_path),
        "trees": [tree_dict(row) for row in rows],
    }


def handle_snapshot(args: dict[str, Any]) -> dict[str, Any]:
    tree_id = require_text(args, "tree_id")
    include_deleted = optional_boolean(args, "include_deleted", False)
    max_depth = optional_integer(args, "max_depth", 50, 0, 500)
    max_nodes = optional_integer(args, "max_nodes", 200, 1, 1000)
    store = open_store(args)
    with store.connect() as connection:
        tree = tree_dict(get_tree_row(connection, tree_id))
        deleted_filter = "" if include_deleted else "WHERE deleted_at IS NULL"
        rows = connection.execute(
            "WITH RECURSIVE walk AS ("
            " SELECT n.*, 0 AS walk_depth, printf('%020d', n.seq) AS walk_path"
            " FROM nodes n WHERE n.tree_id = ? AND n.id = ?"
            " UNION ALL"
            " SELECT n.*, walk.walk_depth + 1,"
            "        walk.walk_path || '.' || printf('%020d', n.seq)"
            " FROM nodes n JOIN walk ON n.parent_id = walk.id"
            " WHERE n.tree_id = ? AND walk.walk_depth < ?"
            ") SELECT * FROM walk "
            f"{deleted_filter} ORDER BY walk_path LIMIT ?",
            (tree_id, tree["root_node_id"], tree_id, max_depth, max_nodes + 1),
        ).fetchall()
        truncated = len(rows) > max_nodes
        rows = rows[:max_nodes]
        counts = connection.execute(
            "SELECT COUNT(*) AS total_nodes, "
            "SUM(CASE WHEN deleted_at IS NULL THEN 1 ELSE 0 END) AS active_nodes, "
            "SUM(CASE WHEN deleted_at IS NOT NULL THEN 1 ELSE 0 END) AS deleted_nodes "
            "FROM nodes WHERE tree_id = ?",
            (tree_id,),
        ).fetchone()
        recent_rows = connection.execute(
            "SELECT * FROM events WHERE tree_id = ? ORDER BY seq DESC LIMIT 10",
            (tree_id,),
        ).fetchall()
    return {
        "database_path": str(store.database_path),
        "tree": tree,
        "counts": dict(counts),
        "nodes": [node_dict(row) for row in rows],
        "truncated": truncated,
        "recent_events": [event_dict(row) for row in reversed(recent_rows)],
    }


def handle_update_tree(args: dict[str, Any]) -> dict[str, Any]:
    tree_id = require_text(args, "tree_id")
    expected_version = require_integer(args, "expected_version", 1)
    changes: dict[str, Any] = {}
    if "title" in args:
        changes["title"] = require_text(args, "title")
    if "status" in args:
        changes["status"] = enum_value(args, "status", TREE_STATUSES)
    if not changes:
        raise ToolFailure("provide at least one mutable field: `title` or `status`")

    store = open_store(args)
    timestamp = utc_now()
    with store.connect() as connection, write_transaction(connection):
        before = get_tree_row(connection, tree_id)
        if before["version"] != expected_version:
            raise ToolFailure(
                f"version conflict for tree {tree_id}: expected {expected_version}, "
                f"current {before['version']}"
            )
        assignments = [f"{key} = ?" for key in changes]
        values = list(changes.values()) + [timestamp, tree_id, expected_version]
        cursor = connection.execute(
            f"UPDATE trees SET {', '.join(assignments)}, updated_at = ?, "
            "version = version + 1 WHERE id = ? AND version = ?",
            values,
        )
        if cursor.rowcount != 1:
            raise ToolFailure(f"tree update lost a concurrent write: {tree_id}")
        after = get_tree_row(connection, tree_id)
        event_seq = append_event(
            connection,
            tree_id,
            before["root_node_id"],
            "tree.updated",
            {
                "before": {key: before[key] for key in changes},
                "after": {key: after[key] for key in changes},
                "version": after["version"],
            },
            timestamp,
        )
    return {"tree": tree_dict(after), "event_seq": event_seq}


def handle_create_node(args: dict[str, Any]) -> dict[str, Any]:
    tree_id = require_text(args, "tree_id")
    parent_id = require_text(args, "parent_id")
    kind = enum_value(args, "kind", tuple(value for value in NODE_KINDS if value != "root"))
    title = require_text(args, "title")
    content = require_text(args, "content")
    prior = optional_number(args, "prior", 1.0)
    if prior < 0 or prior > 1:
        raise ToolFailure("`prior` must be between 0 and 1")
    metadata = optional_object(args, "metadata", {})

    store = open_store(args)
    node_id = new_id("node")
    timestamp = utc_now()
    with store.connect() as connection, write_transaction(connection):
        tree = get_tree_row(connection, tree_id)
        ensure_tree_writable(tree)
        parent = get_node_row(connection, tree_id, parent_id)
        if parent["deleted_at"] is not None:
            raise ToolFailure(f"cannot add a child to deleted node {parent_id}")
        connection.execute(
            "INSERT INTO nodes(id, tree_id, parent_id, kind, title, content, status, prior, "
            "visits, value_sum, version, metadata_json, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, 'open', ?, 0, 0, 1, ?, ?, ?)",
            (
                node_id,
                tree_id,
                parent_id,
                kind,
                title,
                content,
                prior,
                compact_json(metadata),
                timestamp,
                timestamp,
            ),
        )
        event_seq = append_event(
            connection,
            tree_id,
            node_id,
            "node.created",
            {
                "parent_id": parent_id,
                "kind": kind,
                "title": title,
                "prior": prior,
            },
            timestamp,
        )
        node = node_dict(get_node_row(connection, tree_id, node_id))
    return {"node": node, "event_seq": event_seq}


def handle_get_node(args: dict[str, Any]) -> dict[str, Any]:
    tree_id = require_text(args, "tree_id")
    node_id = require_text(args, "node_id")
    history_limit = optional_integer(args, "history_limit", 20, 0, 200)
    store = open_store(args)
    with store.connect() as connection:
        get_tree_row(connection, tree_id)
        node = node_dict(get_node_row(connection, tree_id, node_id))
        evaluation_rows = connection.execute(
            "SELECT * FROM evaluations WHERE tree_id = ? AND node_id = ? "
            "ORDER BY seq DESC LIMIT ?",
            (tree_id, node_id, history_limit),
        ).fetchall()
        event_rows = connection.execute(
            "SELECT * FROM events WHERE tree_id = ? AND node_id = ? "
            "ORDER BY seq DESC LIMIT ?",
            (tree_id, node_id, history_limit),
        ).fetchall()
    return {
        "node": node,
        "evaluations": [evaluation_dict(row) for row in reversed(evaluation_rows)],
        "events": [event_dict(row) for row in reversed(event_rows)],
    }


def handle_list_nodes(args: dict[str, Any]) -> dict[str, Any]:
    tree_id = require_text(args, "tree_id")
    include_deleted = optional_boolean(args, "include_deleted", False)
    after_seq = optional_integer(args, "after_seq", 0, 0, 2_147_483_647)
    limit = optional_integer(args, "limit", 100, 1, 500)
    clauses = ["tree_id = ?", "seq > ?"]
    parameters: list[Any] = [tree_id, after_seq]
    if not include_deleted:
        clauses.append("deleted_at IS NULL")
    if "parent_id" in args:
        clauses.append("parent_id = ?")
        parameters.append(require_text(args, "parent_id"))
    if "kind" in args:
        clauses.append("kind = ?")
        parameters.append(enum_value(args, "kind", NODE_KINDS))
    if "status" in args:
        clauses.append("status = ?")
        parameters.append(enum_value(args, "status", NODE_STATUSES))
    if "query" in args:
        query = require_text(args, "query")
        clauses.append("(title LIKE ? OR content LIKE ? OR metadata_json LIKE ?)")
        like = f"%{query}%"
        parameters.extend([like, like, like])
    parameters.append(limit)

    store = open_store(args)
    with store.connect() as connection:
        get_tree_row(connection, tree_id)
        rows = connection.execute(
            f"SELECT * FROM nodes WHERE {' AND '.join(clauses)} ORDER BY seq LIMIT ?",
            parameters,
        ).fetchall()
    return {
        "nodes": [node_dict(row) for row in rows],
        "next_after_seq": rows[-1]["seq"] if len(rows) == limit else None,
    }


def handle_update_node(args: dict[str, Any]) -> dict[str, Any]:
    tree_id = require_text(args, "tree_id")
    node_id = require_text(args, "node_id")
    expected_version = require_integer(args, "expected_version", 1)
    changes: dict[str, Any] = {}
    for key in ("title", "content"):
        if key in args:
            changes[key] = require_text(args, key)
    if "status" in args:
        changes["status"] = enum_value(
            args,
            "status",
            tuple(value for value in NODE_STATUSES if value != "deleted"),
        )
    if "prior" in args:
        prior = require_number(args, "prior")
        if prior < 0 or prior > 1:
            raise ToolFailure("`prior` must be between 0 and 1")
        changes["prior"] = prior
    metadata_patch = None
    if "metadata_patch" in args:
        metadata_patch = optional_object(args, "metadata_patch", {})
    if not changes and metadata_patch is None:
        raise ToolFailure(
            "provide at least one mutable field: `title`, `content`, `status`, `prior`, "
            "or `metadata_patch`"
        )

    store = open_store(args)
    timestamp = utc_now()
    with store.connect() as connection, write_transaction(connection):
        tree = get_tree_row(connection, tree_id)
        ensure_tree_writable(tree)
        before = get_node_row(connection, tree_id, node_id)
        if before["kind"] == "root":
            raise ToolFailure("the root contains the frozen goal and cannot be updated")
        if before["deleted_at"] is not None:
            raise ToolFailure(f"deleted node {node_id} cannot be updated")
        if before["version"] != expected_version:
            raise ToolFailure(
                f"version conflict for node {node_id}: expected {expected_version}, "
                f"current {before['version']}"
            )
        if metadata_patch is not None:
            metadata = decode_json(before["metadata_json"], {})
            for key, value in metadata_patch.items():
                if value is None:
                    metadata.pop(key, None)
                else:
                    metadata[key] = value
            changes["metadata_json"] = compact_json(metadata)

        assignments = [f"{key} = ?" for key in changes]
        values = list(changes.values()) + [timestamp, node_id, expected_version]
        cursor = connection.execute(
            f"UPDATE nodes SET {', '.join(assignments)}, updated_at = ?, "
            "version = version + 1 WHERE id = ? AND version = ?",
            values,
        )
        if cursor.rowcount != 1:
            raise ToolFailure(f"node update lost a concurrent write: {node_id}")
        after = get_node_row(connection, tree_id, node_id)
        visible_keys = [key for key in changes if key != "metadata_json"]
        event_payload: dict[str, Any] = {
            "before": {key: before[key] for key in visible_keys},
            "after": {key: after[key] for key in visible_keys},
            "version": after["version"],
        }
        if metadata_patch is not None:
            event_payload["metadata_patch"] = metadata_patch
        event_seq = append_event(
            connection,
            tree_id,
            node_id,
            "node.updated",
            event_payload,
            timestamp,
        )
        result = node_dict(after)
    return {"node": result, "event_seq": event_seq}


def handle_delete_node(args: dict[str, Any]) -> dict[str, Any]:
    tree_id = require_text(args, "tree_id")
    node_id = require_text(args, "node_id")
    expected_version = require_integer(args, "expected_version", 1)
    cascade = optional_boolean(args, "cascade", False)
    reason = require_text(args, "reason")
    store = open_store(args)
    timestamp = utc_now()

    with store.connect() as connection, write_transaction(connection):
        tree = get_tree_row(connection, tree_id)
        ensure_tree_writable(tree)
        node = get_node_row(connection, tree_id, node_id)
        if node["kind"] == "root":
            raise ToolFailure("the root node cannot be deleted")
        if node["deleted_at"] is not None:
            raise ToolFailure(f"node is already deleted: {node_id}")
        if node["version"] != expected_version:
            raise ToolFailure(
                f"version conflict for node {node_id}: expected {expected_version}, "
                f"current {node['version']}"
            )
        descendants = connection.execute(
            "WITH RECURSIVE subtree(id) AS ("
            " SELECT id FROM nodes WHERE tree_id = ? AND id = ?"
            " UNION ALL"
            " SELECT n.id FROM nodes n JOIN subtree s ON n.parent_id = s.id"
            " WHERE n.tree_id = ? AND n.deleted_at IS NULL"
            ") SELECT id FROM subtree",
            (tree_id, node_id, tree_id),
        ).fetchall()
        ids = [row["id"] for row in descendants]
        if len(ids) > 1 and not cascade:
            raise ToolFailure(
                f"node {node_id} has {len(ids) - 1} active descendants; "
                "set `cascade` to true only if the whole subtree should be tombstoned"
            )
        if not cascade:
            ids = [node_id]

        placeholders = ",".join("?" for _ in ids)
        status_rows = connection.execute(
            f"SELECT id, status FROM nodes WHERE id IN ({placeholders})",
            ids,
        ).fetchall()
        previous_statuses = {row["id"]: row["status"] for row in status_rows}
        connection.execute(
            f"UPDATE nodes SET status = 'deleted', deleted_at = ?, updated_at = ?, "
            f"version = version + 1 WHERE id IN ({placeholders})",
            [timestamp, timestamp, *ids],
        )
        invalidation_reason = f"node subtree tombstoned: {node_id}"
        cursor = connection.execute(
            f"UPDATE evaluations SET active = 0, invalidated_at = ?, invalidation_reason = ? "
            f"WHERE active = 1 AND node_id IN ({placeholders})",
            [timestamp, invalidation_reason, *ids],
        )
        stats_changed = rebuild_stats(connection, tree_id, timestamp)
        event_seq = append_event(
            connection,
            tree_id,
            node_id,
            "node.deleted",
            {
                "reason": reason,
                "cascade": cascade,
                "tombstoned_ids": ids,
                "previous_statuses": previous_statuses,
                "invalidated_evaluations": cursor.rowcount,
                "stats_changed_ids": stats_changed,
            },
            timestamp,
        )
        deleted_rows = connection.execute(
            f"SELECT * FROM nodes WHERE id IN ({placeholders}) ORDER BY seq",
            ids,
        ).fetchall()
    return {
        "tombstoned_nodes": [node_dict(row) for row in deleted_rows],
        "records_physically_erased": False,
        "event_seq": event_seq,
    }


def uct_candidate(parent: sqlite3.Row, child: sqlite3.Row, constant: float) -> dict[str, Any]:
    visits = int(child["visits"])
    if visits == 0:
        return {
            "node_id": child["id"],
            "title": child["title"],
            "visits": 0,
            "mean_value": None,
            "exploration_bonus": None,
            "uct_score": None,
            "unvisited": True,
            "prior": child["prior"],
            "seq": child["seq"],
        }
    mean = float(child["value_sum"]) / visits
    parent_visits = max(int(parent["visits"]), 1)
    bonus = constant * math.sqrt(math.log(parent_visits + 1) / visits)
    return {
        "node_id": child["id"],
        "title": child["title"],
        "visits": visits,
        "mean_value": mean,
        "exploration_bonus": bonus,
        "uct_score": mean + bonus,
        "unvisited": False,
        "prior": child["prior"],
        "seq": child["seq"],
    }


def handle_select(args: dict[str, Any]) -> dict[str, Any]:
    tree_id = require_text(args, "tree_id")
    max_depth = optional_integer(args, "max_depth", 100, 1, 500)
    store = open_store(args)
    with store.connect() as connection:
        tree = get_tree_row(connection, tree_id)
        start_id = args.get("start_node_id", tree["root_node_id"])
        if not isinstance(start_id, str) or not start_id.strip():
            raise ToolFailure("`start_node_id` must be a non-empty string when provided")
        current = get_node_row(connection, tree_id, start_id)
        if current["deleted_at"] is not None:
            raise ToolFailure(f"cannot select from deleted node {start_id}")
        constant = float(tree["exploration_constant"])
        path = [node_dict(current)]
        steps: list[dict[str, Any]] = []
        reason = ""

        for _ in range(max_depth):
            if current["kind"] != "root" and current["status"] in LIVE_STATUSES and current["visits"] == 0:
                reason = "selected_unvisited_node"
                break
            children = connection.execute(
                "SELECT * FROM nodes WHERE tree_id = ? AND parent_id = ? "
                "AND deleted_at IS NULL AND status IN ('open', 'survived') ORDER BY seq",
                (tree_id, current["id"]),
            ).fetchall()
            if not children:
                if current["status"] in LIVE_STATUSES:
                    reason = "selected_live_leaf_for_expansion_or_evaluation"
                else:
                    reason = "subtree_exhausted"
                break

            candidates = [uct_candidate(current, child, constant) for child in children]
            unvisited = [candidate for candidate in candidates if candidate["unvisited"]]
            if unvisited:
                chosen_summary = max(
                    unvisited,
                    key=lambda candidate: (candidate["prior"], -candidate["seq"]),
                )
            else:
                chosen_summary = max(
                    candidates,
                    key=lambda candidate: (
                        candidate["uct_score"],
                        candidate["prior"],
                        -candidate["seq"],
                    ),
                )
            for candidate in candidates:
                candidate["chosen"] = candidate["node_id"] == chosen_summary["node_id"]
                candidate.pop("seq", None)
            steps.append({"parent_id": current["id"], "candidates": candidates})
            current = get_node_row(connection, tree_id, chosen_summary["node_id"])
            path.append(node_dict(current))
        else:
            reason = "max_depth_reached"

    selected = path[-1] if reason != "subtree_exhausted" else None
    return {
        "tree_id": tree_id,
        "exploration_constant": constant,
        "reason": reason,
        "selected_node": selected,
        "path": path,
        "selection_steps": steps,
        "mutated_state": False,
    }


def handle_evaluate(args: dict[str, Any]) -> dict[str, Any]:
    tree_id = require_text(args, "tree_id")
    node_id = require_text(args, "node_id")
    expected_version = require_integer(args, "expected_version", 1)
    value = require_number(args, "value")
    if value < -1 or value > 1:
        raise ToolFailure("`value` must be between -1 and 1")
    rationale = require_text(args, "rationale")
    evidence = require_string_list(args, "evidence")
    status_after = None
    if "status_after" in args:
        status_after = enum_value(
            args,
            "status_after",
            tuple(status for status in NODE_STATUSES if status != "deleted"),
        )

    store = open_store(args)
    timestamp = utc_now()
    evaluation_id = new_id("eval")
    with store.connect() as connection, write_transaction(connection):
        tree = get_tree_row(connection, tree_id)
        ensure_tree_writable(tree)
        node = get_node_row(connection, tree_id, node_id)
        if node["kind"] == "root":
            raise ToolFailure("evaluate a branch, idea, or synthesis node rather than the root")
        if node["deleted_at"] is not None or node["status"] not in LIVE_STATUSES:
            raise ToolFailure(
                f"node {node_id} is not live; current status is {node['status']}"
            )
        if node["version"] != expected_version:
            raise ToolFailure(
                f"version conflict for node {node_id}: expected {expected_version}, "
                f"current {node['version']}"
            )
        path_ids = ancestor_path(connection, tree_id, node_id)
        connection.execute(
            "INSERT INTO evaluations(id, tree_id, node_id, value, rationale, evidence_json, "
            "active, created_at) VALUES (?, ?, ?, ?, ?, ?, 1, ?)",
            (
                evaluation_id,
                tree_id,
                node_id,
                value,
                rationale,
                compact_json(evidence),
                timestamp,
            ),
        )
        for path_id in path_ids:
            if path_id == node_id and status_after is not None:
                connection.execute(
                    "UPDATE nodes SET visits = visits + 1, value_sum = value_sum + ?, "
                    "status = ?, updated_at = ?, version = version + 1 WHERE id = ?",
                    (value, status_after, timestamp, path_id),
                )
            else:
                connection.execute(
                    "UPDATE nodes SET visits = visits + 1, value_sum = value_sum + ?, "
                    "updated_at = ?, version = version + 1 WHERE id = ?",
                    (value, timestamp, path_id),
                )
        event_seq = append_event(
            connection,
            tree_id,
            node_id,
            "node.evaluated",
            {
                "evaluation_id": evaluation_id,
                "value": value,
                "rationale": rationale,
                "evidence": evidence,
                "status_before": node["status"],
                "status_after": status_after or node["status"],
                "backpropagated_path": path_ids,
            },
            timestamp,
        )
        evaluation = connection.execute(
            "SELECT * FROM evaluations WHERE id = ?", (evaluation_id,)
        ).fetchone()
        updated_nodes = [
            node_dict(get_node_row(connection, tree_id, path_id)) for path_id in path_ids
        ]
    return {
        "evaluation": evaluation_dict(evaluation),
        "updated_path": updated_nodes,
        "event_seq": event_seq,
    }


def handle_invalidate_evaluation(args: dict[str, Any]) -> dict[str, Any]:
    tree_id = require_text(args, "tree_id")
    evaluation_id = require_text(args, "evaluation_id")
    expected_version = require_integer(args, "expected_version", 1)
    reason = require_text(args, "reason")
    store = open_store(args)
    timestamp = utc_now()

    with store.connect() as connection, write_transaction(connection):
        tree = get_tree_row(connection, tree_id)
        ensure_tree_writable(tree)
        evaluation = connection.execute(
            "SELECT * FROM evaluations WHERE tree_id = ? AND id = ?",
            (tree_id, evaluation_id),
        ).fetchone()
        if evaluation is None:
            raise ToolFailure(f"evaluation not found in tree {tree_id}: {evaluation_id}")
        if not evaluation["active"]:
            raise ToolFailure(f"evaluation is already inactive: {evaluation_id}")
        node = get_node_row(connection, tree_id, evaluation["node_id"])
        if node["version"] != expected_version:
            raise ToolFailure(
                f"version conflict for node {node['id']}: expected {expected_version}, "
                f"current {node['version']}"
            )
        connection.execute(
            "UPDATE evaluations SET active = 0, invalidated_at = ?, invalidation_reason = ? "
            "WHERE id = ?",
            (timestamp, reason, evaluation_id),
        )
        stats_changed = rebuild_stats(connection, tree_id, timestamp)
        event_seq = append_event(
            connection,
            tree_id,
            node["id"],
            "evaluation.invalidated",
            {
                "evaluation_id": evaluation_id,
                "reason": reason,
                "stats_changed_ids": stats_changed,
            },
            timestamp,
        )
        updated_evaluation = connection.execute(
            "SELECT * FROM evaluations WHERE id = ?", (evaluation_id,)
        ).fetchone()
        updated_node = node_dict(get_node_row(connection, tree_id, node["id"]))
    return {
        "evaluation": evaluation_dict(updated_evaluation),
        "node": updated_node,
        "event_seq": event_seq,
    }


def handle_history(args: dict[str, Any]) -> dict[str, Any]:
    tree_id = require_text(args, "tree_id")
    after_seq = optional_integer(args, "after_seq", 0, 0, 2_147_483_647)
    limit = optional_integer(args, "limit", 100, 1, 500)
    clauses = ["tree_id = ?", "seq > ?"]
    parameters: list[Any] = [tree_id, after_seq]
    if "node_id" in args:
        clauses.append("node_id = ?")
        parameters.append(require_text(args, "node_id"))
    parameters.append(limit)
    store = open_store(args)
    with store.connect() as connection:
        get_tree_row(connection, tree_id)
        rows = connection.execute(
            f"SELECT * FROM events WHERE {' AND '.join(clauses)} ORDER BY seq LIMIT ?",
            parameters,
        ).fetchall()
    return {
        "events": [event_dict(row) for row in rows],
        "next_after_seq": rows[-1]["seq"] if len(rows) == limit else None,
    }


WORKSPACE_PROPERTY = {
    "type": "string",
    "description": (
        "Absolute existing workspace directory. State is stored in "
        "<workspace>/.idea-tree/ideas.sqlite3."
    ),
}
TREE_ID_PROPERTY = {"type": "string", "description": "Tree ID returned by a tree tool."}
NODE_ID_PROPERTY = {"type": "string", "description": "Node ID returned by a node tool."}
VERSION_PROPERTY = {
    "type": "integer",
    "minimum": 1,
    "description": "Current version read immediately before this mutation.",
}


def make_tool(
    name: str,
    title: str,
    description: str,
    properties: dict[str, Any],
    required: list[str],
    *,
    read_only: bool,
    destructive: bool = False,
) -> dict[str, Any]:
    return {
        "name": name,
        "title": title,
        "description": description,
        "inputSchema": {
            "type": "object",
            "properties": properties,
            "required": required,
            "additionalProperties": False,
        },
        "annotations": {
            "readOnlyHint": read_only,
            "destructiveHint": destructive,
            "idempotentHint": read_only,
            "openWorldHint": False,
        },
    }


TOOLS = [
    make_tool(
        "idea_tree_create_tree",
        "Create idea tree",
        "Create a project-local idea tree with an immutable goal and root node.",
        {
            "workspace": WORKSPACE_PROPERTY,
            "title": {"type": "string"},
            "goal": {"type": "string", "description": "Frozen, judgeable goal contract."},
            "exploration_constant": {
                "type": "number",
                "exclusiveMinimum": 0,
                "maximum": 10,
                "default": math.sqrt(2.0),
            },
        },
        ["workspace", "title", "goal"],
        read_only=False,
    ),
    make_tool(
        "idea_tree_list_trees",
        "List idea trees",
        "Recover tree IDs and current process state in a workspace.",
        {
            "workspace": WORKSPACE_PROPERTY,
            "include_archived": {"type": "boolean", "default": True},
        },
        ["workspace"],
        read_only=True,
    ),
    make_tool(
        "idea_tree_snapshot",
        "Read tree snapshot",
        "Read a bounded hierarchy, aggregate counts, and recent mutation events.",
        {
            "workspace": WORKSPACE_PROPERTY,
            "tree_id": TREE_ID_PROPERTY,
            "include_deleted": {"type": "boolean", "default": False},
            "max_depth": {"type": "integer", "minimum": 0, "maximum": 500, "default": 50},
            "max_nodes": {"type": "integer", "minimum": 1, "maximum": 1000, "default": 200},
        },
        ["workspace", "tree_id"],
        read_only=True,
    ),
    make_tool(
        "idea_tree_update_tree",
        "Update tree state",
        "Rename a tree or set its process state. The frozen goal is intentionally immutable.",
        {
            "workspace": WORKSPACE_PROPERTY,
            "tree_id": TREE_ID_PROPERTY,
            "expected_version": VERSION_PROPERTY,
            "title": {"type": "string"},
            "status": {"type": "string", "enum": list(TREE_STATUSES)},
        },
        ["workspace", "tree_id", "expected_version"],
        read_only=False,
    ),
    make_tool(
        "idea_node_create",
        "Create idea node",
        "Expand a live tree with a mechanism branch, testable idea, or synthesis node.",
        {
            "workspace": WORKSPACE_PROPERTY,
            "tree_id": TREE_ID_PROPERTY,
            "parent_id": NODE_ID_PROPERTY,
            "kind": {"type": "string", "enum": ["branch", "idea", "synthesis"]},
            "title": {"type": "string"},
            "content": {
                "type": "string",
                "description": "Mechanism, observable effect, comparator, and kill condition.",
            },
            "prior": {"type": "number", "minimum": 0, "maximum": 1, "default": 1},
            "metadata": {"type": "object", "additionalProperties": True},
        },
        ["workspace", "tree_id", "parent_id", "kind", "title", "content"],
        read_only=False,
    ),
    make_tool(
        "idea_node_get",
        "Read idea node",
        "Read one node with its evaluations and mutation events.",
        {
            "workspace": WORKSPACE_PROPERTY,
            "tree_id": TREE_ID_PROPERTY,
            "node_id": NODE_ID_PROPERTY,
            "history_limit": {"type": "integer", "minimum": 0, "maximum": 200, "default": 20},
        },
        ["workspace", "tree_id", "node_id"],
        read_only=True,
    ),
    make_tool(
        "idea_node_list",
        "Query idea nodes",
        "Query nodes with bounded pagination instead of relying on remembered or truncated state.",
        {
            "workspace": WORKSPACE_PROPERTY,
            "tree_id": TREE_ID_PROPERTY,
            "parent_id": NODE_ID_PROPERTY,
            "kind": {"type": "string", "enum": list(NODE_KINDS)},
            "status": {"type": "string", "enum": list(NODE_STATUSES)},
            "query": {"type": "string"},
            "include_deleted": {"type": "boolean", "default": False},
            "after_seq": {"type": "integer", "minimum": 0, "default": 0},
            "limit": {"type": "integer", "minimum": 1, "maximum": 500, "default": 100},
        },
        ["workspace", "tree_id"],
        read_only=True,
    ),
    make_tool(
        "idea_node_update",
        "Update idea node",
        "Update mutable idea fields under an optimistic version check. Lineage and kind stay fixed.",
        {
            "workspace": WORKSPACE_PROPERTY,
            "tree_id": TREE_ID_PROPERTY,
            "node_id": NODE_ID_PROPERTY,
            "expected_version": VERSION_PROPERTY,
            "title": {"type": "string"},
            "content": {"type": "string"},
            "status": {
                "type": "string",
                "enum": ["open", "survived", "rejected", "blocked", "finalist"],
            },
            "prior": {"type": "number", "minimum": 0, "maximum": 1},
            "metadata_patch": {
                "type": "object",
                "description": "Merge keys; a null value removes a key.",
                "additionalProperties": True,
            },
        },
        ["workspace", "tree_id", "node_id", "expected_version"],
        read_only=False,
    ),
    make_tool(
        "idea_node_delete",
        "Tombstone idea node",
        "Remove a node from active search without physical erasure. Non-leaf deletion requires explicit cascade.",
        {
            "workspace": WORKSPACE_PROPERTY,
            "tree_id": TREE_ID_PROPERTY,
            "node_id": NODE_ID_PROPERTY,
            "expected_version": VERSION_PROPERTY,
            "reason": {"type": "string"},
            "cascade": {"type": "boolean", "default": False},
        },
        ["workspace", "tree_id", "node_id", "expected_version", "reason"],
        read_only=False,
        destructive=True,
    ),
    make_tool(
        "idea_tree_select",
        "Select exploration node",
        "Use UCT to select an unvisited or high-value live path without mutating state.",
        {
            "workspace": WORKSPACE_PROPERTY,
            "tree_id": TREE_ID_PROPERTY,
            "start_node_id": NODE_ID_PROPERTY,
            "max_depth": {"type": "integer", "minimum": 1, "maximum": 500, "default": 100},
        },
        ["workspace", "tree_id"],
        read_only=True,
    ),
    make_tool(
        "idea_evaluate",
        "Evaluate and backpropagate",
        "Append an evidence-backed value and atomically backpropagate it to every ancestor.",
        {
            "workspace": WORKSPACE_PROPERTY,
            "tree_id": TREE_ID_PROPERTY,
            "node_id": NODE_ID_PROPERTY,
            "expected_version": VERSION_PROPERTY,
            "value": {"type": "number", "minimum": -1, "maximum": 1},
            "rationale": {"type": "string"},
            "evidence": {
                "type": "array",
                "minItems": 1,
                "items": {"type": "string"},
            },
            "status_after": {
                "type": "string",
                "enum": ["open", "survived", "rejected", "blocked", "finalist"],
            },
        },
        [
            "workspace",
            "tree_id",
            "node_id",
            "expected_version",
            "value",
            "rationale",
            "evidence",
        ],
        read_only=False,
    ),
    make_tool(
        "idea_evaluation_invalidate",
        "Invalidate evaluation",
        "Deactivate one invalid evaluation with a reason and recompute affected tree statistics.",
        {
            "workspace": WORKSPACE_PROPERTY,
            "tree_id": TREE_ID_PROPERTY,
            "evaluation_id": {"type": "string"},
            "expected_version": VERSION_PROPERTY,
            "reason": {"type": "string"},
        },
        ["workspace", "tree_id", "evaluation_id", "expected_version", "reason"],
        read_only=False,
        destructive=True,
    ),
    make_tool(
        "idea_tree_history",
        "Read mutation history",
        "Read the append-only event trail with bounded pagination.",
        {
            "workspace": WORKSPACE_PROPERTY,
            "tree_id": TREE_ID_PROPERTY,
            "node_id": NODE_ID_PROPERTY,
            "after_seq": {"type": "integer", "minimum": 0, "default": 0},
            "limit": {"type": "integer", "minimum": 1, "maximum": 500, "default": 100},
        },
        ["workspace", "tree_id"],
        read_only=True,
    ),
]


HANDLERS: dict[str, Callable[[dict[str, Any]], dict[str, Any]]] = {
    "idea_tree_create_tree": handle_create_tree,
    "idea_tree_list_trees": handle_list_trees,
    "idea_tree_snapshot": handle_snapshot,
    "idea_tree_update_tree": handle_update_tree,
    "idea_node_create": handle_create_node,
    "idea_node_get": handle_get_node,
    "idea_node_list": handle_list_nodes,
    "idea_node_update": handle_update_node,
    "idea_node_delete": handle_delete_node,
    "idea_tree_select": handle_select,
    "idea_evaluate": handle_evaluate,
    "idea_evaluation_invalidate": handle_invalidate_evaluation,
    "idea_tree_history": handle_history,
}


def server_meta() -> dict[str, Any]:
    return {"io.modelcontextprotocol/serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION}}


def is_modern_request(message: dict[str, Any]) -> bool:
    params = message.get("params", {})
    if not isinstance(params, dict):
        return False
    meta = params.get("_meta", {})
    if not isinstance(meta, dict):
        return False
    return meta.get("io.modelcontextprotocol/protocolVersion") == "2026-07-28"


def complete_result(payload: dict[str, Any], modern: bool) -> dict[str, Any]:
    result = dict(payload)
    if modern:
        result["resultType"] = "complete"
        current_meta = result.get("_meta", {})
        if not isinstance(current_meta, dict):
            current_meta = {}
        result["_meta"] = {**current_meta, **server_meta()}
    return result


def tool_result(payload: dict[str, Any], modern: bool, is_error: bool = False) -> dict[str, Any]:
    body: dict[str, Any] = {
        "content": [{"type": "text", "text": compact_json(payload)}],
        "structuredContent": payload,
        "isError": is_error,
    }
    return complete_result(body, modern)


def rpc_result(request_id: Any, result: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def rpc_error(request_id: Any, code: int, message: str, data: Any = None) -> dict[str, Any]:
    error: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        error["data"] = data
    return {"jsonrpc": "2.0", "id": request_id, "error": error}


def handle_rpc_message(message: Any) -> dict[str, Any] | None:
    if not isinstance(message, dict):
        return rpc_error(None, -32600, "Invalid Request")
    request_id = message.get("id")
    has_id = "id" in message
    method = message.get("method")
    if message.get("jsonrpc") != "2.0" or not isinstance(method, str):
        return rpc_error(request_id if has_id else None, -32600, "Invalid Request")

    if not has_id:
        return None

    params = message.get("params", {})
    if params is None:
        params = {}
    if not isinstance(params, dict):
        return rpc_error(request_id, -32602, "Invalid params: expected object")
    modern = is_modern_request(message) or method == "server/discover"

    if method == "server/discover":
        return rpc_result(
            request_id,
            {
                "resultType": "complete",
                "supportedVersions": list(SUPPORTED_PROTOCOLS),
                "capabilities": {"tools": {"listChanged": False}},
                "instructions": (
                    "Use an absolute workspace path on every call. Read tree state before "
                    "mutating and use evidence, not model confidence, for evaluations."
                ),
                "ttlMs": 3_600_000,
                "cacheScope": "public",
                "_meta": server_meta(),
            },
        )

    if method == "initialize":
        requested = params.get("protocolVersion")
        protocol = requested if requested in SUPPORTED_PROTOCOLS else "2025-11-25"
        return rpc_result(
            request_id,
            {
                "protocolVersion": protocol,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
                "instructions": (
                    "Use an absolute workspace path on every call. Read tree state before "
                    "mutating and use evidence, not model confidence, for evaluations."
                ),
            },
        )

    if method == "ping":
        return rpc_result(request_id, complete_result({}, modern))

    if method == "tools/list":
        payload: dict[str, Any] = {"tools": TOOLS}
        if modern:
            payload["ttlMs"] = 3_600_000
            payload["cacheScope"] = "public"
        return rpc_result(request_id, complete_result(payload, modern))

    if method == "tools/call":
        tool_name = params.get("name")
        arguments = params.get("arguments", {})
        if not isinstance(tool_name, str) or not tool_name:
            return rpc_error(request_id, -32602, "Invalid params: missing tool name")
        if not isinstance(arguments, dict):
            return rpc_error(request_id, -32602, "Invalid params: arguments must be an object")
        handler = HANDLERS.get(tool_name)
        if handler is None:
            return rpc_error(request_id, -32602, f"Unknown tool: {tool_name}")
        try:
            payload = handler(arguments)
            return rpc_result(request_id, tool_result(payload, modern))
        except ToolFailure as exc:
            return rpc_result(
                request_id,
                tool_result(
                    {"error": {"type": "validation_or_state_error", "message": str(exc)}},
                    modern,
                    is_error=True,
                ),
            )
        except sqlite3.Error as exc:
            print(f"SQLite error in {tool_name}: {exc}", file=sys.stderr, flush=True)
            return rpc_result(
                request_id,
                tool_result(
                    {"error": {"type": "database_error", "message": str(exc)}},
                    modern,
                    is_error=True,
                ),
            )
        except Exception as exc:  # Keep the MCP process alive after one failed call.
            print(f"Unexpected error in {tool_name}: {exc}", file=sys.stderr, flush=True)
            return rpc_result(
                request_id,
                tool_result(
                    {"error": {"type": "internal_error", "message": str(exc)}},
                    modern,
                    is_error=True,
                ),
            )

    return rpc_error(request_id, -32601, f"Method not found: {method}")


def dispatch(parsed: Any) -> Any:
    if isinstance(parsed, list):
        if not parsed:
            return rpc_error(None, -32600, "Invalid Request")
        responses = [response for item in parsed if (response := handle_rpc_message(item)) is not None]
        return responses or None
    return handle_rpc_message(parsed)


def main() -> int:
    for raw_line in sys.stdin:
        if not raw_line.strip():
            continue
        try:
            parsed = json.loads(raw_line)
        except json.JSONDecodeError as exc:
            response = rpc_error(None, -32700, "Parse error", {"detail": str(exc)})
        else:
            response = dispatch(parsed)
        if response is not None:
            sys.stdout.write(compact_json(response) + "\n")
            sys.stdout.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
