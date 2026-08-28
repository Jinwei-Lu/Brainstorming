---
name: idea-tree-brainstorming
description: Run multi-round brainstorming as a durable, MCTS-inspired idea tree with explicit selection, expansion, evidence-based evaluation, backpropagation, and auditable CRUD. Use when an exploration must survive context loss or needs disciplined branching and comparison; do not use for a one-shot idea list.
---

# Idea-tree brainstorming

## Compatibility

This skill is agent-agnostic. It requires a host that can load an Agent Skills-compatible `SKILL.md` file and connect to the bundled standard-input MCP server. Host-specific manifests, permissions, launch configuration, and installation paths belong in thin runtime adapters rather than in this skill.

Treat the local idea-tree database as the single source of truth. Chat text is commentary, not state. Never rely on remembered node names, scores, versions, or verdicts when the tools can read them.

## Start or resume

1. Use the absolute workspace path in every tool call. The database lives at `<workspace>/.idea-tree/ideas.sqlite3`.
2. Call `idea_tree_list_trees`. Resume the matching active tree when its frozen goal matches the request; otherwise call `idea_tree_create_tree`.
3. On every resumed run, read `idea_tree_snapshot` and the latest `idea_tree_history` before proposing or changing anything.
4. A material goal change creates a new tree. Do not rewrite a tree's frozen goal.

## The learning loop

Repeat one stateful loop, not a sequence of disposable answers:

1. **Select.** Call `idea_tree_select`. It uses an upper-confidence score to balance strong branches with neglected ones. A score chooses where to look next; it never proves an idea is good.
2. **Expand.** Research or reason about a materially different mechanism, then call `idea_node_create` before discussing it as a live candidate. Use `branch` for a mechanism family, `idea` for a testable proposal, and `synthesis` for a complete combination.
3. **Evaluate.** Run the cheapest valid discriminator. Call `idea_evaluate` only with a concrete logical trace, source, observation, user judgment, or experiment receipt. The tool appends the evaluation and backpropagates its value to every ancestor.
4. **Learn.** Keep failed nodes. A material repair is a new sibling node with `predecessor_id` and the counterexample in metadata; never overwrite the failed idea into its repair.
5. **Synthesize.** When complementary ideas survive, create a `synthesis` node and record their IDs in `source_ids`. Evaluate the composed result, not only its parts.
6. **Stop honestly.** Mark a tree `completed` only when the requested decision or bounded closure is supported. Use `active` when agent-owned exploration remains and `archived` only for a tree intentionally taken out of use.

## Node contract

Keep `content` concise but judgeable: mechanism, expected observable effect, strongest simple comparator, and kill condition. Put structured facts in `metadata`, using only keys that matter, such as:

```json
{
  "assumptions": ["..."],
  "evidence_refs": ["..."],
  "predecessor_id": "node_...",
  "source_ids": ["node_..."],
  "counterexample_disposition": "PERMANENT_DEATH | SUCCESSOR_FROZEN | UNKNOWN_WITH_MINIMUM_EXPERIMENT"
}
```

Use statuses consistently:

- `open`: live and not yet cleared;
- `survived`: cleared by the current discriminator but still provisional;
- `rejected`: a load-bearing claim failed;
- `blocked`: the decisive evidence is currently unavailable;
- `finalist`: the unchanged idea or synthesis cleared the frozen goal;
- `deleted`: a tombstone created only by the delete tool.

Use evaluation values only for branch allocation, on this anchored scale: `-1` decisive failure, `-0.5` material loss, `0` genuinely inconclusive, `0.5` material support, `1` decisive success against the named comparator. Intermediate values require an explicit reason. Do not turn model confidence or eloquence into a score.

## Mutation rules

- Read a node immediately before changing it and pass its current `version` as `expected_version`. A conflict means reread and reconsider; never blindly retry with a new version.
- `idea_node_update` changes the current record but preserves the event history. Parent and kind are immutable so lineage cannot silently move.
- `idea_node_delete` creates a tombstone. It refuses a non-leaf unless `cascade` is explicitly true; even then the database preserves the records and event trail.
- If an evaluation is invalid, use `idea_evaluation_invalidate` with a reason. Do not bury it under compensating scores.
- Query with `idea_node_list` instead of assuming the snapshot was exhaustive. Use the returned cursor until the requested scope is complete.
- Never edit the SQLite file directly.

## Delivery

Lead with the strongest surviving synthesis or the bounded reason no candidate cleared. Name the decisive evidence, comparator, remaining blocked nodes, and kill/reopening condition. The database is the detailed ledger; do not replay every event into the answer.
