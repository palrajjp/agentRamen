# GitGraph

GitGraph is a local-first memory layer for Git repositories. It maintains a SQLite repository graph with files, symbols, imports, calls, dependencies, commits, renames, and co-change relationships, then retrieves task-relevant files and source excerpts. The goal is to help coding agents find useful context without repeatedly exploring every file.

GitGraph's default installation runs locally with no runtime dependencies. Python files use the standard-library AST; other supported extensions use conservative regex analysis unless the optional Tree-sitter extra is installed. Source excerpts are read on demand for context. When semantic retrieval is enabled, locally generated embeddings are also stored in `.gitgraph/graph.db`; source and embeddings are not sent to a GitGraph service.

## Install

Requires Python 3.10+ and Git.

```bash
python -m pip install .
cd /path/to/a/git/repository
gitgraph init
gitgraph context "Add authentication"
```

Optional extras enable structured parsing for additional languages and local semantic retrieval:

```bash
python -m pip install '.[treesitter]'
python -m pip install '.[semantic]'
python -m pip install '.[tokenizer]'
```

Semantic retrieval uses FastEmbed and may download/initialize its configured model on first use; embedding inference runs locally.

`gitgraph init` creates `.gitgraph.yml` and a GitHub Actions caller workflow if they do not exist, then creates/updates `.gitgraph/graph.db`. `gitgraph update` and `gitgraph index` incrementally refresh changed files. The database is ignored by Git.

### Configuration

GitGraph reads the following fields from `.gitgraph.yml`:

```yaml
version: 1
indexing:
  incremental: true
git:
  history: true
  co_changes: true
history:
  commits: 100
semantic:
  enabled: false
  model: BAAI/bge-small-en-v1.5
context:
  default_budget: 2000
  tokenizer_model: ""
ignore:
  - .env
  - "*.pem"
  - private/
```

`indexing.incremental: false` reparses every eligible file on each index. `git.history: false` removes stored commit, rename, and co-change history; enabling it later rebuilds the configured recent history. `history.commits` controls retained commits (1–100,000), and `git.co_changes` toggles co-change edges. `context.default_budget` is used by the CLI, MCP, and HTTP API when no budget is passed. Configuration is a dependency-free YAML subset; unsupported/malformed values are rejected with a setting-specific error. `.gitgraphignore` patterns are added to, rather than replacing, the built-in and YAML ignore rules.

`semantic.enabled: true` opts into local embedding generation and semantic context ranking; install `gitgraph[semantic]` first. `semantic.model` selects a FastEmbed model. `gitgraph[treesitter]` opts into Tree-sitter-based parsing for supported non-Python languages; without it, or if a grammar is unavailable, GitGraph falls back to regex analysis.

`context.tokenizer_model` optionally selects a model supported by `tiktoken` for tokenizer-based context budgets; install `gitgraph[tokenizer]` to use it. The tokenizer vocabulary may be downloaded and cached on first use, then counting runs locally. Leave it empty to use the dependency-free approximate character-based fallback. Context responses identify `token_count_method` (`tiktoken` or `approximate`) and the tokenizer model when configured. The reported total counts the compact serialized `files` payload (the retrieved context), using the same method as selection and budgeting; per-file counts are provided as `estimated_tokens`.

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
gitgraph export [--at <commit>] [--json]
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

Context includes source excerpts only when the file still matches its indexed hash and does not contain a detected credential pattern. Token counts use the configured model tokenizer when available; otherwise they are approximate character-based estimates, not tokenizer measurements or performance claims.

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

`gitgraph serve` listens only on `127.0.0.1` by default. The browser UI is available at `/` and `/ui`. The API provides `GET /health`, `GET /api/v1/repository`, `/architecture`, `/graph`, `/files/{path}`, `/impact?path=...`, `/history?path=...`, `/hotspots`, and `POST /api/v1/context` or `/api/v1/search`. Append `?at=<commit>` to `/api/v1/graph` or `/api/v1/architecture` to query a cached, commit-addressable graph snapshot. POST requests accept JSON objects such as `{"task":"Add OAuth","token_budget":2000}`. No authentication is provided; do not bind to a public interface without placing an authenticated access-control layer in front.

## Benchmarking

Run `gitgraph benchmark --files 10 1000` to measure synthetic initial indexing, a one-file incremental update, and context retrieval. Add `--repository /path/to/repo` to measure a temporary copy of a real repository's tracked working-tree files as well. Use `--semantic-mode both` to compare semantic retrieval off/on; the enabled run requires `gitgraph[semantic]` and downloads its configured model if needed. If model setup is unavailable, the comparison reports that mode as an error and still returns measurements for the successful mode. Output includes lexical candidate count, database size, and retrieval/index timings. Results are local measurements, not cross-machine guarantees.

## Privacy and exclusions

The index stays in `.gitgraph/graph.db` on the local machine or in the configured CI artifact. With semantic retrieval disabled, the database stores source metadata and hashes; when enabled, it additionally stores locally generated embeddings. GitGraph skips common generated directories, environment files, private-key files, oversized files, binary files, and files containing recognizable private-key or credential assignment patterns. Add more path patterns to `.gitgraphignore` (one pattern per line). Review your ignore rules before publishing generated artifacts.

## Current limitations

Tree-sitter parsing, semantic embeddings, the local web UI, and cached commit-addressable graph snapshots are implemented. Tree-sitter and semantic retrieval are optional extras; parsing falls back to regex where needed. Import resolution supports common file/module layouts, not every workspace alias or language-specific build system. Historical indexing retains at most 100 commits by default; PR architecture assessment is based on indexed dependency edges rather than semantic boundary rules. Context token estimates are not measured with a model tokenizer, and benchmark outputs must be measured locally rather than treated as performance guarantees.

Lexical context retrieval uses an incremental SQLite inverted index over paths, symbols, and imports, along with indexed document frequencies. History and graph relationships are queried only for lexical/semantic candidates and their relevant neighbors, rather than scanning all indexed rows and edges. Ranking is covered by parity tests against the former full-scan behavior. When semantic retrieval is enabled, exact similarity still evaluates stored embeddings; consider an optional approximate-nearest-neighbor index only if repository benchmarks show this is a bottleneck.

## Development

```bash
python -m unittest discover -s tests -v
python -m gitgraph.cli status --json
```

Apache-2.0 licensed. Contributions that add language analyzers should keep parser-specific behavior separate from the indexing and context interfaces.
