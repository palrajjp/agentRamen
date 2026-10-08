"""Deterministic temporal resolution for reviewed repository memories."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone


EPISTEMIC_STATUSES = frozenset({"ACTIVE", "SUPERSEDED", "VERIFIED", "INFERRED"})


def utc_timestamp(value: datetime | None = None) -> str:
    moment = value or datetime.now(timezone.utc)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc).isoformat(timespec="microseconds")


def activate_memory(
    conn: sqlite3.Connection,
    *,
    memory_id: str,
    subject: str,
    content: str,
    category: str,
    source: str | None,
    importance_score: float,
    epistemic_status: str,
    valid_from: str | None = None,
) -> None:
    """Insert a memory and supersede older, conflicting facts with the same subject."""
    if epistemic_status not in EPISTEMIC_STATUSES - {"SUPERSEDED"}:
        raise ValueError("New memories must be ACTIVE, VERIFIED, or INFERRED.")
    if not 0.0 <= importance_score <= 1.0:
        raise ValueError("importance_score must be between 0 and 1.")
    timestamp = valid_from or utc_timestamp()
    conn.execute(
        "UPDATE memory_nodes SET epistemic_status='SUPERSEDED', valid_to=? "
        "WHERE subject=? AND content<>? AND valid_to IS NULL "
        "AND epistemic_status IN ('ACTIVE', 'VERIFIED', 'INFERRED')",
        (timestamp, subject, content),
    )
    conn.execute(
        "INSERT INTO memory_nodes "
        "(id, subject, content, category, source, epistemic_status, valid_from, "
        "valid_to, importance_score) VALUES (?, ?, ?, ?, ?, ?, ?, NULL, ?)",
        (
            memory_id,
            subject,
            content,
            category,
            source,
            epistemic_status,
            timestamp,
            importance_score,
        ),
    )
