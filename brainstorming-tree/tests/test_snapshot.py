"""The one reading tool: what `idea_tree_snapshot` shows and what it hides.

The snapshot is the whole read surface, so every property that used to be
spread across several readers is asserted here: the node payload, the tombstone
rules, the event tail, and `shared_assumptions` -- the only number the server
computes that no caller supplied.
"""

from __future__ import annotations

import os
import sqlite3
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from harness import ServerTestCase  # noqa: E402


class SnapshotShapeTest(ServerTestCase):
    def test_a_node_carries_its_whole_row_plus_its_walk_depth_and_comparison_count(
        self,
    ) -> None:
        tree_id, root_id = self.make_tree()
        parent = self.make_parent(tree_id, root_id, "Family")
        child = self.make_idea(
            tree_id, parent, "Idea A", kill_condition="the measurement that retires it"
        )
        other = self.make_idea(tree_id, parent, "Idea B")
        self.compare(tree_id, child, other, "a")

        nodes = self.nodes_by_id(self.snapshot(tree_id))
        self.assertEqual(
            set(nodes[child]),
            {
                "seq",
                "id",
                "tree_id",
                "parent_id",
                "kind",
                "title",
                "content",
                "status",
                "kill_condition",
                "assumptions",
                "version",
                "metadata",
                "created_at",
                "updated_at",
                "deleted_at",
                "walk_depth",
                "comparison_count",
            },
        )
        self.assertEqual(nodes[child]["kind"], "idea")
        self.assertEqual(nodes[child]["status"], "open")
        self.assertEqual(nodes[child]["parent_id"], parent)
        self.assertEqual(nodes[child]["assumptions"], ["assumption of idea a"])
        self.assertEqual(nodes[child]["kill_condition"], "the measurement that retires it")
        self.assertEqual(nodes[child]["version"], 1)
        self.assertIsNone(nodes[child]["deleted_at"])
        self.assertEqual([nodes[root_id]["walk_depth"], nodes[parent]["walk_depth"]], [0, 1])
        self.assertEqual(nodes[child]["walk_depth"], 2)
        self.assertEqual(nodes[child]["comparison_count"], 1)
        self.assertEqual(nodes[parent]["comparison_count"], 0)

    def test_the_tree_block_carries_the_frozen_goal_and_the_supersede_pointers(self) -> None:
        old_id, _old_root = self.make_tree(title="First take", goal="Ship a judgeable thing")
        new_id, _new_root = self.supersede(old_id)

        old = self.snapshot(old_id)["tree"]
        self.assertEqual(old["goal"], "Ship a judgeable thing")
        self.assertEqual(old["superseded_by"], new_id)
        self.assertIsNone(old["supersedes"])
        self.assertEqual(self.snapshot(new_id)["tree"]["supersedes"], old_id)

    def test_recent_events_is_the_oldest_first_tail_of_the_ledger(self) -> None:
        tree_id, root_id = self.make_tree()
        a = self.make_idea(tree_id, root_id, "Idea A")
        b = self.make_idea(tree_id, root_id, "Idea B")
        self.compare(tree_id, a, b, "a")

        full = self.events(self.snapshot(tree_id))
        self.assertEqual(
            [event["operation"] for event in full],
            ["tree.created", "node.created", "node.created", "comparison.recorded"],
        )
        self.assertEqual(
            [event["seq"] for event in full], sorted(event["seq"] for event in full)
        )

        tail = self.events(self.snapshot(tree_id, events_limit=2))
        self.assertEqual(
            [event["operation"] for event in tail], ["node.created", "comparison.recorded"]
        )
        self.assertEqual(tail[-1]["payload"]["winner"], "a")
        self.assertEqual(self.events(self.snapshot(tree_id, events_limit=0)), [])

    def test_a_ranking_built_only_from_agent_comparisons_says_agent_only(self) -> None:
        tree_id, _parent_id, (a, b) = self.make_siblings(2)
        self.compare(tree_id, a, b, "a", source="agent")
        entries = self.rankings_by_node(self.snapshot(tree_id))
        self.assertTrue(entries[a]["agent_only"])
        self.assertTrue(entries[b]["agent_only"])

        self.compare(tree_id, a, b, "a", source="user", criterion="reach")
        entries = self.rankings_by_node(self.snapshot(tree_id))
        self.assertFalse(entries[a]["agent_only"])
        self.assertFalse(entries[b]["agent_only"])

    def test_truncated_fires_when_the_walk_hits_max_nodes(self) -> None:
        tree_id, root_id = self.make_tree()
        for name in ("A", "B", "C"):
            self.make_idea(tree_id, root_id, f"Idea {name}")

        full = self.snapshot(tree_id)
        self.assertEqual(len(full["nodes"]), 4)
        self.assertFalse(full["truncated"])

        clipped = self.snapshot(tree_id, max_nodes=2)
        self.assertEqual(len(clipped["nodes"]), 2)
        self.assertTrue(clipped["truncated"])
        self.assertEqual(clipped["counts"]["total_nodes"], 4)


