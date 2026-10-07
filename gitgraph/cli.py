from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .core import (
    GitGraphError,
    connect,
    context_for,
    file_history,
    find_root,
    index_repository,
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
    conn = connect(root)
    files = conn.execute("SELECT path, imports, symbols FROM files").fetchall()
    by_name = {}
    for path, imports, symbols in files:
        by_name[path] = (json.loads(imports), json.loads(symbols))
    target_name = Path(target).name
    target_stem = Path(target).stem
    direct = []
    for path, (imports, symbols) in by_name.items():
        if path == target:
            continue
        if any(Path(item).name in (target_name, target_stem) for item in imports):
            direct.append(path)
        elif any(symbol in " ".join(imports) for symbol in by_name.get(target, ([], []))[1]):
            direct.append(path)
    history = file_history(root, target)
    conn.close()
    return {"target": target, "direct_dependents": sorted(set(direct)), "history": history}


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
