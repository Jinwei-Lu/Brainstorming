"""The loop: what `idea_tree_select` says to do next, and who is still in play.

Priority is external evidence, then asking the human, then comparing. The
invariant under all of it: while any candidate exists, `least_examined` is not
null, so the loop can never stall.
"""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from harness import ServerTestCase  # noqa: E402

REASONS = {
    "run_observation",
    "ask_question",
    "compare_least_examined",
    "expand_only_child",
    "no_live_candidates",
}


class NeverStallsTest(ServerTestCase):
    def test_a_tree_with_only_a_root_reports_no_live_candidates(self) -> None:
        tree_id, _root_id = self.make_tree()
        selection = self.select(tree_id)
        self.assertEqual(selection["reason"], "no_live_candidates")
        self.assertEqual(selection["candidate_count"], 0)
        self.assertIsNone(selection["least_examined"])
        self.assertFalse(selection["mutated_state"])

    def test_a_lone_candidate_is_returned_as_least_examined_with_expand_only_child(self) -> None:
        tree_id, root_id = self.make_tree()
        only = self.make_idea(tree_id, root_id, "Idea A")
        selection = self.select(tree_id)
        self.assertIn(selection["reason"], REASONS)
        self.assertEqual(selection["reason"], "expand_only_child")
        self.assertEqual(selection["least_examined"]["node_id"], only)
        self.assertEqual(selection["least_examined"]["comparisons"], 0)
        self.assertEqual(selection["least_examined"]["evaluations"], 0)

    def test_with_no_observation_and_no_comparison_select_falls_back_to_comparing(self) -> None:
        """The bare case: two fresh siblings, nothing recorded about either."""
        tree_id, _parent_id, node_ids = self.make_siblings(2, parent_kind="root")
        selection = self.select(tree_id)
        self.assertEqual(selection["reason"], "compare_least_examined")
        self.assertIsNone(selection["next_observation"])
        self.assertIsNone(selection["next_question"])
        self.assertIsNotNone(selection["least_examined"])
        self.assertIn(selection["least_examined"]["node_id"], node_ids)
        self.assertEqual(self.undominated_ids(selection), set(node_ids))

    def test_least_examined_prefers_the_node_with_the_fewest_comparisons(self) -> None:
        tree_id, _parent_id, (a, b, c) = self.make_siblings(3, parent_kind="root")
        self.compare(tree_id, a, b, "tie", source="agent")
        self.assertEqual(self.select(tree_id)["least_examined"]["node_id"], c)

    def test_a_domination_cycle_leaves_nobody_undominated_and_still_names_a_move(self) -> None:
        """A > B > C > A. `undominated` is empty; the loop must not stall on that."""
        tree_id, _parent_id, (a, b, c) = self.make_siblings(3, parent_kind="root")
        self.compare(tree_id, a, b, "a", source="agent")
        self.compare(tree_id, b, c, "a", source="agent")
        self.compare(tree_id, c, a, "a", source="agent")
        selection = self.select(tree_id)
        self.assertEqual(selection["undominated"], [])
        self.assertEqual(selection["reason"], "compare_least_examined")
        self.assertIsNotNone(
            selection["least_examined"], "an empty undominated set must fall back to all candidates"
        )
        self.assertIn(selection["least_examined"]["node_id"], {a, b, c})

    def test_start_node_id_restricts_the_candidate_scope(self) -> None:
        tree_id, root_id = self.make_tree()
        left = self.make_branch(tree_id, root_id, "Left")
        right = self.make_branch(tree_id, root_id, "Right")
        inside = self.make_idea(tree_id, left, "Idea A")
        self.make_idea(tree_id, right, "Idea B")
        selection = self.select(tree_id, start_node_id=left)
        self.assertEqual(selection["scope_node_id"], left)
        self.assertEqual({entry["node_id"] for entry in selection["undominated"]}, {left, inside})


