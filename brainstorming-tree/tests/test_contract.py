"""The refusals: what the server will not let a caller record.

Workspace shape, schema version, optimistic concurrency, the structural fields
an `idea` must carry, and the rule that two live siblings may not claim the
same assumption set.
"""

from __future__ import annotations

import os
import sqlite3
import sys
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from harness import ServerTestCase, ToolFailure  # noqa: E402


class WorkspaceTest(ServerTestCase):
    def test_a_relative_workspace_is_refused_before_anything_is_written(self) -> None:
        with self.assertToolFailure("must be an absolute directory path"):
            self.call_in("relative/workspace", "idea_tree_create_tree", title="T", goal="G")
        self.assertFalse(
            (Path.cwd() / "relative").exists(), "a refused call must not create directories"
        )

    def test_a_workspace_that_does_not_exist_is_refused_and_is_not_created(self) -> None:
        missing = Path(self.workspace) / "missing"
        with self.assertToolFailure("workspace does not exist"):
            self.call_in(str(missing), "idea_tree_create_tree", title="T", goal="G")
        self.assertFalse(missing.exists(), "a refused call must not create the workspace")

    def test_a_workspace_that_is_a_file_is_refused(self) -> None:
        target = Path(self.workspace) / "notadir.txt"
        target.write_text("", encoding="utf-8")
        with self.assertToolFailure("workspace is not a directory"):
            self.call_in(str(target), "idea_tree_create_tree", title="T", goal="G")

    def test_reading_a_workspace_with_no_database_says_to_create_a_tree_first(self) -> None:
        with self.assertToolFailure("create a tree first"):
            self.call("idea_tree_list_trees")

    def test_creating_a_tree_puts_the_database_under_dot_idea_tree(self) -> None:
        result = self.call("idea_tree_create_tree", title="T", goal="G")
        expected = Path(self.workspace) / ".idea-tree" / "ideas.sqlite3"
        self.assertEqual(result["database_path"], str(expected))
        self.assertTrue(expected.is_file())


class SchemaVersionTest(ServerTestCase):
    def workspace_at_version(self, version: int) -> str:
        """A workspace holding a database stamped with `version` and nothing else."""
        workspace = Path(self.workspace) / f"user_version_{version}"
        (workspace / ".idea-tree").mkdir(parents=True)
        connection = sqlite3.connect(workspace / ".idea-tree" / "ideas.sqlite3")
        connection.executescript(
            f"CREATE TABLE legacy(id TEXT); PRAGMA user_version = {version};"
        )
        connection.close()
        return str(workspace)

    def test_a_pre_v0_3_database_is_refused_because_v0_3_is_a_fresh_schema(self) -> None:
        """There is no migration: a v0.1 or v0.2 file is told to move aside."""
        for version in (1, 2):
            with self.subTest(version=version):
                workspace = self.workspace_at_version(version)
                with self.assertToolFailure(f"v0.{version} database found"):
                    self.call_in(workspace, "idea_tree_create_tree", title="T", goal="G")

    def test_an_unknown_future_schema_version_is_refused_by_number(self) -> None:
        workspace = self.workspace_at_version(4)
        with self.assertToolFailure("schema version 4 is unsupported; expected 3"):
            self.call_in(workspace, "idea_tree_create_tree", title="T", goal="G")


