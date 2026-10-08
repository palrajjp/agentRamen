"""Human-review staging operations for repository memories."""

from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path

from .core import AgentRamenError, connect
from .graph.resolution import activate_memory, utc_timestamp


def stage_memory(
    root: Path,
    subject: str,
    content: str,
    *,
    category: str = "context",
    source: str | None = None,
    intent_vector: list[float] | None = None,
    importance_score: float = 0.5,
) -> str:
    if not subject.strip() or not content.strip():
        raise AgentRamenError("A staged memory requires a subject and content.")
    if not 0.0 <= importance_score <= 1.0:
        raise AgentRamenError("importance_score must be between 0 and 1.")
    memory_id = str(uuid.uuid4())
    conn = connect(root)
    try:
        conn.execute(
            "INSERT INTO staged_memories "
            "(id, subject, content, category, source, intent_vector, importance_score, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                memory_id,
                subject.strip(),
                content.strip(),
                category,
                source,
                json.dumps(intent_vector) if intent_vector is not None else None,
                importance_score,
                utc_timestamp(),
            ),
        )
        conn.commit()
    finally:
        conn.close()
    return memory_id


def review_queue(root: Path, limit: int = 50) -> list[dict[str, object]]:
    conn = connect(root)
    try:
        rows = conn.execute(
            "SELECT id, subject, content, category, source, importance_score, created_at "
            "FROM staged_memories WHERE status='pending' "
            "ORDER BY created_at, id LIMIT ?",
            (max(1, min(limit, 500)),),
        ).fetchall()
        return [
            {
                "id": row[0],
                "subject": row[1],
                "content": row[2],
                "category": row[3],
                "source": row[4],
                "importance_score": row[5],
                "created_at": row[6],
            }
            for row in rows
        ]
    finally:
        conn.close()


def approve_memory(
    root: Path, memory_id: str, epistemic_status: str = "VERIFIED"
) -> dict[str, object]:
    conn = connect(root)
    try:
        with conn:
            row = conn.execute(
                "SELECT subject, content, category, source, importance_score "
                "FROM staged_memories WHERE id=? AND status='pending'",
                (memory_id,),
            ).fetchone()
            if row is None:
                raise AgentRamenError("Pending memory not found.")
            activate_memory(
                conn,
                memory_id=memory_id,
                subject=row[0],
                content=row[1],
                category=row[2],
                source=row[3],
                importance_score=row[4],
                epistemic_status=epistemic_status,
            )
            reviewed_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
            conn.execute(
                "UPDATE staged_memories SET status='approved', reviewed_at=? WHERE id=?",
                (reviewed_at, memory_id),
            )
        return {"id": memory_id, "status": "approved", "epistemic_status": epistemic_status}
    except (sqlite3.Error, ValueError) as exc:
        if isinstance(exc, ValueError):
            raise AgentRamenError(str(exc)) from exc
        raise
    finally:
        conn.close()


def reject_memory(root: Path, memory_id: str, note: str = "") -> dict[str, str]:
    conn = connect(root)
    try:
        with conn:
            cursor = conn.execute(
                "UPDATE staged_memories SET status='rejected', reviewed_at=?, review_note=? "
                "WHERE id=? AND status='pending'",
                (datetime.now(timezone.utc).isoformat(timespec="seconds"), note, memory_id),
            )
            if cursor.rowcount != 1:
                raise AgentRamenError("Pending memory not found.")
        return {"id": memory_id, "status": "rejected"}
    finally:
        conn.close()
