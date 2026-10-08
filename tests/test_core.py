import subprocess
import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from urllib.request import Request, urlopen

from gitgraph.cli import _init
from gitgraph.core import (
    architecture,
    connect,
    context_for,
    dependency_impact,
    explain_file,
    graph_export,
    hotspots,
    index_repository,
    repo_search,
    repository_status,
)


class GitGraphIndexTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        subprocess.run(["git", "init", "-q", str(self.root)], check=True)
        subprocess.run(
            ["git", "-C", str(self.root), "config", "user.email", "test@example.invalid"],
            check=True,
        )
        subprocess.run(
            ["git", "-C", str(self.root), "config", "user.name", "Test User"],
            check=True,
        )

    def tearDown(self):
        self.temp_dir.cleanup()

    def commit(self, message):
        subprocess.run(["git", "-C", str(self.root), "add", "-A"], check=True)
        subprocess.run(["git", "-C", str(self.root), "commit", "-qm", message], check=True)

    def test_initial_index_and_context(self):
        (self.root / "src").mkdir()
        (self.root / "src" / "auth_service.py").write_text(
            "class AuthService:\n    def login(self):\n        return True\n",
            encoding="utf-8",
        )
        (self.root / ".env").write_text("SECRET=value\n", encoding="utf-8")
        self.commit("add auth service")

        result = index_repository(self.root)
        context = context_for(self.root, "auth login")

        self.assertEqual(result["indexed"], 1)
        self.assertEqual(repository_status(self.root)["files"], 1)
        self.assertEqual(context["files"][0]["path"], "src/auth_service.py")
        self.assertIn("class AuthService", context["files"][0]["excerpt"])
        self.assertLessEqual(context["estimated_tokens"], 2000)
        self.assertFalse((self.root / ".gitgraph" / "graph.db").stat().st_size == 0)

    def test_update_indexes_only_changed_file_and_removes_deleted_file(self):
        (self.root / "auth.py").write_text("class Auth:\n    pass\n", encoding="utf-8")
        (self.root / "billing.py").write_text("class Billing:\n    pass\n", encoding="utf-8")
        self.commit("add modules")
        initial = index_repository(self.root)
        self.assertEqual(initial["indexed"], 2)
        self.assertEqual(index_repository(self.root)["indexed"], 0)

        (self.root / "auth.py").write_text("class Auth:\n    def login(self): pass\n", encoding="utf-8")
        (self.root / "billing.py").unlink()
        self.commit("update auth and remove billing")
        updated = index_repository(self.root)

        self.assertEqual(updated["indexed"], 1)
        self.assertEqual(updated["removed"], 1)
        self.assertEqual(repository_status(self.root)["files"], 1)

    def test_reindex_after_history_rewind(self):
        (self.root / "old.py").write_text("class Old:\n    pass\n", encoding="utf-8")
        self.commit("add old")
        index_repository(self.root)
        (self.root / "new.py").write_text("class New:\n    pass\n", encoding="utf-8")
        self.commit("add new")
        index_repository(self.root)

        subprocess.run(
            ["git", "-C", str(self.root), "reset", "--hard", "HEAD~1"],
            check=True,
            stdout=subprocess.DEVNULL,
        )
        result = index_repository(self.root)

        self.assertEqual(result["files"], 1)
        self.assertEqual(repository_status(self.root)["files"], 1)
        nodes = graph_export(self.root)["nodes"]
        self.assertFalse(any(node["path"] == "new.py" for node in nodes))

    def test_secret_content_is_removed_from_graph_and_context(self):
        source = self.root / "credentials.py"
        source.write_text("class Credentials:\n    pass\n", encoding="utf-8")
        self.commit("add credentials module")
        index_repository(self.root)
        source.write_text(
            "class Credentials:\n    pass\n"
            "api_key = 'this-is-a-long-sensitive-token-value'\n",
            encoding="utf-8",
        )

        index_repository(self.root)

        self.assertEqual(repository_status(self.root)["files"], 0)
        self.assertFalse(
            any(
                node["type"] == "Symbol" and node["path"] == "credentials.py"
                for node in graph_export(self.root)["nodes"]
            )
        )
        self.assertEqual(context_for(self.root, "Credentials")["files"], [])

    def test_configured_ignore_patterns_are_applied(self):
        (self.root / ".gitgraph.yml").write_text("ignore:\n  - private/\n", encoding="utf-8")
        (self.root / "private").mkdir()
        (self.root / "private" / "module.py").write_text("class Hidden:\n    pass\n", encoding="utf-8")
        (self.root / "visible.py").write_text("class Visible:\n    pass\n", encoding="utf-8")
        self.commit("add ignored and visible files")

        index_repository(self.root)

        self.assertEqual(repository_status(self.root)["files"], 2)
        self.assertEqual(repo_search(self.root, "Hidden"), [])

    def test_local_http_api_exposes_versioned_context(self):
        from gitgraph.server import create_server

        (self.root / "auth.py").write_text("class AuthService:\n    pass\n", encoding="utf-8")
        self.commit("add auth")
        index_repository(self.root)
        server = create_server(self.root, "127.0.0.1", 0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            address = f"http://127.0.0.1:{server.server_port}"
            with urlopen(address + "/api/v1/architecture") as response:
                architecture_value = json.load(response)
            request = Request(
                address + "/api/v1/context",
                data=json.dumps({"task": "AuthService", "token_budget": 500}).encode(),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urlopen(request) as response:
                context_value = json.load(response)
            self.assertEqual(architecture_value["files"], 1)
            self.assertEqual(context_value["files"][0]["path"], "auth.py")
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_init_creates_yaml_configuration_and_workflow(self):
        result = _init(self.root)

        config = (self.root / result["config"]).read_text(encoding="utf-8")
        workflow = (self.root / result["workflow"]).read_text(encoding="utf-8")
        self.assertIn("version: 1", config)
        self.assertIn("uses: palrajjp/gitGraph/.github/workflows/index.yml@main", workflow)

    def test_mcp_exposes_context_tool(self):
        requests = [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        ]
        response = subprocess.run(
            [sys.executable, "-m", "gitgraph.cli", "mcp"],
            cwd=self.root,
            input="".join(json.dumps(item) + "\n" for item in requests),
            text=True,
            capture_output=True,
            check=True,
        )
        results = [json.loads(line) for line in response.stdout.splitlines()]
        tools = [tool["name"] for tool in results[-1]["result"]["tools"]]
        self.assertIn("repo_context", tools)
        self.assertIn("repo_graph", tools)

    def test_graph_tracks_imports_calls_and_history(self):
        (self.root / "auth.py").write_text(
            "class AuthService:\n    def login(self):\n        return True\n",
            encoding="utf-8",
        )
        (self.root / "views.py").write_text(
            "from auth import AuthService\n\n"
            "def login_view():\n    return AuthService().login()\n",
            encoding="utf-8",
        )
        (self.root / "dashboard.py").write_text(
            "from views import login_view\n\n"
            "def dashboard():\n    return login_view()\n",
            encoding="utf-8",
        )
        self.commit("add auth and views")
        index_repository(self.root)

        graph = graph_export(self.root)
        edge_types = {edge["type"] for edge in graph["edges"]}
        self.assertIn("DEPENDS_ON", edge_types)
        self.assertIn("CALLS", edge_types)
        self.assertIn("CHANGED_IN", edge_types)
        self.assertIn("CO_CHANGED_WITH", edge_types)
        self.assertEqual(explain_file(self.root, "AuthService")["path"], "auth.py")
        impact = dependency_impact(self.root, "auth.py")
        self.assertEqual(impact["direct_dependents"], ["views.py"])
        self.assertEqual(impact["indirect_dependents"], ["dashboard.py"])
        context = context_for(self.root, "AuthService views")
        self.assertTrue(context["files"])

    def test_incremental_update_resolves_new_target_and_removes_deleted_target(self):
        (self.root / "views.py").write_text(
            "from auth import AuthService\n\n"
            "def view():\n    return AuthService()\n",
            encoding="utf-8",
        )
        self.commit("add importer first")
        index_repository(self.root)
        (self.root / "auth.py").write_text("class AuthService:\n    pass\n", encoding="utf-8")
        self.commit("add imported module")
        index_repository(self.root)

        self.assertEqual(explain_file(self.root, "views.py")["dependencies"], ["auth.py"])
        (self.root / "auth.py").unlink()
        self.commit("remove imported module")
        index_repository(self.root)
        self.assertEqual(explain_file(self.root, "views.py")["dependencies"], [])

    def test_rename_is_stored_and_file_analysis_commands_work(self):
        (self.root / "old_name.py").write_text("def search_accounts(): pass\n", encoding="utf-8")
        self.commit("add account search")
        index_repository(self.root)
        subprocess.run(
            ["git", "-C", str(self.root), "mv", "old_name.py", "account_search.py"],
            check=True,
        )
        subprocess.run(
            ["git", "-C", str(self.root), "commit", "-qm", "rename account search"],
            check=True,
        )
        index_repository(self.root)

        conn = connect(self.root)
        rename = conn.execute("SELECT old_path, new_path FROM renames").fetchone()
        conn.close()
        self.assertEqual(rename, ("old_name.py", "account_search.py"))
        self.assertEqual(repo_search(self.root, "search accounts")[0]["path"], "account_search.py")
        self.assertEqual(architecture(self.root)["files"], 1)
        self.assertTrue(hotspots(self.root))


if __name__ == "__main__":
    unittest.main()
