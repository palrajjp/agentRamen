"""Human-review staging operations for repository memories."""

from __future__ import annotations

import json
import hashlib
import getpass
import re
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

from .core import AgentRamenError, _terms, connect, git, has_secret
from .graph.resolution import activate_memory, utc_timestamp

SHARED_MEMORY_PATH = ".agentramen-shared/memories"
MAX_SHARED_MEMORY_BYTES = 64 * 1024
MAX_SHARED_MEMORY_CONTENT_CHARS = 6_000


def _memory_text(memory: dict[str, object]) -> str:
    return "\n".join(
        str(memory.get(field) or "") for field in ("subject", "content", "category", "source")
    )


def _approval_identity(root: Path) -> str:
    configured_name = git(root, "config", "user.name", check=False).strip()
    return configured_name or getpass.getuser() or "local reviewer"


def capture_memory_evidence(root: Path, paths: list[str]) -> list[dict[str, str]]:
    """Capture path digests from the current indexed revision for a context result."""
    conn = connect(root)
    try:
        commit_row = conn.execute(
            "SELECT value FROM metadata WHERE key='last_commit'"
        ).fetchone()
        if commit_row is None or not commit_row[0]:
            return []
        indexed = dict(conn.execute("SELECT path, digest FROM files"))
        commit = str(commit_row[0])
    finally:
        conn.close()
    evidence = []
    for raw_path in sorted(set(paths)):
        path = PurePosixPath(raw_path)
        if (
            path.is_absolute()
            or not path.parts
            or "\\" in raw_path
            or "\x00" in raw_path
            or re.match(r"^[a-zA-Z]:", raw_path)
            or any(part in {"", ".", ".."} for part in path.parts)
            or path.parts[0] in {".agentramen", ".agentramen-shared"}
        ):
            continue
        relative = path.as_posix()
        digest = indexed.get(relative)
        if digest is None:
            continue
        source = root.joinpath(*path.parts)
        if source.is_symlink() or not source.is_file():
            continue
        try:
            content = source.read_bytes()
        except OSError:
            continue
        if hashlib.sha256(content).hexdigest() != digest:
            continue
        if has_secret(relative, content.decode("utf-8", errors="replace")):
            continue
        evidence.append({"path": relative, "digest": digest, "commit": commit})
    return evidence


def _validate_evidence(evidence: list[dict[str, str]] | None) -> list[tuple[str, str, str]]:
    rows = []
    seen_paths = set()
    for item in evidence or []:
        if not isinstance(item, dict):
            raise AgentRamenError("Memory evidence entries must be objects.")
        path_value = item.get("path", "")
        digest = item.get("digest", "")
        commit = item.get("commit", "")
        path = PurePosixPath(path_value) if isinstance(path_value, str) else None
        if (
            path is None
            or path.is_absolute()
            or not path.parts
                or "\\" in path_value
                or "\x00" in path_value
                or re.match(r"^[a-zA-Z]:", path_value)
            or any(part in {"", ".", ".."} for part in path.parts)
                or path.parts[0] in {".agentramen", ".agentramen-shared"}
            or path.parts[0] in {".agentramen", ".agentramen-shared"}
            or not isinstance(digest, str)
            or not re.fullmatch(r"[0-9a-f]{64}", digest)
            or not isinstance(commit, str)
            or not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", commit)
        ):
            raise AgentRamenError("Memory evidence requires a safe path, SHA-256 digest, and Git commit.")
        relative = path.as_posix()
        if relative not in seen_paths:
            seen_paths.add(relative)
            rows.append((relative, digest, commit))
    return rows


def _stale_evidence_paths(root: Path, evidence: list[dict[str, object]]) -> list[str]:
    stale = []
    for item in evidence:
        path = item.get("path")
        digest = item.get("digest")
        if not isinstance(path, str) or not isinstance(digest, str):
            stale.append(str(path or "<invalid evidence>"))
            continue
        if not _evidence_file_matches(root, path, digest):
            stale.append(path)
    return sorted(set(stale))


