# GitGraph

GitGraph is a local-first memory layer for Git repositories. It maintains a small SQLite index of repository paths, detected languages, symbols, imports, and Git change history, then ranks a compact set of files for a coding task. The goal is to help coding agents find useful context without repeatedly exploring every file.

This initial release is deliberately dependency-free and heuristic: it provides incremental file metadata indexing and lexical context retrieval, not a full AST or semantic code graph. Source contents are read locally for indexing and are not sent to any service.

## Install

Requires Python 3.10+ and Git.

```bash
python -m pip install .
cd /path/to/a/git/repository
gitgraph init
gitgraph context "Add authentication"
```

`gitgraph init` creates `.gitgraph.yml` and a GitHub Actions caller workflow if they do not exist, then creates/updates `.gitgraph/graph.db`. `gitgraph update` and `gitgraph index` incrementally refresh changed files. The database is ignored by Git.

## Commands

```text
gitgraph init
gitgraph index [--json]
gitgraph update [--json]
gitgraph status [--json]
gitgraph context "Add OAuth login" [--budget 2000] [--json]
gitgraph impact src/auth/AuthService.ts [--json]
gitgraph history src/auth/AuthService.ts [--json]
gitgraph mcp
```

Example context response:

```json
{
  "task": "Add authentication",
  "files": [
    {
      "path": "src/auth_service.py",
      "language": "python",
      "symbols": ["AuthService"],
      "imports": [],
      "recent_change": "add authentication",
      "estimated_tokens": 18
    }
  ],
  "token_budget": 2000,
  "files_avoided": 12,
  "confidence": "medium"
}
```

The token count is a rough metadata estimate, not a measured model-token count or a performance claim.

## GitHub Actions

The workflow created by `gitgraph init` calls the reusable workflow in this repository:

```yaml
name: GitGraph
on:
  push:
    branches: ["**"]
  pull_request:
  workflow_dispatch:
  schedule:
    - cron: "17 4 * * 1"
jobs:
  gitgraph:
    uses: palrajjp/gitGraph/.github/workflows/index.yml@main
```

The reusable workflow fetches Git history, restores a cache, indexes the checked-out revision, and publishes the local SQLite artifact. Use a full-depth checkout when running the CLI outside this reusable workflow to retain history.

## MCP

Run `gitgraph mcp` from a repository and configure your MCP-compatible agent to launch that command in the repository working directory. The stdio server currently exposes `repo_context`, `repo_status`, and `repo_history`. It needs no API keys and does not access a network service.

## Privacy and exclusions

The index stays in `.gitgraph/graph.db` on the local machine or in the configured CI artifact. GitGraph skips common generated directories, environment files, private-key files, oversized files, binary files, and files containing recognizable private-key or credential assignment patterns. Add more path patterns to `.gitgraphignore` (one pattern per line). Review your ignore rules before publishing generated artifacts.

## Current limitations

The first milestone does not yet implement Tree-sitter parsing, semantic embeddings, graph snapshots, a web UI, or complete symbol-level call/dependency analysis. Symbol and import extraction is intentionally lightweight and language-agnostic. Context retrieval uses lexical overlap and recent file history; its confidence and token estimates are heuristic. The SQLite artifact contains repository path and source-derived metadata only, not stored source text.

## Development

```bash
python -m unittest discover -s tests -v
python -m gitgraph.cli status --json
```

Apache-2.0 licensed. Contributions that add language analyzers should keep parser-specific behavior separate from the indexing and context interfaces.
