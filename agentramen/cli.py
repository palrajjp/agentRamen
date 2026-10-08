from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from . import __version__
from .benchmark import run_comparisons, run_quality_benchmark
from .core import (
    AgentRamenError,
    architecture,
    architecture_at,
    connect,
    context_for,
    dependency_impact,
    explain_file,
    find_dependencies,
    file_history,
    find_root,
    git,
    graph_at,
    graph_export,
    hotspots,
    index_repository,
    pull_request_summary,
    recent_changes,
    repo_search,
    repository_status,
)
from .memory import (
    approve_memory,
    memory_audit,
    publish_shared_memory,
    reject_memory,
    review_queue,
    search_shared_memories,
    stage_memory,
)
from .snapshots import export_snapshot, mounted_snapshot, verify_snapshot

def _output(value: object, as_json: bool = False) -> None:
    if as_json:
        print(json.dumps(value, indent=2, sort_keys=True))
    elif isinstance(value, dict):
        for key, item in value.items():
            print(f"{key.replace('_', ' ').title()}: {item}")
    else:
        print(value)


def _init(root: Path) -> dict[str, object]:
    config = root / ".agentramen.yml"
    workflow = root / ".github" / "workflows" / "agentramen.yml"
    if not config.exists():
        config.write_text(
            f"version: 1\nrepository:\n  name: {json.dumps(root.name)}\n"
            "indexing:\n  incremental: true\n"
            "languages:\n  auto_detect: true\n"
            "git:\n  history: true\n  co_changes: true\n"
            "history:\n  commits: 100\n"
            "semantic:\n  enabled: false\n"
            "context:\n  default_budget: 2000\n"
            "ignore:\n  - .env\n  - \"*.pem\"\n  - \"*.key\"\n  - secrets/\n  - credentials/\n",
            encoding="utf-8",
        )
    if not workflow.exists():
        workflow.parent.mkdir(parents=True, exist_ok=True)
        workflow.write_text(
            "name: agentRamen\n\n"
            "on:\n"
            "  push:\n"
            "    branches: [\"**\"]\n"
            "  pull_request:\n"
            "  workflow_dispatch:\n"
            "  schedule:\n"
            "    - cron: \"17 4 * * 1\"\n\n"
            "jobs:\n"
            "  agentramen:\n"
            "    uses: palrajjp/agentRamen/.github/workflows/index.yml@main\n",
            encoding="utf-8",
        )
    stats = index_repository(root)
    return {"config": str(config.relative_to(root)), "workflow": str(workflow.relative_to(root)), **stats}


def _impact(root: Path, target: str) -> dict[str, object]:
    return dependency_impact(root, target)


def _diff(root: Path, commit1: str, commit2: str) -> list[dict[str, str]]:
    fields = git(root, "diff", "--name-status", "-M", "-z", commit1, commit2).split("\0")
    changes = []
    index = 0
    while index < len(fields) and fields[index]:
        status = fields[index]
        index += 1
        if status.startswith(("R", "C")) and index + 1 < len(fields):
            changes.append(
                {"status": status, "previous_path": fields[index], "path": fields[index + 1]}
            )
            index += 2
        elif index < len(fields):
            changes.append({"status": status, "path": fields[index]})
            index += 1
    return changes