def _evidence_file_matches(root: Path, path: str, digest: str) -> bool:
    normalized = PurePosixPath(path)
    if (
        normalized.is_absolute()
        or not normalized.parts
        or "\\" in path
        or "\x00" in path
        or re.match(r"^[a-zA-Z]:", path)
        or any(part in {"", ".", ".."} for part in normalized.parts)
        or normalized.parts[0] in {".agentramen", ".agentramen-shared"}
    ):
        return False
    try:
        source = root
        for part in normalized.parts:
            source = source / part
            if source.is_symlink():
                return False
        source.resolve(strict=True).relative_to(root.resolve())
        if not source.is_file():
            return False
        content = source.read_bytes()
    except (OSError, ValueError):
        return False
    return hashlib.sha256(content).hexdigest() == digest


def invalidate_stale_memories(root: Path) -> list[dict[str, object]]:
    """Mark approved local memories stale when any evidence file changed or disappeared."""
    conn = connect(root)
    try:
        rows = conn.execute(
            "SELECT m.id, m.subject, e.path, e.digest FROM memory_nodes m "
            "JOIN memory_evidence e ON e.memory_id=m.id "
            "WHERE m.epistemic_status IN ('ACTIVE', 'VERIFIED', 'INFERRED') "
            "ORDER BY m.id, e.path"
        ).fetchall()
        commit_row = conn.execute(
            "SELECT value FROM metadata WHERE key='last_commit'"
        ).fetchone()
        checked_against_commit = str(commit_row[0]) if commit_row and commit_row[0] else None
        stale_paths: dict[str, tuple[str, list[str]]] = {}
        for memory_id, subject, path, digest in rows:
            if not _evidence_file_matches(root, path, digest):
                if memory_id not in stale_paths:
                    stale_paths[memory_id] = (subject, [])
                stale_paths[memory_id][1].append(path)
        invalidated_at = utc_timestamp()
        with conn:
            for memory_id in stale_paths:
                conn.execute(
                    "UPDATE memory_nodes SET epistemic_status='STALE', valid_to=? "
                    "WHERE id=? AND epistemic_status IN ('ACTIVE', 'VERIFIED', 'INFERRED')",
                    (invalidated_at, memory_id),
                )
        return [
            {
                "id": memory_id,
                "subject": subject,
                "stale_paths": sorted(paths),
                "invalidated_at": invalidated_at,
                "checked_against_commit": checked_against_commit,
                "status": "STALE",
            }
            for memory_id, (subject, paths) in sorted(stale_paths.items())
        ]
    finally:
        conn.close()


