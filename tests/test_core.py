import subprocess
import json
import sys
import tempfile
import unittest
from pathlib import Path

from gitgraph.cli import _init
from gitgraph.core import (
    architecture,
    connect,
    context_for,
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
        self.assertFalse((self.root / ".gitgraph" / "graph.db").stat().st_size == 0)

    def test_update_indexes_only_changed_file_and_removes_deleted_file(self):
        (self.root / "auth.py").write_text("class Auth:\n    pass\n", encoding="utf-8")
        (self.root / "billing.py").write_text("class Billing:\n    pass\n", encoding="utf-8")
        self.commit("add modules")
        initial = index_repository(self.root)
        self.assertEqual(initial["indexed"], 2)

        (self.root / "auth.py").write_text("class Auth:\n    def login(self): pass\n", encoding="utf-8")
        (self.root / "billing.py").unlink()
        self.commit("update auth and remove billing")
        updated = index_repository(self.root)

        self.assertEqual(updated["indexed"], 1)
        self.assertEqual(updated["removed"], 1)
        self.assertEqual(repository_status(self.root)["files"], 1)

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
        self.commit("add auth and views")
        index_repository(self.root)

        graph = graph_export(self.root)
        edge_types = {edge["type"] for edge in graph["edges"]}
        self.assertIn("DEPENDS_ON", edge_types)
        self.assertIn("CALLS", edge_types)
        self.assertIn("CHANGED_IN", edge_types)
        self.assertIn("CO_CHANGED_WITH", edge_types)
        self.assertEqual(explain_file(self.root, "AuthService")["path"], "auth.py")
        context = context_for(self.root, "AuthService views")
        self.assertTrue(context["files"])

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