def _mcp(root: Path) -> None:
    tools = [
        {
            "name": "repo_context",
            "description": "Retrieve budgeted repository context with path, digest, and source-commit evidence.",
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
        {
            "name": "repo_dependencies",
            "description": "List files directly depended on by a target file.",
            "inputSchema": {
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
        },
        {
            "name": "repo_tests",
            "description": "Find tests and affected files for a target file.",
            "inputSchema": {
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
        },
        {
            "name": "repo_changes",
            "description": "Show the latest indexed commits and changed files.",
            "inputSchema": {
                "type": "object",
                "properties": {"limit": {"type": "integer"}},
            },
        },
        {
            "name": "repo_review_queue",
            "description": "List pending memory proposals and stale approved memories that need fresh context.",
            "inputSchema": {
                "type": "object",
                "properties": {"limit": {"type": "integer", "default": 50}},
            },
        },
        {
            "name": "repo_stage_memory",
            "description": "Stage a private proposal using evidence from the latest repo_context call; it is not shared until approved and published.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "subject": {"type": "string"},
                    "content": {"type": "string"},
                    "category": {"type": "string", "default": "context"},
                    "source": {"type": "string"},
                    "importance_score": {"type": "number", "default": 0.5},
                },
                "required": ["subject", "content"],
            },
        },
        {
            "name": "repo_approve_memory",
            "description": "Approve a staged memory and resolve temporal conflicts.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "epistemic_status": {
                        "type": "string",
                        "enum": ["VERIFIED", "ACTIVE", "INFERRED"],
                        "default": "VERIFIED",
                    },
                },
                "required": ["id"],
            },
        },
        {
            "name": "repo_publish_memory",
            "description": "Publish a locally approved memory as a Git-versioned team memory file for review and commit.",
            "inputSchema": {
                "type": "object",
                "properties": {"id": {"type": "string"}},
                "required": ["id"],
            },
        },
        {
            "name": "repo_team_memory_search",
            "description": "Search approved team memories checked out from Git; local/private staged memories are not included.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "limit": {"type": "integer", "default": 10},
                },
                "required": ["query"],
            },
        },
        {
            "name": "repo_memory_audit",
            "description": "Audit local and Git-shared memories for stale or missing source evidence.",
            "inputSchema": {"type": "object", "properties": {}},
        },
        {
            "name": "repo_reject_memory",
            "description": "Reject a staged repository memory.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "note": {"type": "string"},
                },
                "required": ["id"],
            },
        },
    ]
    last_context_evidence: list[dict[str, str]] = []
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
                    "serverInfo": {"name": "agentramen", "version": __version__},
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
                    token_budget = args.get("token_budget")
                    value = context_for(
                        root,
                        args.get("task", ""),
                        int(token_budget) if token_budget is not None else None,
                    )
                    last_context_evidence = list(value.get("evidence", []))
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
                elif name == "repo_dependencies":
                    value = find_dependencies(root, args.get("path", ""))
                elif name == "repo_tests":
                    value = dependency_impact(root, args.get("path", ""))
                elif name == "repo_changes":
                    value = recent_changes(root, int(args.get("limit", 20)))
                elif name == "repo_review_queue":
                    value = review_queue(root, int(args.get("limit", 50)))
                elif name == "repo_stage_memory":
                    value = {
                        "id": stage_memory(
                            root,
                            str(args.get("subject", "")),
                            str(args.get("content", "")),
                            category=str(args.get("category", "context")),
                            source=(str(args["source"]) if args.get("source") is not None else None),
                            importance_score=float(args.get("importance_score", 0.5)),
                            evidence=last_context_evidence,
                        ),
                        "status": "pending_review",
                        "shared": False,
                    }
                elif name == "repo_approve_memory":
                    value = approve_memory(
                        root,
                        str(args.get("id", "")),
                        str(args.get("epistemic_status", "VERIFIED")),
                    )
                elif name == "repo_reject_memory":
                    value = reject_memory(
                        root, str(args.get("id", "")), str(args.get("note", ""))
                    )
                elif name == "repo_publish_memory":
                    value = publish_shared_memory(root, str(args.get("id", "")))
                elif name == "repo_team_memory_search":
                    value = search_shared_memories(
                        root, str(args.get("query", "")), int(args.get("limit", 10))
                    )
                elif name == "repo_memory_audit":
                    value = memory_audit(root)
                else:
                    raise AgentRamenError(f"Unknown MCP tool: {name}")
                result = {
                    "content": [{"type": "text", "text": json.dumps(value, indent=2)}],
                    "structuredContent": value,
                }
            else:
                raise AgentRamenError(f"Unknown MCP method: {method}")
            response = {"jsonrpc": "2.0", "id": message.get("id"), "result": result}
        except Exception as exc:
            response = {
                "jsonrpc": "2.0",
                "id": message.get("id") if "message" in locals() else None,
                "error": {"code": -32603, "message": str(exc)},
            }
        print(json.dumps(response, separators=(",", ":")), flush=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="agentramen", description="Local-first Git repository memory")
    subparsers = parser.add_subparsers(dest="command", required=True)
    for command in ("init", "index", "update", "status", "mcp"):
        sub = subparsers.add_parser(command)
        sub.add_argument("--json", action="store_true")
    context = subparsers.add_parser("context", help="Retrieve compact context for a task")
    context.add_argument("task")
    context.add_argument("--budget", type=int)
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
    export.add_argument("--at", dest="revision")
    export.add_argument("--json", action="store_true")
    diff = subparsers.add_parser("diff", help="Compare files changed between two commits")
    diff.add_argument("commit1")
    diff.add_argument("commit2")
    diff.add_argument("--json", action="store_true")
    changes = subparsers.add_parser("changes", help="Show recent commits and changed paths")
    changes.add_argument("--limit", type=int, default=20)
    changes.add_argument("--json", action="store_true")
    pr = subparsers.add_parser("pr", help="Summarize changes against a base revision")
    pr.add_argument("--base", default="origin/main")
    pr.add_argument("--json", action="store_true")
    benchmark = subparsers.add_parser("benchmark", help="Measure indexing and retrieval locally")
    benchmark.add_argument("--files", nargs="+", type=int, default=[10, 1000])
    benchmark.add_argument("--repository", type=Path)
    benchmark.add_argument(
        "--semantic-mode", choices=("off", "on", "both"), default="off"
    )
    benchmark.add_argument(
        "--quality",
        action="store_true",
        help="Measure task-to-file relevance and affected-test accuracy on a labeled reference fixture",
    )
    benchmark.add_argument("--json", action="store_true")
    memory = subparsers.add_parser("memory", help="Review and share repository memories")
    memory_actions = memory.add_subparsers(dest="memory_action", required=True)
    publish = memory_actions.add_parser("publish", help="Publish an approved memory for team review")
    publish.add_argument("memory_id")
    publish.add_argument("--json", action="store_true")
    memory_search = memory_actions.add_parser("search", help="Search Git-shared team memories")
    memory_search.add_argument("query")
    memory_search.add_argument("--limit", type=int, default=10)
    memory_search.add_argument("--json", action="store_true")
    memory_audit_parser = memory_actions.add_parser(
        "audit", help="Report stale, current, and unverified memories"
    )
    memory_audit_parser.add_argument("--json", action="store_true")
    snapshot = subparsers.add_parser("snapshot", help="Export a sanitized central-service snapshot")
    snapshot_actions = snapshot.add_subparsers(dest="snapshot_action", required=True)
    snapshot_export = snapshot_actions.add_parser("export", help="Export the current indexed Git revision")
    snapshot_export.add_argument("--repository-id", required=True)
    snapshot_export.add_argument("--output", type=Path, required=True)
    snapshot_export.add_argument("--json", action="store_true")
    snapshot_verify = snapshot_actions.add_parser("verify", help="Validate a central-service snapshot")
    snapshot_verify.add_argument("--repository-id", required=True)
    snapshot_verify.add_argument("--archive", type=Path, required=True)
    snapshot_verify.add_argument("--json", action="store_true")
    remote = subparsers.add_parser("mcp-http", help="Serve a snapshot over authenticated Streamable HTTP")
    remote.add_argument("--snapshot-archive", type=Path, required=True)
    remote.add_argument("--repository-id", required=True)
    remote.add_argument("--issuer", default=os.environ.get("AGENTRAMEN_OIDC_ISSUER"))
    remote.add_argument("--jwks-url", default=os.environ.get("AGENTRAMEN_OIDC_JWKS_URL"))
    remote.add_argument("--audience", default=os.environ.get("AGENTRAMEN_OIDC_AUDIENCE"))
    remote.add_argument("--resource-url", default=os.environ.get("AGENTRAMEN_OIDC_RESOURCE_URL"))
    remote.add_argument(
        "--required-scope", default=os.environ.get("AGENTRAMEN_OIDC_REQUIRED_SCOPE", "agentramen:read")
    )
    remote.add_argument("--allowed-group", default=os.environ.get("AGENTRAMEN_OIDC_ALLOWED_GROUP"))
    remote.add_argument(
        "--group-claim", default=os.environ.get("AGENTRAMEN_OIDC_GROUP_CLAIM", "groups")
    )
    remote.add_argument(
        "--allowed-host",
        action="append",
        default=[item for item in os.environ.get("AGENTRAMEN_MCP_ALLOWED_HOSTS", "").split(",") if item],
    )
    remote.add_argument(
        "--allowed-origin",
        action="append",
        default=[item for item in os.environ.get("AGENTRAMEN_MCP_ALLOWED_ORIGINS", "").split(",") if item],
    )
    remote.add_argument("--host", default="127.0.0.1")
    remote.add_argument("--port", type=int, default=8000)
    serve = subparsers.add_parser("serve", help="Start the local versioned HTTP API")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8765)
    args = parser.parse_args(argv)
    try:
        root = None if args.command == "mcp-http" else find_root()
        if args.command == "mcp-http":
            required = {
                "issuer": args.issuer,
                "jwks URL": args.jwks_url,
                "audience": args.audience,
                "resource URL": args.resource_url,
                "allowed host": args.allowed_host,
            }
            missing = [name for name, value in required.items() if not value]
            if missing:
                raise AgentRamenError(
                    "mcp-http requires OIDC settings and an allowed Host: " + ", ".join(missing)
                )
            try:
                from .remote_mcp import run_remote_server
            except ImportError as exc:
                raise AgentRamenError(
                    "Central MCP requires the optional dependencies; install agentramen[central]."
                ) from exc
            with mounted_snapshot(args.snapshot_archive, args.repository_id) as (snapshot_root, manifest):
                run_remote_server(
                    snapshot_root,
                    repository_id=args.repository_id,
                    snapshot_commit=str(manifest["commit"]),
                    issuer=args.issuer,
                    jwks_url=args.jwks_url,
                    audience=args.audience,
                    resource_url=args.resource_url,
                    allowed_hosts=args.allowed_host,
                    required_scope=args.required_scope,
                    allowed_group=args.allowed_group,
                    group_claim=args.group_claim,
                    allowed_origins=args.allowed_origin,
                    host=args.host,
                    port=args.port,
                )
            return 0
        assert root is not None
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
            value = graph_at(root, args.revision) if args.revision else graph_export(root)
        elif args.command == "diff":
            value = _diff(root, args.commit1, args.commit2)
        elif args.command == "changes":
            value = recent_changes(root, args.limit)
        elif args.command == "pr":
            value = pull_request_summary(root, args.base)
        elif args.command == "benchmark":
            modes = (False, True) if args.semantic_mode == "both" else (
                args.semantic_mode == "on",
            )
            value = run_comparisons(args.files, args.repository, modes)
            if args.quality:
                value.append(run_quality_benchmark())
        elif args.command == "memory":
            if args.memory_action == "publish":
                value = publish_shared_memory(root, args.memory_id)
            elif args.memory_action == "search":
                value = search_shared_memories(root, args.query, args.limit)
            else:
                value = memory_audit(root)
        elif args.command == "snapshot":
            if args.snapshot_action == "export":
                value = export_snapshot(root, args.repository_id, args.output)
            else:
                value = verify_snapshot(args.archive, args.repository_id)
        elif args.command == "serve":
            from .server import serve as serve_api

            serve_api(root, args.host, args.port)
            return 0
        else:
            _mcp(root)
            return 0
        _output(value, args.json)
        return 0
    except (AgentRamenError, OSError, ValueError) as exc:
        print(f"agentramen: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