class ObservationTest(ServerTestCase):
    """An observation is a question with a cost, ranked on the same list."""

    def test_an_observation_outranks_asking_and_comparing(self) -> None:
        tree_id, _parent_id, node_ids = self.make_siblings(2, parent_kind="root")
        observation = self.raise_observation(tree_id, node_ids, 4.0)
        self.assertEqual(observation["kind"], "observation")
        self.assertEqual(observation["status"], "open")
        self.assertEqual(observation["live_dependents"], node_ids)
        self.assertAlmostEqual(observation["cost"], 4.0)
        self.assertAlmostEqual(observation["score"], 0.5)

        selection = self.select(tree_id)
        self.assertEqual(selection["reason"], "run_observation")
        self.assertEqual(selection["next_observation"]["id"], observation["id"])
        self.assertIsNone(selection["next_question"], "an observation is run, not asked")

    def test_the_cheapest_observation_separating_the_same_branches_ranks_first(self) -> None:
        tree_id, _parent_id, node_ids = self.make_siblings(2, parent_kind="root")
        self.raise_observation(tree_id, node_ids, 4.0, "the expensive way")
        cheap = self.raise_observation(tree_id, node_ids, 1.0, "the cheap way")
        selection = self.select(tree_id)
        self.assertEqual(selection["next_observation"]["id"], cheap["id"])
        self.assertAlmostEqual(selection["next_observation"]["score"], 2.0)

    def test_an_observation_separating_fewer_than_two_live_branches_is_refused(self) -> None:
        """The Platt filter, now at raise time: it cannot change any judgment."""
        tree_id, _parent_id, (a, b) = self.make_siblings(2, parent_kind="root")
        self.compare(tree_id, a, b, "a", source="agent")  # b is now dominated
        with self.assertToolFailure(r"names only 1 undominated candidate\(s\)"):
            self.raise_observation(tree_id, [a, b], 1.0)

    def test_an_observation_with_no_dependents_at_all_is_refused(self) -> None:
        tree_id, _parent_id, _node_ids = self.make_siblings(2, parent_kind="root")
        with self.assertToolFailure(r"names only 0 undominated candidate\(s\)"):
            self.raise_question(tree_id, "measure something", "inferred", kind="observation")

    def test_a_refused_observation_leaves_nothing_behind(self) -> None:
        """The Platt filter runs inside the same write transaction as the insert."""
        tree_id, _parent_id, (a, b) = self.make_siblings(2, parent_kind="root")
        self.compare(tree_id, a, b, "a", source="agent")
        with self.assertToolFailure("undominated candidate"):
            self.raise_observation(tree_id, [a, b], 1.0)
        self.assertEqual(self.call("idea_tree_snapshot", tree_id=tree_id)["open_questions"], [])

    def test_an_open_observation_drops_out_once_its_branches_are_dominated(self) -> None:
        tree_id, _parent_id, (a, b) = self.make_siblings(2, parent_kind="root")
        self.raise_observation(tree_id, [a, b], 1.0)
        self.assertEqual(self.select(tree_id)["reason"], "run_observation")
        self.compare(tree_id, a, b, "a", source="agent")
        selection = self.select(tree_id)
        self.assertIsNone(selection["next_observation"])
        self.assertEqual(selection["reason"], "compare_least_examined")

    def test_an_evaluation_citing_an_observation_answers_it(self) -> None:
        tree_id, _parent_id, (a, b) = self.make_siblings(2, parent_kind="root")
        observation = self.raise_observation(tree_id, [a, b], 1.0)
        self.evaluate(
            tree_id, a, 1, outcome="supports", rationale="the run came out on A's side",
            source="user", question_id=observation["id"],
        )
        self.assertIsNone(self.select(tree_id)["next_observation"])
        self.assertEqual(
            self.call("idea_tree_snapshot", tree_id=tree_id)["open_questions"],
            [],
            "an answered observation is no longer open",
        )
        answered = [
            event
            for event in self.call("idea_tree_history", tree_id=tree_id)["events"]
            if event["operation"] == "question.answered"
        ]
        self.assertEqual(len(answered), 1, "closing an observation leaves a question event")
        self.assertEqual(answered[0]["payload"]["question_id"], observation["id"])
        self.assertEqual(answered[0]["payload"]["answered_by"], "user")
        self.assertTrue(answered[0]["payload"]["evaluation_id"].startswith("eval_"))
        with self.assertToolFailure("is already answered"):
            self.evaluate(
                tree_id, b, 1, rationale="a second reading",
                question_id=observation["id"],
            )

    def test_the_evaluation_records_which_observation_it_answered(self) -> None:
        tree_id, _parent_id, (a, b) = self.make_siblings(2, parent_kind="root")
        observation = self.raise_observation(tree_id, [a, b], 1.0)
        result = self.evaluate(
            tree_id, a, 1, rationale="the run came out on A's side",
            source="user", question_id=observation["id"],
        )
        self.assertEqual(result["evaluation"]["question_id"], observation["id"])

    def test_citing_a_question_instead_of_an_observation_is_refused(self) -> None:
        """A human question is closed by `idea_question_answer`, not by an evaluation."""
        tree_id, _parent_id, (a, _b) = self.make_siblings(2, parent_kind="root")
        question = self.raise_question(tree_id, "which market first?", "inferred")
        constraint = self.raise_question(
            tree_id, "must run on one GPU", "user", kind="constraint"
        )
        for item in (question, constraint):
            with self.assertToolFailure("an evaluation only reports the result"):
                self.evaluate(tree_id, a, 1, question_id=item["id"])

    def test_an_observation_naming_an_unknown_node_is_refused(self) -> None:
        tree_id, _parent_id, node_ids = self.make_siblings(2, parent_kind="root")
        with self.assertToolFailure("node not found in tree"):
            self.raise_observation(tree_id, [node_ids[0], "node_missing"], 1.0)


