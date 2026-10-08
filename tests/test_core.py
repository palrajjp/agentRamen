import subprocess
import json
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
from pathlib import Path
from urllib.request import Request, urlopen

from gitgraph.cli import _init
from gitgraph.core import (
    architecture,
    connect,
    context_for,
    dependency_impact,
    explain_file,
    GitGraphError,
    graph_export,
    graph_at,
    hotspots,
    index_repository,
    load_config,
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

    def test_search_terms_and_document_frequencies_update_incrementally(self):
        (self.root / "auth_service.py").write_text(
            "class AuthService:\n    pass\n", encoding="utf-8"
        )
        (self.root / "billing_service.py").write_text(
            "class BillingService:\n    pass\n", encoding="utf-8"
        )
        self.commit("add services")
        index_repository(self.root)
        conn = connect(self.root)
        self.assertEqual(
            conn.execute(
                "SELECT document_frequency FROM term_document_frequency WHERE term='service'"
            ).fetchone()[0],
            2,
        )
        initial_terms = conn.execute(
            "SELECT COUNT(*) FROM file_search_terms"
        ).fetchone()[0]
        conn.close()

        (self.root / "billing_service.py").unlink()
        (self.root / "auth_service.py").write_text(
            "class AuthService:\n    def login(self): pass\n", encoding="utf-8"
        )
        self.commit("remove billing and update auth")
        index_repository(self.root)

        conn = connect(self.root)
        self.assertEqual(
            conn.execute(
                "SELECT document_frequency FROM term_document_frequency WHERE term='service'"
            ).fetchone()[0],
            1,
        )
        self.assertEqual(
            conn.execute(
                "SELECT COUNT(*) FROM file_search_terms WHERE path='billing_service.py'"
            ).fetchone()[0],
            0,
        )
        self.assertGreater(
            conn.execute(
                "SELECT COUNT(*) FROM file_search_terms WHERE path='auth_service.py'"
            ).fetchone()[0],
            initial_terms // 2,
        )
        conn.close()

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

        self.assertEqual(repository_status(self.root)["files"], 1)
        self.assertEqual(repo_search(self.root, "Hidden"), [])

    def test_configuration_controls_history_retention_and_cochanges(self):
        config_path = self.root / ".gitgraph.yml"
        source = self.root / "module.py"
        for index in range(3):
            source.write_text(f"class Module{index}:\n    pass\n", encoding="utf-8")
            self.commit(f"module change {index}")
        config_path.write_text(
            "git:\n  history: true\n  co_changes: false\n"
            "history:\n  commits: 2\n"
            "context:\n  default_budget: 400\n",
            encoding="utf-8",
        )

        index_repository(self.root)
        self.assertEqual(repository_status(self.root)["commits"], 2)
        self.assertEqual(
            context_for(self.root, "Module2")["token_budget"],
            400,
        )
        conn = connect(self.root)
        self.assertEqual(
            conn.execute(
                "SELECT COUNT(*) FROM graph_edges WHERE type='CO_CHANGED_WITH'"
            ).fetchone()[0],
            0,
        )
        conn.close()

        config_path.write_text(
            "git:\n  history: true\n  co_changes: true\n"
            "history:\n  commits: 1\n",
            encoding="utf-8",
        )
        index_repository(self.root)
        self.assertEqual(repository_status(self.root)["commits"], 1)

        config_path.write_text("git:\n  history: false\n", encoding="utf-8")
        index_repository(self.root)
        self.assertEqual(repository_status(self.root)["commits"], 0)
        self.assertFalse(load_config(self.root).history_enabled)

        config_path.write_text(
            "git:\n  history: true\n  co_changes: true\nhistory:\n  commits: 2\n",
            encoding="utf-8",
        )
        index_repository(self.root)
        self.assertEqual(repository_status(self.root)["commits"], 2)

    def test_non_incremental_configuration_reparses_all_files(self):
        (self.root / "one.py").write_text("class One:\n    pass\n", encoding="utf-8")
        (self.root / "two.py").write_text("class Two:\n    pass\n", encoding="utf-8")
        self.commit("add files")
        config = self.root / ".gitgraph.yml"
        config.write_text("indexing:\n  incremental: false\n", encoding="utf-8")
        index_repository(self.root)

        result = index_repository(self.root)

        self.assertEqual(result["indexed"], 2)

    def test_invalid_context_budget_in_config_is_reported(self):
        (self.root / ".gitgraph.yml").write_text(
            "context:\n  default_budget: 10\n", encoding="utf-8"
        )
        with self.assertRaisesRegex(GitGraphError, "context.default_budget"):
            load_config(self.root)

    def test_local_http_api_exposes_versioned_context(self):
        from gitgraph.server import create_server

        (self.root / ".gitgraph.yml").write_text(
            "context:\n  default_budget: 500\n", encoding="utf-8"
        )
        (self.root / "auth.py").write_text("class AuthService:\n    pass\n", encoding="utf-8")
        self.commit("add auth")
        index_repository(self.root)
        server = create_server(self.root, "127.0.0.1", 0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            address = f"http://127.0.0.1:{server.server_port}"
            with urlopen(address + "/") as response:
                page = response.read().decode("utf-8")
            with urlopen(address + "/api/v1/architecture") as response:
                architecture_value = json.load(response)
            revision = subprocess.check_output(
                ["git", "-C", str(self.root), "rev-parse", "HEAD"], text=True
            ).strip()
            with urlopen(address + f"/api/v1/graph?at={revision}") as response:
                snapshot = json.load(response)
            request = Request(
                address + "/api/v1/context",
                data=json.dumps({"task": "AuthService"}).encode(),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urlopen(request) as response:
                context_value = json.load(response)
            override_request = Request(
                address + "/api/v1/context",
                data=json.dumps({"task": "AuthService", "token_budget": 300}).encode(),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urlopen(override_request) as response:
                override_value = json.load(response)
            self.assertEqual(architecture_value["files"], 1)
            self.assertIn("Graph snapshot", page)
            self.assertEqual(snapshot["revision"], revision)
            self.assertEqual(context_value["files"][0]["path"], "auth.py")
            self.assertEqual(context_value["token_budget"], 500)
            self.assertEqual(override_value["token_budget"], 300)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_init_creates_yaml_configuration_and_workflow(self):
        result = _init(self.root)

        config = (self.root / result["config"]).read_text(encoding="utf-8")
        workflow = (self.root / result["workflow"]).read_text(encoding="utf-8")
        self.assertIn("version: 1", config)
        self.assertIn("commits: 100", config)
        self.assertIn("uses: palrajjp/gitGraph/.github/workflows/index.yml@main", workflow)

    def test_mcp_exposes_context_tool(self):
        (self.root / ".gitgraph.yml").write_text(
            "context:\n  default_budget: 350\n", encoding="utf-8"
        )
        (self.root / "auth.py").write_text("class AuthService:\n    pass\n", encoding="utf-8")
        self.commit("add auth")
        requests = [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {"name": "repo_context", "arguments": {"task": "AuthService"}},
            },
        ]
        import_code = (
            "import sys; "
            f"sys.path.insert(0, {str(Path(__file__).resolve().parents[1])!r}); "
            "from gitgraph.cli import main; "
            "raise SystemExit(main(['mcp']))"
        )
        response = subprocess.run(
            [sys.executable, "-c", import_code],
            cwd=self.root,
            input="".join(json.dumps(item) + "\n" for item in requests),
            text=True,
            capture_output=True,
            check=True,
        )
        results = [json.loads(line) for line in response.stdout.splitlines()]
        tools = [tool["name"] for tool in results[-2]["result"]["tools"]]
        self.assertIn("repo_context", tools)
        self.assertIn("repo_graph", tools)
        context = results[-1]["result"]["structuredContent"]
        self.assertEqual(context["token_budget"], 350)

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

    def test_indexed_context_ranking_matches_full_scan_reference(self):
        from collections import Counter

        from gitgraph.core import _terms

        (self.root / "auth_service.py").write_text(
            "class AuthService:\n    def login(self): pass\n", encoding="utf-8"
        )
        (self.root / "views.py").write_text(
            "from auth_service import AuthService\n"
            "class LoginView:\n    def render(self): return AuthService()\n",
            encoding="utf-8",
        )
        (self.root / "billing.py").write_text(
            "class BillingService:\n    def invoice(self): pass\n", encoding="utf-8"
        )
        self.commit("add auth views and billing")
        index_repository(self.root)
        (self.root / "auth_service.py").write_text(
            "class AuthService:\n    def login(self): return True\n", encoding="utf-8"
        )
        self.commit("update authentication")
        index_repository(self.root)

        conn = connect(self.root)
        rows = conn.execute(
            "SELECT path, language, bytes, symbols, imports, digest FROM files"
        ).fetchall()
        history_rows = conn.execute(
            "SELECT cf.path, c.subject, c.hash FROM commit_files cf "
            "JOIN commits c ON c.hash=cf.commit_hash ORDER BY c.committed_at DESC"
        ).fetchall()
        edge_rows = conn.execute(
            "SELECT source, target, type, weight FROM graph_edges "
            "WHERE type IN ('DEPENDS_ON', 'CO_CHANGED_WITH')"
        ).fetchall()
        conn.close()

        task_terms = _terms("auth login")
        frequencies = Counter(
            term for path, *_ in rows for term in _terms(path.replace("/", " "))
        )
        history = {}
        for path, subject, commit_hash in history_rows:
            history.setdefault(path, []).append((subject, commit_hash))
        relations = {}
        for source, target, kind, weight in edge_rows:
            relations.setdefault(source.removeprefix("file:"), []).append(
                (target.removeprefix("file:"), kind, weight)
            )
        lexical = set()
        ranked = []
        metadata = {}
        for path, language, size, raw_symbols, raw_imports, _digest in rows:
            symbols = json.loads(raw_symbols)
            imports = json.loads(raw_imports)
            metadata[path] = (language, size, symbols, imports)
            overlap = (
                _terms(path.replace("/", " "))
                | _terms(" ".join(symbols + imports))
            ) & task_terms
            if overlap:
                lexical.add(path)
                score = sum(1 / max(1, frequencies[word]) for word in overlap)
                score += min(len(history.get(path, [])), 5) * 0.08
                ranked.append((score, path, language, size, symbols, imports))
        ranked_paths = {item[1] for item in ranked}
        for source, related in relations.items():
            if source in ranked_paths or source not in metadata:
                continue
            matches = [item for item in related if item[0] in lexical]
            if matches:
                language, size, symbols, imports = metadata[source]
                score = max(
                    0.18 + min(weight, 3) * 0.04 + (0.12 if kind == "DEPENDS_ON" else 0)
                    for _target, kind, weight in matches
                )
                ranked.append((score, source, language, size, symbols, imports))
        ranked.sort(key=lambda item: (-item[0], item[1]))

        actual = context_for(self.root, "auth login", 10_000)["files"]
        self.assertEqual(
            [(item["path"], item["score"]) for item in actual],
            [(path, round(score, 3)) for score, path, *_ in ranked],
        )

    def test_tree_sitter_analyzer_falls_back_when_optional_package_is_missing(self):
        from gitgraph.analyzers import TreeSitterAnalyzer

        with patch("gitgraph.analyzers.importlib.import_module", side_effect=ImportError):
            result = TreeSitterAnalyzer("javascript").analyze(
                "auth.js", "export class AuthService {}\n"
            )
        self.assertIn("AuthService", result.symbols)

    def test_tree_sitter_extracts_symbols_and_calls_when_installed(self):
        try:
            import tree_sitter_language_pack
        except ImportError:
            self.skipTest("Tree-sitter optional dependency is not installed")
        from gitgraph.analyzers import analyze

        result = analyze(
            "auth.js",
            "export class AuthService {}\nfunction login() { send(); }",
            "javascript",
        )
        self.assertIn("AuthService", result.symbols)
        self.assertIn("login", result.symbols)
        self.assertIn("send", result.calls)

    def test_semantic_embeddings_can_retrieve_without_lexical_overlap(self):
        (self.root / ".gitgraph.yml").write_text(
            "semantic:\n  enabled: true\n  model: test-model\n", encoding="utf-8"
        )
        (self.root / "access.py").write_text(
            "class LoginManager:\n    pass\n", encoding="utf-8"
        )
        self.commit("add access manager")

        with patch("gitgraph.core._embed_texts", side_effect=lambda _model, texts: [[1.0, 0.0] for _ in texts]):
            index_repository(self.root)
            context = context_for(self.root, "How can a visitor enter?", 500)

        self.assertEqual(context["files"][0]["path"], "access.py")

    def test_graph_at_builds_distinct_commit_addressable_snapshots(self):
        (self.root / "auth.py").write_text("class Auth:\n    pass\n", encoding="utf-8")
        (self.root / "views.py").write_text(
            "from auth import Auth\n\ndef view():\n    return Auth()\n", encoding="utf-8"
        )
        self.commit("add initial modules")
        first = subprocess.check_output(
            ["git", "-C", str(self.root), "rev-parse", "HEAD"], text=True
        ).strip()
        index_repository(self.root)

        (self.root / "views.py").unlink()
        (self.root / "auth.py").write_text("class Identity:\n    pass\n", encoding="utf-8")
        self.commit("replace modules")
        second = subprocess.check_output(
            ["git", "-C", str(self.root), "rev-parse", "HEAD"], text=True
        ).strip()

        first_graph = graph_at(self.root, first)
        second_graph = graph_at(self.root, second)
        self.assertEqual(first_graph["revision"], first)
        self.assertTrue(
            any(edge["type"] == "DEPENDS_ON" for edge in first_graph["edges"])
        )
        self.assertFalse(
            any(edge["type"] == "DEPENDS_ON" for edge in second_graph["edges"])
        )
        self.assertTrue(
            any(node["name"] == "Auth" for node in first_graph["nodes"])
        )
        self.assertTrue(
            any(node["name"] == "Identity" for node in second_graph["nodes"])
        )
        self.assertTrue(any(node["type"] == "Commit" for node in second_graph["nodes"]))
        self.assertTrue(
            any(edge["type"] == "CHANGED_IN" for edge in second_graph["edges"])
        )
        self.assertTrue(
            any(edge["type"] == "CO_CHANGED_WITH" for edge in second_graph["edges"])
        )

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