def memory_audit(root: Path) -> dict[str, object]:
    """Report stale/unverified local and Git-shared memory without revealing local contents."""
    invalidate_stale_memories(root)
    conn = connect(root)
    try:
        stale_local_rows = conn.execute(
            "SELECT m.id, m.subject, COALESCE((SELECT json_group_array(json_object("
            "'path', e.path, 'digest', e.digest, 'commit', e.commit_hash)) "
            "FROM memory_evidence e WHERE e.memory_id=m.id), '[]'), s.reviewed_by "
            "FROM memory_nodes m LEFT JOIN staged_memories s ON s.id=m.id "
            "WHERE m.epistemic_status='STALE' ORDER BY m.subject, m.id"
        ).fetchall()
        unverified_local = [
            {"id": memory_id, "subject": subject, "status": status, "reason": reason}
            for memory_id, subject, status, reason in conn.execute(
                "SELECT m.id, m.subject, m.epistemic_status, "
                "CASE WHEN NOT EXISTS (SELECT 1 FROM memory_evidence e WHERE e.memory_id=m.id) "
                "THEN 'missing evidence' ELSE 'missing approver' END FROM memory_nodes m "
                "WHERE m.epistemic_status IN ('ACTIVE', 'VERIFIED', 'INFERRED') "
                "AND (NOT EXISTS (SELECT 1 FROM memory_evidence e WHERE e.memory_id=m.id) "
                "OR NOT EXISTS (SELECT 1 FROM staged_memories s WHERE s.id=m.id "
                "AND s.reviewed_by IS NOT NULL AND s.reviewed_by != '')) "
                "ORDER BY m.subject, m.id"
            ).fetchall()
        ]
        commit_row = conn.execute(
            "SELECT value FROM metadata WHERE key='last_commit'"
        ).fetchone()
        index_commit = str(commit_row[0]) if commit_row and commit_row[0] else None
    finally:
        conn.close()

    stale_local = []
    for memory_id, subject, raw_evidence, approved_by in stale_local_rows:
        evidence = json.loads(raw_evidence)
        stale_local.append(
            {
                "id": memory_id,
                "subject": subject,
                "status": "STALE",
                "evidence": evidence,
                "approved_by": approved_by,
                "stale_paths": _stale_evidence_paths(root, evidence),
            }
        )

    stale_shared = []
    unverified_shared = []
    current_shared_count = 0
    directory = _shared_memory_directory(root)
    if directory.exists():
        for path in sorted(directory.glob("*.json")):
            memory = _read_shared_memory(path)
            if memory["valid_to"] is not None or memory["epistemic_status"] == "SUPERSEDED":
                continue
            evidence = memory.get("evidence", [])
            if not evidence or not memory.get("approved_by"):
                unverified_shared.append(
                    {
                        "id": memory["id"],
                        "subject": memory["subject"],
                        "path": path.relative_to(root).as_posix(),
                        "reason": "missing evidence" if not evidence else "missing approver",
                    }
                )
                continue
            stale_paths = _stale_evidence_paths(root, evidence)
            if stale_paths:
                stale_shared.append(
                    {
                        "id": memory["id"],
                        "subject": memory["subject"],
                        "path": path.relative_to(root).as_posix(),
                        "approved_by": memory.get("approved_by"),
                        "evidence": evidence,
                        "stale_paths": stale_paths,
                    }
                )
            else:
                current_shared_count += 1
    stale_ids = {
        str(item["id"]) for item in stale_local + stale_shared
    }
    return {
        "stale_local": stale_local,
        "unverified_local": unverified_local,
        "stale_shared": stale_shared,
        "unverified_shared": unverified_shared,
        "current_shared_count": current_shared_count,
        "stale_count": len(stale_ids),
        "index_commit": index_commit,
        "summary": (
            f"{len(stale_ids)} memor{'y is' if len(stale_ids) == 1 else 'ies are'} suspect; "
            f"evidence checked against index commit {index_commit}."
            if stale_ids and index_commit
            else f"{len(stale_ids)} stale memor{'y' if len(stale_ids) == 1 else 'ies'} detected."
            if stale_ids
            else "No stale memories detected."
        ),
    }


def stage_memory(
    root: Path,
    subject: str,
    content: str,
    *,
    category: str = "context",
    source: str | None = None,
    intent_vector: list[float] | None = None,
    importance_score: float = 0.5,
    evidence: list[dict[str, str]] | None = None,
) -> str:
    if not subject.strip() or not content.strip():
        raise AgentRamenError("A staged memory requires a subject and content.")
    if not 0.0 <= importance_score <= 1.0:
        raise AgentRamenError("importance_score must be between 0 and 1.")
    evidence_rows = _validate_evidence(evidence)
    if _stale_evidence_paths(root, [
        {"path": path, "digest": digest} for path, digest, _commit in evidence_rows
    ]):
        raise AgentRamenError("Memory evidence changed on disk; request fresh context before staging.")
    memory_id = str(uuid.uuid4())
    conn = connect(root)
    try:
        current_digests = dict(conn.execute("SELECT path, digest FROM files"))
        for path, digest, _commit in evidence_rows:
            if current_digests.get(path) != digest:
                raise AgentRamenError(
                    f"Evidence for {path} is stale or not indexed; request fresh context before staging."
                )
        with conn:
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
            conn.executemany(
                "INSERT INTO memory_evidence(memory_id, path, digest, commit_hash) "
                "VALUES (?, ?, ?, ?)",
                [(memory_id, path, digest, commit) for path, digest, commit in evidence_rows],
            )
    finally:
        conn.close()
    return memory_id


