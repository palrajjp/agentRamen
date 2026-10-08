"""Sanitized, immutable repository snapshots for a centralized MCP service."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sqlite3
import stat
import tempfile
import zipfile
from contextlib import contextmanager
from pathlib import Path, PurePosixPath
from typing import Iterator

from .core import AgentRamenError, connect, database_path, git, has_secret
from .memory import _read_shared_memory

SNAPSHOT_FORMAT_VERSION = 1
SNAPSHOT_MANIFEST = "agentramen-snapshot.json"
MAX_SNAPSHOT_ENTRIES = 200_000
MAX_SNAPSHOT_BYTES = 5 * 1024 * 1024 * 1024
MAX_INDEXED_FILE_BYTES = 2_000_000
REPOSITORY_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,127}$")


def _validate_repository_id(repository_id: str) -> str:
    if (
        not REPOSITORY_ID.fullmatch(repository_id)
        or ".." in repository_id
        or "//" in repository_id
    ):
        raise AgentRamenError("repository_id must be a stable owner/repo-style identifier.")
    return repository_id


def _safe_relative_path(value: str) -> PurePosixPath:
    if "\\" in value or "\0" in value:
        raise AgentRamenError("Snapshot contains an invalid file path.")
    path = PurePosixPath(value)
    if path.is_absolute() or not path.parts or any(part in {"", ".", ".."} for part in path.parts):
        raise AgentRamenError("Snapshot contains an unsafe file path.")
    return path


def _working_tree_changes(root: Path) -> list[str]:
    status = git(root, "status", "--porcelain", "--untracked-files=all")
    changes = []
    for line in status.splitlines():
        state, _, path = line.partition(" ")
        path = path.strip()
        if state == "??" and (
            path in {".agentramen.yml", ".agentramenignore", ".agentramen"}
            or path.startswith(".agentramen/")
        ):
            continue
        changes.append(line)
    return changes


def export_snapshot(root: Path, repository_id: str, output: Path) -> dict[str, object]:
    """Create a sanitized snapshot archive from a clean indexed Git revision."""
    root = root.resolve()
    repository_id = _validate_repository_id(repository_id)
    output = output.expanduser().resolve()
    head = git(root, "rev-parse", "--verify", "HEAD", check=False).strip()
    if not head:
        raise AgentRamenError("Cannot snapshot a repository without a Git commit.")
    changes = _working_tree_changes(root)
    if changes:
        raise AgentRamenError(
            "Snapshot export requires a clean checkout; commit or discard source changes first."
        )

    db_path = database_path(root)
    if not db_path.is_file():
        raise AgentRamenError("No AgentRamen index exists; run agentramen index before snapshot export.")
    conn = connect(root)
    try:
        indexed_head = conn.execute(
            "SELECT value FROM metadata WHERE key='last_commit'"
        ).fetchone()
        rows = conn.execute("SELECT path, digest FROM files ORDER BY path").fetchall()
        if indexed_head is None or indexed_head[0] != head:
            raise AgentRamenError("Index is stale; run agentramen index before snapshot export.")
        with tempfile.TemporaryDirectory(prefix="agentramen-snapshot-db-") as temporary:
            database_copy = Path(temporary) / "graph.db"
            backup = sqlite3.connect(database_copy)
            try:
                conn.backup(backup)
                with backup:
                    for table in (
                        "staged_memories",
                        "memory_nodes",
                        "context_exclusion_rules",
                    ):
                        backup.execute(f"DELETE FROM {table}")
            finally:
                backup.close()
            database_bytes = database_copy.read_bytes()
    finally:
        conn.close()

    source_files: list[tuple[str, bytes, str]] = []
    for relative, digest in rows:
        safe_path = _safe_relative_path(relative)
        source_path = root.joinpath(*safe_path.parts)
        if source_path.is_symlink() or not source_path.is_file():
            raise AgentRamenError(f"Indexed source file is missing or unsafe: {relative}")
        data = source_path.read_bytes()
        if len(data) > MAX_INDEXED_FILE_BYTES or hashlib.sha256(data).hexdigest() != digest:
            raise AgentRamenError(f"Indexed source changed; reindex before snapshot export: {relative}")
        text = data.decode("utf-8", errors="replace")
        if has_secret(relative, text):
            raise AgentRamenError(f"Credential-like content detected; refusing snapshot: {relative}")
        source_files.append((safe_path.as_posix(), data, digest))

    auxiliary_files: list[tuple[str, bytes]] = []
    for name in (".agentramen.yml", ".agentramenignore"):
        path = root / name
        if path.is_file():
            data = path.read_bytes()
            text = data.decode("utf-8", errors="replace")
            if has_secret(name, text):
                raise AgentRamenError(f"Credential-like content detected; refusing snapshot: {name}")
            auxiliary_files.append((name, data))

    memories_dir = root / ".agentramen-shared" / "memories"
    shared_memories = []
    if memories_dir.exists():
        if memories_dir.is_symlink() or not memories_dir.is_dir():
            raise AgentRamenError("Shared memories path must be a real directory.")
        for path in sorted(memories_dir.glob("*.json")):
            memory = _read_shared_memory(path)
            relative = path.relative_to(root).as_posix()
            shared_memories.append((relative, path.read_bytes(), str(memory["id"])))

    manifest = {
        "format_version": SNAPSHOT_FORMAT_VERSION,
        "repository_id": repository_id,
        "commit": head,
        "graph_sha256": hashlib.sha256(database_bytes).hexdigest(),
        "indexed_files": [
            {"path": path, "sha256": digest} for path, _, digest in source_files
        ],
        "shared_memory_ids": [memory_id for _, _, memory_id in shared_memories],
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        prefix=f".{output.name}.", suffix=".tmp", dir=output.parent, delete=False
    ) as temporary:
        temporary_path = Path(temporary.name)
    try:
        with zipfile.ZipFile(temporary_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr(
                SNAPSHOT_MANIFEST,
                json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            )
            archive.writestr(".agentramen/graph.db", database_bytes)
            for name, data in auxiliary_files:
                archive.writestr(name, data)
            for relative, data, _ in source_files:
                archive.writestr(relative, data)
            for relative, data, _ in shared_memories:
                archive.writestr(relative, data)
        os.replace(temporary_path, output)
    finally:
        temporary_path.unlink(missing_ok=True)
    return {
        "repository_id": repository_id,
        "commit": head,
        "indexed_files": len(source_files),
        "shared_memories": len(shared_memories),
        "archive": str(output),
        "bytes": output.stat().st_size,
    }


@contextmanager
def mounted_snapshot(
    archive_path: Path, expected_repository_id: str
) -> Iterator[tuple[Path, dict[str, object]]]:
    """Validate and mount a snapshot in a private temporary directory."""
    expected_repository_id = _validate_repository_id(expected_repository_id)
    archive_path = archive_path.expanduser().resolve()
    try:
        archive = zipfile.ZipFile(archive_path)
    except (OSError, zipfile.BadZipFile) as exc:
        raise AgentRamenError("Could not open AgentRamen snapshot archive.") from exc

    with archive:
        infos = archive.infolist()
        if not infos or len(infos) > MAX_SNAPSHOT_ENTRIES:
            raise AgentRamenError("Snapshot archive has an invalid number of entries.")
        if sum(info.file_size for info in infos) > MAX_SNAPSHOT_BYTES:
            raise AgentRamenError("Snapshot archive exceeds the configured size limit.")
        names = [info.filename for info in infos]
        if len(names) != len(set(names)) or SNAPSHOT_MANIFEST not in names:
            raise AgentRamenError("Snapshot archive is missing its manifest or has duplicate paths.")
        for info in infos:
            _safe_relative_path(info.filename)
            if info.filename == SNAPSHOT_MANIFEST and info.file_size > 1024 * 1024:
                raise AgentRamenError("Snapshot manifest exceeds the size limit.")
            mode = info.external_attr >> 16
            if stat.S_ISLNK(mode):
                raise AgentRamenError("Snapshot archives cannot contain symbolic links.")

        try:
            manifest = json.loads(archive.read(SNAPSHOT_MANIFEST))
        except (json.JSONDecodeError, KeyError) as exc:
            raise AgentRamenError("Snapshot manifest is invalid.") from exc
        if (
            not isinstance(manifest, dict)
            or manifest.get("format_version") != SNAPSHOT_FORMAT_VERSION
            or manifest.get("repository_id") != expected_repository_id
            or not isinstance(manifest.get("commit"), str)
            or not isinstance(manifest.get("indexed_files"), list)
            or not isinstance(manifest.get("shared_memory_ids"), list)
        ):
            raise AgentRamenError("Snapshot manifest does not match this repository or format.")

        indexed_files = {}
        for item in manifest["indexed_files"]:
            if (
                not isinstance(item, dict)
                or not isinstance(item.get("path"), str)
                or not isinstance(item.get("sha256"), str)
            ):
                raise AgentRamenError("Snapshot manifest contains an invalid indexed file entry.")
            path = _safe_relative_path(item["path"]).as_posix()
            if path in indexed_files or path.startswith((".agentramen/", ".agentramen-shared/")):
                raise AgentRamenError("Snapshot manifest contains duplicate or reserved source paths.")
            indexed_files[path] = item["sha256"]

        shared_paths = {
            f".agentramen-shared/memories/{memory_id}.json"
            for memory_id in manifest["shared_memory_ids"]
            if isinstance(memory_id, str)
        }
        allowed = {
            SNAPSHOT_MANIFEST,
            ".agentramen/graph.db",
            *indexed_files,
            *shared_paths,
        }
        allowed.update(set(names) & {".agentramen.yml", ".agentramenignore"})
        if set(names) != allowed:
            raise AgentRamenError("Snapshot archive contains unlisted files or is missing required data.")

        with tempfile.TemporaryDirectory(prefix="agentramen-central-snapshot-") as temporary:
            mounted_root = Path(temporary)
            for info in infos:
                if info.filename == SNAPSHOT_MANIFEST:
                    continue
                relative = _safe_relative_path(info.filename)
                if info.file_size > MAX_INDEXED_FILE_BYTES and info.filename in indexed_files:
                    raise AgentRamenError(f"Snapshot source file is too large: {info.filename}")
                target = mounted_root.joinpath(*relative.parts)
                target.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(info) as source, target.open("xb") as destination:
                    shutil.copyfileobj(source, destination, length=1024 * 1024)

            for relative, expected_digest in indexed_files.items():
                data = mounted_root.joinpath(*PurePosixPath(relative).parts).read_bytes()
                if hashlib.sha256(data).hexdigest() != expected_digest:
                    raise AgentRamenError(f"Snapshot source hash mismatch: {relative}")
                if has_secret(relative, data.decode("utf-8", errors="replace")):
                    raise AgentRamenError(f"Snapshot contains credential-like source: {relative}")

            graph_path = mounted_root / ".agentramen" / "graph.db"
            if hashlib.sha256(graph_path.read_bytes()).hexdigest() != manifest.get("graph_sha256"):
                raise AgentRamenError("Snapshot graph database hash mismatch.")
            try:
                database = sqlite3.connect(f"file:{graph_path}?mode=ro", uri=True)
                graph_head = database.execute(
                    "SELECT value FROM metadata WHERE key='last_commit'"
                ).fetchone()
                graph_files = dict(database.execute("SELECT path, digest FROM files"))
                local_memory_rows = database.execute(
                    "SELECT (SELECT COUNT(*) FROM staged_memories) + "
                    "(SELECT COUNT(*) FROM memory_nodes) + "
                    "(SELECT COUNT(*) FROM context_exclusion_rules)"
                ).fetchone()[0]
                database.close()
            except sqlite3.Error as exc:
                raise AgentRamenError("Snapshot graph database is invalid.") from exc
            if graph_head is None or graph_head[0] != manifest["commit"]:
                raise AgentRamenError("Snapshot graph commit does not match its manifest.")
            if graph_files != indexed_files:
                raise AgentRamenError("Snapshot graph file manifest does not match its source files.")
            if local_memory_rows:
                raise AgentRamenError("Snapshot graph contains private local memory or exclusion data.")

            for relative in shared_paths:
                path = mounted_root / relative
                memory = _read_shared_memory(path)
                if str(memory["id"]) != path.stem:
                    raise AgentRamenError(f"Snapshot shared-memory id mismatch: {path.name}")
            yield mounted_root, manifest
