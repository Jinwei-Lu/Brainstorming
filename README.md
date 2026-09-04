# Brainstorming Tree

[简体中文](README_zh.md) | English

**A lightweight Agent Plugin for durable, strong-inference brainstorming.** It persists ideas as an auditable tree so long-running agent workflows can survive context loss, interference, and interruption.

> **Status:** early development (`v0.2.0`). The agent-agnostic durable core is implemented; cross-agent packaging, an automated test suite, and validation on a long real-world brainstorming project are still pending. See [Current status](#current-status) for what is and is not reproducible from this repository alone.

## Why this project exists

Brainstorming Tree exists for one purpose: a human and an AI discussing together to produce valuable research ideas. That purpose has two persistent difficulties — staying light enough to actually use, and resisting the pull toward auto-research degenerating into parameter search, where a mechanism with one knob turned poses as a new idea.

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

## Strong-inference brainstorming

Brainstorming Tree ranks ideas the way a strong-inference discipline does: never in isolation, always against a named rival on a named criterion, pruned only when the evidence says so.

| Stage | What happens |
| --- | --- |
| **Expand** | Add a materially different mechanism, a testable idea, or a synthesis, each carrying explicit assumptions and a kill condition. |
| **Compare** | Record a pairwise comparison between two siblings on one named criterion, backed by evidence: an experiment, a source, a logical trace, or an explicit user judgment. |
| **Discriminate** | Score every open discriminator — an observation, or an inferred/assumed question or constraint — by how many still-undominated branches it would separate, divided by its cost; act on the top-scoring one, or fall back to comparing the least-examined node if none qualifies. |
| **Prune** | Drop a branch only when some other live candidate has beaten it on every comparison so far with no tie. An uncompared branch is never treated as inferior. |

An earlier version of this project borrowed Monte Carlo Tree Search's control loop directly — select by an upper-confidence score, evaluate with a scalar, backpropagate that scalar to every ancestor. It was removed: pairwise LLM judgment is measurably more reliable than asking a model to score an idea on its own, tree search only beats a flat re-ranking of candidates when the discriminator choosing between them is very accurate (an accuracy LLM judges don't reliably reach), and a scalar score assigned before an idea is actually tried runs systematically high. What is left of the tree is a ledger of lineage and evidence, not a search procedure; ranking comes from Bradley-Terry aggregation over recorded comparisons, not from a walked score.

Two rules guard the ledger against the more common failure of auto-research: quietly re-running the same idea with a knob turned. A child idea or synthesis is refused unless it adds an assumption its non-root parent does not already hold, so a parameter variation cannot pose as a new mechanism — it becomes a comparison or evaluation on the parent instead. And when every surviving idea already shares an assumption none of them has questioned, tree reads return it as `shared_assumptions`, so the next expansion can target that unquestioned ground rather than refine around it.

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
| Plugin manifest and metadata | Present in the repository; no validator run is recorded here |
| Idea-tree brainstorming skill | Present in the repository; no validator run is recorded here |
| Local MCP server | Core implementation complete (v0.2 schema and tool set) |
| SQLite schema and 16 tree operations | Core implementation complete |
| Automated test suite | In progress under `brainstorming-tree/tests/`, runnable with `cd brainstorming-tree && python3 -m unittest discover -s tests` |
| First runtime adapter | Codex manifest available |
| Additional agent adapters and installation guides | Pending |

Until the test suite is complete, cross-agent packaging is finished, and the plugin has seen a real-world brainstorming project, treat the repository as an experimental Agent Plugin rather than a production-ready tool. Nothing in this table is a substitute for cloning the repository and running the command yourself.

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
│   ├── scripts/                      # State engine and MCP server
│   └── tests/                        # Unittest suite (in progress)
└── docs/important-information/       # Local decisions and project state (Git-ignored)
```

## Planned user flow

1. Give a compatible agent a problem, decision, or open-ended idea.
2. Create a tree with an immutable goal and explicit success and stop conditions.
3. Let the plugin select a branch, add a distinct candidate, and attach evidence-backed evaluations.
4. Resume the same tree in a later session without reconstructing it from chat history.
5. Deliver the strongest surviving synthesis together with decisive evidence, unresolved branches, and reopening conditions.

## Roadmap

### `v0.2` — strong-inference core

- [x] Implement the dependency-free local MCP server.
- [x] Add SQLite-backed tree, node, comparison, question (observations are a question kind), and evaluation operations.
- [x] Replace scalar scoring and backpropagation with pairwise comparison, Bradley-Terry ranking, and Pareto-domination pruning.
- [x] Move to a fresh schema (`SCHEMA_VERSION = 2`) with no migration; opening a `v0.1` database raises an error instead of silently reinterpreting it.
- [ ] Land the automated test suite under `brainstorming-tree/tests/` and record a passing run.
- [ ] Document the portable core contract and package thin adapters for supported agent runtimes.

### Later, only when evidence justifies it

- Improve ranking and evaluation calibration from real usage traces.
- Add and verify runtime adapters based on actual user demand.
- Add import/export and compact human-readable reports.
- Explore visualization, collaboration, or remote synchronization without weakening the local core.

## Design principles

- State lives outside the model.
- Evidence outranks eloquence and confidence.
- Ideas are ranked against named rivals on named criteria, never scored in isolation.
- Counterexamples and failed branches are retained as learning assets.
- The smallest reliable mechanism wins over feature breadth.

## Data and privacy

The planned database lives at `<workspace>/.idea-tree/ideas.sqlite3` and is ignored by Git by default. It may contain sensitive brainstorming content; review it before sharing, exporting, or changing ignore rules. The local `docs/` directory is also ignored so working notes and project records are not published accidentally.

`v0.2` uses a fresh schema (`SCHEMA_VERSION = 2`) with no migration from `v0.1`. Opening a `v0.1` database raises an error naming the workspace; delete that database or point at a different workspace rather than reusing it.

## Contributing

The project is at the architecture-and-core stage. Focus contributions on the durable state boundary, deterministic tree operations, correctness under stale context, and small end-to-end examples. Please avoid adding cloud services or broad interface layers before the local core is proven.

## License

[MIT](LICENSE) © 2026 Jinwei Lu.