class NodeStructureTest(ServerTestCase):
    def test_an_idea_without_assumptions_is_refused(self) -> None:
        tree_id, root_id = self.make_tree()
        with self.assertToolFailure("requires at least one `assumptions` entry"):
            self.call(
                "idea_node_create",
                tree_id=tree_id,
                parent_id=root_id,
                kind="idea",
                title="Idea A",
                content="mechanism",
                kill_condition="the measurement that retires it",
            )

    def test_an_idea_without_a_kill_condition_is_accepted(self) -> None:
        """A kill condition is prose worth having, not a gate on recording an idea."""
        tree_id, root_id = self.make_tree()
        node = self.call(
            "idea_node_create",
            tree_id=tree_id,
            parent_id=root_id,
            kind="idea",
            title="Idea A",
            content="mechanism",
            assumptions=["the sensor is linear"],
        )["node"]
        self.assertEqual(node["kill_condition"], "")
        self.assertEqual(node["assumptions"], ["the sensor is linear"])

    def test_an_update_may_clear_a_kill_condition_back_to_empty(self) -> None:
        tree_id, root_id = self.make_tree()
        node_id = self.make_idea(
            tree_id, root_id, "Idea A", kill_condition="the measurement that retires it"
        )
        node = self.call(
            "idea_node_update",
            tree_id=tree_id,
            node_id=node_id,
            expected_version=1,
            kill_condition="",
        )["node"]
        self.assertEqual(node["kill_condition"], "")

    def test_a_synthesis_needs_assumptions_of_its_own(self) -> None:
        tree_id, root_id = self.make_tree()
        with self.assertToolFailure("requires at least one `assumptions` entry"):
            self.call(
                "idea_node_create",
                tree_id=tree_id,
                parent_id=root_id,
                kind="synthesis",
                title="Merge",
                content="combined mechanism",
                kill_condition="the merged prediction fails",
            )

    def test_only_idea_and_synthesis_can_be_created(self) -> None:
        tree_id, root_id = self.make_tree()
        with self.assertToolFailure("`kind` must be one of: idea, synthesis"):
            self.call(
                "idea_node_create",
                tree_id=tree_id,
                parent_id=root_id,
                kind="branch",
                title="Mechanism family",
                content="a grouping, not a claim",
                assumptions=["groupings exist"],
            )

    def test_the_root_holds_the_frozen_goal_and_cannot_be_updated(self) -> None:
        tree_id, root_id = self.make_tree()
        with self.assertToolFailure("frozen goal"):
            self.call(
                "idea_node_update",
                tree_id=tree_id,
                node_id=root_id,
                expected_version=1,
                title="New goal",
            )


class DuplicateAssumptionsTest(ServerTestCase):
    def test_a_sibling_repeating_a_live_assumption_set_is_refused_by_name(self) -> None:
        tree_id, root_id = self.make_tree()
        parent_id = self.make_parent(tree_id, root_id, "Family")
        first = self.make_idea(
            tree_id, parent_id, "Idea A", assumptions=["Latency  dominates", "cache is warm"]
        )
        with self.assertToolFailure("already claims this exact assumption set"):
            self.make_idea(
                tree_id,
                parent_id,
                "Idea A restated",
                # Same set after case folding, whitespace collapse, and reordering.
                assumptions=["CACHE IS WARM", "latency dominates"],
            )
        try:
            self.make_idea(
                tree_id, parent_id, "Idea A restated", assumptions=["cache is warm", "latency dominates"]
            )
        except Exception as exc:  # noqa: BLE001 - we assert on the message text
            self.assertIn(first, str(exc), "the refusal must name the existing sibling")
            self.assertIn("Idea A", str(exc), "the refusal must name the existing sibling")
        else:
            self.fail("a duplicate assumption set must be refused")

    def test_the_same_assumption_set_is_allowed_under_a_different_parent(self) -> None:
        tree_id, root_id = self.make_tree()
        left = self.make_parent(tree_id, root_id, "Left")
        right = self.make_parent(tree_id, root_id, "Right")
        self.make_idea(tree_id, left, "Idea A", assumptions=["the sensor is linear"])
        node_id = self.make_idea(tree_id, right, "Idea A", assumptions=["the sensor is linear"])
        self.assertTrue(node_id, "assumption uniqueness is scoped to one sibling group")

    def test_a_tombstoned_sibling_frees_its_assumption_set(self) -> None:
        tree_id, root_id = self.make_tree()
        parent_id = self.make_parent(tree_id, root_id, "Family")
        first = self.make_idea(tree_id, parent_id, "Idea A", assumptions=["the sensor is linear"])
        self.call(
            "idea_node_delete",
            tree_id=tree_id,
            node_id=first,
            expected_version=1,
            reason="superseded",
        )
        replacement = self.make_idea(
            tree_id, parent_id, "Idea A again", assumptions=["the sensor is linear"]
        )
        self.assertNotEqual(replacement, first)