class DiscriminatorCostTest(ServerTestCase):
    """`cost` is the work of checking something, so only an observation may name it."""

    def test_an_observation_accepts_a_cost(self) -> None:
        tree_id, _parent_id, node_ids = self.make_siblings(2, parent_kind="root")
        observation = self.raise_observation(tree_id, node_ids, 0.25)
        self.assertAlmostEqual(observation["cost"], 0.25)
        self.assertAlmostEqual(observation["score"], 8.0)

    def test_an_observation_without_a_cost_defaults_to_one(self) -> None:
        tree_id, _parent_id, node_ids = self.make_siblings(2, parent_kind="root")
        observation = self.raise_question(
            tree_id, "measure it", "inferred", kind="observation", depends_on=node_ids
        )
        self.assertAlmostEqual(observation["cost"], 1.0)
        self.assertAlmostEqual(observation["score"], 2.0)

    def test_a_non_positive_observation_cost_is_refused(self) -> None:
        tree_id, _parent_id, node_ids = self.make_siblings(2, parent_kind="root")
        with self.assertToolFailure("must be greater than 0"):
            self.raise_observation(tree_id, node_ids, 0.0)

    def test_a_cost_on_a_question_or_constraint_is_refused(self) -> None:
        """There is no ask-cost knob: that number would be invented, not measured."""
        tree_id, _root_id = self.make_tree()
        for kind in ("question", "constraint"):
            with self.assertToolFailure("`cost` applies only to `observation`"):
                self.raise_question(tree_id, "how much?", "inferred", kind=kind, cost=2.0)
        self.assertAlmostEqual(
            self.raise_question(tree_id, "how much?", "inferred")["cost"], 1.0
        )


