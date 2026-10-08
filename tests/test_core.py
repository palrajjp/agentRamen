import asyncio
import time
import subprocess
import json
import sys
import tempfile
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import patch
from pathlib import Path
from urllib.request import Request, urlopen

from agentramen.cli import _init
from agentramen.indexer.noise_filter import filter_noise
from agentramen.memory import (
    approve_memory,
    capture_memory_evidence,
    memory_audit,
    publish_shared_memory,
    reject_memory,
    review_queue,
    search_shared_memories,
    stage_memory,
)
from agentramen.core import (
    architecture,
    connect,
    context_for,
    dependency_impact,
    explain_file,
    AgentRamenError,
    graph_export,
    graph_at,
    hotspots,
    index_repository,
    load_config,
    repo_search,
    repository_status,
)


class AgentRamenIndexTests(unittest.TestCase):
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
        self.assertFalse((self.root / ".agentramen" / "graph.db").stat().st_size == 0)

    def test_schema_patch_preserves_existing_metadata(self):
        conn = connect(self.root)
        conn.execute(
            "INSERT INTO metadata(key, value) VALUES('user_value', 'keep me')"
        )
        conn.commit()
        conn.close()

        conn = connect(self.root)
        self.assertEqual(
            conn.execute(
                "SELECT value FROM metadata WHERE key='user_value'"
            ).fetchone()[0],
            "keep me",
        )
        self.assertIn(
            "valid_to",
            {row[1] for row in conn.execute("PRAGMA table_info(memory_nodes)")},
        )
        conn.close()

    def test_memory_evidence_schema_migrates_existing_database(self):
        import sqlite3

        from agentramen.models.schema_patches import apply_schema_patches

        conn = sqlite3.connect(":memory:")
        conn.execute("CREATE TABLE metadata(key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        conn.execute(
            "CREATE TABLE staged_memories ("
            "id TEXT PRIMARY KEY, subject TEXT NOT NULL, content TEXT NOT NULL, "
            "category TEXT NOT NULL DEFAULT 'context', source TEXT, intent_vector TEXT, "
            "importance_score REAL NOT NULL DEFAULT 0.5, status TEXT NOT NULL DEFAULT 'pending', "
            "created_at TEXT NOT NULL, reviewed_at TEXT, review_note TEXT)"
        )
        conn.execute(
            "CREATE TABLE memory_nodes ("
            "id TEXT PRIMARY KEY, subject TEXT NOT NULL, content TEXT NOT NULL, "
            "category TEXT NOT NULL DEFAULT 'context', source TEXT, "
            "epistemic_status TEXT NOT NULL DEFAULT 'ACTIVE' "
            "CHECK (epistemic_status IN ('ACTIVE', 'SUPERSEDED', 'VERIFIED', 'INFERRED')), "
            "valid_from TEXT NOT NULL, valid_to TEXT, importance_score REAL NOT NULL DEFAULT 0.5)"
        )
        conn.execute(
            "INSERT INTO memory_nodes(id, subject, content, epistemic_status, valid_from) "
            "VALUES ('existing', 'existing fact', 'Preserve this', 'VERIFIED', '2025-01-01')"
        )

        apply_schema_patches(conn)
        apply_schema_patches(conn)

        evidence_columns = {
            row[1] for row in conn.execute("PRAGMA table_info(memory_evidence)")
        }
        staged_columns = {
            row[1] for row in conn.execute("PRAGMA table_info(staged_memories)")
        }
        upgraded = conn.execute(
            "SELECT content, epistemic_status FROM memory_nodes WHERE id='existing'"
        ).fetchone()
        schema_version = conn.execute(
            "SELECT value FROM metadata WHERE key='memory_schema_version'"
        ).fetchone()[0]

        self.assertEqual(
            evidence_columns, {"memory_id", "path", "digest", "commit_hash"}
        )
        self.assertIn("reviewed_by", staged_columns)
        self.assertEqual(upgraded, ("Preserve this", "VERIFIED"))
        self.assertEqual(schema_version, "3")
        self.assertEqual(
            conn.execute(
                "SELECT COUNT(*) FROM sqlite_master "
                "WHERE type='table' AND name='memory_evidence'"
            ).fetchone()[0],
            1,
        )
        conn.close()

    def test_memory_review_and_temporal_supersession(self):
        first = stage_memory(
            self.root, "authentication provider", "Use AuthService", source="agent"
        )
        second = stage_memory(
            self.root, "authentication provider", "Use OAuthProvider", source="agent"
        )
        self.assertEqual(len(review_queue(self.root)), 2)
        approve_memory(self.root, first)
        approve_memory(self.root, second)
        rejected = stage_memory(self.root, "temporary rule", "Discard this")
        reject_memory(self.root, rejected, "Not a durable rule")
        self.assertEqual(len(review_queue(self.root)), 0)

        conn = connect(self.root)
        nodes = conn.execute(
            "SELECT content, epistemic_status, valid_to FROM memory_nodes "
            "WHERE subject='authentication provider' ORDER BY valid_from"
        ).fetchall()
        conn.close()
        self.assertEqual(nodes[0][0], "Use AuthService")
        self.assertEqual(nodes[0][1], "SUPERSEDED")
        self.assertIsNotNone(nodes[0][2])
        self.assertEqual(nodes[1][0], "Use OAuthProvider")
        self.assertEqual(nodes[1][1], "VERIFIED")
        self.assertIsNone(nodes[1][2])

    def test_shared_memories_publish_search_and_supersede(self):
        (self.root / ".gitignore").write_text(".agentramen/\n", encoding="utf-8")
        (self.root / "auth.py").write_text("class AuthService: pass\n", encoding="utf-8")
        self.commit("add source file")
        index_repository(self.root)
        evidence = capture_memory_evidence(self.root, ["auth.py"])

        first = stage_memory(
            self.root, "authentication provider", "Use AuthService", source="team note",
            evidence=evidence,
        )
        approve_memory(self.root, first)
        published = publish_shared_memory(self.root, first)
        self.assertEqual(published["status"], "published")
        self.assertEqual(published["path"], f".agentramen-shared/memories/{first}.json")
        self.assertEqual(publish_shared_memory(self.root, first)["status"], "already_published")
        self.assertEqual(search_shared_memories(self.root, "authentication provider")[0]["id"], first)

        second = stage_memory(
            self.root, "authentication provider", "Use OAuthProvider", source="team note",
            evidence=evidence,
        )
        approve_memory(self.root, second)
        updated = publish_shared_memory(self.root, second)

        self.assertEqual(updated["superseded_ids"], [first])
        first_file = self.root / ".agentramen-shared" / "memories" / f"{first}.json"
        self.assertEqual(json.loads(first_file.read_text(encoding="utf-8"))["epistemic_status"], "SUPERSEDED")
        results = search_shared_memories(self.root, "OAuth provider")
        self.assertEqual([item["id"] for item in results], [second])
        context = context_for(self.root, "OAuthProvider authentication service", 1000)
        self.assertEqual(context["team_memories"][0]["id"], second)
        self.assertEqual(context["team_memories"][0]["source"], "team note")
        self.assertEqual(context["team_memories"][0]["epistemic_status"], "VERIFIED")
        self.assertEqual(context["team_memories"][0]["approved_by"], "Test User")
        self.assertIsNotNone(context["team_memories"][0]["valid_from"])
        self.assertIsNone(context["team_memories"][0]["valid_to"])
        self.assertLessEqual(context["estimated_tokens"], context["token_budget"])
        self.assertEqual(index_repository(self.root)["files"], 2)
        graph_paths = {
            node["path"] for node in graph_export(self.root)["nodes"] if node["type"] == "File"
        }
        self.assertEqual(graph_paths, {".gitignore", "auth.py"})
        self.commit("share approved team memory")
        with tempfile.TemporaryDirectory() as teammate_directory:
            teammate_root = Path(teammate_directory) / "checkout"
            subprocess.run(
                ["git", "clone", "-q", str(self.root), str(teammate_root)],
                check=True,
            )
            teammate_results = search_shared_memories(teammate_root, "OAuth provider")
            self.assertEqual([item["id"] for item in teammate_results], [second])

    def test_shared_memory_requires_approval_and_rejects_credentials(self):
        (self.root / "deploy.py").write_text("def deploy(): return True\n", encoding="utf-8")
        self.commit("add deployment source")
        index_repository(self.root)
        evidence = capture_memory_evidence(self.root, ["deploy.py"])
        pending = stage_memory(self.root, "deployment rule", "Use signed artifacts")
        with self.assertRaisesRegex(AgentRamenError, "approved"):
            publish_shared_memory(self.root, pending)

        credential = stage_memory(
            self.root, "deployment key", "api_key = 'Abcdefghijklmnop'", evidence=evidence
        )
        approve_memory(self.root, credential)
        with self.assertRaisesRegex(AgentRamenError, "credential-like"):
            publish_shared_memory(self.root, credential)

        source_credential = stage_memory(
            self.root,
            "deployment provenance",
            "Use the approved deployment pipeline",
            source="api_key = 'Abcdefghijklmnop'",
            evidence=evidence,
        )
        approve_memory(self.root, source_credential)
        with self.assertRaisesRegex(AgentRamenError, "credential-like"):
            publish_shared_memory(self.root, source_credential)

    def test_memory_evidence_stales_and_audit_reports_provenance(self):
        import io

        from agentramen.cli import main

        (self.root / "deploy.py").write_text("def deploy(): return True\n", encoding="utf-8")
        self.commit("add deployment source")
        index_repository(self.root)
        evidence = capture_memory_evidence(self.root, ["deploy.py"])
        memory_id = stage_memory(
            self.root,
            "deployment invariant",
            "Deploy only signed artifacts",
            evidence=evidence,
        )
        queued = review_queue(self.root)[0]
        self.assertTrue(queued["evidence_current"])
        self.assertEqual(queued["evidence"][0]["commit"], evidence[0]["commit"])
        approve_memory(self.root, memory_id)
        publish_shared_memory(self.root, memory_id)

        pending_id = stage_memory(
            self.root, "deployment check", "Verify artifact signatures", evidence=evidence
        )
        unverified_id = stage_memory(
            self.root, "legacy local note", "Review before relying on this note"
        )
        approve_memory(self.root, unverified_id)
        (self.root / "deploy.py").write_text("def deploy(): return unsigned()\n", encoding="utf-8")
        self.commit("change deployment implementation")
        index_result = index_repository(self.root)
        invalidated = next(
            item for item in index_result["stale_memories"] if item["id"] == memory_id
        )
        self.assertEqual(
            invalidated["checked_against_commit"],
            subprocess.check_output(
                ["git", "-C", str(self.root), "rev-parse", "HEAD"], text=True
            ).strip(),
        )
        audit = memory_audit(self.root)
        self.assertEqual([item["id"] for item in audit["stale_local"]], [memory_id])
        self.assertEqual(audit["stale_local"][0]["evidence"][0]["path"], "deploy.py")
        self.assertEqual(audit["stale_local"][0]["stale_paths"], ["deploy.py"])
        self.assertEqual(audit["stale_shared"][0]["id"], memory_id)
        self.assertEqual(audit["stale_shared"][0]["stale_paths"], ["deploy.py"])
        self.assertEqual(audit["unverified_local"][0]["id"], unverified_id)
        self.assertEqual(repository_status(self.root)["unverified_memories"], 1)
        self.assertEqual(search_shared_memories(self.root, "deployment invariant"), [])
        queue = review_queue(self.root)
        stale_review = next(item for item in queue if item["id"] == memory_id)
        self.assertEqual(stale_review["status"], "stale")
        self.assertEqual(stale_review["action"], "restage_with_fresh_context")
        self.assertEqual(stale_review["evidence"][0]["commit"], evidence[0]["commit"])
        self.assertFalse(stale_review["evidence_current"])
        with self.assertRaisesRegex(AgentRamenError, "evidence is stale"):
            approve_memory(self.root, pending_id)

        output = io.StringIO()
        with patch("agentramen.cli.find_root", return_value=self.root), patch(
            "sys.stdout", output
        ):
            self.assertEqual(main(["memory", "audit", "--json"]), 0)
        cli_audit = json.loads(output.getvalue())
        self.assertEqual(cli_audit["stale_count"], 1)
        self.assertEqual(cli_audit["index_commit"], invalidated["checked_against_commit"])
        self.assertIn("1 memory is suspect", cli_audit["summary"])

    def test_legacy_shared_memory_is_unverified_and_not_searchable(self):
        import uuid

        memory_id = str(uuid.uuid4())
        directory = self.root / ".agentramen-shared" / "memories"
        directory.mkdir(parents=True)
        (directory / f"{memory_id}.json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "id": memory_id,
                    "subject": "legacy deployment rule",
                    "content": "Use the old deployment path",
                    "category": "context",
                    "source": "older release",
                    "epistemic_status": "VERIFIED",
                    "valid_from": "2025-01-01T00:00:00+00:00",
                    "valid_to": None,
                    "importance_score": 0.5,
                }
            ),
            encoding="utf-8",
        )

        self.assertEqual(search_shared_memories(self.root, "legacy deployment"), [])
        audit = memory_audit(self.root)
        self.assertEqual(audit["unverified_shared"][0]["id"], memory_id)

    def test_central_snapshot_excludes_private_memory_and_includes_shared_memory(self):
        import zipfile

        from agentramen.snapshots import export_snapshot, mounted_snapshot, verify_snapshot

        (self.root / ".gitignore").write_text(".agentramen/\n", encoding="utf-8")
        (self.root / "auth.py").write_text("class AuthService: pass\n", encoding="utf-8")
        (self.root / "credentials.py").write_text(
            "api_key = 'ThisLooksLikeARealSecretValue'\n", encoding="utf-8"
        )
        self.commit("add auth and credential-like source")
        index_repository(self.root)

        private_sentinel = "AGENTRAMEN_PRIVATE_MEMORY_SENTINEL_7fc5a31d"
        private_id = stage_memory(self.root, "personal note", private_sentinel)
        approve_memory(self.root, private_id)
        evidence = capture_memory_evidence(self.root, ["auth.py"])
        shared_id = stage_memory(
            self.root, "authentication provider", "Use OAuthProvider", source="reviewed team note",
            evidence=evidence,
        )
        approve_memory(self.root, shared_id)
        publish_shared_memory(self.root, shared_id)
        self.commit("share reviewed authentication memory")
        index_repository(self.root)
        local_only_id = stage_memory(self.root, "developer note", "Do not share this")
        approve_memory(self.root, local_only_id)

        snapshot = self.root.with_name(self.root.name + "-snapshot.zip")
        self.addCleanup(snapshot.unlink, missing_ok=True)
        info = export_snapshot(self.root, "company/auth-service", snapshot)
        self.assertEqual(info["indexed_files"], 2)
        self.assertEqual(info["shared_memories"], 1)
        verification = verify_snapshot(snapshot, "company/auth-service")
        self.assertTrue(verification["valid"])
        self.assertEqual(verification["commit"], info["commit"])
        with self.assertRaisesRegex(AgentRamenError, "does not match"):
            verify_snapshot(snapshot, "company/other-repo")
        with zipfile.ZipFile(snapshot) as archive:
            graph_bytes = archive.read(".agentramen/graph.db")
        self.assertNotIn(private_sentinel.encode("utf-8"), graph_bytes)

        with mounted_snapshot(snapshot, "company/auth-service") as (snapshot_root, manifest):
            self.assertEqual(manifest["repository_id"], "company/auth-service")
            self.assertFalse((snapshot_root / "credentials.py").exists())
            self.assertEqual(search_shared_memories(snapshot_root, "OAuth provider")[0]["id"], shared_id)
            conn = connect(snapshot_root)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM staged_memories").fetchone()[0], 0)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM memory_nodes").fetchone()[0], 0)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM context_exclusion_rules").fetchone()[0], 0)
            conn.close()

    def test_remote_mcp_is_read_only_and_requires_bearer_auth(self):
        try:
            import jwt
            from agentramen.remote_mcp import OIDCVerifier, create_remote_server
            from agentramen.snapshots import export_snapshot, mounted_snapshot
            from cryptography.hazmat.primitives.asymmetric import rsa
            from mcp.server.transport_security import TransportSecuritySettings
            from starlette.testclient import TestClient
        except ImportError:
            self.skipTest("Install agentramen[central] to test remote MCP")

        (self.root / ".gitignore").write_text(".agentramen/\n", encoding="utf-8")
        (self.root / "auth.py").write_text("class AuthService: pass\n", encoding="utf-8")
        self.commit("add auth service")
        index_repository(self.root)
        snapshot = self.root.with_name(self.root.name + "-remote.zip")
        self.addCleanup(snapshot.unlink, missing_ok=True)
        export_snapshot(self.root, "company/auth-service", snapshot)
        snapshot_context = mounted_snapshot(snapshot, "company/auth-service")
        snapshot_root, manifest = snapshot_context.__enter__()
        self.addCleanup(snapshot_context.__exit__, None, None, None)
        server = create_remote_server(
            snapshot_root,
            repository_id="company/auth-service",
            snapshot_commit=str(manifest["commit"]),
            issuer="https://issuer.example.com/tenant",
            jwks_url="https://issuer.example.com/tenant/keys",
            audience="agentramen-api",
            resource_url="https://mcp.example.com/mcp",
            allowed_hosts=["testserver"],
            allowed_group="engineering",
        )

        tools = asyncio.run(server.list_tools())
        names = {tool.name for tool in tools}
        self.assertIn("repo_context", names)
        self.assertIn("repo_team_memory_search", names)
        self.assertNotIn("repo_publish_memory", names)
        context_result = asyncio.run(
            server.call_tool("repo_context", {"task": "AuthService", "token_budget": 500})
        )
        self.assertEqual(context_result.structured_content["files"][0]["path"], "auth.py")
        self.assertEqual(context_result.structured_content["repository_id"], "company/auth-service")
        self.assertEqual(context_result.structured_content["snapshot_commit"], manifest["commit"])
        status_result = asyncio.run(server.call_tool("repo_status", {}))
        self.assertEqual(status_result.structured_content["snapshot_commit"], manifest["commit"])
        self.assertEqual(status_result.structured_content["files"], 2)

        issuer = "https://issuer.example.com/tenant"
        audience = "agentramen-api"
        verifier = OIDCVerifier(
            issuer=issuer,
            jwks_url="https://issuer.example.com/tenant/keys",
            audience=audience,
            resource_url="https://mcp.example.com/mcp",
            required_scope="agentramen:read",
            allowed_group="engineering",
        )
        self.assertIsNone(asyncio.run(verifier.verify_token("not.a.jwt")))
        signing_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        now = int(time.time())
        access_jwt = jwt.encode(
            {
                "iss": issuer,
                "aud": audience,
                "sub": "developer-123",
                "exp": now + 300,
                "scope": "agentramen:read",
                "groups": ["engineering"],
            },
            signing_key,
            algorithm="RS256",
            headers={"kid": "test-key"},
        )
        with patch.object(
            verifier.jwks,
            "get_signing_key_from_jwt",
            return_value=SimpleNamespace(key=signing_key.public_key()),
        ):
            verified = asyncio.run(verifier.verify_token(access_jwt))
        self.assertIsNotNone(verified)
        self.assertEqual(verified.subject, "developer-123")

        app = server.streamable_http_app(
            transport_security=TransportSecuritySettings(allowed_hosts=["testserver"])
        )
        with TestClient(app) as client:
            response = client.post(
                "/mcp",
                json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
            )
        self.assertEqual(response.status_code, 401)

    def test_central_snapshot_round_trip_contains_only_indexed_safe_source(self):
        from agentramen.snapshots import export_snapshot, mounted_snapshot

        (self.root / ".gitignore").write_text(".agentramen/\n", encoding="utf-8")
        (self.root / "auth.py").write_text(
            "class AuthService:\n    pass\n", encoding="utf-8"
        )
        (self.root / "credentials.py").write_text(
            "api_key = 'ThisLooksLikeARealSecretValue'\n", encoding="utf-8"
        )
        self.commit("add safe auth and excluded credential source")
        index_repository(self.root)
        snapshot = self.root.with_suffix(".snapshot.zip")
        self.addCleanup(snapshot.unlink, missing_ok=True)

        result = export_snapshot(self.root, "palrajjp/test-repo", snapshot)
        self.assertEqual(result["indexed_files"], 2)
        expected_head = subprocess.check_output(
            ["git", "-C", str(self.root), "rev-parse", "HEAD"], text=True
        ).strip()

        with mounted_snapshot(snapshot, "palrajjp/test-repo") as (snapshot_root, manifest):
            self.assertEqual(manifest["commit"], expected_head)
            self.assertFalse((snapshot_root / "credentials.py").exists())
            context = context_for(snapshot_root, "AuthService", 1000)
            self.assertEqual(context["files"][0]["path"], "auth.py")
            self.assertNotIn("ThisLooksLikeARealSecretValue", json.dumps(context))

        with self.assertRaisesRegex(AgentRamenError, "does not match"):
            with mounted_snapshot(snapshot, "other/repo"):
                pass

    def test_noise_filter_removes_boilerplate_and_keeps_structure(self):
        filtered = filter_noise(
            "src/auth.py",
            "Generated by scaffolder; do not edit\n"
            "class AuthService:\n    def login(self):\n        return True\n",
        )
        self.assertNotIn("Generated by", filtered)
        self.assertIn("class AuthService", filtered)
        self.assertEqual(filter_noise("package-lock.json", '{"lockfileVersion": 3}'), "")

    def test_database_context_exclusion_rules_filter_retrieved_files(self):
        (self.root / "auth.py").write_text("class AuthService:\n    pass\n", encoding="utf-8")
        self.commit("add auth")
        index_repository(self.root)
        conn = connect(self.root)
        conn.execute(
            "INSERT INTO context_exclusion_rules(pattern, pattern_type, reason, created_at) "
            "VALUES ('auth.py', 'path', 'private source', '2026-01-01T00:00:00+00:00')"
        )
        conn.commit()
        conn.close()
        self.assertEqual(context_for(self.root, "AuthService")["files"], [])

    def test_snapshot_export_names_init_generated_workflow_blocker(self):
        from agentramen.snapshots import export_snapshot

        (self.root / "auth.py").write_text("class AuthService: pass\n", encoding="utf-8")
        self.commit("add auth service")
        index_repository(self.root)
        workflow = self.root / ".github" / "workflows" / "agentramen.yml"
        workflow.parent.mkdir(parents=True)
        workflow.write_text("name: agentRamen\n", encoding="utf-8")

        with self.assertRaisesRegex(
            AgentRamenError,
            r"may have generated \.github/workflows/agentramen\.yml",
        ):
            export_snapshot(self.root, "company/auth-service", self.root / "snapshot.zip")

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

    def test_test_fixture_placeholder_credentials_stay_indexed(self):
        (self.root / "tests").mkdir()
        (self.root / "tests" / "test_auth.py").write_text(
            "def test_auth():\n    api_key = 'fake-api-key-for-fixture-123'\n",
            encoding="utf-8",
        )
        self.commit("add fixture")
        index_repository(self.root)
        self.assertEqual(repository_status(self.root)["files"], 1)

    def test_real_looking_credentials_in_tests_are_still_excluded(self):
        (self.root / "tests").mkdir()
        (self.root / "tests" / "test_auth.py").write_text(
            "api_key = 'Zq81hTr0Pw93LkdUv72Mx'\n", encoding="utf-8"
        )
        self.commit("add real-looking")
        with self.assertLogs("agentramen", level="WARNING"):
            result = index_repository(self.root)
        self.assertEqual(repository_status(self.root)["files"], 0)
        self.assertEqual(result["excluded_files"], [
            {"path": "tests/test_auth.py", "reason": "credential-like content detected"}
        ])
        status = repository_status(self.root)
        self.assertEqual(status["excluded_count"], 1)
        self.assertEqual(status["excluded_files"], result["excluded_files"])
        self.assertEqual(status["not_indexed_files"], 0)

    def test_index_diagnostic_clears_after_excluded_file_is_fixed(self):
        secret_file = self.root / "credentials.py"
        secret_file.write_text(
            "api_key = 'Zq81hTr0Pw93LkdUv72Mx'\n", encoding="utf-8"
        )
        self.commit("add credential-like file")
        with self.assertLogs("agentramen", level="WARNING"):
            first = index_repository(self.root)
        self.assertEqual(first["excluded_count"], 1)

        secret_file.write_text("class Credentials: pass\n", encoding="utf-8")
        updated = index_repository(self.root)
        self.assertEqual(updated["files"], 1)
        self.assertEqual(updated["excluded_count"], 0)
        self.assertEqual(repository_status(self.root)["excluded_files"], [])

    def test_configured_ignore_patterns_are_applied(self):
        (self.root / ".agentramen.yml").write_text("ignore:\n  - private/\n", encoding="utf-8")
        (self.root / "private").mkdir()
        (self.root / "private" / "module.py").write_text("class Hidden:\n    pass\n", encoding="utf-8")
        (self.root / "visible.py").write_text("class Visible:\n    pass\n", encoding="utf-8")
        self.commit("add ignored and visible files")

        index_repository(self.root)

        self.assertEqual(repository_status(self.root)["files"], 1)
        self.assertEqual(repo_search(self.root, "Hidden"), [])

    def test_configuration_controls_history_retention_and_cochanges(self):
        config_path = self.root / ".agentramen.yml"
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
        config = self.root / ".agentramen.yml"
        config.write_text("indexing:\n  incremental: false\n", encoding="utf-8")
        index_repository(self.root)

        result = index_repository(self.root)

        self.assertEqual(result["indexed"], 2)

    def test_invalid_context_budget_in_config_is_reported(self):
        (self.root / ".agentramen.yml").write_text(
            "context:\n  default_budget: 10\n", encoding="utf-8"
        )
        with self.assertRaisesRegex(AgentRamenError, "context.default_budget"):
            load_config(self.root)

    def test_context_uses_configured_tokenizer_consistently_with_budget(self):
        from agentramen.core import _TOKENIZERS, _count_context_tokens

        (self.root / ".agentramen.yml").write_text(
            "context:\n  tokenizer_model: test-model\n", encoding="utf-8"
        )
        (self.root / "auth.py").write_text(
            "class AuthService:\n    def login(self): return True\n", encoding="utf-8"
        )
        self.commit("add auth")
        index_repository(self.root)

        class CharacterEncoding:
            def encode(self, text):
                return list(text)

        _TOKENIZERS["test-model"] = CharacterEncoding()
        try:
            (self.root / "auth.py").write_text(
                "class AuthService:\n" + "    def login(self): return True\n" * 40,
                encoding="utf-8",
            )
            self.commit("expand auth")
            index_repository(self.root)
            context = context_for(self.root, "auth login", 600)
            content = [
                {key: value for key, value in item.items() if key != "estimated_tokens"}
                for item in context["files"]
            ]
            expected = _count_context_tokens(
                {
                    "files": content,
                    "team_memories": context["team_memories"],
                    "evidence": context["evidence"],
                },
                "test-model",
            )
            self.assertEqual(context["token_count_method"], "tiktoken")
            self.assertEqual(context["tokenizer_model"], "test-model")
            self.assertEqual(context["estimated_tokens"], expected)
            self.assertLessEqual(context["estimated_tokens"], context["token_budget"])
            self.assertTrue(context["files"])
            self.assertGreater(context["files"][0]["estimated_tokens"], 0)
            self.assertLess(len(context["files"][0]["excerpt"]), 480)
        finally:
            _TOKENIZERS.pop("test-model", None)

    def test_context_fallback_reports_approximate_serialized_payload_count(self):
        from agentramen.core import _count_context_tokens

        (self.root / "auth.py").write_text("class Auth:\n    pass\n", encoding="utf-8")
        self.commit("add auth")
        index_repository(self.root)

        context = context_for(self.root, "Auth", 300)
        content = [
            {key: value for key, value in item.items() if key != "estimated_tokens"}
            for item in context["files"]
        ]
        expected = _count_context_tokens(
            {
                "files": content,
                "team_memories": context["team_memories"],
                "evidence": context["evidence"],
            },
            "",
        )
        self.assertEqual(context["token_count_method"], "approximate")
        self.assertIsNone(context["tokenizer_model"])
        self.assertEqual(context["estimated_tokens"], expected)
        self.assertLessEqual(context["estimated_tokens"], context["token_budget"])

    def test_context_uses_configured_tiktoken_model_encoding(self):
        try:
            import tiktoken
        except ImportError:
            self.skipTest("Tokenizer optional dependency is not installed")
        from agentramen.core import _TOKENIZERS

        model = "gpt-4o-mini"
        (self.root / ".agentramen.yml").write_text(
            f"context:\n  tokenizer_model: {model}\n", encoding="utf-8"
        )
        (self.root / "auth.py").write_text("class Auth:\n    pass\n", encoding="utf-8")
        self.commit("add auth")
        index_repository(self.root)
        _TOKENIZERS.pop(model, None)

        class CharacterEncoding:
            def encode(self, text):
                return list(text)

        with patch(
            "tiktoken.encoding_for_model", return_value=CharacterEncoding()
        ) as encoding_for_model:
            context = context_for(self.root, "Auth", 500)
        encoding_for_model.assert_called_once_with(model)
        content = [
            {key: value for key, value in item.items() if key != "estimated_tokens"}
            for item in context["files"]
        ]
        payload = {
            "files": content,
            "team_memories": context["team_memories"],
            "evidence": context["evidence"],
        }
        self.assertEqual(
            context["estimated_tokens"],
            len(json.dumps(payload, ensure_ascii=False, separators=(",", ":"))),
        )
        self.assertEqual(context["token_count_method"], "tiktoken")
        _TOKENIZERS.pop(model, None)

    def test_tokenizer_configuration_requires_string(self):
        (self.root / ".agentramen.yml").write_text(
            "context:\n  tokenizer_model: true\n", encoding="utf-8"
        )
        with self.assertRaisesRegex(AgentRamenError, "context.tokenizer_model"):
            load_config(self.root)

    def test_semantic_benchmark_comparison_reports_unavailable_model(self):
        from agentramen.benchmark import run_comparisons

        with patch(
            "agentramen.benchmark.run_benchmark",
            side_effect=[
                {"semantic_enabled": False, "context_retrieval_seconds": 0.01},
                AgentRamenError("embedding model unavailable"),
            ],
        ):
            result = run_comparisons([10], None, (False, True))

        self.assertEqual(len(result), 2)
        self.assertFalse(result[0]["semantic_enabled"])
        self.assertTrue(result[1]["semantic_enabled"])
        self.assertEqual(result[1]["error"], "embedding model unavailable")

    def test_labeled_quality_benchmark_reports_relevance_and_test_accuracy(self):
        from agentramen.benchmark import run_quality_benchmark

        result = run_quality_benchmark()

        self.assertEqual(result["scenario"], "synthetic-labeled-quality-reference")
        self.assertEqual(result["mean_file_precision"], 1.0)
        self.assertEqual(result["mean_file_recall"], 1.0)
        self.assertEqual(result["mean_test_precision"], 1.0)
        self.assertEqual(result["mean_test_recall"], 1.0)
        self.assertTrue(result["scenarios"][0]["test_evidence"])

    def test_local_http_api_exposes_versioned_context(self):
        from agentramen.server import create_server

        (self.root / ".agentramen.yml").write_text(
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
            with urlopen(address + "/assets/agentramen-logo.png") as response:
                self.assertEqual(response.headers.get_content_type(), "image/png")
                logo = response.read()
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
            stage_request = Request(
                address + "/api/v1/memories",
                data=json.dumps(
                    {"subject": "authentication", "content": "Use OAuthProvider"}
                ).encode(),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urlopen(stage_request) as response:
                staged = json.load(response)
            with urlopen(address + "/api/v1/memories/review-queue") as response:
                queue = json.load(response)
            approve_request = Request(
                address + "/api/v1/memories/approve",
                data=json.dumps({"id": staged["id"]}).encode(),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urlopen(approve_request) as response:
                approval = json.load(response)
            self.assertEqual(architecture_value["files"], 1)
            self.assertIn("Task context", page)
            self.assertIn("Architecture", page)
            self.assertIn('id="hotspot-list"', page)
            self.assertIn('src="/assets/agentramen-logo.png"', page)
            self.assertTrue(logo.startswith(b"\x89PNG\r\n\x1a\n"))
            self.assertIn(r"lines.join('\n')", page)
            self.assertEqual(snapshot["revision"], revision)
            self.assertEqual(context_value["files"][0]["path"], "auth.py")
            self.assertEqual(context_value["token_budget"], 500)
            self.assertEqual(override_value["token_budget"], 300)
            self.assertEqual(queue[0]["content"], "Use OAuthProvider")
            self.assertEqual(approval["status"], "approved")
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
        self.assertIn("uses: palrajjp/agentRamen/.github/workflows/index.yml@main", workflow)

    def test_mcp_exposes_context_and_team_memory_workflow(self):
        (self.root / ".agentramen.yml").write_text(
            "context:\n  default_budget: 350\n", encoding="utf-8"
        )
        (self.root / "auth.py").write_text("class AuthService:\n    pass\n", encoding="utf-8")
        self.commit("add auth")
        index_repository(self.root)
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
            {
                "jsonrpc": "2.0",
                "id": 4,
                "method": "tools/call",
                "params": {
                    "name": "repo_stage_memory",
                    "arguments": {
                        "subject": "authentication provider",
                        "content": "Use OAuthProvider",
                        "source": "team review",
                    },
                },
            },
            {
                "jsonrpc": "2.0",
                "id": 5,
                "method": "tools/call",
                "params": {"name": "repo_review_queue", "arguments": {}},
            },
        ]
        import_code = (
            "import sys; "
            f"sys.path.insert(0, {str(Path(__file__).resolve().parents[1])!r}); "
            "from agentramen.cli import main; "
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
        results_by_id = {result.get("id"): result for result in results}
        tools = [tool["name"] for tool in results_by_id[2]["result"]["tools"]]
        self.assertIn("repo_context", tools)
        self.assertIn("repo_graph", tools)
        self.assertIn("repo_review_queue", tools)
        self.assertIn("repo_stage_memory", tools)
        self.assertIn("repo_approve_memory", tools)
        self.assertIn("repo_reject_memory", tools)
        self.assertIn("repo_publish_memory", tools)
        self.assertIn("repo_team_memory_search", tools)
        self.assertIn("repo_memory_audit", tools)
        context = results_by_id[3]["result"]["structuredContent"]
        self.assertEqual(context["token_budget"], 350)
        self.assertTrue(context["evidence"])
        staged = results_by_id[4]["result"]["structuredContent"]
        memory_id = staged["id"]
        self.assertEqual(staged["shared"], False)
        reviewed = results_by_id[5]["result"]["structuredContent"][0]
        self.assertEqual(reviewed["id"], memory_id)
        self.assertTrue(reviewed["evidence_current"])
        self.assertEqual(reviewed["evidence"][0]["commit"], context["evidence"][0]["commit"])

        publish_requests = [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {"name": "repo_approve_memory", "arguments": {"id": memory_id}},
            },
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {"name": "repo_publish_memory", "arguments": {"id": memory_id}},
            },
            {
                "jsonrpc": "2.0",
                "id": 4,
                "method": "tools/call",
                "params": {
                    "name": "repo_team_memory_search",
                    "arguments": {"query": "OAuthProvider"},
                },
            },
            {
                "jsonrpc": "2.0",
                "id": 5,
                "method": "tools/call",
                "params": {"name": "repo_memory_audit", "arguments": {}},
            },
        ]
        publish_response = subprocess.run(
            [sys.executable, "-c", import_code],
            cwd=self.root,
            input="".join(json.dumps(item) + "\n" for item in publish_requests),
            text=True,
            capture_output=True,
            check=True,
        )
        published = {
            result.get("id"): result
            for result in map(json.loads, publish_response.stdout.splitlines())
        }
        self.assertEqual(published[2]["result"]["structuredContent"]["status"], "approved")
        self.assertEqual(published[3]["result"]["structuredContent"]["status"], "published")
        self.assertEqual(published[4]["result"]["structuredContent"][0]["id"], memory_id)
        self.assertEqual(
            published[5]["result"]["structuredContent"]["current_shared_count"], 1
        )

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

    def test_affected_test_suggestions_use_import_evidence(self):
        (self.root / "src").mkdir()
        (self.root / "tests").mkdir()
        (self.root / "src" / "auth_service.ts").write_text(
            "export class AuthService {}\n", encoding="utf-8"
        )
        (self.root / "src" / "billing.ts").write_text(
            "export class BillingService {}\n", encoding="utf-8"
        )
        (self.root / "tests" / "auth_service.test.ts").write_text(
            'import { AuthService } from "../src/auth_service";\n'
            "test('auth', () => new AuthService());\n",
            encoding="utf-8",
        )
        (self.root / "tests" / "billing.test.ts").write_text(
            'import { BillingService } from "../src/billing";\n'
            "test('billing', () => new BillingService());\n",
            encoding="utf-8",
        )
        (self.root / "tests" / "test_auth_secret.test.ts").write_text(
            'api_key = "Zq81hTr0Pw93LkdUv72Mx"\n', encoding="utf-8"
        )
        self.commit("add source and related/unrelated tests")
        with self.assertLogs("agentramen", level="WARNING"):
            index_repository(self.root)

        impact = dependency_impact(self.root, "src/auth_service.ts")
        self.assertEqual(impact["tests"], ["tests/auth_service.test.ts"])
        self.assertEqual(
            impact["test_associations"],
            [
                {
                    "path": "tests/auth_service.test.ts",
                    "confidence": "confirmed",
                    "depth": 1,
                    "evidence": ["tests/auth_service.test.ts", "src/auth_service.ts"],
                }
            ],
        )
        self.assertNotIn("tests/billing.test.ts", impact["tests"])
        self.assertEqual(
            impact["unindexed_test_candidates"],
            [
                {
                    "path": "tests/test_auth_secret.test.ts",
                    "reason": "credential-like content detected",
                    "confidence": "not_assessed",
                }
            ],
        )

    def test_indexed_context_ranking_matches_full_scan_reference(self):
        from collections import Counter

        from agentramen.core import _terms

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
        from agentramen.analyzers import TreeSitterAnalyzer

        with patch("agentramen.analyzers.importlib.import_module", side_effect=ImportError):
            result = TreeSitterAnalyzer("javascript").analyze(
                "auth.js", "export class AuthService {}\n"
            )
        self.assertIn("AuthService", result.symbols)

    def test_tree_sitter_extracts_symbols_and_calls_when_installed(self):
        try:
            import tree_sitter_language_pack
        except ImportError:
            self.skipTest("Tree-sitter optional dependency is not installed")
        from agentramen.analyzers import analyze

        result = analyze(
            "auth.js",
            "export class AuthService {}\nfunction login() { send(); }",
            "javascript",
        )
        self.assertIn("AuthService", result.symbols)
        self.assertIn("login", result.symbols)
        self.assertIn("send", result.calls)

    def test_semantic_embeddings_can_retrieve_without_lexical_overlap(self):
        (self.root / ".agentramen.yml").write_text(
            "semantic:\n  enabled: true\n  model: test-model\n", encoding="utf-8"
        )
        (self.root / "access.py").write_text(
            "class LoginManager:\n    pass\n", encoding="utf-8"
        )
        self.commit("add access manager")

        with patch("agentramen.core._embed_texts", side_effect=lambda _model, texts: [[1.0, 0.0] for _ in texts]):
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
