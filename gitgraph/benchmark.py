"""Small reproducible benchmark for initial and one-file incremental indexing."""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

from .core import LANGUAGES, _terms, context_for, connect, index_repository


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(root), *args],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
    ).stdout


def _setup_git(root: Path) -> None:
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    _git(root, "config", "user.email", "benchmark@example.invalid")
    _git(root, "config", "user.name", "GitGraph Benchmark")


def _measure(root: Path, files: int, task: str, target: Path) -> dict[str, object]:
    started = time.perf_counter()
    initial = index_repository(root)
    initial_seconds = time.perf_counter() - started
    target.write_text(
        target.read_text(encoding="utf-8") + "\n# benchmark incremental edit\n",
        encoding="utf-8",
    )
    started = time.perf_counter()
    incremental = index_repository(root)
    incremental_seconds = time.perf_counter() - started
    started = time.perf_counter()
    context = context_for(root, task, 500)
    retrieval_seconds = time.perf_counter() - started
    terms = _terms(task)
    conn = connect(root)
    placeholders = ",".join("?" for _ in terms)
    candidates = (
        conn.execute(
            f"SELECT COUNT(DISTINCT path) FROM file_search_terms "
            f"WHERE term IN ({placeholders})",
            sorted(terms),
        ).fetchone()[0]
        if terms
        else 0
    )
    database_bytes = sum(
        path.stat().st_size
        for path in (root / ".gitgraph").glob("graph.db*")
        if path.is_file()
    )
    conn.close()
    return {
        "files": files,
        "task": task,
        "initial_index_seconds": round(initial_seconds, 6),
        "incremental_index_seconds": round(incremental_seconds, 6),
        "context_retrieval_seconds": round(retrieval_seconds, 6),
        "initial": initial,
        "incremental": incremental,
        "lexical_candidates": candidates,
        "returned_files": len(context["files"]),
        "context_estimated_tokens": context["estimated_tokens"],
        "database_bytes": database_bytes,
        "note": "Measured locally for this run; not a cross-repository performance claim.",
    }


def run_benchmark(file_count: int) -> dict[str, object]:
    if file_count < 1:
        raise ValueError("File count must be positive.")
    with tempfile.TemporaryDirectory(prefix="gitgraph-benchmark-") as directory:
        root = Path(directory)
        _setup_git(root)
        source = root / "src"
        source.mkdir()
        for index in range(file_count):
            identifier = 1000 + index
            (source / f"service_{identifier}.py").write_text(
                f"class Service{identifier}:\n"
                f"    def run(self):\n        return {index}\n",
                encoding="utf-8",
            )
        _git(root, "add", "src")
        _git(root, "commit", "-qm", "benchmark fixture")
        identifier = 1000 + file_count - 1
        target = source / f"service_{identifier}.py"
        return _measure(root, file_count, str(identifier), target)


def run_repository_benchmark(source_root: Path) -> dict[str, object]:
    source_root = source_root.resolve()
    tracked = [path for path in _git(source_root, "ls-files", "-z").split("\0") if path]
    if not tracked:
        raise ValueError(f"No tracked files found in {source_root}.")
    with tempfile.TemporaryDirectory(prefix="gitgraph-repository-benchmark-") as directory:
        root = Path(directory) / "repository"
        root.mkdir()
        copied = []
        for relative in tracked:
            source = source_root / relative
            if not source.is_file() or source.is_symlink():
                continue
            destination = root / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)
            copied.append(relative)
        eligible = [
            relative for relative in copied
            if Path(relative).suffix.lower() in LANGUAGES
            and (root / relative).stat().st_size <= 2_000_000
        ]
        if not eligible:
            raise ValueError(f"No supported source files found in {source_root}.")
        target_relative = eligible[0]
        target = root / target_relative
        task = Path(target_relative).stem.strip("_").replace("_", " ")
        _setup_git(root)
        _git(root, "add", "-A")
        _git(root, "commit", "-qm", "repository benchmark fixture")
        return _measure(root, len(eligible), task, target)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Measure GitGraph indexing and context retrieval")
    parser.add_argument("--files", nargs="+", type=int, default=[10, 1000])
    parser.add_argument(
        "--repository",
        type=Path,
        help="Also benchmark a copy of a Git repository's tracked working-tree files",
    )
    args = parser.parse_args(argv)
    try:
        results = [run_benchmark(count) for count in args.files]
        if args.repository:
            results.append(run_repository_benchmark(args.repository))
    except (OSError, ValueError, subprocess.CalledProcessError) as exc:
        parser.exit(1, f"gitgraph-benchmark: {exc}\n")
    print(json.dumps(results, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