class UnifiedRankingTest(ServerTestCase):
    """One list: |undominated dependents| / cost, whatever the kind."""

    def build(self) -> tuple[str, list[str]]:
        tree_id, root_id = self.make_tree()
        nodes = [self.make_idea(tree_id, root_id, f"Idea {name}") for name in "ABC"]
        return tree_id, nodes

    def test_a_cheap_observation_on_two_nodes_outranks_a_constraint_on_three(self) -> None:
        tree_id, nodes = self.build()
        constraint = self.raise_question(
            tree_id, "must run on one GPU", "inferred", kind="constraint", depends_on=nodes
        )
        observation = self.raise_observation(tree_id, nodes[:2], 0.5)
        self.assertAlmostEqual(constraint["score"], 3.0)  # 3 / 1
        self.assertAlmostEqual(observation["score"], 4.0)  # 2 / 0.5

        selection = self.select(tree_id)
        self.assertEqual(selection["reason"], "run_observation")
        self.assertEqual(selection["next_observation"]["id"], observation["id"])
        self.assertIsNone(selection["next_question"])
        self.assertEqual(
            [entry["id"] for entry in self.open_questions(tree_id)],
            [observation["id"], constraint["id"]],
        )

    def test_the_same_observation_at_cost_two_loses_to_that_constraint(self) -> None:
        tree_id, nodes = self.build()
        constraint = self.raise_question(
            tree_id, "must run on one GPU", "inferred", kind="constraint", depends_on=nodes
        )
        observation = self.raise_observation(tree_id, nodes[:2], 2.0)
        self.assertAlmostEqual(observation["score"], 1.0)  # 2 / 2

        selection = self.select(tree_id)
        self.assertEqual(selection["reason"], "ask_question")
        self.assertEqual(selection["next_question"]["id"], constraint["id"])
        self.assertIsNone(selection["next_observation"])
        self.assertEqual(
            [entry["id"] for entry in self.open_questions(tree_id)],
            [constraint["id"], observation["id"]],
        )


