"""Aggregation: the Bradley-Terry ranking the snapshot reports.

Nothing here scores a node in isolation. Every number in the per-sibling-group
`rankings` and in the tree-wide `ranked_shortlist` comes from pairwise
`idea_compare` records, and from nothing else.
"""

from __future__ import annotations

import math
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from harness import ServerTestCase  # noqa: E402


class BradleyTerryOrderTest(ServerTestCase):
    def test_three_siblings_rank_in_the_order_their_comparisons_imply(self) -> None:
        """A beats B and C, B beats C. The fit must read A > B > C."""
        tree_id, parent_id, (a, b, c) = self.make_siblings(3)
        self.compare(tree_id, a, b, "a", source="agent")
        self.compare(tree_id, a, c, "a", source="agent")
        self.compare(tree_id, b, c, "a", source="agent")

        entries = self.rankings_by_node(self.snapshot(tree_id))
        self.assertEqual([entries[node]["rank"] for node in (a, b, c)], [1, 2, 3])
        strengths = [entries[node]["strength"] for node in (a, b, c)]
        self.assertTrue(all(math.isfinite(value) for value in strengths))
        self.assertGreater(strengths[0], strengths[1])
        self.assertGreater(strengths[1], strengths[2])

        self.assertEqual(
            [entries[a]["wins"], entries[a]["losses"], entries[a]["ties"]], [2, 0, 0]
        )
        self.assertEqual(entries[c]["wins"], 0)
        self.assertEqual(entries[c]["losses"], 2)

    def test_an_unbeaten_node_ranks_first_with_a_finite_strength(self) -> None:
        """The virtual-opponent prior is what keeps one-sided evidence bounded."""
        tree_id, parent_id, (a, b, c) = self.make_siblings(3)
        self.compare(tree_id, a, b, "a", source="agent")
        self.compare(tree_id, a, c, "a", source="agent")

        entries = self.rankings_by_node(self.snapshot(tree_id))
        self.assertEqual(entries[a]["rank"], 1)
        self.assertTrue(math.isfinite(entries[a]["strength"]))
        self.assertGreater(entries[a]["strength"], 1.0, "beating siblings must beat the prior")

    def test_a_tie_splits_the_win_between_both_sides(self) -> None:
        tree_id, parent_id, (a, b) = self.make_siblings(2)
        self.compare(tree_id, a, b, "tie", source="agent")
        entries = self.rankings_by_node(self.snapshot(tree_id))
        self.assertEqual(entries[a]["ties"], 1)
        self.assertEqual(entries[b]["ties"], 1)
        self.assertAlmostEqual(entries[a]["strength"], entries[b]["strength"])


class AgentOnlyTest(ServerTestCase):
    def test_a_ranking_built_only_from_agent_judgments_says_so(self) -> None:
        tree_id, parent_id, (a, b, c) = self.make_siblings(3)
        self.compare(tree_id, a, b, "a", source="agent")
        self.compare(tree_id, b, c, "a", source="agent")

        groups = self.snapshot(tree_id)["rankings"]
        sibling_group = next(group for group in groups if group["parent_id"] == parent_id)
        self.assertEqual(sibling_group["user_comparison_count"], 0)
        entries = {entry["node_id"]: entry for entry in sibling_group["nodes"]}
        self.assertTrue(entries[a]["agent_only"])
        self.assertTrue(entries[b]["agent_only"])
        self.assertTrue(entries[c]["agent_only"])

    def test_an_uncompared_node_is_not_marked_agent_only(self) -> None:
        """`agent_only` reports tainted evidence, not missing evidence."""
        tree_id, parent_id, (a, b, c) = self.make_siblings(3)
        self.compare(tree_id, a, b, "a", source="agent")
        entries = self.rankings_by_node(self.snapshot(tree_id))
        self.assertTrue(entries[a]["agent_only"])
        self.assertEqual(entries[c]["comparisons"], 0)
        self.assertFalse(entries[c]["agent_only"])

    def test_one_user_comparison_clears_the_flag_for_the_pair_it_touches(self) -> None:
        tree_id, parent_id, (a, b, c) = self.make_siblings(3)
        self.compare(tree_id, a, b, "a", source="user")
        self.compare(tree_id, b, c, "a", source="agent")

        groups = self.snapshot(tree_id)["rankings"]
        sibling_group = next(group for group in groups if group["parent_id"] == parent_id)
        self.assertEqual(sibling_group["user_comparison_count"], 1)
        entries = {entry["node_id"]: entry for entry in sibling_group["nodes"]}
        self.assertFalse(entries[a]["agent_only"])
        self.assertFalse(entries[b]["agent_only"])
        self.assertTrue(entries[c]["agent_only"], "c was only ever judged by the agent")


