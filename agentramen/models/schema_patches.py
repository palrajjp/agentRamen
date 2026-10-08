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
                review_note TEXT
            );
            CREATE TABLE IF NOT EXISTS memory_nodes (
                id TEXT PRIMARY KEY,
                subject TEXT NOT NULL,
                content TEXT NOT NULL,
                category TEXT NOT NULL DEFAULT 'context',
                source TEXT,
                epistemic_status TEXT NOT NULL DEFAULT 'ACTIVE'
                    CHECK (epistemic_status IN ('ACTIVE', 'SUPERSEDED', 'VERIFIED', 'INFERRED')),
                valid_from TEXT NOT NULL,
                valid_to TEXT,
                importance_score REAL NOT NULL DEFAULT 0.5
                    CHECK (importance_score >= 0 AND importance_score <= 1)
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
        columns = {
            row[1] for row in conn.execute("PRAGMA table_info(memory_nodes)")
        }
        for name, definition in _MEMORY_NODE_COLUMNS.items():
            if name not in columns:
                conn.execute(f"ALTER TABLE memory_nodes ADD COLUMN {name} {definition}")
        conn.execute(
            "INSERT INTO metadata(key, value) VALUES('memory_schema_version', '1') "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value"
        )
