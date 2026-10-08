"""Reproducible synthetic and temporary-copy repository indexing benchmarks."""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

from .core import (
    DEFAULT_EMBEDDING_MODEL,
    AgentRamenError,
    LANGUAGES,
    _terms,
    context_for,
    connect,
    dependency_impact,
    index_repository,
)


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
    _git(root, "config", "user.name", "agentRamen Benchmark")


def _measure(
    root: Path, files: int, task: str, target: Path, semantic: bool = False
) -> dict[str, object]:
    if semantic:
        (root / ".agentramen.yml").write_text(
            "semantic:\n  enabled: true\n"
            f"  model: {DEFAULT_EMBEDDING_MODEL}\n",
            encoding="utf-8",
        )
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
        for path in (root / ".agentramen").glob("graph.db*")
        if path.is_file()
    )
    conn.close()
    return {
        "files": files,
        "task": task,
        "semantic_enabled": semantic,
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


def run_benchmark(file_count: int, semantic: bool = False) -> dict[str, object]:
    if file_count < 1:
        raise ValueError("File count must be positive.")
    with tempfile.TemporaryDirectory(prefix="agentramen-benchmark-") as directory:
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
        return _measure(root, file_count, str(identifier), target, semantic)


def run_repository_benchmark(source_root: Path, semantic: bool = False) -> dict[str, object]:
    source_root = source_root.resolve()
    tracked = [path for path in _git(source_root, "ls-files", "-z").split("\0") if path]
    if not tracked:
        raise ValueError(f"No tracked files found in {source_root}.")
    with tempfile.TemporaryDirectory(prefix="agentramen-repository-benchmark-") as directory:
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
        return _measure(root, len(eligible), task, target, semantic)


def run_quality_benchmark() -> dict[str, object]:
    """Measure retrieval and affected-test accuracy on a deterministic labeled fixture."""
    fixture = {
        "src/auth/login_session_expiry.py": (
            "class LoginSessionExpiry:\n"
            "    def expire_login_session(self):\n"
            "        return True\n"
        ),
        "src/billing/tax_rounding.py": (
            "class TaxRounding:\n"
            "    def round_invoice_tax(self):\n"
            "        return 0\n"
        ),
        "src/billing/invoice.py": "from src.billing.tax_rounding import TaxRounding\n",
        "tests/test_login_session_expiry.py": (
            "from src.auth.login_session_expiry import LoginSessionExpiry\n\n"
            "def test_login_session_expiry():\n"
            "    assert LoginSessionExpiry().expire_login_session()\n"
        ),
        "tests/test_tax_rounding.py": (
            "from src.billing.tax_rounding import TaxRounding\n\n"
            "def test_tax_rounding():\n"
            "    assert TaxRounding().round_invoice_tax() == 0\n"
        ),
        "tests/test_unrelated_notifications.py": (
            "def test_notification_copy():\n    assert 'sent' == 'sent'\n"
        ),
    }
    scenarios = [
        {
            "task": "change login session expiry behavior",
            "target": "src/auth/login_session_expiry.py",
            "relevant_files": [
                "src/auth/login_session_expiry.py",
                "tests/test_login_session_expiry.py",
            ],
            "affected_tests": ["tests/test_login_session_expiry.py"],
        },
        {
            "task": "change invoice tax rounding",
            "target": "src/billing/tax_rounding.py",
            "relevant_files": [
                "src/billing/tax_rounding.py",
                "src/billing/invoice.py",
                "tests/test_tax_rounding.py",
            ],
            "affected_tests": ["tests/test_tax_rounding.py"],
        },
    ]
    with tempfile.TemporaryDirectory(prefix="agentramen-quality-reference-") as directory:
        root = Path(directory)
        _setup_git(root)
        (root / ".gitignore").write_text(".agentramen/\n", encoding="utf-8")
        _git(root, "add", ".gitignore")
        _git(root, "commit", "-qm", "initialize reference fixture")
        for relative, source in fixture.items():
            path = root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(source, encoding="utf-8")
        _git(root, "add", "src/auth/login_session_expiry.py", "tests/test_login_session_expiry.py")
        _git(root, "commit", "-qm", "add login session expiry")
        _git(root, "add", "src/billing/tax_rounding.py", "src/billing/invoice.py", "tests/test_tax_rounding.py")
        _git(root, "commit", "-qm", "add invoice tax rounding")
        _git(root, "add", "tests/test_unrelated_notifications.py")
        _git(root, "commit", "-qm", "add notification tests")
        index_repository(root)

        results = []
        for scenario in scenarios:
            context = context_for(root, scenario["task"], 2000)
            retrieved = [item["path"] for item in context["files"]]
            expected_files = set(scenario["relevant_files"])
            file_hits = expected_files & set(retrieved)
            actual_tests = set(dependency_impact(root, scenario["target"])["tests"])
            expected_tests = set(scenario["affected_tests"])
            test_hits = expected_tests & actual_tests
            evidence = [
                item
                for item in dependency_impact(root, scenario["target"])["test_associations"]
                if item["path"] in expected_tests
            ]
            results.append(
                {
                    "task": scenario["task"],
                    "target": scenario["target"],
                    "expected_files": sorted(expected_files),
                    "retrieved_files": retrieved,
                    "file_precision": round(len(file_hits) / len(retrieved), 3)
                    if retrieved
                    else 0.0,
                    "file_recall": round(len(file_hits) / len(expected_files), 3),
                    "expected_tests": sorted(expected_tests),
                    "confirmed_tests": sorted(actual_tests),
                    "test_precision": round(len(test_hits) / len(actual_tests), 3)
                    if actual_tests
                    else 0.0,
                    "test_recall": round(len(test_hits) / len(expected_tests), 3),
                    "test_evidence": evidence,
                }
            )
    return {
        "scenario": "synthetic-labeled-quality-reference",
        "scenarios": results,
        "mean_file_precision": round(
            sum(float(item["file_precision"]) for item in results) / len(results), 3
        ),
        "mean_file_recall": round(
            sum(float(item["file_recall"]) for item in results) / len(results), 3
        ),
        "mean_test_precision": round(
            sum(float(item["test_precision"]) for item in results) / len(results), 3
        ),
        "mean_test_recall": round(
            sum(float(item["test_recall"]) for item in results) / len(results), 3
        ),
        "note": "Deterministic synthetic reference fixture; not a production-repository or cross-model accuracy guarantee.",
    }


def run_comparisons(
    file_counts: list[int],
    repository: Path | None,
    semantic_modes: tuple[bool, ...],
) -> list[dict[str, object]]:
    results = []
    semantic_error: str | None = None
    for semantic in semantic_modes:
        scenarios = [
            (f"synthetic-{count}", lambda count=count: run_benchmark(count, semantic))
            for count in file_counts
        ]
        if repository:
            scenarios.append(
                (
                    f"repository-{repository}",
                    lambda: run_repository_benchmark(repository, semantic),
                )
            )
        for name, run in scenarios:
            if semantic and semantic_error:
                results.append(
                    {
                        "scenario": name,
                        "semantic_enabled": True,
                        "error": f"Skipped after semantic setup failed: {semantic_error}",
                    }
                )
                continue
            try:
                results.append(run())
            except (AgentRamenError, OSError, ValueError, subprocess.CalledProcessError) as exc:
                if len(semantic_modes) == 1:
                    raise
                if semantic:
                    semantic_error = str(exc)
                results.append(
                    {
                        "scenario": name,
                        "semantic_enabled": semantic,
                        "error": str(exc),
                    }
                )
    return results


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Measure agentRamen indexing and context retrieval")
    parser.add_argument("--files", nargs="+", type=int, default=[10, 1000])
    parser.add_argument(
        "--repository",
        type=Path,
        help="Also benchmark a copy of a Git repository's tracked working-tree files",
    )
    parser.add_argument(
        "--semantic-mode",
        choices=("off", "on", "both"),
        default="off",
        help="Benchmark semantic retrieval off, on, or in both modes",
    )
    parser.add_argument(
        "--quality",
        action="store_true",
        help="Measure task-to-file relevance and affected-test accuracy on a labeled reference fixture",
    )
    args = parser.parse_args(argv)
    try:
        modes = (False, True) if args.semantic_mode == "both" else (
            args.semantic_mode == "on",
        )
        results = run_comparisons(args.files, args.repository, modes)
        if args.quality:
            results.append(run_quality_benchmark())
    except (OSError, ValueError, subprocess.CalledProcessError) as exc:
        parser.exit(1, f"agentramen-benchmark: {exc}\n")
    print(json.dumps(results, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
