"""Human-review staging operations for repository memories."""

from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path

from .core import AgentRamenError, _terms, connect, has_secret
from .graph.resolution import activate_memory, utc_timestamp

SHARED_MEMORY_PATH = ".agentramen-shared/memories"
MAX_SHARED_MEMORY_BYTES = 64 * 1024
MAX_SHARED_MEMORY_CONTENT_CHARS = 6_000


def _memory_text(memory: dict[str, object]) -> str:
    return "\n".join(
        str(memory.get(field) or "") for field in ("subject", "content", "category", "source")
    )


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


def _shared_memory_directory(root: Path) -> Path:
    parent = root / ".agentramen-shared"
    directory = root / SHARED_MEMORY_PATH
    if parent.is_symlink() or directory.is_symlink():
        raise AgentRamenError("Shared memory directory cannot be a symbolic link.")
    return directory


def _read_shared_memory(path: Path) -> dict[str, object]:
    if path.is_symlink():
        raise AgentRamenError(f"Shared memory file cannot be a symbolic link: {path.name}")
    try:
        if path.stat().st_size > MAX_SHARED_MEMORY_BYTES:
            raise AgentRamenError(f"Shared memory file is too large: {path.name}")
        memory = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise AgentRamenError(f"Invalid shared memory file: {path.name}") from exc
    required = {
        "schema_version",
        "id",
        "subject",
        "content",
        "category",
        "source",
        "epistemic_status",
        "valid_from",
        "valid_to",
        "importance_score",
    }
    if (
        not isinstance(memory, dict)
        or not required.issubset(memory)
        or memory["schema_version"] != 1
        or memory["id"] != path.stem
        or not isinstance(memory["subject"], str)
        or not isinstance(memory["content"], str)
        or not isinstance(memory["category"], str)
        or (memory["source"] is not None and not isinstance(memory["source"], str))
        or not isinstance(memory["epistemic_status"], str)
        or not isinstance(memory["valid_from"], str)
        or (memory["valid_to"] is not None and not isinstance(memory["valid_to"], str))
        or not isinstance(memory["importance_score"], (int, float))
        or isinstance(memory["importance_score"], bool)
        or not 0 <= memory["importance_score"] <= 1
        or memory["epistemic_status"] not in {"ACTIVE", "VERIFIED", "INFERRED", "SUPERSEDED"}
    ):
        raise AgentRamenError(f"Invalid shared memory schema: {path.name}")
    if len(memory["content"]) > MAX_SHARED_MEMORY_CONTENT_CHARS:
        raise AgentRamenError(f"Shared memory content is too long: {path.name}")
    if has_secret(path.name, _memory_text(memory)):
        raise AgentRamenError(f"Shared memory contains credential-like content: {path.name}")
    return memory


def _write_shared_memory(path: Path, memory: dict[str, object]) -> None:
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(
            json.dumps(memory, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def publish_shared_memory(root: Path, memory_id: str) -> dict[str, object]:
    """Export a reviewed local memory as a Git-versioned team memory file."""
    try:
        canonical_id = str(uuid.UUID(memory_id))
    except ValueError as exc:
        raise AgentRamenError("Memory id must be a UUID.") from exc
    conn = connect(root)
    try:
        row = conn.execute(
            "SELECT s.status, n.subject, n.content, n.category, n.source, "
            "n.epistemic_status, n.valid_from, n.valid_to, n.importance_score "
            "FROM staged_memories s JOIN memory_nodes n ON n.id=s.id WHERE s.id=?",
            (canonical_id,),
        ).fetchone()
    finally:
        conn.close()
    if row is None or row[0] != "approved":
        raise AgentRamenError("Only locally approved memories can be shared.")
    if row[6] is None or row[7] is not None or row[5] == "SUPERSEDED":
        raise AgentRamenError("Superseded memories cannot be published as active team knowledge.")
    if len(row[1]) > 240 or len(row[2]) > MAX_SHARED_MEMORY_CONTENT_CHARS:
        raise AgentRamenError("Shared memory subject or content exceeds its publication size limit.")
    memory = {
        "schema_version": 1,
        "id": canonical_id,
        "subject": row[1],
        "content": row[2],
        "category": row[3],
        "source": row[4],
        "epistemic_status": row[5],
        "valid_from": row[6],
        "valid_to": row[7],
        "importance_score": row[8],
    }
    if has_secret(canonical_id, _memory_text(memory)):
        raise AgentRamenError("Memory contains credential-like content and cannot be shared.")

    directory = _shared_memory_directory(root)
    directory.mkdir(parents=True, exist_ok=True)
    destination = directory / f"{canonical_id}.json"
    if destination.exists():
        existing = _read_shared_memory(destination)
        if existing == memory:
            return {
                "id": canonical_id,
                "status": "already_published",
                "path": destination.relative_to(root).as_posix(),
                "superseded_ids": [],
            }
        raise AgentRamenError("A different shared memory already uses this id.")

    shared_files = sorted(directory.glob("*.json"))
    superseded_ids = []
    superseded_memories = []
    for path in shared_files:
        existing = _read_shared_memory(path)
        if (
            existing["subject"] == memory["subject"]
            and existing["valid_to"] is None
            and existing["epistemic_status"] in {"ACTIVE", "VERIFIED", "INFERRED"}
        ):
            if str(existing["valid_from"]) > str(memory["valid_from"]):
                raise AgentRamenError(
                    "A newer shared memory exists for this subject; sync and review it before publishing."
                )
            existing["epistemic_status"] = "SUPERSEDED"
            existing["valid_to"] = memory["valid_from"]
            superseded_memories.append((path, existing))
            superseded_ids.append(str(existing["id"]))

    for path, existing in superseded_memories:
        _write_shared_memory(path, existing)
    _write_shared_memory(destination, memory)
    return {
        "id": canonical_id,
        "status": "published",
        "path": destination.relative_to(root).as_posix(),
        "superseded_ids": superseded_ids,
    }


def search_shared_memories(
    root: Path, query: str, limit: int = 10
) -> list[dict[str, object]]:
    """Search approved team memories checked out from the repository."""
    terms = _terms(query)
    if not terms:
        return []
    directory = _shared_memory_directory(root)
    if not directory.exists():
        return []
    results = []
    for path in sorted(directory.glob("*.json")):
        memory = _read_shared_memory(path)
        if (
            memory["valid_to"] is not None
            or memory["epistemic_status"] not in {"ACTIVE", "VERIFIED", "INFERRED"}
        ):
            continue
        overlap = terms & _terms(
            str(memory["subject"]) + " " + str(memory["content"]) + " "
            + str(memory["category"])
        )
        if not overlap:
            continue
        score = sum(1 / max(1, len(terms)) for _ in overlap)
        results.append(
            {
                "id": memory["id"],
                "subject": memory["subject"],
                "content": memory["content"],
                "category": memory["category"],
                "source": memory["source"],
                "epistemic_status": memory["epistemic_status"],
                "valid_from": memory["valid_from"],
                "valid_to": memory["valid_to"],
                "importance_score": memory["importance_score"],
                "score": round(score + float(memory["importance_score"]) * 0.1, 3),
                "path": path.relative_to(root).as_posix(),
            }
        )
    results.sort(
        key=lambda item: (
            -float(item["score"]),
            -float(item["importance_score"]),
            str(item["subject"]).casefold(),
            str(item["id"]),
        )
    )
    return results[: max(1, min(limit, 50))]
