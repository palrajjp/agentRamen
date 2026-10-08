"""Additive migrations for local memory-engine tables."""

from __future__ import annotations

import sqlite3


_MEMORY_NODE_COLUMNS = {
    "epistemic_status": "TEXT NOT NULL DEFAULT 'ACTIVE'",
    "valid_from": "TEXT NOT NULL DEFAULT ''",
    "valid_to": "TEXT",
    "importance_score": "REAL NOT NULL DEFAULT 0.5",
}


def apply_schema_patches(conn: sqlite3.Connection) -> None:
    """Create memory tables and add missing columns without replacing user data."""
    with conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS staged_memories (
                id TEXT PRIMARY KEY,
                subject TEXT NOT NULL,
                content TEXT NOT NULL,
                category TEXT NOT NULL DEFAULT 'context',
                source TEXT,
                intent_vector TEXT,
                importance_score REAL NOT NULL DEFAULT 0.5
                    CHECK (importance_score >= 0 AND importance_score <= 1),
                status TEXT NOT NULL DEFAULT 'pending'
                    CHECK (status IN ('pending', 'approved', 'rejected')),
                created_at TEXT NOT NULL,
                reviewed_at TEXT,
                reviewed_by TEXT,
                review_note TEXT
            );
            CREATE TABLE IF NOT EXISTS memory_nodes (
                id TEXT PRIMARY KEY,
                subject TEXT NOT NULL,
                content TEXT NOT NULL,
                category TEXT NOT NULL DEFAULT 'context',
                source TEXT,
                epistemic_status TEXT NOT NULL DEFAULT 'ACTIVE'
                    CHECK (epistemic_status IN ('ACTIVE', 'SUPERSEDED', 'VERIFIED', 'INFERRED', 'STALE')),
                valid_from TEXT NOT NULL,
                valid_to TEXT,
                importance_score REAL NOT NULL DEFAULT 0.5
                    CHECK (importance_score >= 0 AND importance_score <= 1)
            );
            CREATE TABLE IF NOT EXISTS memory_evidence (
                memory_id TEXT NOT NULL,
                path TEXT NOT NULL,
                digest TEXT NOT NULL,
                commit_hash TEXT NOT NULL,
                PRIMARY KEY (memory_id, path)
            );
            CREATE TABLE IF NOT EXISTS context_exclusion_rules (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                pattern TEXT NOT NULL,
                pattern_type TEXT NOT NULL DEFAULT 'glob'
                    CHECK (pattern_type IN ('glob', 'regex', 'path', 'content')),
                reason TEXT NOT NULL DEFAULT '',
                enabled INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS staged_memories_by_status
                ON staged_memories(status, created_at);
            CREATE INDEX IF NOT EXISTS memory_nodes_by_subject_status
                ON memory_nodes(subject, epistemic_status, valid_from);
            CREATE INDEX IF NOT EXISTS context_exclusion_rules_enabled
                ON context_exclusion_rules(enabled, pattern_type);
            """
        )
        staged_columns = {
            row[1] for row in conn.execute("PRAGMA table_info(staged_memories)")
        }
        if "reviewed_by" not in staged_columns:
            conn.execute("ALTER TABLE staged_memories ADD COLUMN reviewed_by TEXT")
        columns = {
            row[1] for row in conn.execute("PRAGMA table_info(memory_nodes)")
        }
        for name, definition in _MEMORY_NODE_COLUMNS.items():
            if name not in columns:
                conn.execute(f"ALTER TABLE memory_nodes ADD COLUMN {name} {definition}")
        schema_row = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='memory_nodes'"
        ).fetchone()
        if schema_row and "'STALE'" not in (schema_row[0] or "").upper():
            column_rows = conn.execute("PRAGMA table_info(memory_nodes)").fetchall()
            column_names = [row[1] for row in column_rows]
            primary_keys = [(row[5], row[1]) for row in column_rows if row[5]]
            definitions = []
            for row in column_rows:
                _, name, kind, not_null, default, primary_key = row
                quoted_name = '"' + name.replace('"', '""') + '"'
                definition = quoted_name + (" " + kind if kind else "")
                if primary_key and len(primary_keys) == 1:
                    definition += " PRIMARY KEY"
                elif not_null:
                    definition += " NOT NULL"
                if default is not None:
                    definition += " DEFAULT " + str(default)
                if name == "epistemic_status":
                    definition += (
                        " CHECK (epistemic_status IN "
                        "('ACTIVE', 'SUPERSEDED', 'VERIFIED', 'INFERRED', 'STALE'))"
                    )
                elif name == "importance_score":
                    definition += " CHECK (importance_score >= 0 AND importance_score <= 1)"
                definitions.append(definition)
            if len(primary_keys) > 1:
                ordered_keys = [name for _, name in sorted(primary_keys)]
                definitions.append(
                    "PRIMARY KEY (" + ", ".join('"' + name + '"' for name in ordered_keys) + ")"
                )
            conn.execute("DROP INDEX IF EXISTS memory_nodes_by_subject_status")
            conn.execute("CREATE TABLE memory_nodes_with_stale_status (" + ", ".join(definitions) + ")")
            quoted_columns = ", ".join('"' + name + '"' for name in column_names)
            conn.execute(
                f"INSERT INTO memory_nodes_with_stale_status ({quoted_columns}) "
                f"SELECT {quoted_columns} FROM memory_nodes"
            )
            conn.execute("DROP TABLE memory_nodes")
            conn.execute(
                "ALTER TABLE memory_nodes_with_stale_status RENAME TO memory_nodes"
            )
            conn.execute(
                "CREATE INDEX memory_nodes_by_subject_status "
                "ON memory_nodes(subject, epistemic_status, valid_from)"
            )
        conn.execute(
            "INSERT INTO metadata(key, value) VALUES('memory_schema_version', '3') "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value"
        )
