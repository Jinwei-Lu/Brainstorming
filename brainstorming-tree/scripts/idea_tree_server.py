#!/usr/bin/env python3
"""Dependency-free MCP server for a durable strong-inference brainstorming ledger.

The server never calls a model. It stores judgments, aggregates them into a
Bradley-Terry ranking per sibling group, computes Pareto domination for pruning,
ranks open discriminators -- questions, constraints, and observations alike -- by
how many live branches depend on them per unit of cost, and reports what to do
next. Every judgment arrives as a tool argument.
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
SERVER_VERSION = "0.2.0"
DATABASE_DIR = ".idea-tree"
DATABASE_FILE = "ideas.sqlite3"
SCHEMA_VERSION = 2
SUPPORTED_PROTOCOLS = (
    "2025-11-25",
    "2025-06-18",
    "2025-03-26",
    "2024-11-05",
)
SERVER_INSTRUCTIONS = (
    "Use an absolute workspace path on every call. Never score an idea in isolation: "
    "compare candidates pairwise on a named criterion, record evidence as typed "
    "evaluations, and let `idea_tree_select` say whether to run an observation, ask "
    "the human, or compare. Deliver from `ranked_shortlist`, and read `dominated` for "
    "why each pruned branch left the live set."
)

NODE_KINDS = ("root", "branch", "idea", "synthesis")
NODE_STATUSES = ("open", "survived", "rejected", "blocked", "finalist", "deleted")
CANDIDATE_STATUSES = ("open", "survived", "finalist", "blocked")
TREE_STATUSES = ("active", "completed", "archived")
QUESTION_KINDS = ("question", "constraint", "observation")
QUESTION_SOURCES = ("user", "inferred", "assumed")
QUESTION_STATUSES = ("open", "answered", "withdrawn")
RESOLVED_QUESTION_STATUSES = ("answered", "withdrawn")
JUDGMENT_SOURCES = ("user", "agent")
EVIDENCE_KINDS = ("user_judgment", "experiment", "source", "logic_trace")
EVALUATION_OUTCOMES = ("supports", "kills", "inconclusive")
COMPARISON_WINNERS = ("a", "b", "tie")

ASSUMPTION_SEPARATOR = "\x1f"
BRADLEY_TERRY_ITERATIONS = 200
BRADLEY_TERRY_TOLERANCE = 1e-10
VIRTUAL_OPPONENT_STRENGTH = 1.0
QUESTION_WEIGHT_THRESHOLD = 2
QUESTION_RULE = "weight>=2"
DEFAULT_QUESTION_COST = 1.0
# Ties on score break toward the cheaper move: run it, then ask, then compare.
DISCRIMINATOR_ORDER = {"observation": 0, "constraint": 1, "question": 2}

RECORD_TABLES = {
    "eval_": ("evaluations", "evaluation"),
    "cmp_": ("comparisons", "comparison"),
}


SCHEMA_SQL = """
CREATE TABLE trees (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    id TEXT NOT NULL UNIQUE,
    title TEXT NOT NULL,
    goal TEXT NOT NULL,
    root_node_id TEXT NOT NULL UNIQUE,
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
    kill_condition TEXT NOT NULL DEFAULT '',
    assumptions_json TEXT NOT NULL DEFAULT '[]',
    assumptions_key TEXT NOT NULL DEFAULT '',
    version INTEGER NOT NULL DEFAULT 1 CHECK(version > 0),
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    deleted_at TEXT,
    CHECK (kind IN ('root', 'branch') OR length(trim(kill_condition)) > 0)
);

CREATE UNIQUE INDEX one_root_per_tree
ON nodes(tree_id)
WHERE parent_id IS NULL;

CREATE UNIQUE INDEX sibling_assumptions
ON nodes(tree_id, parent_id, assumptions_key)
WHERE deleted_at IS NULL AND assumptions_key <> '';

CREATE INDEX nodes_by_parent ON nodes(tree_id, parent_id, seq);
CREATE INDEX nodes_by_status ON nodes(tree_id, status, seq);

CREATE TABLE questions (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    id TEXT NOT NULL UNIQUE,
    tree_id TEXT NOT NULL REFERENCES trees(id) ON DELETE CASCADE,
    kind TEXT NOT NULL CHECK(kind IN ('question', 'constraint', 'observation')),
    text TEXT NOT NULL,
    cost REAL NOT NULL DEFAULT 1 CHECK(cost > 0),
    source TEXT NOT NULL CHECK(source IN ('user', 'inferred', 'assumed')),
    status TEXT NOT NULL CHECK(status IN ('open', 'answered', 'withdrawn')),
    answer TEXT,
    answered_by TEXT CHECK(answered_by IN ('user', 'agent')),
    version INTEGER NOT NULL DEFAULT 1 CHECK(version > 0),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    answered_at TEXT
);

CREATE INDEX questions_by_status ON questions(tree_id, status, seq);

CREATE TABLE node_questions (
    node_id TEXT NOT NULL REFERENCES nodes(id),
    question_id TEXT NOT NULL REFERENCES questions(id),
    tree_id TEXT NOT NULL REFERENCES trees(id) ON DELETE CASCADE,
    created_at TEXT NOT NULL,
    PRIMARY KEY (node_id, question_id)
) WITHOUT ROWID;

CREATE INDEX node_questions_by_question ON node_questions(tree_id, question_id);

CREATE TABLE comparisons (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    id TEXT NOT NULL UNIQUE,
    tree_id TEXT NOT NULL REFERENCES trees(id) ON DELETE CASCADE,
    a_node_id TEXT NOT NULL REFERENCES nodes(id),
    b_node_id TEXT NOT NULL REFERENCES nodes(id),
    criterion TEXT NOT NULL,
    winner TEXT NOT NULL CHECK(winner IN ('a', 'b', 'tie')),
    basis_json TEXT NOT NULL DEFAULT '[]',
    source TEXT NOT NULL CHECK(source IN ('user', 'agent')),
    active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0, 1)),
    created_at TEXT NOT NULL,
    invalidated_at TEXT,
    invalidation_reason TEXT,
    CHECK (a_node_id <> b_node_id)
);

