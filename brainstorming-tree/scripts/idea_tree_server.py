#!/usr/bin/env python3
"""Dependency-free MCP server for a durable brainstorming ledger.

The server never calls a model and never scores an idea on its own. It stores a
tree of ideas under one frozen goal, refuses a node that is not structurally new
against its parent and its live siblings, and aggregates pairwise comparisons
into a Bradley-Terry ranking per sibling group. Every judgment arrives as a tool
argument; a judgment the human made is recorded with `source` set to `user`.

The goal is frozen: when the premise changes, the answer is a new tree that
names the old one in `supersedes`, not an edit to the old one.
"""

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
SERVER_VERSION = "0.3.0"
DATABASE_DIR = ".idea-tree"
DATABASE_FILE = "ideas.sqlite3"
SCHEMA_VERSION = 3
SUPPORTED_PROTOCOLS = (
    "2025-11-25",
    "2025-06-18",
    "2025-03-26",
    "2024-11-05",
)
SERVER_INSTRUCTIONS = (
    "Use an absolute workspace path on every call. A tree's goal is frozen: when the "
    "premise changes, create a new tree naming the old one in `supersedes`, which seals "
    "the predecessor against further writes. A new node must carry at least one "
    "assumption its parent does not already make, and no two live siblings may claim the "
    "same assumption set. Ranking comes only from `idea_compare`: nothing is scored in "
    "isolation. Record a judgment the human made with `source` set to `user`; "
    "`agent_only` in a ranking says the ordering rests on agent input alone."
)

NODE_KINDS = ("root", "idea", "synthesis")
JUDGMENT_SOURCES = ("user", "agent")
COMPARISON_WINNERS = ("a", "b", "tie")

ASSUMPTION_SEPARATOR = "\x1f"
BRADLEY_TERRY_ITERATIONS = 200
BRADLEY_TERRY_TOLERANCE = 1e-10
VIRTUAL_OPPONENT_STRENGTH = 1.0


