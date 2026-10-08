"""Local-only, versioned HTTP API for GitGraph."""

from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from .core import (
    GitGraphError,
    architecture,
    context_for,
    dependency_impact,
    explain_file,
    file_history,
    graph_export,
    hotspots,
    repo_search,
    repository_status,
)


def create_server(root: Path, host: str = "127.0.0.1", port: int = 8765):
    class Handler(BaseHTTPRequestHandler):
        server_version = "GitGraph/0.1"

        def _respond(self, status: int, value: object) -> None:
            payload = json.dumps(value, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(payload)

        def _query(self):
            parsed = urlparse(self.path)
            return parsed.path, parse_qs(parsed.query)

        def do_GET(self) -> None:
            path, query = self._query()
            try:
                if path == "/health":
                    value = {"status": "ok"}
                elif path == "/api/v1/repository":
                    value = repository_status(root)
                elif path == "/api/v1/architecture":
                    value = architecture(root)
                elif path == "/api/v1/graph":
                    value = graph_export(root)
                elif path.startswith("/api/v1/files/"):
                    file_path = unquote(path.removeprefix("/api/v1/files/"))
                    value = explain_file(root, file_path)
                elif path == "/api/v1/impact":
                    value = dependency_impact(root, query.get("path", [""])[0])
                elif path == "/api/v1/history":
                    value = file_history(root, query.get("path", [""])[0])
                elif path == "/api/v1/hotspots":
                    value = hotspots(root)
                else:
                    self._respond(404, {"error": "Not found"})
                    return
                self._respond(200, value)
            except GitGraphError as exc:
                self._respond(404, {"error": str(exc)})
            except (OSError, ValueError) as exc:
                self._respond(400, {"error": str(exc)})

        def do_POST(self) -> None:
            path, _ = self._query()
            try:
                content_length = int(self.headers.get("Content-Length", "0"))
                if content_length < 0 or content_length > 1_000_000:
                    self._respond(413, {"error": "Request body exceeds 1 MB"})
                    return
                request = json.loads(self.rfile.read(content_length) or b"{}")
                if not isinstance(request, dict):
                    raise ValueError("Request body must be a JSON object")
                if path == "/api/v1/context":
                    value = context_for(
                        root,
                        str(request.get("task", "")),
                        int(request["token_budget"])
                        if request.get("token_budget") is not None
                        else None,
                    )
                elif path == "/api/v1/search":
                    value = repo_search(
                        root, str(request.get("query", "")), int(request.get("limit", 20))
                    )
                else:
                    self._respond(404, {"error": "Not found"})
                    return
                self._respond(200, value)
            except (GitGraphError, ValueError, TypeError, json.JSONDecodeError) as exc:
                self._respond(400, {"error": str(exc)})

        def log_message(self, fmt, *args):
            return

    return ThreadingHTTPServer((host, port), Handler)


def serve(root: Path, host: str = "127.0.0.1", port: int = 8765) -> None:
    server = create_server(root, host, port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