class QuestionContractTest(ServerTestCase):
    def test_a_question_round_trips_with_its_source_tag_intact(self) -> None:
        tree_id, _root_id = self.make_tree()
        for source in ("user", "inferred", "assumed"):
            question = self.raise_question(tree_id, f"a {source} item", source)
            self.assertEqual(question["source"], source)
            self.assertEqual(question["status"], "open")
            self.assertEqual(question["kind"], "question")
            self.assertTrue(question["id"].startswith("question_"))
        constraint = self.raise_question(tree_id, "must ship offline", "user", kind="constraint")
        self.assertEqual(constraint["kind"], "constraint")

    def test_an_open_user_question_is_owed_back_and_never_becomes_next_question(self) -> None:
        """A `user` question is the agent's debt to the human, not a question to ask."""
        tree_id, root_id = self.make_tree()
        question = self.raise_question(tree_id, "what is the deadline?", "user")
        first = self.make_idea(tree_id, root_id, "Idea A", depends_on=[question["id"]])
        second = self.make_idea(tree_id, root_id, "Idea B", depends_on=[question["id"]])

        selection = self.select(tree_id)
        owed = selection["open_user_questions"]
        self.assertEqual([entry["id"] for entry in owed], [question["id"]])
        self.assertEqual(owed[0]["weight"], 2)
        self.assertEqual(sorted(owed[0]["live_dependents"]), sorted([first, second]))
        self.assertIsNone(selection["next_question"], "a user question is never asked back")
        self.assertEqual(selection["reason"], "compare_least_examined")

    def test_a_heavier_user_question_does_not_hide_an_askable_inferred_one(self) -> None:
        """The human's owed question sits at the top of the list but is not the agent's move."""
        tree_id, root_id = self.make_tree()
        owed = self.raise_question(tree_id, "what is the deadline?", "user")
        askable = self.raise_question(tree_id, "is latency the binding constraint?", "inferred")
        self.make_idea(tree_id, root_id, "Idea A", depends_on=[owed["id"], askable["id"]])
        self.make_idea(tree_id, root_id, "Idea B", depends_on=[owed["id"], askable["id"]])
        self.make_idea(tree_id, root_id, "Idea C", depends_on=[owed["id"]])

        selection = self.select(tree_id)
        self.assertEqual([e["id"] for e in selection["open_user_questions"]], [owed["id"]])
        self.assertEqual(selection["open_user_questions"][0]["weight"], 3)
        self.assertEqual(selection["next_question"]["id"], askable["id"])
        self.assertEqual(selection["reason"], "ask_question")

    def test_an_inferred_question_two_live_candidates_depend_on_becomes_the_next_question(
        self,
    ) -> None:
        tree_id, root_id = self.make_tree()
        question = self.raise_question(tree_id, "is latency the binding constraint?", "inferred")
        self.make_idea(tree_id, root_id, "Idea A", depends_on=[question["id"]])
        self.make_idea(tree_id, root_id, "Idea B", depends_on=[question["id"]])

        selection = self.select(tree_id)
        self.assertEqual(selection["question_rule"], "weight>=2")
        self.assertEqual(selection["next_question"]["id"], question["id"])
        self.assertEqual(selection["next_question"]["weight"], 2)
        self.assertEqual(selection["reason"], "ask_question")

    def test_one_dependent_is_below_the_weight_threshold(self) -> None:
        tree_id, root_id = self.make_tree()
        question = self.raise_question(tree_id, "does the cache stay warm?", "assumed")
        self.make_idea(tree_id, root_id, "Idea A", depends_on=[question["id"]])
        self.make_idea(tree_id, root_id, "Idea B")
        selection = self.select(tree_id)
        self.assertIsNone(selection["next_question"])
        self.assertEqual(selection["reason"], "compare_least_examined")

    def test_weight_counts_only_undominated_dependents(self) -> None:
        tree_id, root_id = self.make_tree()
        question = self.raise_question(tree_id, "is the sensor linear?", "inferred")
        first = self.make_idea(tree_id, root_id, "Idea A", depends_on=[question["id"]])
        second = self.make_idea(tree_id, root_id, "Idea B", depends_on=[question["id"]])
        self.assertEqual(self.select(tree_id)["reason"], "ask_question")
        self.compare(tree_id, first, second, "a", source="agent")  # second is dominated
        selection = self.select(tree_id)
        self.assertIsNone(selection["next_question"], "a dead branch no longer justifies asking")
        self.assertEqual(selection["reason"], "compare_least_examined")

    def test_a_constraint_is_asked_before_a_question_of_the_same_weight(self) -> None:
        tree_id, root_id = self.make_tree()
        question = self.raise_question(tree_id, "which market first?", "inferred")
        constraint = self.raise_question(
            tree_id, "must run on one GPU", "inferred", kind="constraint"
        )
        depends = [question["id"], constraint["id"]]
        self.make_idea(tree_id, root_id, "Idea A", depends_on=depends)
        self.make_idea(tree_id, root_id, "Idea B", depends_on=depends)
        self.assertEqual(self.select(tree_id)["next_question"]["id"], constraint["id"])

    def test_answering_the_question_takes_it_out_of_the_loop(self) -> None:
        tree_id, root_id = self.make_tree()
        question = self.raise_question(tree_id, "is latency binding?", "inferred")
        self.make_idea(tree_id, root_id, "Idea A", depends_on=[question["id"]])
        self.make_idea(tree_id, root_id, "Idea B", depends_on=[question["id"]])
        answered = self.call(
            "idea_question_answer",
            tree_id=tree_id,
            question_id=question["id"],
            expected_version=1,
            status="answered",
            answer="yes, p99 latency is the binding constraint",
            answered_by="user",
        )["question"]
        self.assertEqual(answered["status"], "answered")
        self.assertEqual(answered["answered_by"], "user")
        self.assertIsNotNone(answered["answered_at"])

        selection = self.select(tree_id)
        self.assertIsNone(selection["next_question"])
        self.assertEqual(selection["open_user_questions"], [])
        self.assertEqual(selection["reason"], "compare_least_examined")

    def test_answering_requires_an_answer_and_an_author(self) -> None:
        tree_id, _root_id = self.make_tree()
        question = self.raise_question(tree_id, "which market first?", "inferred")
        with self.assertToolFailure("`answer` is required"):
            self.call(
                "idea_question_answer",
                tree_id=tree_id,
                question_id=question["id"],
                expected_version=1,
                status="answered",
            )
        with self.assertToolFailure("`answer` only applies"):
            self.call(
                "idea_question_answer",
                tree_id=tree_id,
                question_id=question["id"],
                expected_version=1,
                status="withdrawn",
                answer="never mind",
            )

    def test_a_blocked_node_is_surfaced_for_review_and_never_unblocked_automatically(self) -> None:
        tree_id, root_id = self.make_tree()
        question = self.raise_question(tree_id, "is the licence compatible?", "inferred")
        blocked = self.make_idea(tree_id, root_id, "Idea A", depends_on=[question["id"]])
        self.make_idea(tree_id, root_id, "Idea B")
        self.call(
            "idea_node_update",
            tree_id=tree_id,
            node_id=blocked,
            expected_version=1,
            status="blocked",
        )
        result = self.call(
            "idea_question_answer",
            tree_id=tree_id,
            question_id=question["id"],
            expected_version=1,
            status="answered",
            answer="yes, Apache-2.0",
            answered_by="user",
        )
        self.assertEqual(
            [entry["node_id"] for entry in result["unblocked_candidates"]], [blocked]
        )
        self.assertEqual(
            self.node(tree_id, blocked)["node"]["status"],
            "blocked",
            "the server must not decide to reopen a node",
        )
        self.assertEqual(
            [entry["node_id"] for entry in self.select(tree_id)["unblocked_review"]], [blocked]
        )


