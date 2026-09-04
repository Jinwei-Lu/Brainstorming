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
    def test_a_v0_1_database_is_refused_because_v0_2_is_a_fresh_schema(self) -> None:
        database_dir = Path(self.workspace) / ".idea-tree"
        database_dir.mkdir()
        connection = sqlite3.connect(database_dir / "ideas.sqlite3")
        connection.executescript("CREATE TABLE legacy(id TEXT); PRAGMA user_version = 1;")
        connection.close()
        with self.assertToolFailure("v0.1 database found"):
            self.call("idea_tree_create_tree", title="T", goal="G")

    def test_an_unknown_future_schema_version_is_refused_by_number(self) -> None:
        database_dir = Path(self.workspace) / ".idea-tree"
        database_dir.mkdir()
        connection = sqlite3.connect(database_dir / "ideas.sqlite3")
        connection.executescript("CREATE TABLE future(id TEXT); PRAGMA user_version = 3;")
        connection.close()
        with self.assertToolFailure("schema version 3 is unsupported; expected 2"):
            self.call("idea_tree_create_tree", title="T", goal="G")


class TreeVersionTest(ServerTestCase):
    def test_updating_a_tree_with_a_stale_expected_version_is_a_version_conflict(self) -> None:
        tree_id, _root_id = self.make_tree()
        self.call("idea_tree_update_tree", tree_id=tree_id, expected_version=1, title="Renamed")
        with self.assertToolFailure(r"version conflict for tree .*expected 1, current 2"):
            self.call(
                "idea_tree_update_tree", tree_id=tree_id, expected_version=1, title="Again"
            )

    def test_a_completed_tree_refuses_node_mutations_until_it_is_active_again(self) -> None:
        tree_id, root_id = self.make_tree()
        self.call(
            "idea_tree_update_tree", tree_id=tree_id, expected_version=1, status="completed"
        )
        with self.assertToolFailure("is completed; set it to `active`"):
            self.make_idea(tree_id, root_id, "Idea A")


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

    def test_an_idea_without_a_kill_condition_is_refused(self) -> None:
        tree_id, root_id = self.make_tree()
        with self.assertToolFailure("requires a `kill_condition`"):
            self.call(
                "idea_node_create",
                tree_id=tree_id,
                parent_id=root_id,
                kind="idea",
                title="Idea A",
                content="mechanism",
                assumptions=["the sensor is linear"],
            )

    def test_a_synthesis_needs_both_assumptions_and_a_kill_condition(self) -> None:
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

    def test_a_branch_needs_neither_assumptions_nor_a_kill_condition(self) -> None:
        tree_id, root_id = self.make_tree()
        node = self.call(
            "idea_node_create",
            tree_id=tree_id,
            parent_id=root_id,
            kind="branch",
            title="Mechanism family",
            content="a grouping, not a claim",
        )["node"]
        self.assertEqual(node["kind"], "branch")
        self.assertEqual(node["assumptions"], [])
        self.assertEqual(node["kill_condition"], "")

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
        parent_id = self.make_branch(tree_id, root_id, "Branch")
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
        left = self.make_branch(tree_id, root_id, "Left")
        right = self.make_branch(tree_id, root_id, "Right")
        self.make_idea(tree_id, left, "Idea A", assumptions=["the sensor is linear"])
        node_id = self.make_idea(tree_id, right, "Idea A", assumptions=["the sensor is linear"])
        self.assertTrue(node_id, "assumption uniqueness is scoped to one sibling group")

    def test_a_rejected_sibling_still_holds_its_assumption_set(self) -> None:
        """Restating a killed assumption set is repeating a known failure mode."""
        tree_id, root_id = self.make_tree()
        parent_id = self.make_branch(tree_id, root_id, "Branch")
        first = self.make_idea(tree_id, parent_id, "Idea A", assumptions=["Hard  Cuts break Attention"])
        self.call(
            "idea_node_update",
            tree_id=tree_id,
            node_id=first,
            expected_version=1,
            status="rejected",
        )
        with self.assertToolFailure("already claims this exact assumption set"):
            self.make_idea(
                tree_id, parent_id, "Idea A restated", assumptions=["hard cuts break attention"]
            )

    def test_a_tombstoned_sibling_frees_its_assumption_set(self) -> None:
        tree_id, root_id = self.make_tree()
        parent_id = self.make_branch(tree_id, root_id, "Branch")
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
            self.assertIn("comparison or evaluation on the parent", str(exc))
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

    def test_an_idea_under_a_branch_with_no_assumptions_is_exempt(self) -> None:
        tree_id, root_id = self.make_tree()
        branch = self.make_branch(tree_id, root_id, "Mechanism family")
        node_id = self.make_idea(tree_id, branch, "Idea A", assumptions=["the sensor is linear"])
        self.assertTrue(node_id, "a branch states no assumption to inherit")

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


