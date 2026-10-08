from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .core import (
    GitGraphError,
    architecture,
    architecture_at,
    connect,
    context_for,
    explain_file,
    file_history,
    find_root,
    git,
    graph_export,
    hotspots,
    index_repository,
    repo_search,
    repository_status,
)

def _output(value: object, as_json: bool = False) -> None:
    if as_json:
        print(json.dumps(value, indent=2, sort_keys=True))
    elif isinstance(value, dict):
        for key, item in value.items():
            print(f"{key.replace('_', ' ').title()}: {item}")
    else:
        print(value)


def _init(root: Path) -> dict[str, object]:
    config = root / ".gitgraph.yml"
    workflow = root / ".github" / "workflows" / "gitgraph.yml"
    if not config.exists():
        config.write_text(
            f"version: 1\nrepository:\n  name: {json.dumps(root.name)}\n"
            "indexing:\n  incremental: true\n"
            "languages:\n  auto_detect: true\n"
            "git:\n  history: true\n  co_changes: true\n"
            "semantic:\n  enabled: false\n"
            "context:\n  default_budget: 2000\n"
            "ignore:\n  - .env\n  - \"*.pem\"\n  - \"*.key\"\n  - secrets/\n  - credentials/\n",
            encoding="utf-8",
        )
    if not workflow.exists():
        workflow.parent.mkdir(parents=True, exist_ok=True)
        workflow.write_text(
            "name: GitGraph\n\n"
            "on:\n"
            "  push:\n"
            "    branches: [\"**\"]\n"
            "  pull_request:\n"
            "  workflow_dispatch:\n"
            "  schedule:\n"
            "    - cron: \"17 4 * * 1\"\n\n"
            "jobs:\n"
            "  gitgraph:\n"
            "    uses: palrajjp/gitGraph/.github/workflows/index.yml@main\n",
            encoding="utf-8",
        )
    stats = index_repository(root)
    return {"config": str(config.relative_to(root)), "workflow": str(workflow.relative_to(root)), **stats}


def _impact(root: Path, target: str) -> dict[str, object]:
    report = explain_file(root, target)
    return {
        "target": target,
        "direct_dependents": report["dependents"],
        "dependencies": report["dependencies"],
        "co_changes": report["co_changes"],
        "history": report["history"],
        "estimated_impact": len(report["dependents"]),
    }


