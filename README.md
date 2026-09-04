# Brainstorming Tree

[简体中文](README_zh.md) | English

**A lightweight Agent Plugin for durable human–AI brainstorming.** It persists ideas as an auditable tree so a long exploration survives context loss, interference, and interruption.

> **Status:** early development (`v0.3.0`). The agent-agnostic durable core is implemented; cross-agent packaging and validation on a real forward-design brainstorming project are still pending. See [Current status](#current-status) for what is and is not reproducible from this repository alone.

## Why this project exists

Brainstorming Tree exists for one purpose: a human and an AI discussing together to produce valuable research ideas. That purpose has two persistent difficulties — staying light enough to actually use, and resisting the pull toward auto-research degenerating into parameter search, where a mechanism with one knob turned poses as a new idea.

Long brainstorming sessions ask a language model to remember too much at once: the original goal, competing branches, discarded assumptions, evidence, revisions, and why one idea replaced another. As the conversation grows, older details can leave the active context or become diluted by unrelated text. The result is familiar: repeated ideas, forgotten constraints, inconsistent judgments, and promising branches that disappear.

Brainstorming Tree moves that state out of conversational memory. The conversation remains the interface; a project-local idea tree becomes the source of truth.

## The approach

The Agent Plugin combines four deliberately small pieces:

1. **A portable Agent Skill** defines the human–AI loop: who proposes, when the AI stops, and how a human's words become recorded judgments.
2. **A local MCP server** exposes typed tree operations instead of relying on a model to rewrite prose consistently.
3. **A SQLite database** stores trees, nodes, comparisons, and events under the current workspace.
4. **Thin runtime adapters** connect the same core to the packaging and configuration conventions of individual agent hosts.

A **CLI wrapper**, `scripts/idea_tree_cli.py`, drives the same server from a shell when the host has not loaded the MCP server:

```bash
python3 scripts/idea_tree_cli.py <tool> '<json args>' [workspace]
```

```text
User goal
   │
   ▼
Agent + portable brainstorming skill
   │  typed operations (MCP tools, or the CLI wrapper)
   ▼
Local idea-tree server
   │  transactions
   ▼
<workspace>/.idea-tree/ideas.sqlite3
```

The runtime is local-first and uses Python's standard library. No cloud service, account, embedding model, or second LLM is required for the core loop.

## Cross-agent compatibility

The portable core is built on two open interfaces:

- [Model Context Protocol (MCP)](https://modelcontextprotocol.io/specification/2025-11-25) for typed tools and host–server communication.
- [Agent Skills](https://agentskills.io/specification) for the `SKILL.md` behavior contract.

“Agent Plugin” describes the product boundary: the state engine, MCP tools, and brainstorming behavior are not owned by one agent product. It does **not** mean that every agent can install the same package with zero configuration. Each host must support the relevant open interface and may need a small adapter for its own manifest, launch command, permissions, or installation path.

The current repository includes a `.codex-plugin/plugin.json` manifest as the first adapter. It is a compatibility layer, not the identity or architectural boundary of the project. Additional adapters should remain thin and must reuse the same server and Agent Skill.

## The loop

The human asks. The AI reads the human's materials and proposes materially different leaves. The human gives feedback. The loop repeats, and every judgment in that feedback lands in the tree as a comparison or a deletion.

Three operators drive the tree:

| Operator | What it adds | Trigger |
| --- | --- | --- |
| **WIDE** | A depth-1 leaf resting on an assumption no live leaf rests on — from negating a shared assumption, borrowing a neighboring field's mechanism, or the human's hint. | At the start, until there are at least three materially different directions; again when the human rejects all of them. |
| **DEEP** | A child of a leaf, adding one concrete commitment. | The leaf won a comparison or the human picked it. Never a loser. |
| **MERGE** | A synthesis of two survivors whose assumptions are consistent. | Two live leaves complement rather than contradict each other. |

One rule holds the loop together: **the AI never adds nodes and records comparisons in the same turn.** New leaves are shown to the human first; comparisons come from what the human says next.

## What the ledger enforces

- **Two gates against parameter search.** A sibling whose normalized assumption set equals a live sibling's is refused. A child that adds no assumption beyond its parent is refused as a parameter variation — it becomes a comparison on the parent instead.
- **Ranking from recorded comparisons.** Bradley-Terry aggregation over pairwise comparisons, per sibling group and as a whole-tree shortlist. Each entry carries `agent_only` and a user comparison count, so a ranking the human never touched is visible as such.
- **Tombstones.** Deletion records a reason and keeps the node.
- **Version checks.** Every update and delete passes `expected_version`, so stale model context cannot overwrite newer state.
- **A supersede chain.** A tree's goal is frozen. A changed premise creates a new tree with `supersedes`; the old tree refuses writes and names its successor.

Snapshots also return `shared_assumptions` — the assumptions every live idea shares — so the next WIDE round can target that unquestioned ground instead of refining on top of it.

## What v0.2 tried and why it was removed

`v0.2` was a strong-inference judgment ledger: a `select` operation that scored open discriminators by how many undominated branches they separated divided by their cost; a goal contract of questions and constraints tagged `user` / `inferred` / `assumed`; observations with a cost and a Platt filter; typed evaluations behind every comparison; and Pareto domination for pruning. The first real trial showed that these mechanisms judged well and generated nothing. The tree never grew after its initial creation, all comparisons were agent-only, and the human made no judgment at all — the loop had convergence machinery and no divergence, and no place for the human to act. Divergence and the human's turn now live in the skill's loop. The server only stores and ranks.

## Intended guarantees

- **Durable state:** a session can resume after context compaction or process interruption.
- **Explicit lineage:** every idea keeps its parent, predecessors, and synthesis sources.
- **Auditable changes:** mutations leave an event history.
- **Safe updates:** version checks prevent stale model context from overwriting newer state.
- **Reversible removal:** deletion creates a tombstone rather than erasing history.
- **Local ownership:** project data stays in the workspace by default.

## Current status

| Component | Status |
| --- | --- |
| Plugin manifest and metadata | Present in the repository; no validator run is recorded here |
| Idea-tree brainstorming skill | Present in the repository; no validator run is recorded here |
| Local MCP server | Core implementation complete (v0.3 schema and 7 tools) |
| SQLite schema | 4 tables: trees, nodes, comparisons, events |
| CLI wrapper | `scripts/idea_tree_cli.py` present |
| Automated test suite | 72 tests under `brainstorming-tree/tests/`, runnable with `cd brainstorming-tree && python3 -m unittest discover -s tests` |
| First runtime adapter | Codex manifest available |
| Additional agent adapters and installation guides | Pending |
| Real-world validation | One trial on `v0.2`, which failed to generate ideas; the `v0.3` trial is pending |

Until cross-agent packaging is finished and the plugin has driven a real brainstorming project end to end, treat the repository as an experimental Agent Plugin rather than a production-ready tool. Nothing in this table is a substitute for cloning the repository and running the command yourself.

## Repository layout

```text
.
├── README.md
├── README_zh.md
├── brainstorming-tree/
│   ├── .codex-plugin/plugin.json     # First runtime adapter: Codex
│   ├── .mcp.json                     # Local MCP server declaration
│   ├── skills/
│   │   └── idea-tree-brainstorming/  # Portable Agent Skill and loop rules
│   ├── scripts/
│   │   ├── idea_tree_server.py       # State engine and MCP server
│   │   └── idea_tree_cli.py          # CLI wrapper over the same tools
│   └── tests/                        # Unittest suite
└── docs/teamwork/                    # Local project context (Git-ignored)
```

## Planned user flow

1. Give a compatible agent a problem, decision, or open-ended idea.
2. Create a tree with a frozen goal.
3. Let the agent propose materially different leaves, show them, and record your feedback as comparisons and deletions.
4. Resume the same tree in a later session without reconstructing it from chat history.
5. Deliver the strongest surviving leaf together with the comparisons behind it, the branches still open, and what would reopen the decision.

## Roadmap

### `v0.2` — strong-inference core

- [x] Implement the dependency-free local MCP server.
- [x] Add SQLite-backed tree, node, comparison, question, and evaluation operations.
- [x] Replace scalar scoring and backpropagation with pairwise comparison, Bradley-Terry ranking, and Pareto-domination pruning.
- [x] Move to a fresh schema with no migration; opening an older database raises an error instead of silently reinterpreting it.

### `v0.3` — leaf CRUD and the human loop

- [x] Shrink the server to leaf CRUD plus compare (7 tools, 4 tables).
- [x] Add the supersede chain for a changed premise.
- [x] Ship the CLI wrapper.
- [x] Move the loop and the three operators into the skill.
- [ ] Run a second real trial on a forward-design goal.
- [ ] Document the portable core contract and package thin adapters for supported agent runtimes.

### Later, only when evidence justifies it

- Improve ranking calibration from real usage traces.
- Add and verify runtime adapters based on actual user demand.
- Add import/export and compact human-readable reports.
- Explore visualization, collaboration, or remote synchronization without weakening the local core.

## Design principles

- State lives outside the model.
- Evidence outranks eloquence and confidence.
- Ideas are ranked against named rivals on named criteria, never scored in isolation.
- Counterexamples and failed branches are retained as learning assets.
- The smallest reliable mechanism wins over feature breadth.
- The server stores and ranks; the skill diverges and talks to the human.

## Data and privacy

The database lives at `<workspace>/.idea-tree/ideas.sqlite3` and is ignored by Git by default. It may contain sensitive brainstorming content; review it before sharing, exporting, or changing ignore rules. The local `docs/` directory is also ignored so working notes and project records are not published accidentally.

`v0.3` uses schema version 3 with no migration from `v0.1` or `v0.2`. Opening an older database raises an error naming the workspace; delete that database or point at a different workspace rather than reusing it.

## Contributing

The project is at the architecture-and-core stage. Focus contributions on the durable state boundary, deterministic tree operations, correctness under stale context, and small end-to-end examples. Please avoid adding cloud services or broad interface layers before the local core is proven.

## License

[MIT](LICENSE) © 2026 Jinwei Lu.
