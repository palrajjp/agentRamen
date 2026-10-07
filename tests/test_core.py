import subprocess
import json
import sys
import tempfile
import unittest
from pathlib import Path

from gitgraph.cli import _init
from gitgraph.core import context_for, index_repository, repository_status


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
        request = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}) + "\n"
        response = subprocess.run(
            [sys.executable, "-m", "gitgraph.cli", "mcp"],
            cwd=self.root,
            input=request,
            text=True,
            capture_output=True,
            check=True,
        )
        result = json.loads(response.stdout)
        self.assertIn("repo_context", [tool["name"] for tool in result["result"]["tools"]])


if __name__ == "__main__":
    unittest.main()
