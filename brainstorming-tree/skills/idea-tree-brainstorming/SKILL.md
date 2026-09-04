---
name: idea-tree-brainstorming
description: Run multi-round brainstorming as a durable strong-inference judgment ledger, an idea tree with pairwise comparison, Pareto pruning, explicit goal questions, and auditable CRUD. Use when an exploration must survive context loss or needs disciplined branching and comparison; do not use for a one-shot idea list.
---

# Idea-tree brainstorming

## Compatibility

This skill is agent-agnostic. It requires a host that can load an Agent Skills-compatible `SKILL.md` file and connect to the bundled standard-input MCP server. Host-specific manifests, permissions, launch configuration, and installation paths belong in thin runtime adapters rather than in this skill.

Treat the local idea-tree database as the single source of truth. Chat text is commentary, not state. Never rely on remembered node names, comparisons, versions, or verdicts when the tools can read them.

## Start or resume

1. Use the absolute workspace path in every tool call. The database lives at `<workspace>/.idea-tree/ideas.sqlite3`.
2. Call `idea_tree_list_trees`. Resume the matching active tree when its frozen goal matches the request; otherwise call `idea_tree_create_tree`.
3. On every resumed run, read `idea_tree_snapshot` and the latest `idea_tree_history` before proposing or changing anything, including the tree's **open questions** — do not re-propose a branch that already depends on an unanswered one.
4. A material goal change creates a new tree. Do not rewrite a tree's frozen goal.

## The loop

Repeat one stateful loop, not a sequence of disposable answers:

1. **Register the goal contract.** Capture questions and constraints with `idea_question_raise`, tagged `user` / `inferred` / `assumed`.
2. **Raise and answer questions.** Ask the human only the single top-weighted open `inferred`/`assumed` question at a time — the one the most undominated branches `depends_on` — and record the answer with `idea_question_answer` before continuing. An answer does not auto-unblock dependent nodes; check `unblocked_review` on the next `idea_tree_select`.
3. **Expand with assumptions and a kill condition.** Call `idea_node_create` for a materially different mechanism (`branch`), a testable proposal (`idea`), or a full combination (`synthesis`). Every `idea`/`synthesis` node needs explicit `assumptions` and a `kill_condition` before it is eligible for comparison.
4. **Register a discriminator.** An observation is a kind of question: before running one, call `idea_question_raise` with `kind: observation`, `depends_on` naming which live branches it would separate, and an optional `cost` (default 1, how much work it takes to check). An observation separating fewer than 2 undominated nodes is refused at raise time. `cost` is only valid with `kind: observation` — passing it on a question or constraint is refused.
5. **Compare siblings on a named criterion.** Never score a node in isolation. Call `idea_compare` for one named criterion at a time, with a winner and the evaluation IDs it rests on. Record the underlying evidence first with `idea_evaluate` (`outcome ∈ supports/kills/inconclusive`, typed evidence; cite an observation with `question_id`, which marks it answered).
6. **Select.** Call `idea_tree_select`. It scores every open discriminator — observations, and questions/constraints tagged `inferred`/`assumed` with weight ≥ 2 — by `|undominated dependents| / cost` and acts on the top-scoring one: run the observation, ask the question, or, when none qualifies, fall back to the single least-examined node — never to a score on a node itself.
7. **Deliver.** Rank by Bradley-Terry over the recorded comparisons and report as below.

v0.2 never backpropagates a scalar value to ancestors. The only thing that aggregates into a ranking is the set of recorded pairwise comparisons.

## Goal contract and node fields

Questions and constraints live in the goal contract (`idea_question_raise`/`idea_question_answer`), each tagged `user` / `inferred` / `assumed`. A node declares which questions it `depends_on`. Keep `content` concise but judgeable: mechanism, expected observable effect, strongest simple comparator. Put structured facts in `metadata`, e.g. `predecessor_id`, `source_ids`, `counterexample_disposition`.

Statuses: `open` (live, not yet cleared), `survived` (cleared by the current discriminator, still provisional), `finalist` (cleared the frozen goal), `blocked` (decisive evidence unavailable), `rejected` (a load-bearing claim failed — not selectable, still evaluable), `deleted` (a tombstone). `finalist` and `blocked` stay live and comparable; only `deleted` is terminal. A `supports` evaluation on a `rejected` node is how a rejection gets reopened.

## Mutation rules

- Read a node or record immediately before mutating it and pass its current `version` as `expected_version`. A conflict means reread and reconsider, never blindly retry.
- A repair is a new sibling carrying `predecessor_id`; never overwrite a failed node into its repair, and never create a sibling whose normalized `assumptions` set duplicates a live sibling's — the tool rejects it.
- A child idea must add at least one assumption its parent does not hold; if you cannot name one, it is a parameter variation — record it as a comparison or evaluation on the parent, not as a new node.
- When `shared_assumptions` is non-empty, every surviving idea rests on those assumptions and none has questioned them. Before expanding further, propose at least one idea that drops one of them.
- If a comparison or evaluation is invalid, call `idea_record_invalidate` (`eval_`/`cmp_` prefixes only) with the record ID and a reason. Do not bury it under a compensating record. Withdraw an observation instead, via `idea_question_answer` with `status: withdrawn`.
- Query with `idea_node_list` instead of assuming the snapshot was exhaustive.
- Never edit the SQLite file directly.

## Delivery

Lead with the strongest surviving node by Bradley-Terry ranking, or the bounded reason no candidate cleared. Name the decisive evidence and comparisons behind it, the open questions still outstanding, and the kill/reopening condition. The database is the detailed ledger; do not replay every event into the answer.