class ChildAddsAnAssumptionTest(ServerTestCase):
    """A child that assumes nothing new is a parameter tweak wearing an idea's clothes."""

    def test_a_child_repeating_its_parents_assumptions_is_refused_as_a_variation(self) -> None:
        tree_id, root_id = self.make_tree()
        parent = self.make_idea(tree_id, root_id, "Idea A", assumptions=["the sensor is linear"])
        with self.assertToolFailure("that is a parameter variation, not a new idea"):
            self.make_idea(
                # Same set after case folding and whitespace collapse.
                tree_id, parent, "Idea A at k=8", assumptions=["The Sensor  is Linear"]
            )

    def test_the_refusal_names_the_parent_and_says_what_to_do_instead(self) -> None:
        tree_id, root_id = self.make_tree()
        parent = self.make_idea(tree_id, root_id, "Idea A", assumptions=["the sensor is linear"])
        try:
            self.make_idea(tree_id, parent, "Idea A at k=8", assumptions=["the sensor is linear"])
        except ToolFailure as exc:
            self.assertIn(parent, str(exc))
            self.assertIn("Idea A", str(exc))
            self.assertIn("comparison on the parent", str(exc))
        else:
            self.fail("a child adding no assumption must be refused")

    def test_a_subset_of_the_parents_assumptions_is_still_no_new_assumption(self) -> None:
        tree_id, root_id = self.make_tree()
        parent = self.make_idea(
            tree_id, root_id, "Idea A", assumptions=["the sensor is linear", "the cache is warm"]
        )
        with self.assertToolFailure("parameter variation"):
            self.make_idea(tree_id, parent, "Idea A narrowed", assumptions=["the cache is warm"])

    def test_one_assumption_the_parent_does_not_make_is_enough(self) -> None:
        tree_id, root_id = self.make_tree()
        parent = self.make_idea(tree_id, root_id, "Idea A", assumptions=["the sensor is linear"])
        child = self.make_idea(
            tree_id,
            parent,
            "Idea A with drift",
            assumptions=["the sensor is linear", "drift is bounded"],
        )
        self.assertTrue(child)

    def test_an_idea_under_the_root_is_exempt(self) -> None:
        tree_id, root_id = self.make_tree()
        self.assertTrue(
            self.make_idea(tree_id, root_id, "Idea A", assumptions=["the sensor is linear"])
        )

    def test_updating_a_child_down_to_its_parents_set_is_refused_too(self) -> None:
        tree_id, root_id = self.make_tree()
        parent = self.make_idea(tree_id, root_id, "Idea A", assumptions=["the sensor is linear"])
        child = self.make_idea(
            tree_id,
            parent,
            "Idea A with drift",
            assumptions=["the sensor is linear", "drift is bounded"],
        )
        with self.assertToolFailure("parameter variation"):
            self.call(
                "idea_node_update",
                tree_id=tree_id,
                node_id=child,
                expected_version=1,
                assumptions=["the sensor is linear"],
            )

    def test_the_sibling_dedup_still_fires_under_a_parent_with_assumptions(self) -> None:
        """The two rules are separate: adding an assumption does not license a repeat."""
        tree_id, root_id = self.make_tree()
        parent = self.make_idea(tree_id, root_id, "Idea A", assumptions=["the sensor is linear"])
        self.make_idea(
            tree_id,
            parent,
            "Idea A with drift",
            assumptions=["the sensor is linear", "drift is bounded"],
        )
        with self.assertToolFailure("already claims this exact assumption set"):
            self.make_idea(
                tree_id,
                parent,
                "Idea A with drift restated",
                assumptions=["drift is bounded", "the sensor is linear"],
            )


class TransactionTest(ServerTestCase):
    def test_a_refused_supersede_leaves_neither_a_new_tree_nor_a_changed_predecessor(
        self,
    ) -> None:
        """The predecessor check and both writes share one transaction."""
        tree_id, _root_id = self.make_tree()
        successor_id, _successor_root = self.supersede(tree_id)
        before = self.call("idea_tree_list_trees")["trees"]

        with self.assertToolFailure("already superseded by"):
            self.supersede(tree_id, title="A second successor")

        after = self.call("idea_tree_list_trees")["trees"]
        self.assertEqual([tree["id"] for tree in after], [tree["id"] for tree in before])
        predecessor = next(tree for tree in after if tree["id"] == tree_id)
        self.assertEqual(predecessor["superseded_by"], successor_id)