class StatusLifecycleTest(ServerTestCase):
    def set_status(self, tree_id: str, node_id: str, status: str, version: int = 1) -> int:
        return self.call(
            "idea_node_update",
            tree_id=tree_id,
            node_id=node_id,
            expected_version=version,
            status=status,
        )["node"]["version"]

    def test_a_finalist_is_still_selectable_comparable_and_evaluable(self) -> None:
        """A finalist that could not be overturned would be a conclusion, not a judgment."""
        tree_id, _parent_id, (a, b) = self.make_siblings(2, parent_kind="root")
        version = self.set_status(tree_id, a, "finalist")

        selection = self.select(tree_id)
        self.assertIn(a, self.undominated_ids(selection))
        self.assertEqual(selection["candidate_count"], 2)

        self.compare(tree_id, a, b, "a", source="user")
        result = self.evaluate(tree_id, a, version, outcome="kills",
                               rationale="the finalist fails the new measurement")
        self.assertEqual(result["evaluation"]["outcome"], "kills")
        self.assertFalse(result["reopen_suggested"])

    def test_a_rejected_node_is_no_longer_a_comparison_operand(self) -> None:
        tree_id, _parent_id, (a, b) = self.make_siblings(2, parent_kind="root")
        self.set_status(tree_id, b, "rejected")
        with self.assertToolFailure("is rejected and cannot be a comparison operand"):
            self.compare(tree_id, a, b, "a", source="agent")
        selection = self.select(tree_id)
        self.assertEqual(selection["candidate_count"], 1)
        self.assertNotIn(b, self.undominated_ids(selection))

    def test_a_supports_evaluation_on_a_rejected_node_suggests_reopening_it(self) -> None:
        """The only route back from `rejected` is new evidence, so it must stay evaluable."""
        tree_id, _parent_id, (a, _b) = self.make_siblings(2, parent_kind="root")
        version = self.set_status(tree_id, a, "rejected")
        result = self.evaluate(
            tree_id, a, version, outcome="supports", rationale="the killing run was misconfigured"
        )
        self.assertTrue(result["reopen_suggested"])
        self.assertEqual(result["node"]["status"], "rejected", "reopening stays a human decision")

    def test_a_kills_evaluation_on_a_rejected_node_suggests_nothing(self) -> None:
        tree_id, _parent_id, (a, _b) = self.make_siblings(2, parent_kind="root")
        version = self.set_status(tree_id, a, "rejected")
        result = self.evaluate(tree_id, a, version, outcome="kills", rationale="confirmed dead")
        self.assertFalse(result["reopen_suggested"])

    def test_status_after_moves_the_node_in_the_same_call(self) -> None:
        tree_id, _parent_id, (a, _b) = self.make_siblings(2, parent_kind="root")
        result = self.evaluate(
            tree_id, a, 1, outcome="kills", rationale="the kill condition fired",
            status_after="rejected",
        )
        self.assertEqual(result["node"]["status"], "rejected")

    def test_a_deleted_node_refuses_every_judgment(self) -> None:
        tree_id, _parent_id, (a, b) = self.make_siblings(2, parent_kind="root")
        self.call(
            "idea_node_delete", tree_id=tree_id, node_id=a, expected_version=1, reason="wrong track"
        )
        with self.assertToolFailure("cannot be evaluated"):
            self.evaluate(tree_id, a, 2)
        with self.assertToolFailure("cannot be a comparison operand"):
            self.compare(tree_id, a, b, "a", source="agent")