def review_queue(root: Path, limit: int = 50) -> list[dict[str, object]]:
    conn = connect(root)
    try:
        rows = conn.execute(
            "SELECT id, subject, content, category, source, importance_score, created_at, "
            "(SELECT COUNT(*) FROM memory_evidence e WHERE e.memory_id=staged_memories.id) "
            "FROM staged_memories WHERE status='pending' "
            "ORDER BY created_at, id LIMIT ?",
            (max(1, min(limit, 500)),),
        ).fetchall()
        stale_rows = conn.execute(
            "SELECT n.id, n.subject, n.content, n.category, n.source, n.importance_score, "
            "n.valid_from, s.reviewed_at, s.reviewed_by "
            "FROM memory_nodes n JOIN staged_memories s ON s.id=n.id "
            "WHERE n.epistemic_status='STALE' ORDER BY n.valid_to DESC, n.subject, n.id LIMIT ?",
            (max(1, min(limit, 500)),),
        ).fetchall()
        evidence_by_memory = {
            memory_id: [
                {"path": path, "digest": digest, "commit": commit}
                for path, digest, commit in conn.execute(
                    "SELECT path, digest, commit_hash FROM memory_evidence "
                    "WHERE memory_id=? ORDER BY path",
                    (memory_id,),
                ).fetchall()
            ]
            for memory_id, *_ in rows
        }
        stale_evidence_by_memory = {
            memory_id: [
                {"path": path, "digest": digest, "commit": commit}
                for path, digest, commit in conn.execute(
                    "SELECT path, digest, commit_hash FROM memory_evidence "
                    "WHERE memory_id=? ORDER BY path",
                    (memory_id,),
                ).fetchall()
            ]
            for memory_id, *_ in stale_rows
        }
    finally:
        conn.close()
    stale_reviewed = []
    for row in stale_rows:
        evidence = stale_evidence_by_memory[row[0]]
        stale_reviewed.append(
            {
                "id": row[0],
                "subject": row[1],
                "content": row[2],
                "category": row[3],
                "source": row[4],
                "importance_score": row[5],
                "created_at": row[6],
                "reviewed_at": row[7],
                "approved_by": row[8],
                "status": "stale",
                "action": "restage_with_fresh_context",
                "evidence_count": len(evidence),
                "evidence": evidence,
                "evidence_current": False,
                "stale_paths": _stale_evidence_paths(root, evidence),
            }
        )
    reviewed = []
    for row in rows:
        evidence = evidence_by_memory[row[0]]
        stale_paths = _stale_evidence_paths(root, evidence)
        reviewed.append(
            {
                "id": row[0],
                "subject": row[1],
                "content": row[2],
                "category": row[3],
                "source": row[4],
                "importance_score": row[5],
                "created_at": row[6],
                "status": "pending",
                "evidence_count": row[7],
                "evidence": evidence,
                "evidence_current": bool(evidence) and not stale_paths,
                "stale_paths": stale_paths,
            }
        )
    return (stale_reviewed + reviewed)[: max(1, min(limit, 500))]


