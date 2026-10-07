from __future__ import annotations

import fnmatch
import hashlib
import json
import os
import re
import sqlite3
import subprocess
from collections import Counter
from pathlib import Path
from typing import Iterable

DEFAULT_IGNORES = (
    ".git/",
    ".gitgraph/",
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
WORD = re.compile(r"[A-Za-z_][A-Za-z0-9_]{1,}")
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


class GitGraphError(RuntimeError):
    pass


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
            imports TEXT NOT NULL DEFAULT '[]'
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
        CREATE TABLE IF NOT EXISTS metadata (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
        """
    )
    return conn


def _ignore_patterns(root: Path) -> list[str]:
    patterns = list(DEFAULT_IGNORES)
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


def _commit_history(root: Path, conn: sqlite3.Connection, last_commit: str | None) -> None:
    head = git(root, "rev-parse", "--verify", "HEAD", check=False).strip()
    if not head:
        return
    full_scan = not last_commit or (
        last_commit != head and not _is_ancestor(root, last_commit, head)
    )
    if full_scan:
        conn.execute("DELETE FROM commit_files")
        conn.execute("DELETE FROM commits")
        revisions = git(root, "log", "-n", "100", "--format=%H%x09%an%x09%s%x09%cI").splitlines()
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
        paths = git(
            root,
            "diff-tree",
            "--root",
            "--no-commit-id",
            "--name-only",
            "-r",
            "-z",
            commit_hash,
        )
        for path in paths.split("\0"):
            if path:
                conn.execute(
                    "INSERT OR IGNORE INTO commit_files(commit_hash, path) VALUES (?, ?)",
                    (commit_hash, path),
                )
    conn.execute(
        "INSERT INTO metadata(key, value) VALUES('last_commit', ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (head,),
    )


def index_repository(root: Path) -> dict[str, int]:
    root = root.resolve()
    conn = connect(root)
    patterns = _ignore_patterns(root)
    previous = dict(conn.execute("SELECT path, digest FROM files"))
    last_commit_row = conn.execute("SELECT value FROM metadata WHERE key='last_commit'").fetchone()
    last_commit = last_commit_row[0] if last_commit_row else None
    head = git(root, "rev-parse", "--verify", "HEAD", check=False).strip()
    is_descendant = not last_commit or not head or last_commit == head or _is_ancestor(root, last_commit, head)
    if not is_descendant:
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
    if not last_commit or not is_descendant:
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
        _commit_history(root, conn, last_commit)
        for path in deleted:
            if is_ignored(path, patterns) or path not in tracked_after or not (root / path).is_file():
                conn.execute("DELETE FROM files WHERE path=?", (path,))
                removed += 1
        for path in sorted(to_check):
            full_path = root / path
            if not full_path.is_file() or full_path.is_symlink():
                conn.execute("DELETE FROM files WHERE path=?", (path,))
                continue
            try:
                data = full_path.read_bytes()
            except OSError:
                continue
            if len(data) > 2_000_000 or b"\0" in data:
                conn.execute("DELETE FROM files WHERE path=?", (path,))
                continue
            digest = hashlib.sha256(data).hexdigest()
            if previous.get(path) == digest:
                continue
            text = data.decode("utf-8", errors="replace")
            if any(SECRET_LINE.search(line) for line in text.splitlines()):
                conn.execute("DELETE FROM files WHERE path=?", (path,))
                continue
            symbols = sorted(set(SYMBOL.findall(text)))
            imports = sorted(set(IMPORT.findall(text)))
            conn.execute(
                "INSERT INTO files(path, digest, language, bytes, symbols, imports) "
                "VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(path) DO UPDATE SET "
                "digest=excluded.digest, language=excluded.language, bytes=excluded.bytes, "
                "symbols=excluded.symbols, imports=excluded.imports",
                (
                    path,
                    digest,
                    LANGUAGES.get(Path(path).suffix.lower()),
                    len(data),
                    json.dumps(symbols),
                    json.dumps(imports),
                ),
            )
            updated += 1
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
    return {word.lower() for word in WORD.findall(text)}


def context_for(root: Path, task: str, budget: int = 2000) -> dict[str, object]:
    if budget < 100:
        raise GitGraphError("Token budget must be at least 100.")
    conn = connect(root)
    rows = conn.execute(
        "SELECT path, language, bytes, symbols, imports FROM files"
    ).fetchall()
    history = conn.execute(
        "SELECT cf.path, c.subject, c.hash FROM commit_files cf "
        "JOIN commits c ON c.hash=cf.commit_hash ORDER BY c.committed_at DESC"
    ).fetchall()
    conn.close()
    task_terms = _terms(task)
    frequencies = Counter(term for path, *_ in rows for term in _terms(path.replace("/", " ")))
    history_by_path: dict[str, list[tuple[str, str]]] = {}
    for path, subject, commit_hash in history:
        history_by_path.setdefault(path, []).append((subject, commit_hash))
    ranked = []
    for path, language, size, raw_symbols, raw_imports in rows:
        symbols = json.loads(raw_symbols)
        imports = json.loads(raw_imports)
        searchable = _terms(path.replace("/", " ")) | _terms(" ".join(symbols + imports))
        overlap = searchable & task_terms
        if not overlap:
            continue
        score = sum(1 / max(1, frequencies[word]) for word in overlap)
        score += min(len(history_by_path.get(path, [])), 5) * 0.08
        ranked.append((score, path, language, size, symbols, imports))
    ranked.sort(key=lambda item: (-item[0], item[1]))
    selected = []
    used_tokens = 80
    for score, path, language, size, symbols, imports in ranked:
        details = []
        if symbols:
            details.append("symbols: " + ", ".join(symbols[:8]))
        if imports:
            details.append("imports: " + ", ".join(imports[:6]))
        recent = history_by_path.get(path, [])
        if recent:
            details.append("recent: " + recent[0][0][:100])
        estimate = max(12, (len(path) + len(" ".join(details))) // 4)
        if used_tokens + estimate > budget:
            continue
        selected.append(
            {
                "path": path,
                "language": language,
                "score": round(score, 3),
                "symbols": symbols[:8],
                "imports": imports[:6],
                "recent_change": recent[0][0] if recent else None,
                "estimated_tokens": estimate,
            }
        )
        used_tokens += estimate
    return {
        "task": task,
        "files": selected,
        "estimated_tokens": used_tokens,
        "token_budget": budget,
        "files_avoided": max(0, len(rows) - len(selected)),
        "confidence": "high" if len(selected) >= 3 else "medium" if selected else "low",
    }


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
