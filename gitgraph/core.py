from __future__ import annotations

import fnmatch
import hashlib
import json
import os
import posixpath
import re
import sqlite3
import subprocess
from dataclasses import dataclass
from collections import Counter
from pathlib import Path
from typing import Iterable

from .analyzers import analyze, module_candidates

DEFAULT_IGNORES = (
    ".git/",
    ".gitgraph/",
    ".gitgraph.yml",
    ".gitgraphignore",
    ".gitgraph-action/",
    "node_modules/",
    "dist/",
    "build/",
    "vendor/",
    ".venv/",
    "venv/",
    "__pycache__/",
    ".env",
    ".env.*",
    "*.pem",
    "*.key",
    "*.p12",
    "*.pfx",
    "secrets/",
    "credentials/",
)
SECRET_LINE = re.compile(
    r"(?:-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----|"
    r"(?:api[_-]?key|secret|password|token)\s*[:=]\s*['\"]?[A-Za-z0-9_./+=-]{12,})",
    re.IGNORECASE,
)
SYMBOL = re.compile(
    r"^\s*(?:export\s+)?(?:async\s+)?(?:class|interface|type|enum|function|def|fn|func)\s+([A-Za-z_$][\w$]*)",
    re.MULTILINE,
)
IMPORT = re.compile(
    r"""^\s*(?:import\s+(?:.*?\s+from\s+)?|from\s+[\w.]+\s+import\s+|(?:const|let|var)\s+\w+\s*=\s*require\()\s*['"]([^'"]+)['"]""",
    re.MULTILINE,
)

LANGUAGES = {
    ".c": "c", ".cc": "cpp", ".cpp": "cpp", ".cs": "csharp",
    ".go": "go", ".h": "c", ".hpp": "cpp", ".java": "java",
    ".js": "javascript", ".jsx": "javascript", ".kt": "kotlin",
    ".php": "php", ".py": "python", ".rb": "ruby", ".rs": "rust",
    ".swift": "swift", ".ts": "typescript", ".tsx": "typescript",
}
INDEX_VERSION = "3"
DEFAULT_CONTEXT_BUDGET = 2000
DEFAULT_HISTORY_COMMITS = 100


class GitGraphError(RuntimeError):
    pass


@dataclass(frozen=True)
class RepositoryConfig:
    incremental: bool = True
    history_enabled: bool = True
    co_changes_enabled: bool = True
    history_commits: int = DEFAULT_HISTORY_COMMITS
    default_budget: int = DEFAULT_CONTEXT_BUDGET
    ignore: tuple[str, ...] = ()


def _config_scalar(value: str):
    value = value.strip()
    if value.lower() in ("true", "false"):
        return value.lower() == "true"
    if value.isdigit():
        return int(value)
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
        return value[1:-1]
    return value


def load_config(root: Path) -> RepositoryConfig:
    """Read the simple, dependency-free YAML subset generated/documented by GitGraph."""
    config_path = root / ".gitgraph.yml"
    if not config_path.is_file():
        return RepositoryConfig()

    document: dict[str, object] = {}
    stack: list[tuple[int, dict[str, object] | list[object]]] = [(-1, document)]
    for line_number, raw_line in enumerate(
        config_path.read_text(encoding="utf-8", errors="replace").splitlines(), start=1
    ):
        if not raw_line.strip() or raw_line.lstrip().startswith("#"):
            continue
        indent = len(raw_line) - len(raw_line.lstrip())
        text = raw_line.strip()
        while len(stack) > 1 and indent <= stack[-1][0]:
            stack.pop()
        parent = stack[-1][1]
        if text.startswith("- "):
            if not isinstance(parent, list):
                raise GitGraphError(
                    f"Invalid configuration at line {line_number}: list item without a list."
                )
            parent.append(_config_scalar(text[2:]))
            continue
        if ":" not in text or not isinstance(parent, dict):
            raise GitGraphError(f"Invalid configuration at line {line_number}.")
        key, raw_value = text.split(":", 1)
        key = key.strip()
        if not key:
            raise GitGraphError(f"Invalid configuration key at line {line_number}.")
        if not raw_value.strip():
            child: dict[str, object] | list[object] = [] if key == "ignore" else {}
            parent[key] = child
            stack.append((indent, child))
        else:
            parent[key] = _config_scalar(raw_value)

    def mapping(parent: dict[str, object], key: str) -> dict[str, object]:
        value = parent.get(key, {})
        if not isinstance(value, dict):
            raise GitGraphError(f"Configuration '{key}' must be a mapping.")
        return value

    indexing = mapping(document, "indexing")
    git_options = mapping(document, "git")
    history = mapping(document, "history")
    context = mapping(document, "context")
    ignore = document.get("ignore", [])
    if not isinstance(ignore, list) or any(not isinstance(item, str) for item in ignore):
        raise GitGraphError("Configuration 'ignore' must be a list of patterns.")

    incremental = indexing.get("incremental", True)
    history_enabled = git_options.get("history", True)
    co_changes_enabled = git_options.get("co_changes", True)
    history_commits = history.get("commits", DEFAULT_HISTORY_COMMITS)
    default_budget = context.get("default_budget", DEFAULT_CONTEXT_BUDGET)
    for name, value in (
        ("indexing.incremental", incremental),
        ("git.history", history_enabled),
        ("git.co_changes", co_changes_enabled),
    ):
        if not isinstance(value, bool):
            raise GitGraphError(f"Configuration '{name}' must be true or false.")
    for name, value, minimum, maximum in (
        ("history.commits", history_commits, 1, 100_000),
        ("context.default_budget", default_budget, 100, 100_000),
    ):
        if not isinstance(value, int) or isinstance(value, bool) or not minimum <= value <= maximum:
            raise GitGraphError(
                f"Configuration '{name}' must be an integer from {minimum} to {maximum}."
            )
    return RepositoryConfig(
        incremental=incremental,
        history_enabled=history_enabled,
        co_changes_enabled=co_changes_enabled,
        history_commits=history_commits,
        default_budget=default_budget,
        ignore=tuple(ignore),
    )


