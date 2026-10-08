"""Local-only, versioned HTTP API for GitGraph."""

from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from .core import (
    GitGraphError,
    architecture,
    architecture_at,
    context_for,
    dependency_impact,
    explain_file,
    file_history,
    graph_at,
    graph_export,
    hotspots,
    repo_search,
    repository_status,
)

WEB_UI = """<!doctype html>
<html lang="en">
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>GitGraph</title>
<style>
body{font:16px system-ui,sans-serif;max-width:1000px;margin:2rem auto;padding:0 1rem;color:#182230}
form,section{border:1px solid #ccd3dc;border-radius:8px;padding:1rem;margin:1rem 0}
input,textarea,button{font:inherit;padding:.55rem;margin:.25rem}
input,textarea{max-width:95%;width:36rem}textarea{height:5rem}
button{cursor:pointer}pre{white-space:pre-wrap;overflow-wrap:anywhere;background:#f4f6f8;padding:1rem;border-radius:6px}
</style>
<h1>GitGraph</h1>
<p>Local repository graph and task context</p>
<section><h2>Repository</h2><button id="status">Refresh status</button>
<pre id="status-result">Loading…</pre></section>
<section><h2>Search</h2><form id="search-form"><input id="query" placeholder="Search files and symbols" required>
<button>Search</button></form><pre id="search-result"></pre></section>
<section><h2>Task context</h2><form id="context-form"><textarea id="task" placeholder="Describe a coding task" required></textarea>
<br><input id="budget" type="number" min="100" placeholder="Token budget (optional)">
<button>Get context</button></form><pre id="context-result"></pre></section>
<section><h2>Graph snapshot</h2><form id="graph-form"><input id="revision" placeholder="Commit or ref (blank = indexed graph)">
<button>Load graph</button></form><pre id="graph-result"></pre></section>
<script>
const show=(id,value)=>document.getElementById(id).textContent=JSON.stringify(value,null,2);
async function api(url,options){const response=await fetch(url,options);const value=await response.json();if(!response.ok)throw Error(value.error||response.statusText);return value}
document.getElementById('status').onclick=async()=>{try{show('status-result',await api('/api/v1/repository'))}catch(e){show('status-result',{error:e.message})}};
document.getElementById('search-form').onsubmit=async e=>{e.preventDefault();try{show('search-result',await api('/api/v1/search',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({query:document.getElementById('query').value})}))}catch(err){show('search-result',{error:err.message})}};
document.getElementById('context-form').onsubmit=async e=>{e.preventDefault();const payload={task:document.getElementById('task').value};const budget=document.getElementById('budget').value;if(budget)payload.token_budget=Number(budget);try{show('context-result',await api('/api/v1/context',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)}))}catch(err){show('context-result',{error:err.message})}};
document.getElementById('graph-form').onsubmit=async e=>{e.preventDefault();const revision=document.getElementById('revision').value;try{show('graph-result',await api('/api/v1/graph'+(revision?'?at='+encodeURIComponent(revision):'')))}catch(err){show('graph-result',{error:err.message})}};
document.getElementById('status').click();
</script>
</html>"""


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
                if path in ("/", "/ui"):
                    payload = WEB_UI.encode("utf-8")
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.send_header("Content-Length", str(len(payload)))
                    self.send_header("Cache-Control", "no-store")
                    self.send_header(
                        "Content-Security-Policy",
                        "default-src 'self'; script-src 'self' 'unsafe-inline'; "
                        "style-src 'self' 'unsafe-inline'",
                    )
                    self.end_headers()
                    self.wfile.write(payload)
                    return
                elif path == "/health":
                    value = {"status": "ok"}
                elif path == "/api/v1/repository":
                    value = repository_status(root)
                elif path == "/api/v1/architecture":
                    revision = query.get("at", [""])[0]
                    value = architecture_at(root, revision) if revision else architecture(root)
                elif path == "/api/v1/graph":
                    revision = query.get("at", [""])[0]
                    value = graph_at(root, revision) if revision else graph_export(root)
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
