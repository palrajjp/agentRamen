# GitGraph

GitGraph is a local-first memory layer for Git repositories. It maintains a SQLite repository graph with files, symbols, imports, calls, dependencies, commits, renames, and co-change relationships, then retrieves task-relevant files and source excerpts. The goal is to help coding agents find useful context without repeatedly exploring every file.

GitGraph runs locally and has no runtime dependencies. Python files use the standard-library AST; other supported extensions use conservative regex analysis. Source is read locally, but only metadata and hashes are stored in `.gitgraph/graph.db`. Source excerpts are read on demand for context and are not sent to a service.

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
gitgraph explain src/auth/AuthService.ts [--json]
gitgraph history src/auth/AuthService.ts [--json]
gitgraph search "auth login" [--limit 20] [--json]
gitgraph architecture [--at <commit>] [--json]
gitgraph hotspots [--limit 20] [--json]
gitgraph changes [--limit 20] [--json]
gitgraph diff <commit1> <commit2> [--json]
gitgraph pr [--base origin/main] [--json]
gitgraph export [--json]
gitgraph benchmark --files 10 1000
gitgraph serve [--host 127.0.0.1] [--port 8765]
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

Context includes source excerpts only when the file still matches its indexed hash and does not contain a detected credential pattern. Its token count is a conservative character-based estimate, not a tokenizer measurement or performance claim.

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

The reusable workflow fetches Git history, restores a cache, tests the installed package, indexes the checked-out revision, summarizes pull requests, and publishes the local SQLite artifact. Use a full-depth checkout when running the CLI outside this reusable workflow to retain history.

## MCP

Run `gitgraph mcp` from a repository and configure your MCP-compatible agent to launch that command in the repository working directory. Tools include `repo_context`, `repo_status`, `repo_history`, `repo_search`, `repo_explain`, `repo_impact`, `repo_dependencies`, `repo_tests`, `repo_changes`, `repo_architecture`, `repo_hotspots`, and `repo_graph`. The stdio server needs no API keys and does not access a network service.

## Local HTTP API

`gitgraph serve` listens only on `127.0.0.1` by default. It provides `GET /health`, `GET /api/v1/repository`, `/architecture`, `/graph`, `/files/{path}`, `/impact?path=...`, `/history?path=...`, `/hotspots`, and `POST /api/v1/context` or `/api/v1/search`. POST requests accept JSON objects such as `{"task":"Add OAuth","token_budget":2000}`. No authentication is provided; do not bind to a public interface without placing an authenticated access-control layer in front.

## Benchmarking

Run `gitgraph benchmark --files 10 1000` to measure synthetic initial indexing, a one-file incremental update, and context retrieval on the current machine. The command reports the measured numbers for that run; it does not claim they generalize to other repositories or systems.

## Privacy and exclusions

The index stays in `.gitgraph/graph.db` on the local machine or in the configured CI artifact. GitGraph skips common generated directories, environment files, private-key files, oversized files, binary files, and files containing recognizable private-key or credential assignment patterns. Add more path patterns to `.gitgraphignore` (one pattern per line). Review your ignore rules before publishing generated artifacts.

## Current limitations

GitGraph has not yet implemented Tree-sitter, semantic embeddings, a web UI, or full commit-addressable graph snapshots. Parsing outside Python is heuristic and call edges are currently Python-only. Import resolution supports common file/module layouts, not every workspace alias or language-specific build system. Historical indexing retains at most 100 commits on a fresh/full scan; PR architecture assessment is based on indexed dependency edges rather than semantic boundary rules. Context ranking uses lexical relevance, graph relationships, and recent history; no model-based confidence or measured tokenizer is used. Benchmark outputs must be measured locally and are not performance guarantees.

## Development

```bash
python -m unittest discover -s tests -v
python -m gitgraph.cli status --json
```

Apache-2.0 licensed. Contributions that add language analyzers should keep parser-specific behavior separate from the indexing and context interfaces.