class RecordInvalidateRoutingTest(ServerTestCase):
    def test_each_record_prefix_routes_to_its_own_table(self) -> None:
        tree_id, _parent_id, node_ids = self.make_siblings(2)
        evaluation_id = self.evaluate(tree_id, node_ids[0], 1)["evaluation"]["id"]
        comparison_id = self.compare(tree_id, node_ids[0], node_ids[1], "a")

        self.assertTrue(evaluation_id.startswith("eval_"))
        self.assertTrue(comparison_id.startswith("cmp_"))

        retracted_evaluation = self.call(
            "idea_record_invalidate",
            tree_id=tree_id,
            record_id=evaluation_id,
            reason="the reference was wrong",
        )
        self.assertEqual(retracted_evaluation["record_kind"], "evaluation")
        self.assertFalse(retracted_evaluation["record"]["active"])

        retracted_comparison = self.call(
            "idea_record_invalidate",
            tree_id=tree_id,
            record_id=comparison_id,
            reason="the criterion was mis-stated",
        )
        self.assertEqual(retracted_comparison["record_kind"], "comparison")
        self.assertFalse(retracted_comparison["record"]["active"])

    def test_a_record_id_with_no_known_prefix_is_refused(self) -> None:
        """`obs_` is one of those unknown prefixes now: observations live in `questions`."""
        tree_id, _root_id = self.make_tree()
        for record_id in ("node_deadbeef", "obs_deadbeef", "question_deadbeef"):
            with self.assertToolFailure("must start with one of"):
                self.call(
                    "idea_record_invalidate",
                    tree_id=tree_id,
                    record_id=record_id,
                    reason="typo",
                )

    def test_an_observation_is_dropped_by_withdrawing_it_like_any_question(self) -> None:
        tree_id, _parent_id, node_ids = self.make_siblings(2)
        observation = self.call(
            "idea_question_raise",
            tree_id=tree_id,
            kind="observation",
            text="run the discriminating measurement",
            source="inferred",
            cost=2.0,
            depends_on=node_ids,
        )["question"]
        self.assertTrue(observation["id"].startswith("question_"))
        withdrawn = self.call(
            "idea_question_answer",
            tree_id=tree_id,
            question_id=observation["id"],
            expected_version=1,
            status="withdrawn",
        )["question"]
        self.assertEqual(withdrawn["status"], "withdrawn")
        self.assertIsNone(self.select(tree_id)["next_observation"])

    def test_retracting_the_same_record_twice_is_refused(self) -> None:
        tree_id, _parent_id, node_ids = self.make_siblings(2)
        comparison_id = self.compare(tree_id, node_ids[0], node_ids[1], "a")
        self.call(
            "idea_record_invalidate", tree_id=tree_id, record_id=comparison_id, reason="wrong"
        )
        with self.assertToolFailure("already inactive"):
            self.call(
                "idea_record_invalidate",
                tree_id=tree_id,
                record_id=comparison_id,
                reason="wrong again",
            )


class TransactionTest(ServerTestCase):
    def test_a_node_naming_an_unknown_question_is_not_left_half_created(self) -> None:
        """`depends_on` is validated inside the same write transaction as the insert."""
        tree_id, root_id = self.make_tree()
        with self.assertToolFailure("question not found in tree"):
            self.make_idea(tree_id, root_id, "Idea A", depends_on=["question_missing"])
        nodes = self.call("idea_node_list", tree_id=tree_id, include_deleted=True)["nodes"]
        self.assertEqual([node["kind"] for node in nodes], ["root"])


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
        events = self.call("idea_tree_history", tree_id=tree_id, node_id=node_id)["events"]
        payload = next(
            event for event in events if event["operation"] == "node.updated"
        )["payload"]
        self.assertEqual(payload["assumptions"], ["second claim", "third claim"])
        self.assertNotIn("assumptions_key", payload["before"])
        self.assertNotIn("assumptions_key", payload["after"])


if __name__ == "__main__":
    unittest.main()