def _mcp(root: Path) -> None:
    tools = [
        {
            "name": "repo_context",
            "description": "Retrieve compact repository context for a coding task.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "task": {"type": "string"},
                    "token_budget": {"type": "integer", "default": 2000},
                },
                "required": ["task"],
            },
        },
        {
            "name": "repo_status",
            "description": "Show indexed repository and language counts.",
            "inputSchema": {"type": "object", "properties": {}},
        },
        {
            "name": "repo_history",
            "description": "Show indexed Git history for a file.",
            "inputSchema": {
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
        },
        {
            "name": "repo_search",
            "description": "Search indexed file paths and symbols.",
            "inputSchema": {
                "type": "object",
                "properties": {"query": {"type": "string"}, "limit": {"type": "integer"}},
                "required": ["query"],
            },
        },
        {
            "name": "repo_explain",
            "description": "Explain a file's symbols, dependencies, dependents, and history.",
            "inputSchema": {
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
        },
        {
            "name": "repo_impact",
            "description": "Show direct dependencies, dependents, and historical coupling.",
            "inputSchema": {
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
        },
        {
            "name": "repo_architecture",
            "description": "Summarize repository languages, modules, and graph relationships.",
            "inputSchema": {"type": "object", "properties": {}},
        },
        {
            "name": "repo_hotspots",
            "description": "List files with the most recorded Git changes.",
            "inputSchema": {
                "type": "object",
                "properties": {"limit": {"type": "integer"}},
            },
        },
        {
            "name": "repo_graph",
            "description": "Export the indexed nodes and relationships.",
            "inputSchema": {"type": "object", "properties": {}},
        },
    ]
    for line in sys.stdin:
        message = {}
        try:
            message = json.loads(line)
            method = message.get("method")
            params = message.get("params", {})
            if method == "initialize":
                result = {
                    "protocolVersion": params.get("protocolVersion", "2024-11-05"),
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "gitgraph", "version": "0.1.0"},
                }
            elif method == "notifications/initialized":
                continue
            elif method == "ping":
                result = {}
            elif method == "tools/list":
                result = {"tools": tools}
            elif method == "tools/call":
                name = params.get("name")
                args = params.get("arguments", {})
                if name == "repo_context":
                    value = context_for(root, args.get("task", ""), int(args.get("token_budget", 2000)))
                elif name == "repo_status":
                    value = repository_status(root)
                elif name == "repo_history":
                    value = file_history(root, args.get("path", ""))
                elif name == "repo_search":
                    value = repo_search(root, args.get("query", ""), int(args.get("limit", 20)))
                elif name == "repo_explain":
                    value = explain_file(root, args.get("path", ""))
                elif name == "repo_impact":
                    value = _impact(root, args.get("path", ""))
                elif name == "repo_architecture":
                    value = architecture(root)
                elif name == "repo_hotspots":
                    value = hotspots(root, int(args.get("limit", 20)))
                elif name == "repo_graph":
                    value = graph_export(root)
                else:
                    raise GitGraphError(f"Unknown MCP tool: {name}")
                result = {
                    "content": [{"type": "text", "text": json.dumps(value, indent=2)}],
                    "structuredContent": value,
                }
            else:
                raise GitGraphError(f"Unknown MCP method: {method}")
            response = {"jsonrpc": "2.0", "id": message.get("id"), "result": result}
        except Exception as exc:
            response = {
                "jsonrpc": "2.0",
                "id": message.get("id") if "message" in locals() else None,
                "error": {"code": -32603, "message": str(exc)},
            }
        print(json.dumps(response, separators=(",", ":")), flush=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="gitgraph", description="Local-first Git repository memory")
    subparsers = parser.add_subparsers(dest="command", required=True)
    for command in ("init", "index", "update", "status", "mcp"):
        sub = subparsers.add_parser(command)
        sub.add_argument("--json", action="store_true")
    context = subparsers.add_parser("context", help="Retrieve compact context for a task")
    context.add_argument("task")
    context.add_argument("--budget", type=int, default=2000)
    context.add_argument("--json", action="store_true")
    history = subparsers.add_parser("history", help="Show indexed history for a file")
    history.add_argument("path")
    history.add_argument("--json", action="store_true")
    impact = subparsers.add_parser("impact", help="Find files that import a target")
    impact.add_argument("target")
    impact.add_argument("--json", action="store_true")
    explain = subparsers.add_parser("explain", help="Explain a file and its relationships")
    explain.add_argument("path")
    explain.add_argument("--json", action="store_true")
    search = subparsers.add_parser("search", help="Search indexed paths and symbols")
    search.add_argument("query")
    search.add_argument("--limit", type=int, default=20)
    search.add_argument("--json", action="store_true")
    arch = subparsers.add_parser("architecture", help="Summarize repository structure")
    arch.add_argument("--at", dest="revision")
    arch.add_argument("--json", action="store_true")
    hot = subparsers.add_parser("hotspots", help="List frequently changed files")
    hot.add_argument("--limit", type=int, default=20)
    hot.add_argument("--json", action="store_true")
    export = subparsers.add_parser("export", help="Export the current graph")
    export.add_argument("--json", action="store_true")
    diff = subparsers.add_parser("diff", help="Compare files changed between two commits")
    diff.add_argument("commit1")
    diff.add_argument("commit2")
    diff.add_argument("--json", action="store_true")
    serve = subparsers.add_parser("serve", help="Start the local versioned HTTP API")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8765)
    args = parser.parse_args(argv)
    try:
        root = find_root()
        if args.command == "init":
            value = _init(root)
        elif args.command in ("index", "update"):
            value = index_repository(root)
        elif args.command == "status":
            value = repository_status(root)
        elif args.command == "context":
            value = context_for(root, args.task, args.budget)
        elif args.command == "history":
            value = file_history(root, args.path)
        elif args.command == "impact":
            value = _impact(root, args.target)
        elif args.command == "explain":
            value = explain_file(root, args.path)
        elif args.command == "search":
            value = repo_search(root, args.query, args.limit)
        elif args.command == "architecture":
            value = architecture_at(root, args.revision) if args.revision else architecture(root)
        elif args.command == "hotspots":
            value = hotspots(root, args.limit)
        elif args.command == "export":
            value = graph_export(root)
        elif args.command == "diff":
            value = [
                {"status": line[:1], "path": line[1:].strip()}
                for line in git(root, "diff", "--name-status", args.commit1, args.commit2).splitlines()
                if len(line) >= 2
            ]
        elif args.command == "serve":
            from .server import serve as serve_api

            serve_api(root, args.host, args.port)
            return 0
        else:
            _mcp(root)
            return 0
        _output(value, args.json)
        return 0
    except (GitGraphError, OSError, ValueError) as exc:
        print(f"gitgraph: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