CREATE INDEX active_comparisons ON comparisons(tree_id, active, seq);
CREATE INDEX comparisons_by_a ON comparisons(tree_id, a_node_id, seq);
CREATE INDEX comparisons_by_b ON comparisons(tree_id, b_node_id, seq);

CREATE TABLE evaluations (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    id TEXT NOT NULL UNIQUE,
    tree_id TEXT NOT NULL REFERENCES trees(id) ON DELETE CASCADE,
    node_id TEXT NOT NULL REFERENCES nodes(id),
    outcome TEXT NOT NULL CHECK(outcome IN ('supports', 'kills', 'inconclusive')),
    rationale TEXT NOT NULL,
    evidence_json TEXT NOT NULL,
    source TEXT NOT NULL CHECK(source IN ('user', 'agent')),
    question_id TEXT REFERENCES questions(id),
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

PRAGMA user_version = 2;
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


def require_id_list(args: dict[str, Any], key: str) -> list[str]:
    """Record identifiers, order preserved, duplicates collapsed."""
    result: list[str] = []
    for item in require_string_list(args, key):
        if item not in result:
            result.append(item)
    return result


def require_evidence_objects(args: dict[str, Any], key: str) -> list[dict[str, Any]]:
    value = args.get(key)
    if not isinstance(value, list) or not value:
        raise ToolFailure(
            f"`{key}` must be a non-empty array of "
            "{kind, ref, cost} objects"
        )
    result: list[dict[str, Any]] = []
    for index, item in enumerate(value):
        label = f"{key}[{index}]"
        if not isinstance(item, dict):
            raise ToolFailure(f"`{label}` must be an object with `kind`, `ref`, and `cost`")
        kind = item.get("kind")
        if kind not in EVIDENCE_KINDS:
            raise ToolFailure(f"`{label}.kind` must be one of: {', '.join(EVIDENCE_KINDS)}")
        ref = item.get("ref")
        if not isinstance(ref, str) or not ref.strip():
            raise ToolFailure(f"`{label}.ref` must be a non-empty string")
        cost = item.get("cost")
        if isinstance(cost, bool) or not isinstance(cost, (int, float)):
            raise ToolFailure(f"`{label}.cost` must be a number")
        cost = float(cost)
        if not math.isfinite(cost) or cost < 0:
            raise ToolFailure(f"`{label}.cost` must be a finite number of at least 0")
        unexpected = set(item) - {"kind", "ref", "cost"}
        if unexpected:
            raise ToolFailure(
                f"`{label}` has unexpected keys: {', '.join(sorted(unexpected))}"
            )
        result.append({"kind": kind, "ref": ref.strip(), "cost": cost})
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
        elif version == 1:
            connection.close()
            raise ToolFailure(
                f"v0.1 database found; v0.2 is a fresh schema — delete {self.database_path} "
                "or use another workspace"
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
    result["assumptions"] = decode_json(result.pop("assumptions_json"), [])
    result.pop("assumptions_key", None)
    return result


def node_summary(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "node_id": row["id"],
        "title": row["title"],
        "kind": row["kind"],
        "status": row["status"],
    }


def evaluation_dict(row: sqlite3.Row) -> dict[str, Any]:
    result = dict(row)
    result["active"] = bool(result["active"])
    result["evidence"] = decode_json(result.pop("evidence_json"), [])
    return result


def comparison_dict(row: sqlite3.Row) -> dict[str, Any]:
    result = dict(row)
    result["active"] = bool(result["active"])
    result["basis"] = decode_json(result.pop("basis_json"), [])
    return result


def question_dict(row: sqlite3.Row) -> dict[str, Any]:
    return dict(row)


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
    """Nodes that may be selected, compared, and ranked."""
    placeholders = ",".join("?" for _ in CANDIDATE_STATUSES)
    return connection.execute(
        "SELECT * FROM nodes WHERE tree_id = ? AND deleted_at IS NULL AND kind != 'root' "
        f"AND status IN ({placeholders}) ORDER BY seq",
        (tree_id, *CANDIDATE_STATUSES),
    ).fetchall()


def active_comparison_rows(connection: sqlite3.Connection, tree_id: str) -> list[sqlite3.Row]:
    return connection.execute(
        "SELECT * FROM comparisons WHERE tree_id = ? AND active = 1 ORDER BY seq",
        (tree_id,),
    ).fetchall()


def scoped_node_ids(
    connection: sqlite3.Connection, tree_id: str, start_node_id: str, max_depth: int
) -> set[str]:
    rows = connection.execute(
        "WITH RECURSIVE walk AS ("
        " SELECT n.id, 0 AS walk_depth FROM nodes n WHERE n.tree_id = ? AND n.id = ?"
        " UNION ALL"
        " SELECT n.id, walk.walk_depth + 1 FROM nodes n JOIN walk ON n.parent_id = walk.id"
        " WHERE n.tree_id = ? AND walk.walk_depth < ?"
        ") SELECT id FROM walk",
        (tree_id, start_node_id, tree_id, max_depth),
    ).fetchall()
    return {row["id"] for row in rows}


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


def domination_report(
    rows: list[sqlite3.Row], comparisons: list[sqlite3.Row]
) -> tuple[set[str], list[dict[str, Any]]]:
    """The live set, and the audit trail of everything pruned out of it.

    X is dominated iff some candidate Y won every active X-vs-Y comparison with no
    tie. An uncompared node is undominated: the absence of a comparison is not
    evidence. Each pruned node reports the rival that swept it, on which criteria,
    by which comparison records, so a branch never leaves the live set silently.

    The candidate set is exactly the one `rank_group` ranks for the tree-wide
    shortlist, so every comparison that can eliminate a node is visible in both.
    """
    candidate_ids = {row["id"] for row in rows}
    pair_rows: dict[tuple[str, str], list[sqlite3.Row]] = {}
    for row in comparisons:
        a_id, b_id = row["a_node_id"], row["b_node_id"]
        if a_id not in candidate_ids or b_id not in candidate_ids:
            continue
        key = (a_id, b_id) if a_id < b_id else (b_id, a_id)
        pair_rows.setdefault(key, []).append(row)

    dominated: dict[str, list[dict[str, Any]]] = {}
    for (x_id, y_id), pair in pair_rows.items():
        winners = {
            None if row["winner"] == "tie"
            else (row["a_node_id"] if row["winner"] == "a" else row["b_node_id"])
            for row in pair
        }
        if winners == {x_id}:
            winner, loser = x_id, y_id
        elif winners == {y_id}:
            winner, loser = y_id, x_id
        else:
            continue
        dominated.setdefault(loser, []).append(
            {
                "node_id": winner,
                "criteria": sorted({row["criterion"] for row in pair}),
                "comparison_ids": [row["id"] for row in pair],
            }
        )

    summaries = [
        {**node_summary(row), "dominated_by": dominated[row["id"]]}
        for row in rows
        if row["id"] in dominated
    ]
    return candidate_ids - set(dominated), summaries


def blocked_by_map(connection: sqlite3.Connection, tree_id: str) -> dict[str, list[str]]:
    """Node -> the open questions it declared a dependency on. Never stored."""
    rows = connection.execute(
        "SELECT nq.node_id, nq.question_id FROM node_questions nq "
        "JOIN questions q ON q.id = nq.question_id "
        "WHERE nq.tree_id = ? AND q.status = 'open' ORDER BY q.seq",
        (tree_id,),
    ).fetchall()
    result: dict[str, list[str]] = {}
    for row in rows:
        result.setdefault(row["node_id"], []).append(row["question_id"])
    return result


def question_weights(
    connection: sqlite3.Connection, tree_id: str, undominated: set[str]
) -> dict[str, list[str]]:
    """Open question -> the undominated candidates that depend on it."""
    rows = connection.execute(
        "SELECT nq.question_id, nq.node_id FROM node_questions nq "
        "JOIN questions q ON q.id = nq.question_id "
        "WHERE nq.tree_id = ? AND q.status = 'open' ORDER BY nq.question_id",
        (tree_id,),
    ).fetchall()
    result: dict[str, list[str]] = {}
    for row in rows:
        if row["node_id"] in undominated:
            result.setdefault(row["question_id"], []).append(row["node_id"])
    return result


def open_discriminators(
    rows: list[sqlite3.Row], weights: dict[str, list[str]]
) -> list[dict[str, Any]]:
    """Every open question, constraint, and observation on one ranked list.

    All three are the same object: a pending discriminator whose answer changes
    which nodes survive. Its power is the number of undominated nodes that
    declared a dependency on it; its price is `cost`, which only an observation
    may set, because "how much work is it to check this" is groundable while
    "how much does it cost to ask a human" would be an invented number.
    """
    scored: list[dict[str, Any]] = []
    for row in rows:
        dependents = weights.get(row["id"], [])
        cost = float(row["cost"])
        scored.append(
            {
                **question_dict(row),
                "weight": len(dependents),
                "live_dependents": dependents,
                "score": len(dependents) / cost,
            }
        )
    scored.sort(
        key=lambda entry: (
            -entry["score"],
            DISCRIMINATOR_ORDER[entry["kind"]],
            entry["seq"],
        )
    )
    return scored


def shared_assumptions(rows: list[sqlite3.Row], undominated: set[str]) -> list[str]:
    """What every surviving idea still rests on, and therefore nobody has questioned."""
    sets = [
        set(decode_json(row["assumptions_json"], []))
        for row in rows
        if row["id"] in undominated and row["kind"] in ("idea", "synthesis")
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
        "differs, or record the variation as a comparison or evaluation on the parent "
        "instead"
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


def link_questions(
    connection: sqlite3.Connection,
    tree_id: str,
    node_id: str,
    question_ids: list[str],
    timestamp: str,
) -> None:
    """Replace a node's declared dependencies. No text matching, ever."""
    for question_id in question_ids:
        get_record_row(connection, "questions", tree_id, question_id, "question")
    connection.execute("DELETE FROM node_questions WHERE node_id = ?", (node_id,))
    for question_id in question_ids:
        connection.execute(
            "INSERT INTO node_questions(node_id, question_id, tree_id, created_at) "
            "VALUES (?, ?, ?, ?)",
            (node_id, question_id, tree_id, timestamp),
        )


def link_nodes(
    connection: sqlite3.Connection,
    tree_id: str,
    question_id: str,
    node_ids: list[str],
    timestamp: str,
) -> None:
    """Attach a freshly raised question to the nodes it discriminates."""
    for node_id in node_ids:
        get_node_row(connection, tree_id, node_id)
        connection.execute(
            "INSERT INTO node_questions(node_id, question_id, tree_id, created_at) "
            "VALUES (?, ?, ?, ?)",
            (node_id, question_id, tree_id, timestamp),
        )


def handle_create_tree(args: dict[str, Any]) -> dict[str, Any]:
    title = require_text(args, "title")
    goal = require_text(args, "goal")

    store = open_store(args, allow_create=True)
    tree_id = new_id("tree")
    root_id = new_id("node")
    timestamp = utc_now()
    with store.connect() as connection, write_transaction(connection):
        connection.execute(
            "INSERT INTO trees(id, title, goal, root_node_id, status, version, created_at, "
            "updated_at) VALUES (?, ?, ?, ?, 'active', 1, ?, ?)",
            (tree_id, title, goal, root_id, timestamp, timestamp),
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
        event_seq = append_event(
            connection,
            tree_id,
            root_id,
            "tree.created",
            {"title": title, "goal": goal, "root_node_id": root_id},
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
    max_parents = optional_integer(args, "max_parents", 20, 0, 200)
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
        evidence_counts = {
            row["node_id"]: row["total"]
            for row in connection.execute(
                "SELECT node_id, COUNT(*) AS total FROM evaluations "
                "WHERE tree_id = ? AND active = 1 GROUP BY node_id",
                (tree_id,),
            ).fetchall()
        }
        kill_counts = {
            row["node_id"]: row["total"]
            for row in connection.execute(
                "SELECT node_id, COUNT(*) AS total FROM evaluations "
                "WHERE tree_id = ? AND active = 1 AND outcome = 'kills' GROUP BY node_id",
                (tree_id,),
            ).fetchall()
        }
        comparison_counts = {
            row["node_id"]: row["total"]
            for row in connection.execute(
                "SELECT node_id, COUNT(*) AS total FROM ("
                " SELECT a_node_id AS node_id FROM comparisons WHERE tree_id = ? AND active = 1"
                " UNION ALL"
                " SELECT b_node_id AS node_id FROM comparisons WHERE tree_id = ? AND active = 1"
                ") GROUP BY node_id",
                (tree_id, tree_id),
            ).fetchall()
        }
        blocked = blocked_by_map(connection, tree_id)

        candidates = candidate_rows(connection, tree_id)
        comparisons = active_comparison_rows(connection, tree_id)
        undominated, dominated = domination_report(candidates, comparisons)
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
        weights = question_weights(connection, tree_id, undominated)
        open_question_rows = connection.execute(
            "SELECT * FROM questions WHERE tree_id = ? AND status = 'open' ORDER BY seq",
            (tree_id,),
        ).fetchall()
        recent_rows = connection.execute(
            "SELECT * FROM events WHERE tree_id = ? ORDER BY seq DESC LIMIT 10",
            (tree_id,),
        ).fetchall()

        nodes: list[dict[str, Any]] = []
        for row in rows:
            node = node_dict(row)
            node_id = row["id"]
            node["evidence_count"] = evidence_counts.get(node_id, 0)
            node["kill_count"] = kill_counts.get(node_id, 0)
            node["comparison_count"] = comparison_counts.get(node_id, 0)
            node["blocked_by"] = blocked.get(node_id, [])
            node["blocked_without_open_question"] = (
                row["status"] == "blocked" and not node["blocked_by"]
            )
            nodes.append(node)

        open_questions = open_discriminators(open_question_rows, weights)
        undominated_summaries = [
            node_summary(row) for row in candidates if row["id"] in undominated
        ]
        assumption_floor = shared_assumptions(candidates, undominated)
    return {
        "database_path": str(store.database_path),
        "tree": tree,
        "counts": dict(counts),
        "nodes": nodes,
        "truncated": truncated,
        "rankings": rankings,
        "rankings_truncated": len(rankable) > max_parents,
        "ranked_shortlist": shortlist,
        "open_questions": open_questions,
        "shared_assumptions": assumption_floor,
        "undominated": undominated_summaries,
        "dominated": dominated,
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
    assumptions = normalize_assumptions(
        require_string_list(args, "assumptions") if "assumptions" in args else []
    )
    kill_condition = optional_text(args, "kill_condition") or ""
    depends_on = require_id_list(args, "depends_on") if "depends_on" in args else []
    metadata = optional_object(args, "metadata", {})
    if kind in ("idea", "synthesis"):
        if not assumptions:
            raise ToolFailure(
                f"kind `{kind}` requires at least one `assumptions` entry so siblings can be "
                "told apart structurally"
            )
        if not kill_condition:
            raise ToolFailure(
                f"kind `{kind}` requires a `kill_condition`: the observation that would "
                "retire this node"
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
        link_questions(connection, tree_id, node_id, depends_on, timestamp)
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
                "depends_on": depends_on,
            },
            timestamp,
        )
        node = node_dict(get_node_row(connection, tree_id, node_id))
    node["depends_on"] = depends_on
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
        clauses.append(
            "(title LIKE ? OR content LIKE ? OR assumptions_json LIKE ? "
            "OR kill_condition LIKE ? OR metadata_json LIKE ?)"
        )
        like = f"%{query}%"
        parameters.extend([like, like, like, like, like])
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
    if "kill_condition" in args:
        changes["kill_condition"] = require_text(args, "kill_condition")
    assumptions = None
    if "assumptions" in args:
        assumptions = normalize_assumptions(require_string_list(args, "assumptions"))
        changes["assumptions_json"] = compact_json(assumptions)
        changes["assumptions_key"] = assumptions_key(assumptions)
    depends_on = require_id_list(args, "depends_on") if "depends_on" in args else None
    metadata_patch = None
    if "metadata_patch" in args:
        metadata_patch = optional_object(args, "metadata_patch", {})
    if not changes and depends_on is None and metadata_patch is None:
        raise ToolFailure(
            "provide at least one mutable field: `title`, `content`, `status`, "
            "`kill_condition`, `assumptions`, `depends_on`, or `metadata_patch`"
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
        if before["kind"] in ("idea", "synthesis"):
            if assumptions is not None and not assumptions:
                raise ToolFailure(
                    f"kind `{before['kind']}` requires at least one `assumptions` entry"
                )
            if "kill_condition" in changes and not changes["kill_condition"]:
                raise ToolFailure(
                    f"kind `{before['kind']}` requires a non-empty `kill_condition`"
                )
        if assumptions is not None:
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
        if depends_on is not None:
            link_questions(connection, tree_id, node_id, depends_on, timestamp)

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
        if depends_on is not None:
            event_payload["depends_on"] = depends_on
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
        evaluation_cursor = connection.execute(
            f"UPDATE evaluations SET active = 0, invalidated_at = ?, invalidation_reason = ? "
            f"WHERE active = 1 AND node_id IN ({placeholders})",
            [timestamp, invalidation_reason, *ids],
        )
        comparison_cursor = connection.execute(
            f"UPDATE comparisons SET active = 0, invalidated_at = ?, invalidation_reason = ? "
            f"WHERE active = 1 AND (a_node_id IN ({placeholders}) "
            f"OR b_node_id IN ({placeholders}))",
            [timestamp, invalidation_reason, *ids, *ids],
        )
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
                "invalidated_evaluations": evaluation_cursor.rowcount,
                "invalidated_comparisons": comparison_cursor.rowcount,
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


def handle_select(args: dict[str, Any]) -> dict[str, Any]:
    tree_id = require_text(args, "tree_id")
    max_depth = optional_integer(args, "max_depth", 100, 1, 500)
    store = open_store(args)
    with store.connect() as connection:
        tree = get_tree_row(connection, tree_id)
        start_id = args.get("start_node_id", tree["root_node_id"])
        if not isinstance(start_id, str) or not start_id.strip():
            raise ToolFailure("`start_node_id` must be a non-empty string when provided")
        start_id = start_id.strip()
        start = get_node_row(connection, tree_id, start_id)
        if start["deleted_at"] is not None:
            raise ToolFailure(f"cannot select from deleted node {start_id}")

        scope = scoped_node_ids(connection, tree_id, start_id, max_depth)
        candidates = [row for row in candidate_rows(connection, tree_id) if row["id"] in scope]
        candidate_ids = {row["id"] for row in candidates}
        comparisons = active_comparison_rows(connection, tree_id)
        undominated, dominated = domination_report(candidates, comparisons)

        pair_counts: dict[str, int] = {node_id: 0 for node_id in candidate_ids}
        for row in comparisons:
            a_id, b_id = row["a_node_id"], row["b_node_id"]
            if a_id in candidate_ids and b_id in candidate_ids:
                pair_counts[a_id] += 1
                pair_counts[b_id] += 1
        evaluation_counts = {
            row["node_id"]: row["total"]
            for row in connection.execute(
                "SELECT node_id, COUNT(*) AS total FROM evaluations "
                "WHERE tree_id = ? AND active = 1 GROUP BY node_id",
                (tree_id,),
            ).fetchall()
        }

        groups: dict[str | None, list[sqlite3.Row]] = {}
        for row in candidates:
            groups.setdefault(row["parent_id"], []).append(row)
        ranked_frontier = [
            {"parent_id": parent_id, **rank_group(members, comparisons)}
            for parent_id, members in groups.items()
            if len(members) >= 2
        ]
        ranked_shortlist = rank_group(candidates, comparisons)

        weights = question_weights(connection, tree_id, undominated)
        open_question_rows = connection.execute(
            "SELECT * FROM questions WHERE tree_id = ? AND status = 'open' ORDER BY seq",
            (tree_id,),
        ).fetchall()
        open_questions = open_discriminators(open_question_rows, weights)
        open_user_questions = [entry for entry in open_questions if entry["source"] == "user"]
        # One list, one move: the discriminator carrying the most live branches per unit
        # of cost. An observation is run, an agent-side question or constraint is asked,
        # and a question the human still owes back is left in `open_user_questions`.
        top = next(
            (
                e
                for e in open_questions
                if e["weight"] >= QUESTION_WEIGHT_THRESHOLD
                and (e["kind"] == "observation" or e["source"] != "user")
            ),
            None,
        )
        is_observation = top is not None and top["kind"] == "observation"
        next_observation = top if is_observation else None
        next_question = top if top is not None and not is_observation else None

        blocked = blocked_by_map(connection, tree_id)
        unblocked_review = [
            node_summary(row)
            for row in candidates
            if row["status"] == "blocked" and not blocked.get(row["id"])
        ]

        examinable = [row for row in candidates if row["id"] in undominated] or candidates
        least_examined_row = min(
            examinable,
            key=lambda row: (
                pair_counts.get(row["id"], 0),
                evaluation_counts.get(row["id"], 0),
                row["seq"],
            ),
            default=None,
        )
        least_examined = None
        if least_examined_row is not None:
            least_examined = {
                **node_summary(least_examined_row),
                "comparisons": pair_counts.get(least_examined_row["id"], 0),
                "evaluations": evaluation_counts.get(least_examined_row["id"], 0),
            }

        undominated_summaries = [
            node_summary(row) for row in candidates if row["id"] in undominated
        ]
        assumption_floor = shared_assumptions(candidates, undominated)

    if not candidates:
        reason = "no_live_candidates"
    elif next_observation is not None:
        reason = "run_observation"
    elif next_question is not None:
        reason = "ask_question"
    elif len(candidates) < 2:
        reason = "expand_only_child"
    else:
        reason = "compare_least_examined"

    return {
        "tree_id": tree_id,
        "scope_node_id": start_id,
        "reason": reason,
        "question_rule": QUESTION_RULE,
        "candidate_count": len(candidates),
        "undominated": undominated_summaries,
        "dominated": dominated,
        "next_observation": next_observation,
        "next_question": next_question,
        "open_user_questions": open_user_questions,
        "shared_assumptions": assumption_floor,
        "least_examined": least_examined,
        "ranked_frontier": ranked_frontier,
        "ranked_shortlist": ranked_shortlist,
        "unblocked_review": unblocked_review,
        "mutated_state": False,
    }


def handle_evaluate(args: dict[str, Any]) -> dict[str, Any]:
    tree_id = require_text(args, "tree_id")
    node_id = require_text(args, "node_id")
    expected_version = require_integer(args, "expected_version", 1)
    outcome = enum_value(args, "outcome", EVALUATION_OUTCOMES)
    rationale = require_text(args, "rationale")
    evidence = require_evidence_objects(args, "evidence")
    source = enum_value(args, "source", JUDGMENT_SOURCES)
    question_id = optional_text(args, "question_id")
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
        if node["deleted_at"] is not None:
            raise ToolFailure(f"deleted node {node_id} cannot be evaluated")
        if node["version"] != expected_version:
            raise ToolFailure(
                f"version conflict for node {node_id}: expected {expected_version}, "
                f"current {node['version']}"
            )
        if question_id is not None:
            question = get_record_row(
                connection, "questions", tree_id, question_id, "question"
            )
            if question["kind"] != "observation":
                raise ToolFailure(
                    f"{question_id} is a `{question['kind']}`, and an evaluation only "
                    "reports the result of an `observation`; close a question or "
                    "constraint with `idea_question_answer`"
                )
            if question["status"] != "open":
                raise ToolFailure(
                    f"observation {question_id} is already {question['status']}; "
                    "raise a new one instead"
                )
            connection.execute(
                "UPDATE questions SET status = 'answered', answer = ?, answered_by = ?, "
                "answered_at = ?, updated_at = ?, version = version + 1 WHERE id = ?",
                (rationale, source, timestamp, timestamp, question_id),
            )
            append_event(
                connection,
                tree_id,
                None,
                "question.answered",
                {
                    "question_id": question_id,
                    "answer": rationale,
                    "answered_by": source,
                    "evaluation_id": evaluation_id,
                },
                timestamp,
            )
        connection.execute(
            "INSERT INTO evaluations(id, tree_id, node_id, outcome, rationale, evidence_json, "
            "source, question_id, active, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, ?)",
            (
                evaluation_id,
                tree_id,
                node_id,
                outcome,
                rationale,
                compact_json(evidence),
                source,
                question_id,
                timestamp,
            ),
        )
        if status_after is not None:
            connection.execute(
                "UPDATE nodes SET status = ?, updated_at = ?, version = version + 1 WHERE id = ?",
                (status_after, timestamp, node_id),
            )
        reopen_suggested = node["status"] == "rejected" and outcome == "supports"
        event_seq = append_event(
            connection,
            tree_id,
            node_id,
            "node.evaluated",
            {
                "evaluation_id": evaluation_id,
                "outcome": outcome,
                "rationale": rationale,
                "evidence": evidence,
                "source": source,
                "question_id": question_id,
                "status_before": node["status"],
                "status_after": status_after or node["status"],
                "reopen_suggested": reopen_suggested,
            },
            timestamp,
        )
        evaluation = connection.execute(
            "SELECT * FROM evaluations WHERE id = ?", (evaluation_id,)
        ).fetchone()
        updated_node = node_dict(get_node_row(connection, tree_id, node_id))
    return {
        "evaluation": evaluation_dict(evaluation),
        "node": updated_node,
        "reopen_suggested": reopen_suggested,
        "event_seq": event_seq,
    }


def handle_compare(args: dict[str, Any]) -> dict[str, Any]:
    tree_id = require_text(args, "tree_id")
    a_node_id = require_text(args, "a_node_id")
    b_node_id = require_text(args, "b_node_id")
    criterion = require_text(args, "criterion")
    winner = enum_value(args, "winner", COMPARISON_WINNERS)
    source = enum_value(args, "source", JUDGMENT_SOURCES)
    basis = require_id_list(args, "basis") if "basis" in args else []
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
            if node["deleted_at"] is not None or node["status"] not in CANDIDATE_STATUSES:
                raise ToolFailure(
                    f"node {node_id} is {node['status']} and cannot be a comparison operand; "
                    f"operands must be one of: {', '.join(CANDIDATE_STATUSES)}"
                )
        for evaluation_id in basis:
            evaluation = get_record_row(
                connection, "evaluations", tree_id, evaluation_id, "evaluation"
            )
            if not evaluation["active"]:
                raise ToolFailure(
                    f"evaluation {evaluation_id} is invalidated and cannot back a comparison"
                )
        connection.execute(
            "INSERT INTO comparisons(id, tree_id, a_node_id, b_node_id, criterion, winner, "
            "basis_json, source, active, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, ?)",
            (
                comparison_id,
                tree_id,
                a_node_id,
                b_node_id,
                criterion,
                winner,
                compact_json(basis),
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
                "source": source,
            },
            timestamp,
        )
        comparison = comparison_dict(
            get_record_row(connection, "comparisons", tree_id, comparison_id, "comparison")
        )
    return {"comparison": comparison, "event_seq": event_seq}


def handle_question_raise(args: dict[str, Any]) -> dict[str, Any]:
    tree_id = require_text(args, "tree_id")
    kind = enum_value(args, "kind", QUESTION_KINDS)
    text = require_text(args, "text")
    source = enum_value(args, "source", QUESTION_SOURCES)
    depends_on = require_id_list(args, "depends_on") if "depends_on" in args else []
    cost = DEFAULT_QUESTION_COST
    if "cost" in args:
        if kind != "observation":
            raise ToolFailure(
                "`cost` applies only to `observation`, where it is the work of checking; "
                f"a `{kind}` put to a human has no cost the server could know"
            )
        cost = require_number(args, "cost")
        if cost <= 0:
            raise ToolFailure("`cost` must be greater than 0")

    store = open_store(args)
    question_id = new_id("question")
    timestamp = utc_now()
    with store.connect() as connection, write_transaction(connection):
        tree = get_tree_row(connection, tree_id)
        ensure_tree_writable(tree)
        connection.execute(
            "INSERT INTO questions(id, tree_id, kind, text, cost, source, status, version, "
            "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, 'open', 1, ?, ?)",
            (question_id, tree_id, kind, text, cost, source, timestamp, timestamp),
        )
        link_nodes(connection, tree_id, question_id, depends_on, timestamp)
        undominated, _dominated = domination_report(
            candidate_rows(connection, tree_id), active_comparison_rows(connection, tree_id)
        )
        live = [node_id for node_id in depends_on if node_id in undominated]
        # The Platt filter: an observation that cannot tell two live branches apart
        # changes no judgment, so it is refused rather than stored. A question or a
        # constraint may legitimately concern one node, and is filtered only later,
        # by the same `weight >= 2` gate that decides what select surfaces.
        if kind == "observation" and len(live) < QUESTION_WEIGHT_THRESHOLD:
            raise ToolFailure(
                f"`depends_on` names only {len(live)} undominated candidate(s); an "
                f"observation must separate at least {QUESTION_WEIGHT_THRESHOLD} live "
                "branches to be worth running"
            )
        event_seq = append_event(
            connection,
            tree_id,
            None,
            "question.raised",
            {
                "question_id": question_id,
                "kind": kind,
                "text": text,
                "cost": cost,
                "source": source,
                "depends_on": depends_on,
            },
            timestamp,
        )
        question = question_dict(
            get_record_row(connection, "questions", tree_id, question_id, "question")
        )
    question["live_dependents"] = live
    question["weight"] = len(live)
    question["score"] = len(live) / cost
    return {"question": question, "event_seq": event_seq}


def handle_question_answer(args: dict[str, Any]) -> dict[str, Any]:
    tree_id = require_text(args, "tree_id")
    question_id = require_text(args, "question_id")
    expected_version = require_integer(args, "expected_version", 1)
    status = enum_value(args, "status", RESOLVED_QUESTION_STATUSES)
    answer = optional_text(args, "answer")
    answered_by = None
    if status == "answered":
        if answer is None:
            raise ToolFailure("`answer` is required when `status` is `answered`")
        answered_by = enum_value(args, "answered_by", JUDGMENT_SOURCES)
    elif answer is not None:
        raise ToolFailure("`answer` only applies when `status` is `answered`")

    store = open_store(args)
    timestamp = utc_now()
    with store.connect() as connection, write_transaction(connection):
        tree = get_tree_row(connection, tree_id)
        ensure_tree_writable(tree)
        question = get_record_row(connection, "questions", tree_id, question_id, "question")
        if question["status"] != "open":
            raise ToolFailure(
                f"question {question_id} is already {question['status']}"
            )
        if question["version"] != expected_version:
            raise ToolFailure(
                f"version conflict for question {question_id}: expected {expected_version}, "
                f"current {question['version']}"
            )
        connection.execute(
            "UPDATE questions SET status = ?, answer = ?, answered_by = ?, answered_at = ?, "
            "updated_at = ?, version = version + 1 WHERE id = ? AND version = ?",
            (
                status,
                answer,
                answered_by,
                timestamp,
                timestamp,
                question_id,
                expected_version,
            ),
        )
        blocked = blocked_by_map(connection, tree_id)
        dependents = connection.execute(
            "SELECT node_id FROM node_questions WHERE question_id = ?",
            (question_id,),
        ).fetchall()
        dependent_ids = {row["node_id"] for row in dependents}
        unblocked_candidates = [
            node_summary(row)
            for row in candidate_rows(connection, tree_id)
            if row["id"] in dependent_ids
            and row["status"] == "blocked"
            and not blocked.get(row["id"])
        ]
        event_seq = append_event(
            connection,
            tree_id,
            None,
            f"question.{status}",
            {
                "question_id": question_id,
                "answer": answer,
                "answered_by": answered_by,
                "unblocked_candidates": unblocked_candidates,
            },
            timestamp,
        )
        updated = question_dict(
            get_record_row(connection, "questions", tree_id, question_id, "question")
        )
    return {
        "question": updated,
        "unblocked_candidates": unblocked_candidates,
        "event_seq": event_seq,
    }


def handle_record_invalidate(args: dict[str, Any]) -> dict[str, Any]:
    tree_id = require_text(args, "tree_id")
    record_id = require_text(args, "record_id")
    reason = require_text(args, "reason")
    prefix = next((key for key in RECORD_TABLES if record_id.startswith(key)), None)
    if prefix is None:
        raise ToolFailure(
            "`record_id` must start with one of: " + ", ".join(sorted(RECORD_TABLES))
        )
    table, label = RECORD_TABLES[prefix]

    store = open_store(args)
    timestamp = utc_now()
    with store.connect() as connection, write_transaction(connection):
        tree = get_tree_row(connection, tree_id)
        ensure_tree_writable(tree)
        row = get_record_row(connection, table, tree_id, record_id, label)
        if not row["active"]:
            raise ToolFailure(f"{label} is already inactive: {record_id}")
        connection.execute(
            f"UPDATE {table} SET active = 0, invalidated_at = ?, invalidation_reason = ? "
            "WHERE id = ?",
            (timestamp, reason, record_id),
        )
        node_id = row["node_id"] if table == "evaluations" else row["a_node_id"]
        event_seq = append_event(
            connection,
            tree_id,
            node_id,
            f"{label}.invalidated",
            {"record_id": record_id, "reason": reason},
            timestamp,
        )
        updated = get_record_row(connection, table, tree_id, record_id, label)
        record = (
            evaluation_dict(updated) if table == "evaluations" else comparison_dict(updated)
        )
    return {"record": record, "record_kind": label, "event_seq": event_seq}


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
ASSUMPTIONS_PROPERTY = {
    "type": "array",
    "items": {"type": "string"},
    "description": (
        "The load-bearing assumptions this node makes. Two live siblings may not claim "
        "the same set. Required for `idea` and `synthesis`."
    ),
}
KILL_CONDITION_PROPERTY = {
    "type": "string",
    "description": (
        "The observation that would retire this node. Required for `idea` and `synthesis`."
    ),
}
DEPENDS_ON_PROPERTY = {
    "type": "array",
    "items": {"type": "string"},
    "description": (
        "Question IDs this node depends on. Declared explicitly; the server never matches "
        "question text. Replaces the whole set on update."
    ),
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
        "Create a project-local idea tree with an immutable goal and root node.",
        {
            "workspace": WORKSPACE_PROPERTY,
            "title": {"type": "string"},
            "goal": {"type": "string", "description": "Frozen, judgeable goal contract."},
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
        "Read the hierarchy with per-node evidence counts and open dependencies, plus "
        "sibling rankings, the tree-wide `ranked_shortlist`, the ranked open "
        "discriminators, the assumptions every survivor shares, the undominated set, "
        "and why every other candidate was pruned.",
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
        "Add a mechanism branch, testable idea, or synthesis node. An idea or synthesis "
        "must name its assumptions and its kill condition, no two live siblings may "
        "claim the same assumption set, and a child must add at least one assumption "
        "its parent does not already make.",
        {
            "workspace": WORKSPACE_PROPERTY,
            "tree_id": TREE_ID_PROPERTY,
            "parent_id": NODE_ID_PROPERTY,
            "kind": {"type": "string", "enum": ["branch", "idea", "synthesis"]},
            "title": {"type": "string"},
            "content": {
                "type": "string",
                "description": "Mechanism, observable effect, and comparator.",
            },
            "assumptions": ASSUMPTIONS_PROPERTY,
            "kill_condition": KILL_CONDITION_PROPERTY,
            "depends_on": DEPENDS_ON_PROPERTY,
            "metadata": {"type": "object", "additionalProperties": True},
        },
        ["workspace", "tree_id", "parent_id", "kind", "title", "content"],
        read_only=False,
    ),
    make_tool(
        "idea_node_get",
        "Read idea node",
        "Read one node with its evaluations, comparisons, declared questions, and events.",
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
        "Update mutable idea fields under an optimistic version check. Lineage and kind stay "
        "fixed; a repair is a new sibling with a different assumption set, not an edit.",
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
            "assumptions": ASSUMPTIONS_PROPERTY,
            "kill_condition": KILL_CONDITION_PROPERTY,
            "depends_on": DEPENDS_ON_PROPERTY,
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
        "Remove a node from the live ledger without physical erasure, invalidating its "
        "evaluations and every comparison that touches it. Non-leaf deletion requires cascade.",
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
        "Select the next move",
        "Say what to do next without mutating state. Every open question, constraint, "
        "and observation is ranked on one list by undominated dependents per unit of "
        "cost; the top entry carrying at least two live branches is run if it is an "
        "observation, asked if it is an agent-side question or constraint, and "
        "otherwise the least-examined candidate is compared. `ranked_frontier` ranks "
        "each sibling group, `ranked_shortlist` ranks every candidate in scope for "
        "delivery, `dominated` names the rival and criteria that pruned each node that "
        "left the live set, and `shared_assumptions` names what every survivor still "
        "takes for granted.",
        {
            "workspace": WORKSPACE_PROPERTY,
            "tree_id": TREE_ID_PROPERTY,
            "start_node_id": {
                "type": "string",
                "description": "Restrict candidates to this subtree. Defaults to the root.",
            },
            "max_depth": {"type": "integer", "minimum": 1, "maximum": 500, "default": 100},
        },
        ["workspace", "tree_id"],
        read_only=True,
    ),
    make_tool(
        "idea_evaluate",
        "Record an evaluation",
        "Attach one evidence-backed outcome to a node. Evaluations are never aggregated into "
        "a score and never propagate to ancestors; they are the objects a comparison cites. "
        "A `supports` evaluation on a rejected node reports `reopen_suggested`.",
        {
            "workspace": WORKSPACE_PROPERTY,
            "tree_id": TREE_ID_PROPERTY,
            "node_id": NODE_ID_PROPERTY,
            "expected_version": VERSION_PROPERTY,
            "outcome": {"type": "string", "enum": list(EVALUATION_OUTCOMES)},
            "rationale": {"type": "string"},
            "evidence": {
                "type": "array",
                "minItems": 1,
                "items": {
                    "type": "object",
                    "properties": {
                        "kind": {"type": "string", "enum": list(EVIDENCE_KINDS)},
                        "ref": {"type": "string"},
                        "cost": {"type": "number", "minimum": 0},
                    },
                    "required": ["kind", "ref", "cost"],
                    "additionalProperties": False,
                },
            },
            "source": JUDGMENT_SOURCE_PROPERTY,
            "question_id": {
                "type": "string",
                "description": (
                    "The open `observation` this result came from; answers it with this "
                    "rationale. A `question` or `constraint` is closed with "
                    "`idea_question_answer` instead."
                ),
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
            "outcome",
            "rationale",
            "evidence",
            "source",
        ],
        read_only=False,
    ),
    make_tool(
        "idea_compare",
        "Compare two candidates",
        "Record one pairwise judgment on a named criterion. Pairwise comparison is the only "
        "ranking input: candidates are ranked by a Bradley-Terry fit over these records, and "
        "a candidate that loses every comparison to one rival with no tie is dominated. The "
        "operands need not be siblings; a cross-parent judgment ranks in `ranked_shortlist` "
        "and is reported under `dominated` if it prunes.",
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
                "type": "array",
                "items": {"type": "string"},
                "description": "Evaluation IDs this judgment rests on.",
            },
            "source": JUDGMENT_SOURCE_PROPERTY,
        },
        ["workspace", "tree_id", "a_node_id", "b_node_id", "criterion", "winner", "source"],
        read_only=False,
    ),
    make_tool(
        "idea_question_raise",
        "Raise an open discriminator",
        "Add one open item whose answer changes which nodes survive: a `question` or "
        "`constraint` a human settles, or an `observation` the world settles. Tag where "
        "it came from: `user` said it, the agent `inferred` it, or the agent `assumed` "
        "it. An observation must already separate two undominated candidates, otherwise "
        "it cannot change any judgment and is refused.",
        {
            "workspace": WORKSPACE_PROPERTY,
            "tree_id": TREE_ID_PROPERTY,
            "kind": {"type": "string", "enum": list(QUESTION_KINDS)},
            "text": {"type": "string"},
            "source": {"type": "string", "enum": list(QUESTION_SOURCES)},
            "cost": {
                "type": "number",
                "exclusiveMinimum": 0,
                "description": (
                    "`observation` only: the work of checking it, in any consistent "
                    "unit. Defaults to 1. There is no ask-cost knob for a question or "
                    "constraint."
                ),
            },
            "depends_on": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Node IDs this item discriminates. Required in substance for an "
                    "observation: at least two of them must be undominated."
                ),
            },
        },
        ["workspace", "tree_id", "kind", "text", "source"],
        read_only=False,
    ),
    make_tool(
        "idea_question_answer",
        "Answer or withdraw a question",
        "Close one open question, constraint, or observation; `withdrawn` is how an "
        "observation is dropped. Blocked nodes are never unblocked automatically: the "
        "affected candidates come back as `unblocked_candidates` and in select's "
        "`unblocked_review` for a human decision.",
        {
            "workspace": WORKSPACE_PROPERTY,
            "tree_id": TREE_ID_PROPERTY,
            "question_id": {"type": "string"},
            "expected_version": VERSION_PROPERTY,
            "status": {"type": "string", "enum": list(RESOLVED_QUESTION_STATUSES)},
            "answer": {"type": "string", "description": "Required when answering."},
            "answered_by": {"type": "string", "enum": list(JUDGMENT_SOURCES)},
        },
        ["workspace", "tree_id", "question_id", "expected_version", "status"],
        read_only=False,
    ),
    make_tool(
        "idea_record_invalidate",
        "Retract a ledger record",
        "Retract one evaluation (`eval_`) or comparison (`cmp_`) with a reason. The "
        "record stays in the ledger; rankings and domination simply stop counting it.",
        {
            "workspace": WORKSPACE_PROPERTY,
            "tree_id": TREE_ID_PROPERTY,
            "record_id": {
                "type": "string",
                "description": "An `eval_` or `cmp_` ID.",
            },
            "reason": {"type": "string"},
        },
        ["workspace", "tree_id", "record_id", "reason"],
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
    "idea_compare": handle_compare,
    "idea_question_raise": handle_question_raise,
    "idea_question_answer": handle_question_answer,
    "idea_record_invalidate": handle_record_invalidate,
    "idea_tree_history": handle_history,
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