def approve_memory(
    root: Path, memory_id: str, epistemic_status: str = "VERIFIED"
) -> dict[str, object]:
    conn = connect(root)
    reviewed_by = _approval_identity(root)
    try:
        with conn:
            row = conn.execute(
                "SELECT subject, content, category, source, importance_score "
                "FROM staged_memories WHERE id=? AND status='pending'",
                (memory_id,),
            ).fetchone()
            if row is None:
                raise AgentRamenError("Pending memory not found.")
            evidence = [
                {"path": path, "digest": digest}
                for path, digest in conn.execute(
                    "SELECT path, digest FROM memory_evidence WHERE memory_id=?",
                    (memory_id,),
                ).fetchall()
            ]
            if evidence:
                current = dict(conn.execute("SELECT path, digest FROM files"))
                stale_paths = [
                    item["path"] for item in evidence
                    if current.get(item["path"]) != item["digest"]
                    or not _evidence_file_matches(root, item["path"], item["digest"])
                ]
                if stale_paths:
                    raise AgentRamenError(
                        "Memory evidence is stale; refresh context and stage a new proposal: "
                        + ", ".join(sorted(stale_paths))
                    )
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
                "UPDATE staged_memories SET status='approved', reviewed_at=?, reviewed_by=? WHERE id=?",
                (reviewed_at, reviewed_by, memory_id),
            )
        return {
            "id": memory_id,
            "status": "approved",
            "epistemic_status": epistemic_status,
            "approved_by": reviewed_by,
        }
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
        or memory["schema_version"] not in {1, 2, 3}
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
    if memory.get("approved_by") is not None and not isinstance(memory["approved_by"], str):
        raise AgentRamenError(f"Invalid shared memory approver: {path.name}")
    if memory["schema_version"] == 3 and not memory.get("approved_by"):
        raise AgentRamenError(f"Shared memory approver is missing: {path.name}")
    evidence = memory.get("evidence", [])
    if not isinstance(evidence, list):
        raise AgentRamenError(f"Invalid shared memory evidence: {path.name}")
    for item in evidence:
        if not isinstance(item, dict):
            raise AgentRamenError(f"Invalid shared memory evidence: {path.name}")
        evidence_path = item.get("path")
        normalized = PurePosixPath(evidence_path) if isinstance(evidence_path, str) else None
        if (
            normalized is None
            or normalized.is_absolute()
            or not normalized.parts
            or "\\" in evidence_path
            or "\x00" in evidence_path
            or re.match(r"^[a-zA-Z]:", evidence_path)
            or any(part in {"", ".", ".."} for part in normalized.parts)
            or normalized.parts[0] in {".agentramen", ".agentramen-shared"}
            or not isinstance(item.get("digest"), str)
            or not re.fullmatch(r"[0-9a-f]{64}", item["digest"])
            or not isinstance(item.get("commit"), str)
            or not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", item["commit"])
        ):
            raise AgentRamenError(f"Invalid shared memory evidence: {path.name}")
    memory["evidence"] = evidence
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
            "n.epistemic_status, n.valid_from, n.valid_to, n.importance_score, s.reviewed_by "
            "FROM staged_memories s JOIN memory_nodes n ON n.id=s.id WHERE s.id=?",
            (canonical_id,),
        ).fetchone()
        evidence = [
            {"path": path, "digest": digest, "commit": commit}
            for path, digest, commit in conn.execute(
                "SELECT path, digest, commit_hash FROM memory_evidence "
                "WHERE memory_id=? ORDER BY path",
                (canonical_id,),
            ).fetchall()
        ]
    finally:
        conn.close()
    if row is None or row[0] != "approved":
        raise AgentRamenError("Only locally approved memories can be shared.")
    if row[6] is None or row[7] is not None or row[5] in {"SUPERSEDED", "STALE"}:
        raise AgentRamenError("Superseded memories cannot be published as active team knowledge.")
    if not evidence:
        raise AgentRamenError("Memory has no source evidence and cannot be published as team knowledge.")
    if not row[9]:
        raise AgentRamenError("Memory has no recorded approver and cannot be published as team knowledge.")
    stale_paths = _stale_evidence_paths(root, evidence)
    if stale_paths:
        raise AgentRamenError(
            "Memory evidence is stale and cannot be published: " + ", ".join(stale_paths)
        )
    if len(row[1]) > 240 or len(row[2]) > MAX_SHARED_MEMORY_CONTENT_CHARS:
        raise AgentRamenError("Shared memory subject or content exceeds its publication size limit.")
    memory = {
        "schema_version": 3,
        "id": canonical_id,
        "subject": row[1],
        "content": row[2],
        "category": row[3],
        "source": row[4],
        "epistemic_status": row[5],
        "valid_from": row[6],
        "valid_to": row[7],
        "importance_score": row[8],
        "evidence": evidence,
        "approved_by": row[9],
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
            or not memory.get("evidence")
            or not memory.get("approved_by")
            or _stale_evidence_paths(root, memory["evidence"])
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
                "evidence": memory["evidence"],
                "approved_by": memory["approved_by"],
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
