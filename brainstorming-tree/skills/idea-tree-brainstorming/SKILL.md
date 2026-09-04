---
name: idea-tree-brainstorming
description: Run multi-round brainstorming with a human as a durable idea tree — the human asks, the AI proposes materially different leaves, the human gives feedback, and the loop repeats; every judgment lands in the tree. Use when an exploration runs over several rounds, must survive context loss, and needs the human's judgments recorded; do not use for a one-shot idea list.
---

# Idea-tree brainstorming

## Compatibility and source of truth

This skill is agent-agnostic. It needs a host that can load an Agent Skills-compatible `SKILL.md` and reach the bundled idea-tree server — either as an MCP server, or through the CLI wrapper when the host has not loaded the MCP server:

```bash
python3 <plugin-root>/brainstorming-tree/scripts/idea_tree_cli.py <tool> '<json args>' [workspace]
```

One tool per process; the wrapper prints the result JSON and exits non-zero when the tool refuses. Every call takes an absolute `workspace`. The database lives at `<workspace>/.idea-tree/ideas.sqlite3`.

The tree database is the state. Chat text is commentary. Never rely on a remembered node ID, version, title, or verdict — read it back before you use it.

## Start or resume

1. Call `idea_tree_list_trees`.
2. Resume the open tree whose frozen goal matches what the human is asking about. Otherwise call `idea_tree_create_tree` with a title and goal.
3. If the human's premise no longer matches the goal, call `idea_tree_create_tree` with `supersedes` naming the old tree. Never rewrite a goal. The superseded tree refuses writes and names its successor.
4. On a resumed tree, call `idea_tree_snapshot` before proposing anything: read the live leaves, the ranked shortlist, `shared_assumptions`, and `recent_events`.

## The loop

One turn of the loop:

1. The human asks.
2. Read the human's materials — the files, notes, or links they pointed at.
3. **WIDE**: create 2–3 materially different leaves.
4. **STOP.** Show them to the human. Do not proceed in the same turn.
5. The human gives feedback. Record every judgment in it: a `source=user` `idea_compare`, or an `idea_node_delete` with the reason.
6. **DEEP**: grow the 1–2 leaves the human picked or that won a comparison.
7. Repeat.

Hard rule: never add nodes and record comparisons in the same script or the same turn. The human must see the new leaves in between. The first real trial failed exactly here — the agent batch-built four leaves and compared them in one script, so the tree never grew again and the human never made a single judgment.

## Three operators

**WIDE** — a depth-1 leaf resting on an assumption no live leaf rests on. Sources, in order:
1. Negate one entry of `shared_assumptions`.
2. Borrow a mechanism from a neighboring field.
3. The human's hint.

**DEEP** — a child of a leaf that won a comparison or that the human picked. It adds exactly one concrete commitment. Never deepen a loser.

**MERGE** — a `synthesis` node over two survivors whose assumptions are consistent.

When which:
- WIDE until there are at least 3 materially different directions.
- After feedback or comparisons, DEEP.
- WIDE again only when the human rejects all of them, or when every live leaf shares an assumption the human doubts.
- Stop deepening when a leaf's next step is an experiment or a decision.

## Translating the human

The human never operates the tool and never sees IDs. Translate what they say:

| The human says | You call |
| --- | --- |
| 「我有个想法」 | `idea_node_create` with `metadata.source = "user"` |
| 「X 不行 / 测死了」 | `idea_node_delete` with their reason, or a `source=user` compare |
| 「A 比 B 好」/「重要的是 Y」 | `idea_compare` on criterion Y, `source=user` |
| 「前提变了」 | `idea_tree_create_tree` with `supersedes` |
| 「看这份材料」 | Read it, then propose leaves or record a compare whose `basis` cites it |
| 「继续」 | Proceed with the loop |

## What to show

Plain language. No IDs, no dumps of the ledger.

- Each new leaf in three lines: the mechanism, the one assumption that sets it apart from the others, what would kill it.
- The current top leaf and the single comparison behind it.
- At most one question, and only when no evidence you can reach would settle it.
- Which comparisons are still agent-only.

## When to stop and hand back

- New leaves are ready to show.
- A judgment is needed that evidence cannot give.
- The next step costs real resources.
- The premise has drifted from the tree's goal.

## Mutation rules

- Read a node immediately before changing it and pass its current version as `expected_version`. A conflict means reread and reconsider, never blindly retry.
- A repair is a new sibling carrying `predecessor_id` in `metadata`. Never overwrite a failed node into its repair.
- A sibling whose normalized assumption set equals a live sibling's is refused. A child that adds no assumption beyond its parent is refused as parameter variation — record it as a compare on the parent, not as a node.
- Deletion is a tombstone. It carries a reason.
- Never edit the SQLite file directly.

## Delivery

Lead with the top leaf by Bradley-Terry ranking, then the comparisons behind it, the leaves still open, and what would reopen the decision. Flag any part of the ranking that rests only on agent-only comparisons. The database is the ledger; do not replay it into the answer.