class CascadeDeleteTest(ServerTestCase):
    def build(self) -> tuple[str, str, str, str]:
        """root -> (branch -> inner idea), plus a sibling idea on the root."""
        tree_id, root_id = self.make_tree()
        branch_id = self.make_branch(tree_id, root_id, "Doomed branch")
        inner_id = self.make_idea(tree_id, branch_id, "Inner idea")
        outside_id = self.make_idea(tree_id, root_id, "Surviving idea")
        return tree_id, branch_id, inner_id, outside_id

    def test_a_non_leaf_refuses_to_go_without_cascade(self) -> None:
        tree_id, branch_id, _inner_id, _outside_id = self.build()
        with self.assertToolFailure("has 1 active descendants"):
            self.call(
                "idea_node_delete",
                tree_id=tree_id,
                node_id=branch_id,
                expected_version=1,
                reason="the whole family is wrong",
            )

    def test_cascade_tombstones_the_subtree_and_deactivates_what_touched_it(self) -> None:
        tree_id, branch_id, inner_id, outside_id = self.build()
        evaluation_id = self.evaluate(tree_id, inner_id, 1)["evaluation"]["id"]
        comparison_id = self.compare(tree_id, inner_id, outside_id, "b", source="agent")
        self.assertEqual(
            self.snapshot_node(tree_id, outside_id)["comparison_count"], 1
        )

        result = self.call(
            "idea_node_delete",
            tree_id=tree_id,
            node_id=branch_id,
            expected_version=1,
            cascade=True,
            reason="the mechanism was disproved upstream",
        )

        tombstoned = {node["id"]: node for node in result["tombstoned_nodes"]}
        self.assertEqual(set(tombstoned), {branch_id, inner_id})
        for node in tombstoned.values():
            self.assertEqual(node["status"], "deleted")
            self.assertIsNotNone(node["deleted_at"])
        self.assertFalse(result["records_physically_erased"], "a tombstone is not an erasure")

        evaluations = self.node(tree_id, inner_id)["evaluations"]
        self.assertEqual([row["id"] for row in evaluations], [evaluation_id])
        self.assertFalse(evaluations[0]["active"])
        self.assertIn("tombstoned", evaluations[0]["invalidation_reason"])

        with self.assertToolFailure("already inactive"):
            self.call(
                "idea_record_invalidate",
                tree_id=tree_id,
                record_id=comparison_id,
                reason="already handled by the cascade",
            )
        self.assertEqual(self.snapshot_node(tree_id, outside_id)["comparison_count"], 0)

        selection = self.select(tree_id)
        self.assertEqual(self.undominated_ids(selection), {outside_id})
        self.assertEqual(selection["candidate_count"], 1)

    def test_the_delete_event_records_what_it_invalidated(self) -> None:
        tree_id, branch_id, inner_id, outside_id = self.build()
        self.evaluate(tree_id, inner_id, 1)
        self.compare(tree_id, inner_id, outside_id, "b", source="agent")
        self.call(
            "idea_node_delete",
            tree_id=tree_id,
            node_id=branch_id,
            expected_version=1,
            cascade=True,
            reason="the mechanism was disproved upstream",
        )
        events = self.call("idea_tree_history", tree_id=tree_id, node_id=branch_id)["events"]
        deletion = next(event for event in events if event["operation"] == "node.deleted")
        payload = deletion["payload"]
        self.assertTrue(payload["cascade"])
        self.assertEqual(sorted(payload["tombstoned_ids"]), sorted([branch_id, inner_id]))
        self.assertEqual(payload["invalidated_evaluations"], 1)
        self.assertEqual(payload["invalidated_comparisons"], 1)
        self.assertEqual(payload["previous_statuses"][inner_id], "open")

    def test_deleting_with_a_stale_version_is_a_version_conflict(self) -> None:
        tree_id, _branch_id, inner_id, _outside_id = self.build()
        self.call(
            "idea_node_update",
            tree_id=tree_id,
            node_id=inner_id,
            expected_version=1,
            title="Renamed",
        )
        with self.assertToolFailure(r"version conflict for node .*expected 1, current 2"):
            self.call(
                "idea_node_delete",
                tree_id=tree_id,
                node_id=inner_id,
                expected_version=1,
                reason="stale",
            )

    # -- helper ------------------------------------------------------------

    def snapshot_node(self, tree_id: str, node_id: str) -> dict:
        snapshot = self.call("idea_tree_snapshot", tree_id=tree_id)
        return next(node for node in snapshot["nodes"] if node["id"] == node_id)