SCHEMA_SQL = """
CREATE TABLE trees (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    id TEXT NOT NULL UNIQUE,
    title TEXT NOT NULL,
    goal TEXT NOT NULL,
    root_node_id TEXT NOT NULL UNIQUE,
    supersedes TEXT REFERENCES trees(id),
    superseded_by TEXT REFERENCES trees(id),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE UNIQUE INDEX one_successor_per_predecessor
ON trees(supersedes)
WHERE supersedes IS NOT NULL;

CREATE TABLE nodes (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    id TEXT NOT NULL UNIQUE,
    tree_id TEXT NOT NULL REFERENCES trees(id) ON DELETE CASCADE,
    parent_id TEXT REFERENCES nodes(id),
    kind TEXT NOT NULL CHECK(kind IN ('root', 'idea', 'synthesis')),
    title TEXT NOT NULL,
    content TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('open', 'deleted')),
    kill_condition TEXT NOT NULL DEFAULT '',
    assumptions_json TEXT NOT NULL DEFAULT '[]',
    assumptions_key TEXT NOT NULL DEFAULT '',
    version INTEGER NOT NULL DEFAULT 1 CHECK(version > 0),
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    deleted_at TEXT
);

CREATE UNIQUE INDEX one_root_per_tree
ON nodes(tree_id)
WHERE parent_id IS NULL;

CREATE UNIQUE INDEX sibling_assumptions
ON nodes(tree_id, parent_id, assumptions_key)
WHERE deleted_at IS NULL AND assumptions_key <> '';

CREATE INDEX nodes_by_parent ON nodes(tree_id, parent_id, seq);
CREATE INDEX nodes_by_status ON nodes(tree_id, status, seq);

CREATE TABLE comparisons (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    id TEXT NOT NULL UNIQUE,
    tree_id TEXT NOT NULL REFERENCES trees(id) ON DELETE CASCADE,
    a_node_id TEXT NOT NULL REFERENCES nodes(id),
    b_node_id TEXT NOT NULL REFERENCES nodes(id),
    criterion TEXT NOT NULL,
    winner TEXT NOT NULL CHECK(winner IN ('a', 'b', 'tie')),
    basis TEXT NOT NULL DEFAULT '',
    refs_json TEXT NOT NULL DEFAULT '[]',
    source TEXT NOT NULL CHECK(source IN ('user', 'agent')),
    created_at TEXT NOT NULL,
    CHECK (a_node_id <> b_node_id)
);

CREATE INDEX comparisons_by_tree ON comparisons(tree_id, seq);
CREATE INDEX comparisons_by_a ON comparisons(tree_id, a_node_id, seq);
CREATE INDEX comparisons_by_b ON comparisons(tree_id, b_node_id, seq);

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

PRAGMA user_version = 3;
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


def optional_free_text(args: dict[str, Any], key: str, default: str = "") -> str:
    """Free-form prose the caller may legitimately clear."""
    if key not in args:
        return default
    value = args[key]
    if not isinstance(value, str):
        raise ToolFailure(f"`{key}` must be a string")
    return value.strip()


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


def optional_object(args: dict[str, Any], key: str, default: dict[str, Any]) -> dict[str, Any]:
    if key not in args:
        return dict(default)
    value = args[key]
    if not isinstance(value, dict):
        raise ToolFailure(f"`{key}` must be an object")
    return value


def require_string_list(args: dict[str, Any], key: str) -> list[str]:
    """Trimmed strings; emptiness of the list itself is a domain question."""
    value = args.get(key)
    if not isinstance(value, list):
        raise ToolFailure(f"`{key}` must be an array of strings")
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


def normalize_assumptions(values: list[str]) -> list[str]:
    """Whitespace- and case-normalized, de-duplicated, sorted. No stemming."""
    normalized: list[str] = []
    for value in values:
        collapsed = " ".join(value.lower().split())
        if collapsed and collapsed not in normalized:
            normalized.append(collapsed)
    return sorted(normalized)


def assumptions_key(normalized: list[str]) -> str:
    return ASSUMPTION_SEPARATOR.join(normalized)


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
        elif version in (1, 2):
            connection.close()
            raise ToolFailure(
                f"v0.{version} database found; v0.3 is a fresh schema — delete "
                f"{self.database_path} or use another workspace"
            )
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


def get_record_row(
    connection: sqlite3.Connection, table: str, tree_id: str, record_id: str, label: str
) -> sqlite3.Row:
    row = connection.execute(
        f"SELECT * FROM {table} WHERE tree_id = ? AND id = ?",
        (tree_id, record_id),
    ).fetchone()
    if row is None:
        raise ToolFailure(f"{label} not found in tree {tree_id}: {record_id}")
    return row


def ensure_tree_writable(tree: sqlite3.Row) -> None:
    if tree["superseded_by"] is not None:
        raise ToolFailure(
            f"tree {tree['id']} is superseded by {tree['superseded_by']}; record new work "
            "on the successor tree"
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
    result["assumptions"] = decode_json(result.pop("assumptions_json"), [])
    result.pop("assumptions_key", None)
    return result


def comparison_dict(row: sqlite3.Row) -> dict[str, Any]:
    result = dict(row)
    result["refs"] = decode_json(result.pop("refs_json"), [])
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


def candidate_rows(connection: sqlite3.Connection, tree_id: str) -> list[sqlite3.Row]:
    """Nodes that may be compared and ranked: every live node below the root."""
    return connection.execute(
        "SELECT * FROM nodes WHERE tree_id = ? AND deleted_at IS NULL AND kind != 'root' "
        "ORDER BY seq",
        (tree_id,),
    ).fetchall()


def comparison_rows(connection: sqlite3.Connection, tree_id: str) -> list[sqlite3.Row]:
    return connection.execute(
        "SELECT * FROM comparisons WHERE tree_id = ? ORDER BY seq",
        (tree_id,),
    ).fetchall()


def bradley_terry(
    node_ids: list[str], edges: list[tuple[str, str, str]]
) -> dict[str, float]:
    """Minorize-maximize Bradley-Terry fit.

    A tie counts half a win for each side. Every node additionally plays two games
    against a virtual opponent of fixed strength 1.0 and wins one of them; that Beta
    prior keeps the fit finite under one-sided evidence and anchors the scale, so a
    strength above 1.0 reads as "beats an average unknown sibling".
    """
    ids = list(dict.fromkeys(node_ids))
    if not ids:
        return {}
    wins = {node_id: 0.0 for node_id in ids}
    games: dict[str, dict[str, float]] = {node_id: {} for node_id in ids}
    for a_id, b_id, winner in edges:
        if a_id not in wins or b_id not in wins:
            continue
        games[a_id][b_id] = games[a_id].get(b_id, 0.0) + 1.0
        games[b_id][a_id] = games[b_id].get(a_id, 0.0) + 1.0
        if winner == "a":
            wins[a_id] += 1.0
        elif winner == "b":
            wins[b_id] += 1.0
        else:
            wins[a_id] += 0.5
            wins[b_id] += 0.5

    strengths = {node_id: 1.0 for node_id in ids}
    for _ in range(BRADLEY_TERRY_ITERATIONS):
        updated: dict[str, float] = {}
        for node_id in ids:
            current = strengths[node_id]
            denominator = 2.0 / (current + VIRTUAL_OPPONENT_STRENGTH)
            for other_id, count in games[node_id].items():
                denominator += count / (current + strengths[other_id])
            updated[node_id] = (wins[node_id] + 1.0) / denominator
        delta = max(
            abs(math.log(updated[node_id]) - math.log(strengths[node_id])) for node_id in ids
        )
        strengths = updated
        if delta < BRADLEY_TERRY_TOLERANCE:
            break
    return strengths


def comparison_components(
    node_ids: list[str], edges: list[tuple[str, str, str]]
) -> dict[str, int]:
    """Union-find over compared pairs. Strengths are comparable only within a component."""
    ids = list(dict.fromkeys(node_ids))
    parent = {node_id: node_id for node_id in ids}

    def find(node_id: str) -> str:
        while parent[node_id] != node_id:
            parent[node_id] = parent[parent[node_id]]
            node_id = parent[node_id]
        return node_id

    for a_id, b_id, _winner in edges:
        if a_id in parent and b_id in parent:
            a_root, b_root = find(a_id), find(b_id)
            if a_root != b_root:
                parent[a_root] = b_root

    labels: dict[str, int] = {}
    result: dict[str, int] = {}
    for node_id in ids:
        root = find(node_id)
        if root not in labels:
            labels[root] = len(labels)
        result[node_id] = labels[root]
    return result


def rank_group(
    rows: list[sqlite3.Row], comparisons: list[sqlite3.Row]
) -> dict[str, Any]:
    """Bradley-Terry ranking of one candidate group under the comparisons inside it.

    The group is whatever rows the caller passes: one sibling set, or every
    candidate in scope for the tree-wide shortlist. Strengths are comparable only
    within a `component`, which matters most in the tree-wide view, where two
    sibling sets that were never compared against each other both appear.
    """
    ids = [row["id"] for row in rows]
    id_set = set(ids)
    relevant = [
        row
        for row in comparisons
        if row["a_node_id"] in id_set and row["b_node_id"] in id_set
    ]
    edges = [(row["a_node_id"], row["b_node_id"], row["winner"]) for row in relevant]
    strengths = bradley_terry(ids, edges)
    components = comparison_components(ids, edges)

    tally = {
        node_id: {"wins": 0, "losses": 0, "ties": 0, "comparisons": 0, "user": 0}
        for node_id in ids
    }
    for row in relevant:
        a_id, b_id = row["a_node_id"], row["b_node_id"]
        for node_id in (a_id, b_id):
            tally[node_id]["comparisons"] += 1
            if row["source"] == "user":
                tally[node_id]["user"] += 1
        if row["winner"] == "a":
            tally[a_id]["wins"] += 1
            tally[b_id]["losses"] += 1
        elif row["winner"] == "b":
            tally[b_id]["wins"] += 1
            tally[a_id]["losses"] += 1
        else:
            tally[a_id]["ties"] += 1
            tally[b_id]["ties"] += 1

    entries: list[dict[str, Any]] = []
    for row in rows:
        node_id = row["id"]
        counts = tally[node_id]
        entries.append(
            {
                "node_id": node_id,
                "title": row["title"],
                "status": row["status"],
                "strength": strengths[node_id],
                "wins": counts["wins"],
                "losses": counts["losses"],
                "ties": counts["ties"],
                "comparisons": counts["comparisons"],
                "component": components[node_id],
                "agent_only": counts["comparisons"] > 0 and counts["user"] == 0,
                "seq": row["seq"],
            }
        )
    entries.sort(key=lambda entry: (-entry["strength"], -entry["wins"], entry["seq"]))
    for position, entry in enumerate(entries, start=1):
        entry["rank"] = position
        entry.pop("seq")
    return {
        "user_comparison_count": sum(1 for row in relevant if row["source"] == "user"),
        "nodes": entries,
    }


def shared_assumptions(rows: list[sqlite3.Row]) -> list[str]:
    """What every live idea still rests on, and therefore nobody has questioned."""
    sets = [
        set(decode_json(row["assumptions_json"], []))
        for row in rows
        if row["deleted_at"] is None and row["kind"] in ("idea", "synthesis")
    ]
    if len(sets) < 2:
        return []
    return sorted(set.intersection(*sets))


def assert_adds_an_assumption(
    parent: sqlite3.Row, kind: str, assumptions: list[str]
) -> None:
    """A child that rests on exactly its parent's assumptions is a parameter tweak."""
    if kind not in ("idea", "synthesis") or parent["kind"] == "root":
        return
    inherited = set(decode_json(parent["assumptions_json"], []))
    if not inherited or set(assumptions) - inherited:
        return
    raise ToolFailure(
        f"node adds no assumption beyond its parent {parent['id']} ({parent['title']}) "
        "— that is a parameter variation, not a new idea; name the assumption that "
        "differs, or record the variation as a comparison on the parent instead"
    )


