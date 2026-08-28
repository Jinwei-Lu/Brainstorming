# Repository synchronization

## Decision

The project is synchronized through Git with this remote repository:

`https://github.com/Jinwei-Lu/Brainstorming.git`

The local `main` branch tracks `origin/main`.

## Starting state

- The GitHub repository contained `README.md` and `LICENSE` on `main`.
- The local project contained an untracked `brainstorming-tree/` plugin scaffold.
- The remote history and local project files did not conflict.
- The checked local configuration did not contain credentials. The MCP configuration had an empty server list.

## Constraints and safeguards

- Preserve both the remote history and the local plugin files.
- Do not commit passwords, access tokens, private keys, or generated secrets.
- Empty `scripts/` and `skills/` directories are preserved with `.gitkeep` files until they contain real files.

## Current state

- Remote name: `origin`
- Default working branch: `main`
- Upstream branch: `origin/main`
- Local project content is intended to be committed and pushed to the upstream branch.

## Routine synchronization

Before starting work, run `git pull --ff-only`. After making and reviewing changes, commit them and run `git push`.