class ComponentTest(ServerTestCase):
    def test_strengths_are_only_comparable_inside_a_compared_component(self) -> None:
        tree_id, parent_id, (a, b, c, d) = self.make_siblings(4)
        self.compare(tree_id, a, b, "a", source="agent")
        self.compare(tree_id, c, d, "a", source="agent")
        entries = self.rankings_by_node(self.snapshot(tree_id))
        self.assertEqual(entries[a]["component"], entries[b]["component"])
        self.assertEqual(entries[c]["component"], entries[d]["component"])
        self.assertNotEqual(entries[a]["component"], entries[c]["component"])


class ShortlistTest(ServerTestCase):
    def test_the_shortlist_ranks_every_candidate_in_scope_not_just_one_family(self) -> None:
        tree_id, root_id = self.make_tree()
        left = self.make_parent(tree_id, root_id, "Left")
        right = self.make_parent(tree_id, root_id, "Right")
        a = self.make_idea(tree_id, left, "Idea A")
        b = self.make_idea(tree_id, left, "Idea B")
        c = self.make_idea(tree_id, right, "Idea C")
        self.compare(tree_id, a, b, "a", source="user")
        self.compare(tree_id, a, c, "a", source="user")

        shortlist = self.shortlist_by_node(self.snapshot(tree_id))
        self.assertEqual(
            set(shortlist), {left, right, a, b, c}, "a parent idea is a candidate too"
        )
        self.assertEqual(shortlist[a]["rank"], 1)
        self.assertEqual(shortlist[a]["wins"], 2)
        self.assertEqual(
            [shortlist[node]["rank"] for node in (b, c)],
            sorted(shortlist[node]["rank"] for node in (b, c)),
        )

    def test_an_uncompared_family_lands_in_its_own_component_in_the_shortlist(self) -> None:
        """One list does not make two never-compared sets comparable."""
        tree_id, root_id = self.make_tree()
        left = self.make_parent(tree_id, root_id, "Left")
        right = self.make_parent(tree_id, root_id, "Right")
        a = self.make_idea(tree_id, left, "Idea A")
        b = self.make_idea(tree_id, left, "Idea B")
        c = self.make_idea(tree_id, right, "Idea C")
        d = self.make_idea(tree_id, right, "Idea D")
        self.compare(tree_id, a, b, "a", source="agent")
        self.compare(tree_id, c, d, "a", source="agent")

        shortlist = self.shortlist_by_node(self.snapshot(tree_id))
        self.assertEqual(shortlist[a]["component"], shortlist[b]["component"])
        self.assertEqual(shortlist[c]["component"], shortlist[d]["component"])
        self.assertNotEqual(shortlist[a]["component"], shortlist[c]["component"])

    def test_a_cross_parent_comparison_ranks_in_the_shortlist(self) -> None:
        """`rankings` stays per sibling group; only the shortlist holds this pair."""
        tree_id, root_id = self.make_tree()
        left = self.make_parent(tree_id, root_id, "Left")
        right = self.make_parent(tree_id, root_id, "Right")
        under_left = self.make_idea(tree_id, left, "Idea A")
        under_right = self.make_idea(tree_id, right, "Idea B")
        self.compare(tree_id, under_left, under_right, "b", source="user", criterion="cost")

        snapshot = self.snapshot(tree_id)
        shortlist = self.shortlist_by_node(snapshot)
        self.assertEqual(shortlist[under_right]["rank"], 1)
        self.assertEqual(shortlist[under_right]["wins"], 1)
        self.assertEqual(shortlist[under_left]["losses"], 1)
        self.assertEqual(
            shortlist[under_left]["component"],
            shortlist[under_right]["component"],
            "the comparison joined them, so their strengths are comparable",
        )
        ranked = self.rankings_by_node(snapshot)
        self.assertNotIn(under_left, ranked, "no sibling group holds this pair")
        self.assertNotIn(under_right, ranked)

    def test_max_parents_truncates_sibling_groups_but_never_the_shortlist(self) -> None:
        tree_id, root_id = self.make_tree()
        expected = set()
        for index in range(3):
            parent = self.make_parent(tree_id, root_id, f"Family {index}")
            expected.add(parent)
            for name in ("A", "B"):
                expected.add(self.make_idea(tree_id, parent, f"Idea {index}{name}"))

        snapshot = self.snapshot(tree_id, max_parents=1)
        self.assertEqual(len(snapshot["rankings"]), 1)
        self.assertTrue(snapshot["rankings_truncated"])
        self.assertEqual(set(self.shortlist_by_node(snapshot)), expected)



if __name__ == "__main__":
    unittest.main()