class TombstoneTest(ServerTestCase):
    def test_a_deleted_node_is_hidden_by_default_and_visible_on_request(self) -> None:
        tree_id, root_id = self.make_tree()
        node_id = self.make_idea(tree_id, root_id, "Idea A")
        result = self.call(
            "idea_node_delete",
            tree_id=tree_id,
            node_id=node_id,
            expected_version=1,
            reason="the premise for it went away",
        )
        self.assertFalse(result["records_physically_erased"])

        self.assertNotIn(node_id, self.nodes_by_id(self.snapshot(tree_id)))
        with_deleted = self.snapshot(tree_id, include_deleted=True)
        node = self.nodes_by_id(with_deleted)[node_id]
        self.assertEqual(node["status"], "deleted")
        self.assertIsNotNone(node["deleted_at"])
        self.assertEqual(with_deleted["counts"]["deleted_nodes"], 1)
        self.assertEqual(with_deleted["counts"]["active_nodes"], 1)
        self.assertEqual(with_deleted["counts"]["total_nodes"], 2)

    def test_the_row_and_its_comparisons_stay_in_the_database(self) -> None:
        tree_id, parent_id, (a, b, c) = self.make_siblings(3)
        self.compare(tree_id, a, b, "a")
        snapshot = self.snapshot(tree_id)
        self.call(
            "idea_node_delete",
            tree_id=tree_id,
            node_id=b,
            expected_version=1,
            reason="folded into A",
        )

        after = self.snapshot(tree_id)
        self.assertNotIn(b, self.shortlist_by_node(after))
        self.assertNotIn(b, self.rankings_by_node(after))
        self.assertIn(a, self.rankings_by_node(after))

        connection = sqlite3.connect(snapshot["database_path"])
        try:
            rows = connection.execute(
                "SELECT b_node_id FROM comparisons WHERE tree_id = ?", (tree_id,)
            ).fetchall()
            node_rows = connection.execute(
                "SELECT status, deleted_at FROM nodes WHERE id = ?", (b,)
            ).fetchall()
        finally:
            connection.close()
        self.assertEqual([row[0] for row in rows], [b], "the judgment is never erased")
        self.assertEqual(node_rows[0][0], "deleted")
        self.assertIsNotNone(node_rows[0][1])

    def test_deleting_a_node_with_children_needs_cascade(self) -> None:
        tree_id, root_id = self.make_tree()
        parent = self.make_idea(tree_id, root_id, "Idea A", assumptions=["the sensor is linear"])
        child = self.make_idea(
            tree_id,
            parent,
            "Idea A with drift",
            assumptions=["the sensor is linear", "drift is bounded"],
        )
        with self.assertToolFailure("has 1 active descendants"):
            self.call(
                "idea_node_delete",
                tree_id=tree_id,
                node_id=parent,
                expected_version=1,
                reason="the family is dead",
            )
        self.assertIn(child, self.nodes_by_id(self.snapshot(tree_id)))

        result = self.call(
            "idea_node_delete",
            tree_id=tree_id,
            node_id=parent,
            expected_version=1,
            reason="the family is dead",
            cascade=True,
        )
        self.assertEqual(
            {node["id"] for node in result["tombstoned_nodes"]}, {parent, child}
        )
        live = self.nodes_by_id(self.snapshot(tree_id))
        self.assertEqual(set(live), {root_id})
        self.assertEqual(self.snapshot(tree_id)["counts"]["deleted_nodes"], 2)


class SharedAssumptionsTest(ServerTestCase):
    """The only figure the server computes that nobody typed in."""

    def test_fewer_than_two_live_ideas_share_nothing(self) -> None:
        tree_id, root_id = self.make_tree()
        self.assertEqual(self.snapshot(tree_id)["shared_assumptions"], [])
        self.make_idea(tree_id, root_id, "Idea A", assumptions=["the sensor is linear"])
        self.assertEqual(self.snapshot(tree_id)["shared_assumptions"], [])

    def test_it_is_the_intersection_over_every_live_idea(self) -> None:
        tree_id, root_id = self.make_tree()
        self.make_idea(
            tree_id, root_id, "Idea A", assumptions=["the budget is fixed", "cache is warm"]
        )
        self.make_idea(
            tree_id, root_id, "Idea B", assumptions=["The Budget  is Fixed", "latency dominates"]
        )
        self.assertEqual(
            self.snapshot(tree_id)["shared_assumptions"], ["the budget is fixed"]
        )

    def test_one_idea_that_drops_the_shared_assumption_empties_it(self) -> None:
        tree_id, root_id = self.make_tree()
        self.make_idea(
            tree_id, root_id, "Idea A", assumptions=["the budget is fixed", "cache is warm"]
        )
        self.make_idea(
            tree_id, root_id, "Idea B", assumptions=["the budget is fixed", "latency dominates"]
        )
        self.make_idea(tree_id, root_id, "Idea C", assumptions=["the budget can move"])
        self.assertEqual(self.snapshot(tree_id)["shared_assumptions"], [])

    def test_a_tombstoned_idea_no_longer_holds_the_floor_down(self) -> None:
        tree_id, root_id = self.make_tree()
        self.make_idea(
            tree_id, root_id, "Idea A", assumptions=["the budget is fixed", "cache is warm"]
        )
        self.make_idea(
            tree_id, root_id, "Idea B", assumptions=["the budget is fixed", "latency dominates"]
        )
        odd_one_out = self.make_idea(
            tree_id, root_id, "Idea C", assumptions=["the budget can move"]
        )
        self.assertEqual(self.snapshot(tree_id)["shared_assumptions"], [])

        self.call(
            "idea_node_delete",
            tree_id=tree_id,
            node_id=odd_one_out,
            expected_version=1,
            reason="the budget cannot move after all",
        )
        self.assertEqual(
            self.snapshot(tree_id)["shared_assumptions"], ["the budget is fixed"]
        )


if __name__ == "__main__":
    unittest.main()
