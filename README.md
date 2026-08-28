# Brainstorming Tree

[简体中文](README_zh.md) | English

**A lightweight Agent Plugin for durable, MCTS-inspired brainstorming.** It persists ideas as an auditable tree so long-running agent workflows can survive context loss, interference, and interruption.

> **Status:** early development (`v0.1.0`). The agent-agnostic durable core is implemented and its focused state-transition and protocol checks pass. Cross-agent packaging and validation on a long real-world brainstorming project are still pending.

## Why this project exists

Long brainstorming sessions ask a language model to remember too much at once: the original goal, competing branches, discarded assumptions, evidence, revisions, and why one idea replaced another. As the conversation grows, older details can leave the active context or become diluted by unrelated text. The result is familiar: repeated ideas, forgotten constraints, inconsistent judgments, and promising branches that disappear.

Brainstorming Tree moves that state out of conversational memory. The conversation remains the interface; a project-local idea tree becomes the source of truth.

## The approach

The Agent Plugin combines four deliberately small pieces:

1. **A portable Agent Skill** defines a disciplined loop for selecting, expanding, evaluating, and synthesizing ideas.
2. **A local MCP server** exposes typed tree operations instead of relying on a model to rewrite prose consistently.
3. **A SQLite database** stores nodes, lineage, evaluations, versions, and an operation history under the current workspace.
4. **Thin runtime adapters** connect the same core to the packaging and configuration conventions of individual agent hosts.

```text
User goal
   │
   ▼
Agent + portable brainstorming skill
   │  typed operations
   ▼
Local MCP server
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

The current repository includes a `.codex-plugin/plugin.json` manifest as the first adapter. It is a compatibility layer, not the identity or architectural boundary of the project. Additional adapters should remain thin and must reuse the same MCP server and Agent Skill.

## MCTS, adapted for real brainstorming

Brainstorming Tree borrows the useful control loop from Monte Carlo Tree Search (MCTS), but does not pretend that random rollouts can determine whether a real-world idea is good.

| Phase | What happens |
| --- | --- |
| **Select** | Balance high-value branches with underexplored branches using an upper-confidence score. |
| **Expand** | Add a materially different mechanism, a testable idea, or a synthesis. |
| **Evaluate** | Record the cheapest valid discriminator: evidence, an experiment, a logical trace, or an explicit user judgment. |
| **Backpropagate** | Update the evaluation statistics of the node and its ancestors to guide the next allocation of attention. |

Scores guide exploration; they are not truth, confidence, or proof. Failed ideas remain visible, and a repaired proposal becomes a new linked node instead of silently replacing its predecessor.

## Intended guarantees

- **Durable state:** a session can resume after context compaction or process interruption.
- **Explicit lineage:** every idea keeps its parent, predecessors, and synthesis sources.
- **Auditable changes:** mutations and evaluations leave an operation history.
- **Safe updates:** version checks prevent stale model context from overwriting newer state.
- **Reversible removal:** deletion creates a tombstone rather than erasing history.
- **Local ownership:** project data stays in the workspace by default.

## Current status

| Component | Status |
| --- | --- |
| Plugin manifest and metadata | Validator passed |
| Idea-tree brainstorming skill | Validator passed |
| Local MCP server | Core implementation complete |
| SQLite schema and 13 tree operations | Core implementation complete |
| Focused CRUD, backpropagation, invalidation, and protocol checks | Passed |
| First runtime adapter | Codex manifest available |
| Additional agent adapters and installation guides | Pending |

Until cross-agent packaging and real-world trials are complete, treat the repository as an experimental Agent Plugin rather than a production-ready tool.

## Repository layout

```text
.
├── README.md
├── README_zh.md
├── brainstorming-tree/
│   ├── .codex-plugin/plugin.json     # First runtime adapter: Codex
│   ├── .mcp.json                     # Local MCP server declaration
│   ├── skills/
│   │   └── idea-tree-brainstorming/  # Portable Agent Skill and state rules
│   └── scripts/                      # State engine and MCP server
└── docs/important-information/       # Local decisions and project state (Git-ignored)
```

## Planned user flow

1. Give a compatible agent a problem, decision, or open-ended idea.
2. Create a tree with an immutable goal and explicit success and stop conditions.
3. Let the plugin select a branch, add a distinct candidate, and attach evidence-backed evaluations.
4. Resume the same tree in a later session without reconstructing it from chat history.
5. Deliver the strongest surviving synthesis together with decisive evidence, unresolved branches, and reopening conditions.

## Roadmap

### `v0.1` — durable core

- [x] Implement the dependency-free local MCP server.
- [x] Add SQLite-backed tree, node, evaluation, selection, history, and snapshot operations.
- [x] Demonstrate persistence and ancestor backpropagation in a minimal end-to-end check.
- [x] Pass the plugin manifest and skill validators.
- [x] Check version conflicts, tombstones, invalidation, and both JSON-RPC lifecycle paths.
- [ ] Document the portable core contract and package thin adapters for supported agent runtimes.

### Later, only when evidence justifies it

- Improve ranking and evaluation calibration from real usage traces.
- Add and verify runtime adapters based on actual user demand.
- Add import/export and compact human-readable reports.
- Explore visualization, collaboration, or remote synchronization without weakening the local core.

## Design principles

- State lives outside the model.
- Evidence outranks eloquence and confidence.
- Exploration scores allocate attention; they do not decide truth.
- Counterexamples and failed branches are retained as learning assets.
- The smallest reliable mechanism wins over feature breadth.

## Data and privacy

The planned database lives at `<workspace>/.idea-tree/ideas.sqlite3` and is ignored by Git by default. It may contain sensitive brainstorming content; review it before sharing, exporting, or changing ignore rules. The local `docs/` directory is also ignored so working notes and project records are not published accidentally.

## Contributing

The project is at the architecture-and-core stage. Focus contributions on the durable state boundary, deterministic tree operations, correctness under stale context, and small end-to-end examples. Please avoid adding cloud services or broad interface layers before the local core is proven.

## License

[MIT](LICENSE) © 2026 Jinwei Lu.
