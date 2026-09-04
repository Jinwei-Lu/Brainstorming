"""Aggregation: Bradley-Terry ranking, Pareto domination, and retraction.

Nothing here scores a node in isolation. Every number in `ranked_frontier` and
every member of `undominated` comes from pairwise `idea_compare` records, so
retracting one record has to move both.
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

        entries = self.frontier_by_node(self.select(tree_id))
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

        entries = self.frontier_by_node(self.select(tree_id))
        self.assertEqual(entries[a]["rank"], 1)
        self.assertTrue(math.isfinite(entries[a]["strength"]))
        self.assertGreater(entries[a]["strength"], 1.0, "beating siblings must beat the prior")

    def test_a_tie_splits_the_win_and_leaves_both_undominated(self) -> None:
        tree_id, parent_id, (a, b) = self.make_siblings(2)
        self.compare(tree_id, a, b, "tie", source="agent")
        selection = self.select(tree_id)
        entries = self.frontier_by_node(selection)
        self.assertEqual(entries[a]["ties"], 1)
        self.assertEqual(entries[b]["ties"], 1)
        self.assertAlmostEqual(entries[a]["strength"], entries[b]["strength"])
        self.assertLessEqual({a, b}, self.undominated_ids(selection))


class AgentOnlyTest(ServerTestCase):
    def test_a_ranking_built_only_from_agent_judgments_says_so(self) -> None:
        tree_id, parent_id, (a, b, c) = self.make_siblings(3)
        self.compare(tree_id, a, b, "a", source="agent")
        self.compare(tree_id, b, c, "a", source="agent")

        groups = self.select(tree_id)["ranked_frontier"]
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
        entries = self.frontier_by_node(self.select(tree_id))
        self.assertTrue(entries[a]["agent_only"])
        self.assertEqual(entries[c]["comparisons"], 0)
        self.assertFalse(entries[c]["agent_only"])

    def test_one_user_comparison_clears_the_flag_for_the_pair_it_touches(self) -> None:
        tree_id, parent_id, (a, b, c) = self.make_siblings(3)
        self.compare(tree_id, a, b, "a", source="user")
        self.compare(tree_id, b, c, "a", source="agent")

        groups = self.select(tree_id)["ranked_frontier"]
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
        entries = self.frontier_by_node(self.select(tree_id))
        self.assertEqual(entries[a]["component"], entries[b]["component"])
        self.assertEqual(entries[c]["component"], entries[d]["component"])
        self.assertNotEqual(entries[a]["component"], entries[c]["component"])


class DominationTest(ServerTestCase):
    def test_losing_every_comparison_to_one_rival_with_no_tie_is_domination(self) -> None:
        tree_id, _parent_id, (a, b) = self.make_siblings(2, parent_kind="root")
        self.assertEqual(self.undominated_ids(self.select(tree_id)), {a, b})
        self.compare(tree_id, a, b, "a", source="agent")
        self.assertEqual(self.undominated_ids(self.select(tree_id)), {a})

    def test_one_tie_against_the_same_rival_rescues_a_loser(self) -> None:
        """Domination needs a clean sweep; mixed evidence is not a sweep."""
        tree_id, _parent_id, (a, b) = self.make_siblings(2, parent_kind="root")
        self.compare(tree_id, a, b, "a", source="agent", criterion="cost")
        self.compare(tree_id, a, b, "tie", source="agent", criterion="reach")
        self.assertEqual(self.undominated_ids(self.select(tree_id)), {a, b})

    def test_an_uncompared_node_is_undominated_because_absence_is_not_evidence(self) -> None:
        tree_id, _parent_id, (a, b, c) = self.make_siblings(3, parent_kind="root")
        self.compare(tree_id, a, b, "a", source="agent")
        self.assertEqual(self.undominated_ids(self.select(tree_id)), {a, c})

    def test_a_cross_parent_comparison_prunes_only_where_the_caller_can_see_it(self) -> None:
        """Domination is tree-wide, so the ranking that covers it must be too.

        `domination_report` scans every candidate in scope, and `ranked_shortlist`
        ranks that same set, so a comparison across two parents cannot remove a
        node from the live set without appearing in a ranking. `ranked_frontier`
        stays per-sibling-group and shows nothing here, which is why the shortlist
        and the `dominated` record are the auditable half.
        """
        tree_id, root_id = self.make_tree()
        left = self.make_branch(tree_id, root_id, "Left")
        right = self.make_branch(tree_id, root_id, "Right")
        under_left = self.make_idea(tree_id, left, "Idea A")
        under_right = self.make_idea(tree_id, right, "Idea B")
        self.compare(
            tree_id, under_left, under_right, "a", source="agent", criterion="reach"
        )

        selection = self.select(tree_id)
        self.assertNotIn(under_right, self.undominated_ids(selection), "eliminated tree-wide")

        ranked = self.frontier_by_node(selection)
        self.assertNotIn(under_right, ranked, "no sibling group holds this pair")
        self.assertNotIn(under_left, ranked)

        shortlist = self.shortlist_by_node(selection)
        self.assertIn(under_right, shortlist, "the pruning comparison is visible tree-wide")
        self.assertEqual(shortlist[under_left]["rank"], 1)
        self.assertEqual(shortlist[under_left]["wins"], 1)
        self.assertEqual(shortlist[under_right]["losses"], 1)
        self.assertGreater(
            shortlist[under_left]["strength"], shortlist[under_right]["strength"]
        )
        self.assertEqual(
            shortlist[under_left]["component"],
            shortlist[under_right]["component"],
            "the comparison joined them, so their strengths are comparable",
        )

        pruned = self.dominated_by_node(selection)[under_right]
        self.assertEqual([entry["node_id"] for entry in pruned["dominated_by"]], [under_left])
        self.assertEqual(pruned["dominated_by"][0]["criteria"], ["reach"])
        self.assertEqual(pruned["title"], "Idea B")

    def test_the_domination_record_names_every_criterion_the_sweep_rested_on(self) -> None:
        """Two comparisons, one criterion each, both listed with their record IDs."""
        tree_id, _parent_id, (a, b) = self.make_siblings(2, parent_kind="root")
        first = self.compare(tree_id, a, b, "a", source="user", criterion="cost")
        second = self.compare(tree_id, a, b, "a", source="agent", criterion="reach")

        pruned = self.dominated_by_node(self.select(tree_id))[b]
        self.assertEqual(len(pruned["dominated_by"]), 1)
        entry = pruned["dominated_by"][0]
        self.assertEqual(entry["node_id"], a)
        self.assertEqual(entry["criteria"], ["cost", "reach"])
        self.assertEqual(sorted(entry["comparison_ids"]), sorted([first, second]))

    def test_nothing_is_reported_as_dominated_while_the_evidence_is_mixed(self) -> None:
        tree_id, _parent_id, (a, b) = self.make_siblings(2, parent_kind="root")
        self.compare(tree_id, a, b, "a", source="agent", criterion="cost")
        self.compare(tree_id, a, b, "tie", source="agent", criterion="reach")
        self.assertEqual(self.select(tree_id)["dominated"], [])


class ShortlistTest(ServerTestCase):
    def test_the_shortlist_ranks_every_candidate_in_scope_not_just_one_family(self) -> None:
        tree_id, root_id = self.make_tree()
        left = self.make_branch(tree_id, root_id, "Left")
        right = self.make_branch(tree_id, root_id, "Right")
        a = self.make_idea(tree_id, left, "Idea A")
        b = self.make_idea(tree_id, left, "Idea B")
        c = self.make_idea(tree_id, right, "Idea C")
        self.compare(tree_id, a, b, "a", source="user")
        self.compare(tree_id, a, c, "a", source="user")

        shortlist = self.shortlist_by_node(self.select(tree_id))
        self.assertEqual(
            set(shortlist), {left, right, a, b, c}, "branches are candidates too"
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
        left = self.make_branch(tree_id, root_id, "Left")
        right = self.make_branch(tree_id, root_id, "Right")
        a = self.make_idea(tree_id, left, "Idea A")
        b = self.make_idea(tree_id, left, "Idea B")
        c = self.make_idea(tree_id, right, "Idea C")
        d = self.make_idea(tree_id, right, "Idea D")
        self.compare(tree_id, a, b, "a", source="agent")
        self.compare(tree_id, c, d, "a", source="agent")

        shortlist = self.shortlist_by_node(self.select(tree_id))
        self.assertEqual(shortlist[a]["component"], shortlist[b]["component"])
        self.assertEqual(shortlist[c]["component"], shortlist[d]["component"])
        self.assertNotEqual(shortlist[a]["component"], shortlist[c]["component"])

    def test_the_shortlist_is_restricted_by_start_node_id_like_every_other_reading(
        self,
    ) -> None:
        tree_id, root_id = self.make_tree()
        left = self.make_branch(tree_id, root_id, "Left")
        right = self.make_branch(tree_id, root_id, "Right")
        inside = self.make_idea(tree_id, left, "Idea A")
        outside = self.make_idea(tree_id, right, "Idea B")
        shortlist = self.shortlist_by_node(self.select(tree_id, start_node_id=left))
        self.assertEqual(set(shortlist), {left, inside})
        self.assertNotIn(outside, shortlist)

    def test_the_snapshot_carries_the_same_shortlist_and_pruning_record(self) -> None:
        tree_id, root_id = self.make_tree()
        left = self.make_branch(tree_id, root_id, "Left")
        right = self.make_branch(tree_id, root_id, "Right")
        under_left = self.make_idea(tree_id, left, "Idea A")
        under_right = self.make_idea(tree_id, right, "Idea B")
        self.compare(tree_id, under_left, under_right, "b", source="user", criterion="cost")

        snapshot = self.call("idea_tree_snapshot", tree_id=tree_id)
        shortlist = self.shortlist_by_node(snapshot)
        self.assertEqual(shortlist[under_right]["rank"], 1)
        pruned = self.dominated_by_node(snapshot)[under_left]
        self.assertEqual(pruned["dominated_by"][0]["node_id"], under_right)
        self.assertEqual(pruned["dominated_by"][0]["criteria"], ["cost"])
        self.assertEqual(
            {entry["node_id"] for entry in snapshot["undominated"]},
            {left, right, under_right},
        )

    def test_max_parents_truncates_sibling_groups_but_never_the_shortlist(self) -> None:
        tree_id, root_id = self.make_tree()
        expected = set()
        for index in range(3):
            parent = self.make_branch(tree_id, root_id, f"Branch {index}")
            expected.add(parent)
            for name in ("A", "B"):
                expected.add(self.make_idea(tree_id, parent, f"Idea {index}{name}"))

        snapshot = self.call("idea_tree_snapshot", tree_id=tree_id, max_parents=1)
        self.assertEqual(len(snapshot["rankings"]), 1)
        self.assertTrue(snapshot["rankings_truncated"])
        self.assertEqual(set(self.shortlist_by_node(snapshot)), expected)


class InvalidateRecomputesTest(ServerTestCase):
    def test_retracting_a_comparison_restores_the_loser_and_flattens_the_ranking(self) -> None:
        tree_id, _parent_id, (a, b) = self.make_siblings(2, parent_kind="root")
        comparison_id = self.compare(tree_id, a, b, "a", source="agent")

        before = self.select(tree_id)
        self.assertEqual(self.undominated_ids(before), {a})
        before_entries = self.frontier_by_node(before)
        self.assertGreater(before_entries[a]["strength"], before_entries[b]["strength"])

        self.call(
            "idea_record_invalidate",
            tree_id=tree_id,
            record_id=comparison_id,
            reason="the criterion conflated cost with reach",
        )

        after = self.select(tree_id)
        self.assertEqual(self.undominated_ids(after), {a, b})
        after_entries = self.frontier_by_node(after)
        self.assertAlmostEqual(after_entries[a]["strength"], after_entries[b]["strength"])
        self.assertEqual(after_entries[a]["comparisons"], 0)
        self.assertEqual(after_entries[b]["comparisons"], 0)

    def test_a_retracted_evaluation_can_no_longer_back_a_comparison(self) -> None:
        tree_id, _parent_id, (a, b) = self.make_siblings(2, parent_kind="root")
        evaluation_id = self.evaluate(tree_id, a, 1)["evaluation"]["id"]
        self.call(
            "idea_record_invalidate",
            tree_id=tree_id,
            record_id=evaluation_id,
            reason="the cited run was the wrong build",
        )
        with self.assertToolFailure("is invalidated and cannot back a comparison"):
            self.compare(tree_id, a, b, "a", source="agent", basis=[evaluation_id])


if __name__ == "__main__":
    unittest.main()