def assert_unique_assumptions(
    connection: sqlite3.Connection,
    tree_id: str,
    parent_id: str | None,
    key: str,
    node_id: str,
    assumptions: list[str],
) -> None:
    if not key:
        return
    row = connection.execute(
        "SELECT id, title FROM nodes WHERE tree_id = ? AND parent_id IS ? "
        "AND deleted_at IS NULL AND assumptions_key = ? AND id <> ?",
        (tree_id, parent_id, key, node_id),
    ).fetchone()
    if row is not None:
        raise ToolFailure(
            f"sibling {row['id']} ({row['title']}) already claims this exact assumption set: "
            f"{'; '.join(assumptions)}. Change at least one assumption, or record the new "
            "judgment on that node instead of restating it."
        )


def handle_create_tree(args: dict[str, Any]) -> dict[str, Any]:
    title = require_text(args, "title")
    goal = require_text(args, "goal")
    supersedes = optional_text(args, "supersedes")

    store = open_store(args, allow_create=True)
    tree_id = new_id("tree")
    root_id = new_id("node")
    timestamp = utc_now()
    with store.connect() as connection, write_transaction(connection):
        if supersedes is not None:
            predecessor = get_tree_row(connection, supersedes)
            if predecessor["superseded_by"] is not None:
                raise ToolFailure(
                    f"tree {supersedes} is already superseded by "
                    f"{predecessor['superseded_by']}; supersede that tree instead"
                )
        connection.execute(
            "INSERT INTO trees(id, title, goal, root_node_id, supersedes, created_at, "
            "updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (tree_id, title, goal, root_id, supersedes, timestamp, timestamp),
        )
        connection.execute(
            "INSERT INTO nodes(id, tree_id, parent_id, kind, title, content, status, "
            "kill_condition, assumptions_json, assumptions_key, version, metadata_json, "
            "created_at, updated_at) "
            "VALUES (?, ?, NULL, 'root', ?, ?, 'open', '', '[]', '', 1, ?, ?, ?)",
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
        if supersedes is not None:
            cursor = connection.execute(
                "UPDATE trees SET superseded_by = ?, updated_at = ? "
                "WHERE id = ? AND superseded_by IS NULL",
                (tree_id, timestamp, supersedes),
            )
            if cursor.rowcount != 1:
                raise ToolFailure(f"tree {supersedes} was superseded by a concurrent write")
            append_event(
                connection,
                supersedes,
                predecessor["root_node_id"],
                "tree.superseded",
                {"successor_tree_id": tree_id, "title": title, "goal": goal},
                timestamp,
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
                "supersedes": supersedes,
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
    include_superseded = optional_boolean(args, "include_superseded", True)
    with store.connect() as connection:
        where = "" if include_superseded else "WHERE t.superseded_by IS NULL"
        rows = connection.execute(
            "SELECT t.*, "
            "SUM(CASE WHEN n.deleted_at IS NULL THEN 1 ELSE 0 END) AS active_node_count, "
            "SUM(CASE WHEN n.deleted_at IS NOT NULL THEN 1 ELSE 0 END) AS deleted_node_count "
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
    max_parents = optional_integer(args, "max_parents", 20, 0, 200)
    events_limit = optional_integer(args, "events_limit", 10, 0, 100)
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
        comparison_counts = {
            row["node_id"]: row["total"]
            for row in connection.execute(
                "SELECT node_id, COUNT(*) AS total FROM ("
                " SELECT a_node_id AS node_id FROM comparisons WHERE tree_id = ?"
                " UNION ALL"
                " SELECT b_node_id AS node_id FROM comparisons WHERE tree_id = ?"
                ") GROUP BY node_id",
                (tree_id, tree_id),
            ).fetchall()
        }

        candidates = candidate_rows(connection, tree_id)
        comparisons = comparison_rows(connection, tree_id)
        groups: dict[str | None, list[sqlite3.Row]] = {}
        for row in candidates:
            groups.setdefault(row["parent_id"], []).append(row)
        rankable = [
            (parent_id, members)
            for parent_id, members in groups.items()
            if len(members) >= 2
        ]
        rankings = [
            {"parent_id": parent_id, **rank_group(members, comparisons)}
            for parent_id, members in rankable[:max_parents]
        ]
        shortlist = rank_group(candidates, comparisons)
        recent_rows = connection.execute(
            "SELECT * FROM events WHERE tree_id = ? ORDER BY seq DESC LIMIT ?",
            (tree_id, events_limit),
        ).fetchall()

        nodes: list[dict[str, Any]] = []
        for row in rows:
            node = node_dict(row)
            node.pop("walk_path", None)
            node["comparison_count"] = comparison_counts.get(row["id"], 0)
            nodes.append(node)

        assumption_floor = shared_assumptions(candidates)
    return {
        "database_path": str(store.database_path),
        "tree": tree,
        "counts": dict(counts),
        "nodes": nodes,
        "truncated": truncated,
        "rankings": rankings,
        "rankings_truncated": len(rankable) > max_parents,
        "ranked_shortlist": shortlist,
        "shared_assumptions": assumption_floor,
        "recent_events": [event_dict(row) for row in reversed(recent_rows)],
    }


def handle_create_node(args: dict[str, Any]) -> dict[str, Any]:
    tree_id = require_text(args, "tree_id")
    parent_id = require_text(args, "parent_id")
    kind = enum_value(args, "kind", tuple(value for value in NODE_KINDS if value != "root"))
    title = require_text(args, "title")
    content = require_text(args, "content")
    assumptions = normalize_assumptions(
        require_string_list(args, "assumptions") if "assumptions" in args else []
    )
    kill_condition = optional_free_text(args, "kill_condition")
    metadata = optional_object(args, "metadata", {})
    if not assumptions:
        raise ToolFailure(
            f"kind `{kind}` requires at least one `assumptions` entry so siblings can be "
            "told apart structurally"
        )
    key = assumptions_key(assumptions)

    store = open_store(args)
    node_id = new_id("node")
    timestamp = utc_now()
    with store.connect() as connection, write_transaction(connection):
        tree = get_tree_row(connection, tree_id)
        ensure_tree_writable(tree)
        parent = get_node_row(connection, tree_id, parent_id)
        if parent["deleted_at"] is not None:
            raise ToolFailure(f"cannot add a child to deleted node {parent_id}")
        assert_adds_an_assumption(parent, kind, assumptions)
        assert_unique_assumptions(connection, tree_id, parent_id, key, node_id, assumptions)
        connection.execute(
            "INSERT INTO nodes(id, tree_id, parent_id, kind, title, content, status, "
            "kill_condition, assumptions_json, assumptions_key, version, metadata_json, "
            "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, 'open', ?, ?, ?, 1, ?, ?, ?)",
            (
                node_id,
                tree_id,
                parent_id,
                kind,
                title,
                content,
                kill_condition,
                compact_json(assumptions),
                key,
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
                "assumptions": assumptions,
                "kill_condition": kill_condition,
            },
            timestamp,
        )
        node = node_dict(get_node_row(connection, tree_id, node_id))
    return {"node": node, "event_seq": event_seq}


def handle_update_node(args: dict[str, Any]) -> dict[str, Any]:
    tree_id = require_text(args, "tree_id")
    node_id = require_text(args, "node_id")
    expected_version = require_integer(args, "expected_version", 1)
    changes: dict[str, Any] = {}
    for key in ("title", "content"):
        if key in args:
            changes[key] = require_text(args, key)
    if "kill_condition" in args:
        changes["kill_condition"] = optional_free_text(args, "kill_condition")
    assumptions = None
    if "assumptions" in args:
        assumptions = normalize_assumptions(require_string_list(args, "assumptions"))
        changes["assumptions_json"] = compact_json(assumptions)
        changes["assumptions_key"] = assumptions_key(assumptions)
    metadata_patch = None
    if "metadata_patch" in args:
        metadata_patch = optional_object(args, "metadata_patch", {})
    if not changes and metadata_patch is None:
        raise ToolFailure(
            "provide at least one mutable field: `title`, `content`, `kill_condition`, "
            "`assumptions`, or `metadata_patch`"
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
        if assumptions is not None:
            if not assumptions:
                raise ToolFailure(
                    f"kind `{before['kind']}` requires at least one `assumptions` entry"
                )
            assert_adds_an_assumption(
                get_node_row(connection, tree_id, before["parent_id"]),
                before["kind"],
                assumptions,
            )
            assert_unique_assumptions(
                connection,
                tree_id,
                before["parent_id"],
                changes["assumptions_key"],
                node_id,
                assumptions,
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
        assignments.extend(["updated_at = ?", "version = version + 1"])
        values = list(changes.values()) + [timestamp, node_id, expected_version]
        cursor = connection.execute(
            f"UPDATE nodes SET {', '.join(assignments)} WHERE id = ? AND version = ?",
            values,
        )
        if cursor.rowcount != 1:
            raise ToolFailure(f"node update lost a concurrent write: {node_id}")
        after = get_node_row(connection, tree_id, node_id)
        visible_keys = [
            key
            for key in changes
            if key not in ("metadata_json", "assumptions_json", "assumptions_key")
        ]
        event_payload: dict[str, Any] = {
            "before": {key: before[key] for key in visible_keys},
            "after": {key: after[key] for key in visible_keys},
            "version": after["version"],
        }
        if assumptions is not None:
            event_payload["assumptions"] = assumptions
        if metadata_patch is not None:
            event_payload["metadata_patch"] = metadata_patch
        event_seq = append_event(
            connection, tree_id, node_id, "node.updated", event_payload, timestamp
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
        connection.execute(
            f"UPDATE nodes SET status = 'deleted', deleted_at = ?, updated_at = ?, "
            f"version = version + 1 WHERE id IN ({placeholders})",
            [timestamp, timestamp, *ids],
        )
        event_seq = append_event(
            connection,
            tree_id,
            node_id,
            "node.deleted",
            {"reason": reason, "cascade": cascade, "tombstoned_ids": ids},
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


def handle_compare(args: dict[str, Any]) -> dict[str, Any]:
    tree_id = require_text(args, "tree_id")
    a_node_id = require_text(args, "a_node_id")
    b_node_id = require_text(args, "b_node_id")
    criterion = require_text(args, "criterion")
    winner = enum_value(args, "winner", COMPARISON_WINNERS)
    source = enum_value(args, "source", JUDGMENT_SOURCES)
    basis = optional_free_text(args, "basis")
    refs = require_string_list(args, "refs") if "refs" in args else []
    if a_node_id == b_node_id:
        raise ToolFailure("a comparison needs two different nodes")

    store = open_store(args)
    comparison_id = new_id("cmp")
    timestamp = utc_now()
    with store.connect() as connection, write_transaction(connection):
        tree = get_tree_row(connection, tree_id)
        ensure_tree_writable(tree)
        for node_id in (a_node_id, b_node_id):
            node = get_node_row(connection, tree_id, node_id)
            if node["kind"] == "root":
                raise ToolFailure("the root holds the frozen goal and is not a comparison operand")
            if node["deleted_at"] is not None:
                raise ToolFailure(
                    f"node {node_id} is deleted and cannot be a comparison operand"
                )
        connection.execute(
            "INSERT INTO comparisons(id, tree_id, a_node_id, b_node_id, criterion, winner, "
            "basis, refs_json, source, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                comparison_id,
                tree_id,
                a_node_id,
                b_node_id,
                criterion,
                winner,
                basis,
                compact_json(refs),
                source,
                timestamp,
            ),
        )
        event_seq = append_event(
            connection,
            tree_id,
            a_node_id,
            "comparison.recorded",
            {
                "comparison_id": comparison_id,
                "a_node_id": a_node_id,
                "b_node_id": b_node_id,
                "criterion": criterion,
                "winner": winner,
                "basis": basis,
                "refs": refs,
                "source": source,
            },
            timestamp,
        )
        comparison = comparison_dict(
            get_record_row(connection, "comparisons", tree_id, comparison_id, "comparison")
        )
    return {"comparison": comparison, "event_seq": event_seq}


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
ASSUMPTIONS_PROPERTY = {
    "type": "array",
    "items": {"type": "string"},
    "description": (
        "The load-bearing assumptions this node makes. Two live siblings may not claim "
        "the same set, and a child must add one its parent does not make."
    ),
}
KILL_CONDITION_PROPERTY = {
    "type": "string",
    "description": "Optional: the observation that would retire this node.",
}
JUDGMENT_SOURCE_PROPERTY = {
    "type": "string",
    "enum": list(JUDGMENT_SOURCES),
    "description": "Who made this judgment. Rankings report whether they rest on agent input only.",
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
        "Create a project-local idea tree with an immutable goal and root node. When the "
        "premise of an existing tree changed, name it in `supersedes`: the new tree "
        "carries the work forward and the old one is sealed against further writes.",
        {
            "workspace": WORKSPACE_PROPERTY,
            "title": {"type": "string"},
            "goal": {"type": "string", "description": "Frozen, judgeable goal contract."},
            "supersedes": {
                "type": "string",
                "description": (
                    "The tree this one replaces because its premise changed. That tree "
                    "stops accepting nodes and comparisons."
                ),
            },
        },
        ["workspace", "title", "goal"],
        read_only=False,
    ),
    make_tool(
        "idea_tree_list_trees",
        "List idea trees",
        "Recover tree IDs and the supersede chain in a workspace.",
        {
            "workspace": WORKSPACE_PROPERTY,
            "include_superseded": {"type": "boolean", "default": True},
        },
        ["workspace"],
        read_only=True,
    ),
    make_tool(
        "idea_tree_snapshot",
        "Read tree snapshot",
        "Read the hierarchy with per-node comparison counts, the sibling rankings, the "
        "tree-wide `ranked_shortlist`, the assumptions every live idea shares, and the "
        "most recent events.",
        {
            "workspace": WORKSPACE_PROPERTY,
            "tree_id": TREE_ID_PROPERTY,
            "include_deleted": {"type": "boolean", "default": False},
            "max_depth": {"type": "integer", "minimum": 0, "maximum": 500, "default": 50},
            "max_nodes": {"type": "integer", "minimum": 1, "maximum": 1000, "default": 200},
            "max_parents": {
                "type": "integer",
                "minimum": 0,
                "maximum": 200,
                "default": 20,
                "description": (
                    "How many sibling groups to rank. `ranked_shortlist` covers every "
                    "candidate and is never truncated by this."
                ),
            },
            "events_limit": {"type": "integer", "minimum": 0, "maximum": 100, "default": 10},
        },
        ["workspace", "tree_id"],
        read_only=True,
    ),
    make_tool(
        "idea_node_create",
        "Create idea node",
        "Add a testable idea or a synthesis of two of them. A node must name its "
        "assumptions, no two live siblings may claim the same assumption set, and a "
        "child must add at least one assumption its parent does not already make.",
        {
            "workspace": WORKSPACE_PROPERTY,
            "tree_id": TREE_ID_PROPERTY,
            "parent_id": NODE_ID_PROPERTY,
            "kind": {"type": "string", "enum": ["idea", "synthesis"]},
            "title": {"type": "string"},
            "content": {
                "type": "string",
                "description": "Mechanism, observable effect, and comparator.",
            },
            "assumptions": ASSUMPTIONS_PROPERTY,
            "kill_condition": KILL_CONDITION_PROPERTY,
            "metadata": {"type": "object", "additionalProperties": True},
        },
        ["workspace", "tree_id", "parent_id", "kind", "title", "content", "assumptions"],
        read_only=False,
    ),
    make_tool(
        "idea_node_update",
        "Update idea node",
        "Update mutable idea fields under an optimistic version check. Lineage and kind stay "
        "fixed; a repair is a new sibling with a different assumption set, not an edit.",
        {
            "workspace": WORKSPACE_PROPERTY,
            "tree_id": TREE_ID_PROPERTY,
            "node_id": NODE_ID_PROPERTY,
            "expected_version": VERSION_PROPERTY,
            "title": {"type": "string"},
            "content": {"type": "string"},
            "assumptions": ASSUMPTIONS_PROPERTY,
            "kill_condition": KILL_CONDITION_PROPERTY,
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
        "Remove a node from the live ledger without physical erasure. Its comparisons stay "
        "in the ledger; it simply stops being ranked. Non-leaf deletion requires cascade.",
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
        "idea_compare",
        "Compare two candidates",
        "Record one pairwise judgment on a named criterion. Pairwise comparison is the only "
        "ranking input: candidates are ranked by a Bradley-Terry fit over these records. The "
        "operands need not be siblings; a cross-parent judgment ranks in `ranked_shortlist`.",
        {
            "workspace": WORKSPACE_PROPERTY,
            "tree_id": TREE_ID_PROPERTY,
            "a_node_id": NODE_ID_PROPERTY,
            "b_node_id": NODE_ID_PROPERTY,
            "criterion": {
                "type": "string",
                "description": "The single named dimension this judgment is about.",
            },
            "winner": {"type": "string", "enum": list(COMPARISON_WINNERS)},
            "basis": {
                "type": "string",
                "description": "Why this judgment came out this way, in the judge's own words.",
            },
            "refs": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Pointers the basis rests on: a run, a file, a message.",
            },
            "source": JUDGMENT_SOURCE_PROPERTY,
        },
        ["workspace", "tree_id", "a_node_id", "b_node_id", "criterion", "winner", "source"],
        read_only=False,
    ),
]


HANDLERS: dict[str, Callable[[dict[str, Any]], dict[str, Any]]] = {
    "idea_tree_create_tree": handle_create_tree,
    "idea_tree_list_trees": handle_list_trees,
    "idea_tree_snapshot": handle_snapshot,
    "idea_node_create": handle_create_node,
    "idea_node_update": handle_update_node,
    "idea_node_delete": handle_delete_node,
    "idea_compare": handle_compare,
}


def tool_result(payload: dict[str, Any], is_error: bool = False) -> dict[str, Any]:
    return {
        "content": [{"type": "text", "text": compact_json(payload)}],
        "structuredContent": payload,
        "isError": is_error,
    }


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

    if method == "initialize":
        requested = params.get("protocolVersion")
        protocol = requested if requested in SUPPORTED_PROTOCOLS else SUPPORTED_PROTOCOLS[0]
        return rpc_result(
            request_id,
            {
                "protocolVersion": protocol,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
                "instructions": SERVER_INSTRUCTIONS,
            },
        )

    if method == "ping":
        return rpc_result(request_id, {})

    if method == "tools/list":
        return rpc_result(request_id, {"tools": TOOLS})

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
            return rpc_result(request_id, tool_result(payload))
        except ToolFailure as exc:
            return rpc_result(
                request_id,
                tool_result(
                    {"error": {"type": "validation_or_state_error", "message": str(exc)}},
                    is_error=True,
                ),
            )
        except sqlite3.Error as exc:
            print(f"SQLite error in {tool_name}: {exc}", file=sys.stderr, flush=True)
            return rpc_result(
                request_id,
                tool_result(
                    {"error": {"type": "database_error", "message": str(exc)}},
                    is_error=True,
                ),
            )
        except Exception as exc:  # Keep the MCP process alive after one failed call.
            print(f"Unexpected error in {tool_name}: {exc}", file=sys.stderr, flush=True)
            return rpc_result(
                request_id,
                tool_result(
                    {"error": {"type": "internal_error", "message": str(exc)}},
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
