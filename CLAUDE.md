# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

Brainstorming Tree: an agent-agnostic plugin for human–AI brainstorming that keeps the state in a local SQLite ledger instead of model context. `v0.3` shrank the server to leaf CRUD plus pairwise comparison and moved the brainstorming loop into the skill. The division of labour is the architecture: **the server stores and ranks; the skill diverges and talks to the human.** Do not push generation, selection, or scheduling logic back into the server.

## Commands

Standard library only, no dependencies, no venv.

```bash
cd brainstorming-tree
python3 -m unittest discover -s tests                    # full suite (72 tests)
python3 -m unittest tests.test_contract -k assumption    # by name pattern
```

Drive the server one tool per process (this is how a brainstorming session uses it):

```bash
python3 brainstorming-tree/scripts/idea_tree_cli.py idea_tree_list_trees '{}' /abs/workspace
```

Wire-level smoke test of the stdio MCP server:

```bash
printf '%s\n' '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-11-25"}}' '{"jsonrpc":"2.0","id":2,"method":"tools/list"}' | python3 brainstorming-tree/scripts/idea_tree_server.py
```

No linter, formatter, or build step is configured.

## Architecture contract

- `scripts/idea_tree_server.py` is the entire state engine and MCP server. It never calls a model; every judgment arrives as a tool argument, tagged `source` `user` or `agent`.
- `scripts/idea_tree_cli.py` is a thin one-shot JSON-RPC driver over the same server. It adds no behavior and must not grow any.
- `skills/idea-tree-brainstorming/SKILL.md` is the behavior contract the host agent follows. It names tools, argument keys, and result fields literally, so **any rename or semantic change in the server must be mirrored there**, and vice versa. Adding a tool means editing `TOOLS`, `HANDLERS`, and `SKILL.md` together.
- `.mcp.json` and `.codex-plugin/plugin.json` are thin runtime adapters. New host adapters must reuse the same server, CLI, and skill, not fork them.
- The skill is installed globally by symlink at `~/.claude/skills/idea-tree-brainstorming`, so an edit here is live in the next session.

## The loop this serves

Human asks → AI reads the human's materials → **WIDE** 2–3 materially different leaves → **stop and show them in plain language** → the human's feedback is recorded as `source=user` comparisons or deletions → **DEEP** the picked or winning 1–2 → repeat. `MERGE` combines two survivors with consistent assumptions.

The hard rule, and the reason v0.3 exists: **never add nodes and record comparisons in the same turn or the same script.** The first real trial (see `docs/teamwork/records/`) batch-built four leaves and compared them in one script; the tree never grew again and the human never made a single judgment.

## Gotchas and invariants

- `SCHEMA_VERSION = 3` lives in SQLite `user_version`. A `v0.1` or `v0.2` database is refused with an error naming the path and is never migrated; do not add a migration path.
- Only `idea_tree_create_tree` may create the database (`allow_create=True`); every other tool refuses a missing database.
- `assert_adds_an_assumption` (a child of a non-root parent must add an assumption, otherwise it is a parameter variation and belongs as a comparison on the parent) and `assert_unique_assumptions` (no two live siblings with the same normalized assumption set) are the project's defense against auto-research degenerating into parameter search. Keep them.
- A tree's goal is frozen. A changed premise creates a new tree with `supersedes`; the predecessor gets `superseded_by` in the same transaction and refuses every write afterwards, naming its successor. `one_successor_per_predecessor` is a partial unique index, so the chain cannot fan out.
- Ranking is Bradley-Terry over recorded comparisons only — per sibling group and as a whole-tree `ranked_shortlist`. Never reintroduce scalar node scores or backpropagation to ancestors. `agent_only` and `user_comparison_count` exist so a ranking the human never touched is visible as such; do not hide them.
- `shared_assumptions` is a computed snapshot field (intersection over live ideas), not a table. It is the first input of a WIDE round.
- Optimistic concurrency via `expected_version` on update and delete. Deletion is a tombstone with a reason (`records_physically_erased: False`), never a row delete. Every mutation appends to `events`, which the snapshot returns as `recent_events`.
- `ToolFailure` becomes an `isError` tool result, not a JSON-RPC error.

## Tests

Use the in-process driver (`ServerTestCase` in `tests/harness.py`) for all state-machine behavior. Reserve the subprocess driver for wire-framing properties only (`test_protocol.py`). Every driver has a timeout so the suite cannot hang; keep it that way. `test_protocol.py` asserts the tool count and that `TOOLS` and `HANDLERS` name the same set — that assertion is what catches a half-added tool.

## Conventions

- `README.md` and `README_zh.md` are kept in sync; edit both when changing user-facing claims. The "Current status" table is deliberately honest about what is verified; do not upgrade a row without evidence.
- Local-first is a hard rule: no cloud services, embeddings, second LLM, or third-party Python packages in the core.
- Ideas are ranked against named rivals on named criteria, never scored in isolation. This governs both code and skill changes.
- Prefer the smallest mechanism. v0.2 was cut in half because precise machinery that judged well produced no ideas; a new mechanism must serve idea generation or the human's turn, or it is weight.
