"""Two drivers for the idea-tree MCP server, plus the fixtures the tests share.

In-process driver: import `scripts/idea_tree_server.py` and call its handlers
directly against a throwaway workspace. Real `ToolFailure` exceptions, no
JSON envelope in the way. Every state-machine test uses this.

Subprocess driver: run the server as its own process and speak line-delimited
JSON-RPC to it. Only the framing behaviors that the in-process path cannot
prove -- the stdin loop, notifications writing nothing, batch arrays -- use
this. `exchange` writes every line, closes stdin, and waits with a timeout, so
it can never hang the suite.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from typing import Any

TESTS_DIR = Path(__file__).resolve().parent
PLUGIN_ROOT = TESTS_DIR.parent
SCRIPTS_DIR = PLUGIN_ROOT / "scripts"
SERVER_PATH = SCRIPTS_DIR / "idea_tree_server.py"


def load_server():
    """Import the server module from its path, without any PYTHONPATH help."""
    already_loaded = sys.modules.get("idea_tree_server")
    if already_loaded is not None and hasattr(already_loaded, "HANDLERS"):
        return already_loaded
    if not SERVER_PATH.is_file():
        raise RuntimeError(f"server not found at {SERVER_PATH}")
    if str(SCRIPTS_DIR) not in sys.path:
        sys.path.insert(0, str(SCRIPTS_DIR))
    spec = importlib.util.spec_from_file_location("idea_tree_server", SERVER_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load a module spec from {SERVER_PATH}")
    module = importlib.util.module_from_spec(spec)
    sys.modules.setdefault("idea_tree_server", module)
    spec.loader.exec_module(module)
    return module


server = load_server()
ToolFailure = server.ToolFailure


# --------------------------------------------------------------------------
# Subprocess driver
# --------------------------------------------------------------------------


def exchange(lines: list[str], timeout: float = 30.0) -> tuple[list[str], str]:
    """Feed raw stdin lines to a fresh server process; return its stdout lines.

    stdin is closed after the last line, so `main()`'s `for raw_line in stdin`
    loop ends on its own and the process exits. Nothing here can block forever.
    """
    process = subprocess.Popen(
        [sys.executable, str(SERVER_PATH)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    payload = "".join(line + "\n" for line in lines)
    try:
        stdout, stderr = process.communicate(payload, timeout=timeout)
    except subprocess.TimeoutExpired:
        process.kill()
        process.communicate()
        raise AssertionError(
            f"server did not exit within {timeout}s after stdin was closed"
        ) from None
    return [line for line in stdout.splitlines() if line.strip()], stderr


def request(request_id: Any, method: str, params: dict[str, Any] | None = None) -> str:
    message: dict[str, Any] = {"jsonrpc": "2.0", "id": request_id, "method": method}
    if params is not None:
        message["params"] = params
    return json.dumps(message)


def notification(method: str, params: dict[str, Any] | None = None) -> str:
    message: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
    if params is not None:
        message["params"] = params
    return json.dumps(message)


def rpc(lines: list[str]) -> list[Any]:
    """Run the given raw lines and parse each stdout line as JSON."""
    stdout_lines, _stderr = exchange(lines)
    return [json.loads(line) for line in stdout_lines]


class LiveServer:
    """A server process kept alive across turns, spoken to one line at a time.

    `exchange` closes stdin, which is enough for most framing tests but cannot
    show that the loop answers *while* the pipe is still open. This driver can.
    A watchdog kills the process after `timeout`, so a `read_line` waiting for a
    line that will never come returns "" and fails the assertion instead of
    hanging the suite forever.
    """

    def __init__(self, timeout: float = 20.0) -> None:
        self.process = subprocess.Popen(
            [sys.executable, str(SERVER_PATH)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
        self._watchdog = threading.Timer(timeout, self._kill)
        self._watchdog.daemon = True
        self._watchdog.start()

    def _kill(self) -> None:
        if self.process.poll() is None:
            self.process.kill()

    def send(self, line: str) -> None:
        assert self.process.stdin is not None
        self.process.stdin.write(line + "\n")
        self.process.stdin.flush()

    def read_json(self) -> Any:
        assert self.process.stdout is not None
        line = self.process.stdout.readline()
        if not line:
            raise AssertionError("the server closed stdout before answering")
        return json.loads(line)

    def close(self) -> None:
        self._watchdog.cancel()
        if self.process.stdin is not None and not self.process.stdin.closed:
            try:
                self.process.stdin.close()
            except OSError:
                pass
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=5)
        for stream in (self.process.stdout, self.process.stderr):
            if stream is not None and not stream.closed:
                stream.close()


# --------------------------------------------------------------------------
# In-process driver + fixtures
# --------------------------------------------------------------------------


class ServerTestCase(unittest.TestCase):
    """Base case: one private workspace directory per test, cleaned up after."""

    def setUp(self) -> None:
        holder = tempfile.TemporaryDirectory(prefix="idea-tree-test-")
        self.addCleanup(holder.cleanup)
        # Resolve so the path the server echoes back matches the one we assert on.
        self.workspace = str(Path(holder.name).resolve(strict=True))

    # -- raw calls ---------------------------------------------------------

    def call(self, tool: str, **arguments: Any) -> dict[str, Any]:
        handler = server.HANDLERS[tool]
        return handler({"workspace": self.workspace, **arguments})

    def call_in(self, workspace: str, tool: str, **arguments: Any) -> dict[str, Any]:
        return server.HANDLERS[tool]({"workspace": workspace, **arguments})

    def assertToolFailure(self, expected_substring: str):
        """Context manager asserting a ToolFailure whose message says why."""
        return self.assertRaisesRegex(ToolFailure, expected_substring)

    # -- fixtures ----------------------------------------------------------

    def make_tree(self, title: str = "Tree", goal: str = "A judgeable goal") -> tuple[str, str]:
        """Create a tree; return (tree_id, root_node_id)."""
        result = self.call("idea_tree_create_tree", title=title, goal=goal)
        return result["tree"]["id"], result["root"]["id"]

    def make_idea(
        self,
        tree_id: str,
        parent_id: str,
        title: str,
        assumptions: list[str] | None = None,
        kill_condition: str = "a measurement that retires it",
        **extra: Any,
    ) -> str:
        """Create an `idea` node with a distinct assumption set; return its id."""
        result = self.call(
            "idea_node_create",
            tree_id=tree_id,
            parent_id=parent_id,
            kind="idea",
            title=title,
            content=f"mechanism, effect, and comparator for {title}",
            assumptions=assumptions if assumptions is not None else [f"assumption of {title}"],
            kill_condition=kill_condition,
            **extra,
        )
        return result["node"]["id"]

    def make_branch(self, tree_id: str, parent_id: str, title: str, **extra: Any) -> str:
        result = self.call(
            "idea_node_create",
            tree_id=tree_id,
            parent_id=parent_id,
            kind="branch",
            title=title,
            content=f"grouping for {title}",
            **extra,
        )
        return result["node"]["id"]

    def make_siblings(self, count: int, parent_kind: str = "branch") -> tuple[str, str, list[str]]:
        """Build tree -> branch -> N ideas. Return (tree_id, parent_id, [node_ids])."""
        tree_id, root_id = self.make_tree()
        parent_id = (
            self.make_branch(tree_id, root_id, "Branch") if parent_kind == "branch" else root_id
        )
        names = [chr(ord("A") + index) for index in range(count)]
        node_ids = [self.make_idea(tree_id, parent_id, f"Idea {name}") for name in names]
        return tree_id, parent_id, node_ids

    def compare(
        self,
        tree_id: str,
        a_node_id: str,
        b_node_id: str,
        winner: str,
        criterion: str = "expected information per unit cost",
        source: str = "user",
        **extra: Any,
    ) -> str:
        result = self.call(
            "idea_compare",
            tree_id=tree_id,
            a_node_id=a_node_id,
            b_node_id=b_node_id,
            criterion=criterion,
            winner=winner,
            source=source,
            **extra,
        )
        return result["comparison"]["id"]

    def evaluate(
        self,
        tree_id: str,
        node_id: str,
        expected_version: int,
        outcome: str = "supports",
        rationale: str = "the measurement came out on this side",
        source: str = "agent",
        **extra: Any,
    ) -> dict[str, Any]:
        return self.call(
            "idea_evaluate",
            tree_id=tree_id,
            node_id=node_id,
            expected_version=expected_version,
            outcome=outcome,
            rationale=rationale,
            evidence=[{"kind": "experiment", "ref": "run-1", "cost": 1.0}],
            source=source,
            **extra,
        )

    def raise_question(
        self, tree_id: str, text: str, source: str, kind: str = "question", **extra: Any
    ) -> dict[str, Any]:
        return self.call(
            "idea_question_raise",
            tree_id=tree_id,
            kind=kind,
            text=text,
            source=source,
            **extra,
        )["question"]

    def raise_observation(
        self,
        tree_id: str,
        depends_on: list[str],
        cost: float,
        text: str = "run the discriminating measurement",
        source: str = "inferred",
    ) -> dict[str, Any]:
        """An `observation`: the one kind of open item that carries a cost."""
        return self.raise_question(
            tree_id,
            text,
            source,
            kind="observation",
            cost=cost,
            depends_on=depends_on,
        )

    # -- readers -----------------------------------------------------------

    def select(self, tree_id: str, **extra: Any) -> dict[str, Any]:
        return self.call("idea_tree_select", tree_id=tree_id, **extra)

    def node(self, tree_id: str, node_id: str) -> dict[str, Any]:
        return self.call("idea_node_get", tree_id=tree_id, node_id=node_id)

    def open_questions(self, tree_id: str) -> list[dict[str, Any]]:
        """The snapshot's one ranked list of open questions, constraints, observations."""
        return self.call("idea_tree_snapshot", tree_id=tree_id)["open_questions"]

    def frontier_by_node(self, selection: dict[str, Any]) -> dict[str, dict[str, Any]]:
        """Flatten `ranked_frontier` into node_id -> ranking entry."""
        entries: dict[str, dict[str, Any]] = {}
        for group in selection["ranked_frontier"]:
            for entry in group["nodes"]:
                entries[entry["node_id"]] = entry
        return entries

    def undominated_ids(self, selection: dict[str, Any]) -> set[str]:
        return {entry["node_id"] for entry in selection["undominated"]}

    def shortlist_by_node(self, payload: dict[str, Any]) -> dict[str, dict[str, Any]]:
        """Flatten the tree-wide `ranked_shortlist` into node_id -> ranking entry."""
        return {
            entry["node_id"]: entry for entry in payload["ranked_shortlist"]["nodes"]
        }

    def dominated_by_node(self, payload: dict[str, Any]) -> dict[str, dict[str, Any]]:
        """Flatten `dominated` into node_id -> the record of why it was pruned."""
        return {entry["node_id"]: entry for entry in payload["dominated"]}