def git(root: Path, *args: str, check: bool = True) -> str:
    try:
        result = subprocess.run(
            ["git", "-C", str(root), *args],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    except FileNotFoundError as exc:
        raise GitGraphError("Git is required but was not found on PATH.") from exc
    if check and result.returncode:
        raise GitGraphError(result.stderr.strip() or "Git command failed.")
    return result.stdout


def _is_ancestor(root: Path, ancestor: str, descendant: str) -> bool:
    try:
        result = subprocess.run(
            ["git", "-C", str(root), "merge-base", "--is-ancestor", ancestor, descendant],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except FileNotFoundError as exc:
        raise GitGraphError("Git is required but was not found on PATH.") from exc
    return result.returncode == 0


def find_root(path: Path | None = None) -> Path:
    path = (path or Path.cwd()).resolve()
    root = git(path, "rev-parse", "--show-toplevel", check=False).strip()
    if not root:
        raise GitGraphError(f"{path} is not inside a Git repository.")
    return Path(root)


def database_path(root: Path) -> Path:
    return root / ".gitgraph" / "graph.db"


def connect(root: Path) -> sqlite3.Connection:
    db_path = database_path(root)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS files (
            path TEXT PRIMARY KEY,
            digest TEXT NOT NULL,
            language TEXT,
            bytes INTEGER NOT NULL,
            symbols TEXT NOT NULL DEFAULT '[]',
            imports TEXT NOT NULL DEFAULT '[]',
            calls TEXT NOT NULL DEFAULT '[]'
        );
        CREATE TABLE IF NOT EXISTS commits (
            hash TEXT PRIMARY KEY,
            author TEXT NOT NULL,
            subject TEXT NOT NULL,
            committed_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS commit_files (
            commit_hash TEXT NOT NULL,
            path TEXT NOT NULL,
            PRIMARY KEY (commit_hash, path)
        );
        CREATE TABLE IF NOT EXISTS renames (
            commit_hash TEXT NOT NULL,
            old_path TEXT NOT NULL,
            new_path TEXT NOT NULL,
            PRIMARY KEY (commit_hash, old_path, new_path)
        );
        CREATE TABLE IF NOT EXISTS metadata (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS graph_nodes (
            id TEXT PRIMARY KEY,
            type TEXT NOT NULL,
            name TEXT NOT NULL,
            path TEXT,
            metadata TEXT NOT NULL DEFAULT '{}'
        );
        CREATE TABLE IF NOT EXISTS graph_edges (
            source TEXT NOT NULL,
            target TEXT NOT NULL,
            type TEXT NOT NULL,
            weight REAL NOT NULL DEFAULT 1,
            metadata TEXT NOT NULL DEFAULT '{}',
            PRIMARY KEY(source, target, type)
        );
        CREATE TABLE IF NOT EXISTS file_imports (
            importer TEXT NOT NULL,
            import_name TEXT NOT NULL,
            PRIMARY KEY(importer, import_name)
        );
        CREATE INDEX IF NOT EXISTS file_imports_by_name ON file_imports(import_name);
        """
    )
    file_columns = {row[1] for row in conn.execute("PRAGMA table_info(files)")}
    if "calls" not in file_columns:
        conn.execute("ALTER TABLE files ADD COLUMN calls TEXT NOT NULL DEFAULT '[]'")
    return conn


def _ignore_patterns(root: Path) -> list[str]:
    patterns = [*DEFAULT_IGNORES, *load_config(root).ignore]
    ignore_file = root / ".gitgraphignore"
    if ignore_file.is_file():
        patterns.extend(
            line.strip()
            for line in ignore_file.read_text(encoding="utf-8", errors="replace").splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        )
    return patterns


def is_ignored(path: str, patterns: Iterable[str]) -> bool:
    normalized = path.replace(os.sep, "/")
    parts = normalized.split("/")
    for pattern in patterns:
        pattern = pattern.strip().lstrip("/")
        if not pattern:
            continue
        if pattern.endswith("/"):
            directory = pattern[:-1]
            if normalized == directory or normalized.startswith(directory + "/") or any(
                part == directory for part in parts[:-1]
            ):
                return True
        elif fnmatch.fnmatch(normalized, pattern) or fnmatch.fnmatch(Path(normalized).name, pattern):
            return True
    return False


def _tracked_and_new_paths(root: Path) -> set[str]:
    raw = git(root, "ls-files", "-z", "--cached", "--others", "--exclude-standard")
    return {name.decode("utf-8", errors="replace") for name in raw.encode().split(b"\0") if name}


def _commit_history(
    root: Path,
    conn: sqlite3.Connection,
    last_commit: str | None,
    history_enabled: bool,
    history_commits: int,
) -> None:
    head = git(root, "rev-parse", "--verify", "HEAD", check=False).strip()
    previous_settings = dict(
        conn.execute(
            "SELECT key, value FROM metadata WHERE key IN "
            "('history_enabled', 'history_commits')"
        )
    )
    settings_changed = (
        previous_settings.get("history_enabled") != str(history_enabled).lower()
        or previous_settings.get("history_commits") != str(history_commits)
    )
    if not history_enabled:
        conn.execute("DELETE FROM commit_files")
        conn.execute("DELETE FROM renames")
        conn.execute("DELETE FROM commits")
        conn.execute(
            "DELETE FROM graph_edges WHERE type IN ('CHANGED_IN', 'RENAMED_IN', 'CO_CHANGED_WITH')"
        )
        conn.execute("DELETE FROM graph_nodes WHERE type='Commit'")
        conn.execute(
            "DELETE FROM graph_nodes WHERE type='File' AND id NOT IN "
            "(SELECT 'file:' || path FROM files)"
        )
        if head:
            conn.execute(
                "INSERT INTO metadata(key, value) VALUES('last_commit', ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (head,),
            )
        conn.execute(
            "INSERT INTO metadata(key, value) VALUES('history_enabled', 'false') "
            "ON CONFLICT(key) DO UPDATE SET value='false'"
        )
        conn.execute(
            "INSERT INTO metadata(key, value) VALUES('history_commits', ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (str(history_commits),),
        )
        return
    if not head:
        return
    full_scan = not last_commit or (
        last_commit != head and not _is_ancestor(root, last_commit, head)
    ) or settings_changed
    if full_scan:
        conn.execute("DELETE FROM commit_files")
        conn.execute("DELETE FROM renames")
        conn.execute("DELETE FROM commits")
        conn.execute(
            "DELETE FROM graph_edges WHERE type IN ('CHANGED_IN', 'RENAMED_IN', 'CO_CHANGED_WITH')"
        )
        conn.execute("DELETE FROM graph_nodes WHERE type='Commit'")
        revisions = git(
            root,
            "log",
            "-n",
            str(history_commits),
            "--format=%H%x09%an%x09%s%x09%cI",
        ).splitlines()
        revisions.reverse()
    elif last_commit == head:
        revisions = []
    else:
        revisions = git(
            root,
            "log",
            "--reverse",
            f"{last_commit}..{head}",
            "--format=%H%x09%an%x09%s%x09%cI",
        ).splitlines()
    for row in revisions:
        parts = row.split("\t", 3)
        if len(parts) != 4:
            continue
        commit_hash, author, subject, committed_at = parts
        conn.execute(
            "INSERT OR IGNORE INTO commits(hash, author, subject, committed_at) VALUES (?, ?, ?, ?)",
            (commit_hash, author, subject, committed_at),
        )
        changes = git(
            root,
            "diff-tree",
            "--root",
            "--no-commit-id",
            "--name-status",
            "-M",
            "-r",
            "-z",
            commit_hash,
        )
        fields = changes.split("\0")
        paths: list[str] = []
        index = 0
        while index < len(fields) and fields[index]:
            status = fields[index]
            index += 1
            if status.startswith(("R", "C")) and index + 1 < len(fields):
                old_path, new_path = fields[index], fields[index + 1]
                paths.extend((old_path, new_path))
                if status.startswith("R"):
                    conn.execute(
                        "INSERT OR IGNORE INTO renames(commit_hash, old_path, new_path) "
                        "VALUES (?, ?, ?)",
                        (commit_hash, old_path, new_path),
                    )
                    conn.execute(
                        "INSERT OR REPLACE INTO graph_edges(source, target, type) "
                        "VALUES (?, ?, 'RENAMED_IN')",
                        (f"file:{old_path}", f"file:{new_path}"),
                    )
                index += 2
            elif index < len(fields):
                paths.append(fields[index])
                index += 1
        for path in paths:
            if path:
                conn.execute(
                    "INSERT OR IGNORE INTO commit_files(commit_hash, path) VALUES (?, ?)",
                    (commit_hash, path),
                )
        conn.execute(
            "INSERT OR REPLACE INTO graph_nodes(id, type, name, metadata) "
            "VALUES (?, 'Commit', ?, ?)",
            (
                f"commit:{commit_hash}",
                parts[2],
                json.dumps({"author": author, "committed_at": committed_at}),
            ),
        )
        for path in paths:
            if path:
                conn.execute(
                    "INSERT OR IGNORE INTO graph_nodes(id, type, name, path, metadata) "
                    "VALUES (?, 'File', ?, ?, '{\"historical\": true}')",
                    (f"file:{path}", path, path),
                )
                conn.execute(
                    "INSERT OR IGNORE INTO graph_edges(source, target, type) "
                    "VALUES (?, ?, 'CHANGED_IN')",
                    (f"file:{path}", f"commit:{commit_hash}"),
                )
    conn.execute(
        "INSERT INTO metadata(key, value) VALUES('last_commit', ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (head,),
    )
    conn.execute(
        "DELETE FROM commits WHERE hash NOT IN "
        "(SELECT hash FROM commits ORDER BY committed_at DESC LIMIT ?)",
        (history_commits,),
    )
    conn.execute(
        "DELETE FROM commit_files WHERE commit_hash NOT IN (SELECT hash FROM commits)"
    )
    conn.execute("DELETE FROM renames WHERE commit_hash NOT IN (SELECT hash FROM commits)")
    conn.execute(
        "DELETE FROM graph_edges WHERE type='CHANGED_IN' "
        "AND target NOT IN (SELECT 'commit:' || hash FROM commits)"
    )
    conn.execute("DELETE FROM graph_nodes WHERE type='Commit' AND id NOT IN "
                 "(SELECT 'commit:' || hash FROM commits)")
    conn.execute("DELETE FROM graph_edges WHERE type='RENAMED_IN'")
    for old_path, new_path in conn.execute(
        "SELECT DISTINCT old_path, new_path FROM renames"
    ).fetchall():
        conn.execute(
            "INSERT OR REPLACE INTO graph_edges(source, target, type) "
            "VALUES (?, ?, 'RENAMED_IN')",
            (f"file:{old_path}", f"file:{new_path}"),
        )
    conn.execute(
        "DELETE FROM graph_nodes WHERE type='File' AND id NOT IN "
        "(SELECT 'file:' || path FROM files) "
        "AND id NOT IN ("
        "SELECT source FROM graph_edges WHERE type IN ('CHANGED_IN', 'RENAMED_IN') "
        "UNION SELECT target FROM graph_edges WHERE type IN ('CHANGED_IN', 'RENAMED_IN'))"
    )
    conn.execute(
        "INSERT INTO metadata(key, value) VALUES('history_enabled', 'true') "
        "ON CONFLICT(key) DO UPDATE SET value='true'"
    )
    conn.execute(
        "INSERT INTO metadata(key, value) VALUES('history_commits', ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (str(history_commits),),
    )


def _sync_graph(conn: sqlite3.Connection, changed_paths: set[str]) -> None:
    existing_changed = set()
    for path in changed_paths:
        file_id = f"file:{path}"
        conn.execute(
            "DELETE FROM graph_edges WHERE source=? AND type IN ('CONTAINS', 'CALLS', 'DEPENDS_ON')",
            (file_id,),
        )
        conn.execute("DELETE FROM file_imports WHERE importer=?", (path,))
        conn.execute("DELETE FROM graph_nodes WHERE path=?", (path,))
        row = conn.execute(
            "SELECT language, bytes, symbols, imports, calls FROM files WHERE path=?",
            (path,),
        ).fetchone()
        if row is None:
            conn.execute(
                "DELETE FROM graph_edges WHERE target=? AND type IN ('DEPENDS_ON', 'CALLS')",
                (file_id,),
            )
            historic = conn.execute(
                "SELECT 1 FROM graph_edges WHERE type IN ('CHANGED_IN', 'RENAMED_IN') "
                "AND (source=? OR target=?) LIMIT 1",
                (file_id, file_id),
            ).fetchone()
            if historic:
                conn.execute(
                    "INSERT OR IGNORE INTO graph_nodes(id, type, name, path, metadata) "
                    "VALUES (?, 'File', ?, ?, '{\"deleted\": true}')",
                    (file_id, path, path),
                )
            continue
        existing_changed.add(path)
        language, size, raw_symbols, raw_imports, raw_calls = row
        imports = json.loads(raw_imports)
        conn.executemany(
            "INSERT OR IGNORE INTO file_imports(importer, import_name) VALUES (?, ?)",
            [(path, import_name) for import_name in imports],
        )
        conn.execute(
            "INSERT OR REPLACE INTO graph_nodes(id, type, name, path, metadata) "
            "VALUES (?, 'File', ?, ?, ?)",
            (file_id, path, path, json.dumps({"language": language, "bytes": size})),
        )
        for symbol in json.loads(raw_symbols):
            symbol_id = f"symbol:{path}::{symbol}"
            conn.execute(
                "INSERT OR REPLACE INTO graph_nodes(id, type, name, path) VALUES (?, 'Symbol', ?, ?)",
                (symbol_id, symbol, path),
            )
            conn.execute(
                "INSERT OR REPLACE INTO graph_edges(source, target, type) VALUES (?, ?, 'CONTAINS')",
                (file_id, symbol_id),
            )
        for call in json.loads(raw_calls):
            conn.execute(
                "INSERT OR IGNORE INTO graph_nodes(id, type, name, path) "
                "VALUES (?, 'SymbolReference', ?, ?)",
                (f"reference:{path}::{call}", call, path),
            )
    conn.execute("DELETE FROM graph_edges WHERE type='CALLS'")
    references = conn.execute(
        "SELECT path, name FROM graph_nodes WHERE type='SymbolReference' ORDER BY path, name"
    ).fetchall()
    for path, call in references:
        targets = conn.execute(
            "SELECT id FROM graph_nodes WHERE type='Symbol' AND "
            "(name=? OR name LIKE ? OR name LIKE ?) ORDER BY id LIMIT 20",
            (call, f"%.{call}", f"%::{call}"),
        ).fetchall()
        for (target_id,) in targets:
            if target_id != f"symbol:{path}::{call}":
                conn.execute(
                    "INSERT OR IGNORE INTO graph_edges(source, target, type) "
                    "VALUES (?, ?, 'CALLS')",
                    (f"file:{path}", target_id),
                )
    candidates_by_source: list[tuple[str, set[str]]] = []
    candidate_paths: set[str] = set()
    for source_path in existing_changed:
        imports = [
            row[0] for row in conn.execute(
                "SELECT import_name FROM file_imports WHERE importer=?", (source_path,)
            )
        ]
        candidates = set()
        for import_name in imports:
            candidates.update(module_candidates(import_name, source_path))
        candidates.discard(source_path)
        candidates_by_source.append((source_path, candidates))
        candidate_paths.update(candidates)
    available = set()
    candidates = sorted(candidate_paths)
    for offset in range(0, len(candidates), 900):
        batch = candidates[offset : offset + 900]
        if not batch:
            continue
        placeholders = ",".join("?" for _ in batch)
        available.update(
            row[0] for row in conn.execute(
                f"SELECT path FROM files WHERE path IN ({placeholders})", batch
            )
        )
    for source_path, candidates in candidates_by_source:
        for target_path in candidates & available:
            conn.execute(
                "INSERT OR IGNORE INTO graph_edges(source, target, type, weight) "
                "VALUES (?, ?, 'DEPENDS_ON', 1)",
                (f"file:{source_path}", f"file:{target_path}"),
            )

    changed_aliases = set()
    for path in existing_changed:
        module = posixpath.splitext(path)[0]
        if posixpath.basename(module) in ("__init__", "index"):
            module = posixpath.dirname(module)
        parts = module.split("/")
        changed_aliases.update(".".join(parts[index:]) for index in range(len(parts)))
    if changed_aliases:
        aliases = sorted(changed_aliases)
        for offset in range(0, len(aliases), 900):
            batch = aliases[offset : offset + 900]
            placeholders = ",".join("?" for _ in batch)
            rows = conn.execute(
                f"SELECT importer, import_name FROM file_imports "
                f"WHERE import_name IN ({placeholders})",
                batch,
            ).fetchall()
            for importer, import_name in rows:
                for target_path in existing_changed:
                    if target_path in module_candidates(import_name, importer, {target_path}):
                        conn.execute(
                            "INSERT OR IGNORE INTO graph_edges(source, target, type, weight) "
                            "VALUES (?, ?, 'DEPENDS_ON', 1)",
                            (f"file:{importer}", f"file:{target_path}"),
                        )
        relative_imports = conn.execute(
            "SELECT importer, import_name FROM file_imports WHERE import_name LIKE '.%'"
        ).fetchall()
        for importer, import_name in relative_imports:
            for target_path in existing_changed:
                if target_path in module_candidates(import_name, importer, {target_path}):
                    conn.execute(
                        "INSERT OR IGNORE INTO graph_edges(source, target, type, weight) "
                        "VALUES (?, ?, 'DEPENDS_ON', 1)",
                        (f"file:{importer}", f"file:{target_path}"),
                    )


def _sync_cochanges(conn: sqlite3.Connection, enabled: bool, history_commits: int) -> None:
    conn.execute("DELETE FROM graph_edges WHERE type='CO_CHANGED_WITH'")
    if not enabled:
        return
    commit_rows = conn.execute(
        "SELECT commit_hash, path FROM commit_files "
        "WHERE commit_hash IN "
        "(SELECT hash FROM commits ORDER BY committed_at DESC LIMIT ?) "
        "ORDER BY commit_hash, path"
        ,
        (history_commits,),
    ).fetchall()
    by_commit: dict[str, list[str]] = {}
    for commit_hash, path in commit_rows:
        by_commit.setdefault(commit_hash, []).append(path)
    weights: Counter[tuple[str, str]] = Counter()
    for paths in by_commit.values():
        paths = sorted(set(paths))[:40]
        for index, left in enumerate(paths):
            for right in paths[index + 1 :]:
                weights[(left, right)] += 1
    for (left, right), count in weights.items():
        conn.execute(
            "INSERT OR REPLACE INTO graph_edges(source, target, type, weight) "
            "VALUES (?, ?, 'CO_CHANGED_WITH', ?)",
            (f"file:{left}", f"file:{right}", float(count)),
        )
        conn.execute(
            "INSERT OR REPLACE INTO graph_edges(source, target, type, weight) "
            "VALUES (?, ?, 'CO_CHANGED_WITH', ?)",
            (f"file:{right}", f"file:{left}", float(count)),
        )


def index_repository(root: Path) -> dict[str, int]:
    root = root.resolve()
    config = load_config(root)
    conn = connect(root)
    patterns = _ignore_patterns(root)
    previous = dict(conn.execute("SELECT path, digest FROM files"))
    changed_graph_paths: set[str] = set()
    version_row = conn.execute("SELECT value FROM metadata WHERE key='index_version'").fetchone()
    force_reindex = version_row is None or version_row[0] != INDEX_VERSION
    last_commit_row = conn.execute("SELECT value FROM metadata WHERE key='last_commit'").fetchone()
    last_commit = last_commit_row[0] if last_commit_row else None
    head = git(root, "rev-parse", "--verify", "HEAD", check=False).strip()
    is_descendant = not last_commit or not head or last_commit == head or _is_ancestor(root, last_commit, head)
    if not is_descendant:
        changed_graph_paths.update(previous)
        previous = {}
        conn.execute("DELETE FROM files")
    changed_by_commits: set[str] = set()
    if last_commit and head and last_commit != head and is_descendant:
        names = git(root, "diff", "--name-only", "-z", f"{last_commit}..{head}")
        changed_by_commits.update(name for name in names.split("\0") if name)
    status_names = git(root, "status", "--porcelain", "-z", "--untracked-files=all")
    working_changed = set()
    entries = status_names.split("\0")
    i = 0
    while i < len(entries) and entries[i]:
        entry = entries[i]
        path = entry[3:] if len(entry) >= 4 else ""
        if path:
            working_changed.add(path)
        i += 1
        if ("R" in entry[:2] or "C" in entry[:2]) and i < len(entries):
            working_changed.add(entries[i])
            i += 1
    if force_reindex or not config.incremental or not last_commit or not is_descendant:
        to_check = _tracked_and_new_paths(root)
    else:
        to_check = changed_by_commits | working_changed
        # New untracked files are included even when Git status did not expose them as modified.
        to_check.update(_tracked_and_new_paths(root) - set(previous))
    to_check = {p for p in to_check if not is_ignored(p, patterns)}
    tracked_after = _tracked_and_new_paths(root)
    deleted = {
        path for path in previous
        if path not in tracked_after or is_ignored(path, patterns)
        or path in changed_by_commits | working_changed
    }
    updated = removed = 0
    with conn:
        _commit_history(
            root,
            conn,
            last_commit,
            config.history_enabled,
            config.history_commits,
        )
        for path in deleted:
            if is_ignored(path, patterns) or path not in tracked_after or not (root / path).is_file():
                conn.execute("DELETE FROM files WHERE path=?", (path,))
                changed_graph_paths.add(path)
                removed += 1
        for path in sorted(to_check):
            full_path = root / path
            if not full_path.is_file() or full_path.is_symlink():
                conn.execute("DELETE FROM files WHERE path=?", (path,))
                changed_graph_paths.add(path)
                continue
            try:
                data = full_path.read_bytes()
            except OSError:
                continue
            if len(data) > 2_000_000 or b"\0" in data:
                conn.execute("DELETE FROM files WHERE path=?", (path,))
                changed_graph_paths.add(path)
                continue
            digest = hashlib.sha256(data).hexdigest()
            if config.incremental and not force_reindex and previous.get(path) == digest:
                continue
            text = data.decode("utf-8", errors="replace")
            if any(SECRET_LINE.search(line) for line in text.splitlines()):
                conn.execute("DELETE FROM files WHERE path=?", (path,))
                changed_graph_paths.add(path)
                continue
            language = LANGUAGES.get(Path(path).suffix.lower())
            analysis = analyze(path, text, language)
            conn.execute(
                "INSERT INTO files(path, digest, language, bytes, symbols, imports, calls) "
                "VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT(path) DO UPDATE SET "
                "digest=excluded.digest, language=excluded.language, bytes=excluded.bytes, "
                "symbols=excluded.symbols, imports=excluded.imports, calls=excluded.calls",
                (
                    path,
                    digest,
                    language,
                    len(data),
                    json.dumps(analysis.symbols),
                    json.dumps(analysis.imports),
                    json.dumps(analysis.calls),
                ),
            )
            changed_graph_paths.add(path)
            updated += 1
        _sync_graph(conn, changed_graph_paths)
        _sync_cochanges(conn, config.co_changes_enabled, config.history_commits)
        conn.execute(
            "INSERT INTO metadata(key, value) VALUES('index_version', ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (INDEX_VERSION,),
        )
    file_count = conn.execute("SELECT COUNT(*) FROM files").fetchone()[0]
    conn.close()
    return {"indexed": updated, "removed": removed, "files": file_count}


def repository_status(root: Path) -> dict[str, object]:
    conn = connect(root)
    count = conn.execute("SELECT COUNT(*) FROM files").fetchone()[0]
    languages = conn.execute(
        "SELECT language, COUNT(*) FROM files WHERE language IS NOT NULL "
        "GROUP BY language ORDER BY COUNT(*) DESC, language"
    ).fetchall()
    latest = conn.execute("SELECT value FROM metadata WHERE key='last_commit'").fetchone()
    commits = conn.execute("SELECT COUNT(*) FROM commits").fetchone()[0]
    conn.close()
    return {
        "files": count,
        "languages": dict(languages),
        "last_commit": latest[0] if latest else None,
        "commits": commits,
    }


def _terms(text: str) -> set[str]:
    terms = set()
    words = []
    current = []
    for character in text:
        if character.isalnum():
            current.append(character)
        elif current:
            words.append("".join(current))
            current = []
    if current:
        words.append("".join(current))
    for word in words:
        terms.add(word.lower())
        pieces = []
        current_piece = []
        for index, character in enumerate(word):
            if (
                character.isupper()
                and current_piece
                and (
                    current_piece[-1].islower()
                    or current_piece[-1].isdigit()
                    or (
                        current_piece[-1].isupper()
                        and index + 1 < len(word)
                        and word[index + 1].islower()
                    )
                )
            ):
                pieces.append("".join(current_piece))
                current_piece = []
            current_piece.append(character)
        if current_piece:
            pieces.append("".join(current_piece))
        terms.update(piece.lower() for piece in pieces if len(piece) > 1)
    return terms


def context_for(
    root: Path, task: str, budget: int | None = None
) -> dict[str, object]:
    if budget is None:
        budget = load_config(root).default_budget
    if budget < 100:
        raise GitGraphError("Token budget must be at least 100.")
    conn = connect(root)
    rows = conn.execute(
        "SELECT path, language, bytes, symbols, imports, digest FROM files"
    ).fetchall()
    history = conn.execute(
        "SELECT cf.path, c.subject, c.hash FROM commit_files cf "
        "JOIN commits c ON c.hash=cf.commit_hash ORDER BY c.committed_at DESC"
    ).fetchall()
    conn.close()
    task_terms = _terms(task)
    frequencies = Counter(term for path, *_ in rows for term in _terms(path.replace("/", " ")))
    digest_by_path = {row[0]: row[5] for row in rows}
    history_by_path: dict[str, list[tuple[str, str]]] = {}
    for path, subject, commit_hash in history:
        history_by_path.setdefault(path, []).append((subject, commit_hash))
    conn = connect(root)
    edge_rows = conn.execute(
        "SELECT source, target, type, weight FROM graph_edges "
        "WHERE type IN ('DEPENDS_ON', 'CO_CHANGED_WITH')"
    ).fetchall()
    conn.close()
    relations: dict[str, list[tuple[str, str, float]]] = {}
    for source, target, kind, weight in edge_rows:
        source_path = source.removeprefix("file:")
        target_path = target.removeprefix("file:")
        relations.setdefault(source_path, []).append((target_path, kind, weight))
    ranked = []
    lexical_matches: set[str] = set()
    for path, language, size, raw_symbols, raw_imports, _digest in rows:
        symbols = json.loads(raw_symbols)
        imports = json.loads(raw_imports)
        searchable = _terms(path.replace("/", " ")) | _terms(" ".join(symbols + imports))
        overlap = searchable & task_terms
        if not overlap:
            continue
        lexical_matches.add(path)
        score = sum(1 / max(1, frequencies[word]) for word in overlap)
        score += min(len(history_by_path.get(path, [])), 5) * 0.08
        ranked.append((score, path, language, size, symbols, imports))
    ranked_paths = {row[1] for row in ranked}
    metadata_by_path = {
        path: (language, size, json.loads(raw_symbols), json.loads(raw_imports))
        for path, language, size, raw_symbols, raw_imports, _digest in rows
    }
    for source_path, related in relations.items():
        if source_path in ranked_paths or source_path not in metadata_by_path:
            continue
        matched_relations = [
            (target, kind, weight) for target, kind, weight in related if target in lexical_matches
        ]
        if not matched_relations:
            continue
        language, size, symbols, imports = metadata_by_path[source_path]
        relation_score = max(
            0.18 + min(weight, 3) * 0.04 + (0.12 if kind == "DEPENDS_ON" else 0)
            for _, kind, weight in matched_relations
        )
        ranked.append((relation_score, source_path, language, size, symbols, imports))
    ranked.sort(key=lambda item: (-item[0], item[1]))
    selected = []
    used_tokens = 30
    for score, path, language, size, symbols, imports in ranked:
        details = []
        if symbols:
            details.append("symbols: " + ", ".join(symbols[:8]))
        if imports:
            details.append("imports: " + ", ".join(imports[:6]))
        recent = history_by_path.get(path, [])
        if recent:
            details.append("recent: " + recent[0][0][:100])
        related_files = [
            {"path": target, "relationship": kind.lower(), "weight": weight}
            for target, kind, weight in relations.get(path, [])[:8]
        ]
        if related_files:
            details.append("related: " + ", ".join(item["path"] for item in related_files[:4]))
        estimate = max(12, (len(path) + len(" ".join(details))) // 4)
        if used_tokens + estimate > budget:
            continue
        excerpt = ""
        source_path = root / path
        try:
            source_bytes = source_path.read_bytes()
            if (
                hashlib.sha256(source_bytes).hexdigest() == digest_by_path[path]
                and b"\0" not in source_bytes
                and len(source_bytes) <= 2_000_000
            ):
                source_text = source_bytes.decode("utf-8", errors="replace")
                if not any(SECRET_LINE.search(line) for line in source_text.splitlines()):
                    source_lines = source_text.splitlines()
                    relevant = [
                        line_number
                        for line_number, line in enumerate(source_lines)
                        if _terms(line) & task_terms
                    ]
                    selected_lines: set[int] = set()
                    for line_number in relevant[:8]:
                        selected_lines.update(range(max(0, line_number - 1), min(len(source_lines), line_number + 2)))
                    if not selected_lines:
                        selected_lines.update(range(min(8, len(source_lines))))
                    remaining_chars = min(480, max(0, (budget - used_tokens - estimate) * 4))
                    snippet = "\n".join(
                        f"{line_number + 1}: {source_lines[line_number]}"
                        for line_number in sorted(selected_lines)
                    )
                    excerpt = snippet[:remaining_chars] if remaining_chars >= 8 else ""
        except OSError:
            pass
        excerpt_tokens = (len(excerpt) + 3) // 4
        if used_tokens + estimate + excerpt_tokens > budget:
            excerpt = ""
            excerpt_tokens = 0
        selected.append(
            {
                "path": path,
                "language": language,
                "score": round(score, 3),
                "symbols": symbols[:8],
                "imports": imports[:6],
                "recent_change": recent[0][0] if recent else None,
                "relationships": related_files,
                "excerpt": excerpt,
                "estimated_tokens": estimate + excerpt_tokens,
            }
        )
        used_tokens += estimate + excerpt_tokens
    return {
        "task": task,
        "files": selected,
        "estimated_tokens": used_tokens,
        "token_budget": budget,
        "files_avoided": max(0, len(rows) - len(selected)),
        "confidence": "high" if len(selected) >= 3 else "medium" if selected else "low",
    }


def repo_search(root: Path, query: str, limit: int = 20) -> list[dict[str, object]]:
    terms = _terms(query)
    if not terms:
        return []
    conn = connect(root)
    rows = conn.execute(
        "SELECT path, language, symbols, imports FROM files ORDER BY path"
    ).fetchall()
    conn.close()
    document_frequency = Counter(
        term for path, *_ in rows for term in _terms(path.replace("/", " "))
    )
    results = []
    for path, language, raw_symbols, raw_imports in rows:
        symbols = json.loads(raw_symbols)
        imports = json.loads(raw_imports)
        overlap = terms & (
            _terms(path.replace("/", " ")) | _terms(" ".join(symbols + imports))
        )
        if overlap:
            score = sum(1 / max(1, document_frequency[word]) for word in overlap)
            results.append(
                {
                    "path": path,
                    "language": language,
                    "symbols": symbols[:8],
                    "score": round(score, 3),
                }
            )
    results.sort(key=lambda row: (-row["score"], row["path"]))
    return results[: max(1, min(limit, 100))]


def architecture(root: Path) -> dict[str, object]:
    conn = connect(root)
    files = conn.execute("SELECT path, language FROM files ORDER BY path").fetchall()
    edge_counts = conn.execute(
        "SELECT type, COUNT(*) FROM graph_edges GROUP BY type ORDER BY type"
    ).fetchall()
    conn.close()
    modules: Counter[str] = Counter()
    languages: Counter[str] = Counter()
    for path, language in files:
        modules[path.split("/", 1)[0] if "/" in path else "." ] += 1
        if language:
            languages[language] += 1
    return {
        "files": len(files),
        "languages": dict(sorted(languages.items())),
        "modules": dict(sorted(modules.items())),
        "relationships": dict(edge_counts),
        "dependency_flow": "Files → symbols; import references → resolved file dependencies",
    }


def architecture_at(root: Path, revision: str) -> dict[str, object]:
    raw_paths = git(root, "ls-tree", "-r", "--name-only", "-z", revision)
    paths = [path for path in raw_paths.split("\0") if path]
    languages: Counter[str] = Counter()
    modules: Counter[str] = Counter()
    for path in paths:
        language = LANGUAGES.get(Path(path).suffix.lower())
        if language:
            languages[language] += 1
        modules[path.split("/", 1)[0] if "/" in path else "."] += 1
    return {
        "revision": revision,
        "files": len(paths),
        "languages": dict(sorted(languages.items())),
        "modules": dict(sorted(modules.items())),
        "relationships": {},
        "note": "Historical summary is based on tracked paths and file extensions.",
    }


def hotspots(root: Path, limit: int = 20) -> list[dict[str, object]]:
    conn = connect(root)
    rows = conn.execute(
        "SELECT cf.path, COUNT(DISTINCT c.hash) AS changes, COUNT(DISTINCT c.author) AS authors, "
        "MAX(c.committed_at) AS last_changed "
        "FROM commit_files cf JOIN commits c ON c.hash=cf.commit_hash "
        "GROUP BY cf.path ORDER BY changes DESC, last_changed DESC, cf.path LIMIT ?",
        (max(1, min(limit, 100)),),
    ).fetchall()
    conn.close()
    return [
        {"path": path, "changes": changes, "authors": authors, "last_changed": last_changed}
        for path, changes, authors, last_changed in rows
    ]


def find_dependencies(root: Path, path: str) -> list[str]:
    report = explain_file(root, path)
    return report["dependencies"]


def find_dependents(root: Path, path: str, depth: int = 1) -> list[dict[str, object]]:
    report = explain_file(root, path)
    target = report["path"]
    conn = connect(root)
    discovered: dict[str, int] = {}
    frontier = {target}
    for distance in range(1, max(1, min(depth, 10)) + 1):
        if not frontier:
            break
        placeholders = ",".join("?" for _ in frontier)
        rows = conn.execute(
            f"SELECT source, target FROM graph_edges WHERE type='DEPENDS_ON' "
            f"AND target IN ({placeholders})",
            tuple(f"file:{item}" for item in sorted(frontier)),
        ).fetchall()
        next_frontier = set()
        for source, _target in rows:
            dependent = source.removeprefix("file:")
            if dependent != target and dependent not in discovered:
                discovered[dependent] = distance
                next_frontier.add(dependent)
        frontier = next_frontier
    conn.close()
    return [
        {"path": path, "depth": distance}
        for path, distance in sorted(discovered.items(), key=lambda item: (item[1], item[0]))
    ]


def dependency_impact(root: Path, target: str, depth: int = 3) -> dict[str, object]:
    report = explain_file(root, target)
    dependents = find_dependents(root, report["path"], depth)
    direct = [item["path"] for item in dependents if item["depth"] == 1]
    indirect = [item["path"] for item in dependents if item["depth"] > 1]
    terms = _terms(" ".join(report["symbols"]) + " " + report["path"])
    conn = connect(root)
    candidates = [row[0] for row in conn.execute("SELECT path FROM files")]
    conn.close()
    tests = [
        path for path in candidates
        if ("test" in path.lower() or path.lower().endswith("_spec.py"))
        and (not terms or bool(terms & _terms(path)))
    ]
    return {
        "target": report["path"],
        "direct_dependents": direct,
        "indirect_dependents": indirect,
        "dependencies": report["dependencies"],
        "tests": sorted(tests),
        "co_changes": report["co_changes"],
        "history": report["history"],
        "estimated_impact": len(dependents),
        "risk": "high" if len(dependents) >= 10 else "medium" if dependents else "low",
    }


def explain_file(root: Path, path: str) -> dict[str, object]:
    path = path.removeprefix("./")
    conn = connect(root)
    row = conn.execute(
        "SELECT language, bytes, symbols, imports, calls FROM files WHERE path=?", (path,)
    ).fetchone()
    if row is None:
        matches = conn.execute(
            "SELECT DISTINCT path FROM graph_nodes WHERE type='Symbol' AND name=? ORDER BY path",
            (path,),
        ).fetchall()
        if len(matches) == 1:
            path = matches[0][0]
            row = conn.execute(
                "SELECT language, bytes, symbols, imports, calls FROM files WHERE path=?", (path,)
            ).fetchone()
    if row is None:
        conn.close()
        raise GitGraphError(f"File is not indexed: {path}")
    language, size, symbols, imports, calls = row
    dependencies = [
        target.removeprefix("file:")
        for (target,) in conn.execute(
            "SELECT target FROM graph_edges WHERE source=? AND type='DEPENDS_ON' ORDER BY target",
            (f"file:{path}",),
        )
    ]
    dependents = [
        source.removeprefix("file:")
        for (source,) in conn.execute(
            "SELECT source FROM graph_edges WHERE target=? AND type='DEPENDS_ON' ORDER BY source",
            (f"file:{path}",),
        )
    ]
    co_changes = [
        {"path": target.removeprefix("file:"), "count": int(weight)}
        for target, weight in conn.execute(
            "SELECT target, weight FROM graph_edges WHERE source=? AND type='CO_CHANGED_WITH' "
            "ORDER BY weight DESC, target LIMIT 10",
            (f"file:{path}",),
        )
    ]
    conn.close()
    return {
        "path": path,
        "language": language,
        "bytes": size,
        "symbols": json.loads(symbols),
        "imports": json.loads(imports),
        "calls": json.loads(calls),
        "dependencies": dependencies,
        "dependents": dependents,
        "co_changes": co_changes,
        "history": file_history(root, path),
    }


def graph_export(root: Path) -> dict[str, object]:
    conn = connect(root)
    nodes = [
        {"id": row[0], "type": row[1], "name": row[2], "path": row[3], "metadata": json.loads(row[4])}
        for row in conn.execute(
            "SELECT id, type, name, path, metadata FROM graph_nodes ORDER BY type, id"
        )
    ]
    edges = [
        {"source": row[0], "target": row[1], "type": row[2], "weight": row[3]}
        for row in conn.execute(
            "SELECT source, target, type, weight FROM graph_edges ORDER BY type, source, target"
        )
    ]
    conn.close()
    return {"nodes": nodes, "edges": edges}


def file_history(root: Path, path: str) -> list[dict[str, str]]:
    conn = connect(root)
    rows = conn.execute(
        "SELECT c.hash, c.author, c.subject, c.committed_at FROM commit_files cf "
        "JOIN commits c ON c.hash=cf.commit_hash WHERE cf.path=? "
        "ORDER BY c.committed_at DESC LIMIT 20",
        (path,),
    ).fetchall()
    conn.close()
    return [
        {"commit": row[0][:12], "author": row[1], "subject": row[2], "date": row[3]}
        for row in rows
    ]


def recent_changes(root: Path, limit: int = 20) -> list[dict[str, object]]:
    conn = connect(root)
    commits = conn.execute(
        "SELECT hash, author, subject, committed_at FROM commits "
        "ORDER BY committed_at DESC LIMIT ?",
        (max(1, min(limit, 100)),),
    ).fetchall()
    result = []
    for commit_hash, author, subject, committed_at in commits:
        paths = [
            row[0] for row in conn.execute(
                "SELECT path FROM commit_files WHERE commit_hash=? ORDER BY path LIMIT 100",
                (commit_hash,),
            )
        ]
        result.append(
            {
                "commit": commit_hash[:12],
                "author": author,
                "subject": subject,
                "date": committed_at,
                "files": paths,
            }
        )
    conn.close()
    return result


def pull_request_summary(root: Path, base: str = "origin/main") -> dict[str, object]:
    changed = [
        path for path in git(root, "diff", "--name-only", "-z", f"{base}...HEAD").split("\0")
        if path
    ]
    conn = connect(root)
    dependents: set[str] = set()
    if changed:
        placeholders = ",".join("?" for _ in changed)
        dependents.update(
            row[0].removeprefix("file:")
            for row in conn.execute(
                f"SELECT source FROM graph_edges WHERE type='DEPENDS_ON' "
                f"AND target IN ({placeholders})",
                tuple(f"file:{path}" for path in changed),
            )
        )
    conn.close()
    affected = sorted(set(changed) | dependents)
    test_paths = [path for path in affected if "test" in path.lower() or "_spec." in path.lower()]
    hotspot_paths = {item["path"] for item in hotspots(root, 100)}
    return {
        "base": base,
        "changed": changed,
        "changed_count": len(changed),
        "affected": affected,
        "affected_count": len(affected),
        "tests_affected": test_paths,
        "hotspots": sorted(set(changed) & hotspot_paths),
        "architecture": "Change set summary is based on indexed dependency edges.",
    }