class EventPayloadTest(ServerTestCase):
    def test_the_node_updated_event_does_not_leak_the_internal_assumptions_key(self) -> None:
        """The audit trail carries the assumption list, never the internal key.

        `handle_update_node` filters all three internal columns -- `metadata_json`,
        `assumptions_json`, and the U+001F-joined `assumptions_key` -- out of the
        `node.updated` payload. The readable list is the payload's own
        `assumptions` field, so the key would be internal and redundant both.
        """
        tree_id, root_id = self.make_tree()
        node_id = self.make_idea(tree_id, root_id, "Idea A", assumptions=["first claim"])
        self.call(
            "idea_node_update",
            tree_id=tree_id,
            node_id=node_id,
            expected_version=1,
            assumptions=["second claim", "third claim"],
        )
        events = self.events(self.snapshot(tree_id))
        payload = next(
            event for event in events if event["operation"] == "node.updated"
        )["payload"]
        self.assertEqual(payload["assumptions"], ["second claim", "third claim"])
        self.assertNotIn("assumptions_key", payload["before"])
        self.assertNotIn("assumptions_key", payload["after"])


class SupersedeTest(ServerTestCase):
    """The goal is frozen: a changed premise is a new tree, not an edited one."""

    def test_superseding_sets_the_pointer_in_both_directions(self) -> None:
        old_id, _old_root = self.make_tree()
        new_id, _new_root = self.supersede(old_id)
        trees = {tree["id"]: tree for tree in self.call("idea_tree_list_trees")["trees"]}
        self.assertEqual(trees[old_id]["superseded_by"], new_id)
        self.assertIsNone(trees[old_id]["supersedes"])
        self.assertEqual(trees[new_id]["supersedes"], old_id)
        self.assertIsNone(trees[new_id]["superseded_by"])

    def test_a_superseded_tree_refuses_every_mutation_and_names_its_successor(self) -> None:
        old_id, old_root = self.make_tree()
        first = self.make_idea(old_id, old_root, "Idea A")
        second = self.make_idea(old_id, old_root, "Idea B")
        new_id, _new_root = self.supersede(old_id)

        calls = [
            ("idea_node_create", dict(
                tree_id=old_id, parent_id=old_root, kind="idea", title="Idea C",
                content="mechanism", assumptions=["a later thought"],
            )),
            ("idea_node_update", dict(
                tree_id=old_id, node_id=first, expected_version=1, title="Renamed",
            )),
            ("idea_node_delete", dict(
                tree_id=old_id, node_id=first, expected_version=1, reason="second thoughts",
            )),
            ("idea_compare", dict(
                tree_id=old_id, a_node_id=first, b_node_id=second, criterion="cost",
                winner="a", source="user",
            )),
        ]
        for tool, arguments in calls:
            with self.subTest(tool=tool):
                with self.assertToolFailure(f"superseded by {new_id}"):
                    self.call(tool, **arguments)

    def test_superseding_the_same_tree_twice_is_refused_naming_the_successor(self) -> None:
        old_id, _old_root = self.make_tree()
        new_id, _new_root = self.supersede(old_id)
        with self.assertToolFailure(f"already superseded by {new_id}"):
            self.supersede(old_id, title="A second successor")

    def test_superseding_an_unknown_tree_is_refused(self) -> None:
        self.make_tree()
        with self.assertToolFailure("tree not found: tree_missing"):
            self.supersede("tree_missing")

    def test_list_trees_can_show_only_the_heads_of_each_chain(self) -> None:
        old_id, _old_root = self.make_tree()
        new_id, _new_root = self.supersede(old_id)
        all_ids = [tree["id"] for tree in self.call("idea_tree_list_trees")["trees"]]
        self.assertEqual(all_ids, [old_id, new_id])
        heads = self.call("idea_tree_list_trees", include_superseded=False)["trees"]
        self.assertEqual([tree["id"] for tree in heads], [new_id])

    def test_the_predecessors_ledger_records_that_it_was_superseded(self) -> None:
        old_id, _old_root = self.make_tree()
        new_id, _new_root = self.supersede(old_id)
        event = next(
            item
            for item in self.events(self.snapshot(old_id))
            if item["operation"] == "tree.superseded"
        )
        self.assertEqual(event["payload"]["successor_tree_id"], new_id)