class SharedAssumptionsTest(ServerTestCase):
    """What every survivor still rests on -- a readout, not a score."""

    def build(self, shared: str = "attention is the bottleneck") -> tuple[str, str, list[str]]:
        tree_id, root_id = self.make_tree()
        nodes = [
            self.make_idea(tree_id, root_id, f"Idea {name}", assumptions=[shared, f"and {name}"])
            for name in "ABC"
        ]
        return tree_id, root_id, nodes

    def test_the_one_assumption_three_undominated_ideas_share_is_reported(self) -> None:
        tree_id, _root_id, _nodes = self.build()
        self.assertEqual(
            self.select(tree_id)["shared_assumptions"], ["attention is the bottleneck"]
        )

    def test_the_snapshot_reports_the_same_floor(self) -> None:
        tree_id, _root_id, _nodes = self.build()
        snapshot = self.call("idea_tree_snapshot", tree_id=tree_id)
        self.assertEqual(snapshot["shared_assumptions"], ["attention is the bottleneck"])

    def test_a_fourth_idea_that_does_not_share_it_empties_the_intersection(self) -> None:
        tree_id, root_id, _nodes = self.build()
        self.make_idea(tree_id, root_id, "Idea D", assumptions=["memory is the bottleneck"])
        self.assertEqual(self.select(tree_id)["shared_assumptions"], [])

    def test_a_dominated_dissenter_does_not_break_the_floor(self) -> None:
        """The floor is over survivors: a pruned branch no longer questions anything."""
        tree_id, root_id, nodes = self.build()
        dissenter = self.make_idea(
            tree_id, root_id, "Idea D", assumptions=["memory is the bottleneck"]
        )
        self.assertEqual(self.select(tree_id)["shared_assumptions"], [])
        self.compare(tree_id, nodes[0], dissenter, "a", source="user")
        self.assertNotIn(dissenter, self.undominated_ids(self.select(tree_id)))
        self.assertEqual(
            self.select(tree_id)["shared_assumptions"], ["attention is the bottleneck"]
        )

    def test_one_lone_idea_reports_no_floor_at_all(self) -> None:
        tree_id, root_id = self.make_tree()
        self.make_idea(tree_id, root_id, "Idea A", assumptions=["attention is the bottleneck"])
        self.assertEqual(self.select(tree_id)["shared_assumptions"], [])

    def test_branches_are_not_counted_because_they_state_no_assumption(self) -> None:
        tree_id, root_id = self.make_tree()
        self.make_branch(tree_id, root_id, "Mechanism family")
        first = self.make_idea(tree_id, root_id, "Idea A", assumptions=["shared", "own a"])
        second = self.make_idea(tree_id, root_id, "Idea B", assumptions=["shared", "own b"])
        self.assertTrue(first and second)
        self.assertEqual(self.select(tree_id)["shared_assumptions"], ["shared"])

    def test_ideas_that_share_nothing_report_an_empty_floor(self) -> None:
        tree_id, root_id = self.make_tree()
        self.make_idea(tree_id, root_id, "Idea A", assumptions=["alpha"])
        self.make_idea(tree_id, root_id, "Idea B", assumptions=["beta"])
        self.assertEqual(self.select(tree_id)["shared_assumptions"], [])


if __name__ == "__main__":
    unittest.main()