class NodeVersionTest(ServerTestCase):
    def test_a_stale_expected_version_is_refused_naming_both_numbers(self) -> None:
        tree_id, root_id = self.make_tree()
        node_id = self.make_idea(tree_id, root_id, "Idea A")
        self.call(
            "idea_node_update",
            tree_id=tree_id,
            node_id=node_id,
            expected_version=1,
            title="Idea A, sharpened",
        )
        with self.assertToolFailure(r"version conflict for node .*expected 1, current 2"):
            self.call(
                "idea_node_update",
                tree_id=tree_id,
                node_id=node_id,
                expected_version=1,
                title="Idea A, sharpened again",
            )

    def test_every_accepted_update_increments_the_version(self) -> None:
        tree_id, root_id = self.make_tree()
        node_id = self.make_idea(tree_id, root_id, "Idea A")
        for expected in (1, 2, 3):
            node = self.call(
                "idea_node_update",
                tree_id=tree_id,
                node_id=node_id,
                expected_version=expected,
                content=f"mechanism, revision {expected}",
            )["node"]
            self.assertEqual(node["version"], expected + 1)

    def test_the_version_the_snapshot_reports_is_the_one_an_update_accepts(self) -> None:
        tree_id, root_id = self.make_tree()
        node_id = self.make_idea(tree_id, root_id, "Idea A")
        self.call(
            "idea_node_update",
            tree_id=tree_id,
            node_id=node_id,
            expected_version=1,
            title="Idea A, sharpened",
        )
        current = self.nodes_by_id(self.snapshot(tree_id))[node_id]["version"]
        updated = self.call(
            "idea_node_update",
            tree_id=tree_id,
            node_id=node_id,
            expected_version=current,
            title="Idea A, sharpened twice",
        )["node"]
        self.assertEqual(updated["version"], current + 1)


class CompareBasisTest(ServerTestCase):
    def test_the_basis_and_its_refs_round_trip(self) -> None:
        tree_id, _parent_id, (a, b) = self.make_siblings(2)
        comparison = self.call(
            "idea_compare",
            tree_id=tree_id,
            a_node_id=a,
            b_node_id=b,
            criterion="cost",
            winner="a",
            source="user",
            basis="A needs one rig, B needs three.",
            refs=["run-14", "notes/2026-09-04.md"],
        )["comparison"]
        self.assertEqual(comparison["basis"], "A needs one rig, B needs three.")
        self.assertEqual(comparison["refs"], ["run-14", "notes/2026-09-04.md"])
        self.assertEqual(comparison["source"], "user")

    def test_both_the_basis_and_the_refs_are_optional(self) -> None:
        tree_id, _parent_id, (a, b) = self.make_siblings(2)
        comparison = self.call(
            "idea_compare",
            tree_id=tree_id,
            a_node_id=a,
            b_node_id=b,
            criterion="cost",
            winner="tie",
            source="agent",
        )["comparison"]
        self.assertEqual(comparison["basis"], "")
        self.assertEqual(comparison["refs"], [])

    def test_comparing_a_node_with_itself_is_refused(self) -> None:
        tree_id, _parent_id, (a, _b) = self.make_siblings(2)
        with self.assertToolFailure("needs two different nodes"):
            self.compare(tree_id, a, a, "a")

    def test_a_tombstoned_node_cannot_be_a_comparison_operand(self) -> None:
        tree_id, _parent_id, (a, b) = self.make_siblings(2)
        self.call(
            "idea_node_delete",
            tree_id=tree_id,
            node_id=b,
            expected_version=1,
            reason="folded into A",
        )
        with self.assertToolFailure(f"node {b} is deleted"):
            self.compare(tree_id, a, b, "a")

    def test_the_root_is_not_a_comparison_operand(self) -> None:
        tree_id, root_id = self.make_tree()
        node_id = self.make_idea(tree_id, root_id, "Idea A")
        with self.assertToolFailure("frozen goal"):
            self.compare(tree_id, node_id, root_id, "a")


if __name__ == "__main__":
    unittest.main()
