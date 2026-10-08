"""Small reproducible benchmark for initial and one-file incremental indexing."""

from __future__ import annotations

import argparse
import json
import subprocess
import tempfile
import time
from pathlib import Path

from .core import context_for, index_repository


def run_benchmark(file_count: int) -> dict[str, object]:
    if file_count < 1:
        raise ValueError("File count must be positive.")
    with tempfile.TemporaryDirectory(prefix="gitgraph-benchmark-") as directory:
        root = Path(directory)
        subprocess.run(["git", "init", "-q", str(root)], check=True)
        subprocess.run(
            ["git", "-C", str(root), "config", "user.email", "benchmark@example.invalid"],
            check=True,
        )
        subprocess.run(
            ["git", "-C", str(root), "config", "user.name", "GitGraph Benchmark"],
            check=True,
        )
        source = root / "src"
        source.mkdir()
        for index in range(file_count):
            (source / f"service_{index}.py").write_text(
                f"class Service{index}:\n"
                f"    def run(self):\n        return {index}\n",
                encoding="utf-8",
            )
        subprocess.run(["git", "-C", str(root), "add", "src"], check=True)
        subprocess.run(
            ["git", "-C", str(root), "commit", "-qm", "benchmark fixture"],
            check=True,
        )

        started = time.perf_counter()
        initial = index_repository(root)
        initial_seconds = time.perf_counter() - started
        target = source / "service_0.py"
        target.write_text(target.read_text(encoding="utf-8") + "\n# changed\n", encoding="utf-8")
        started = time.perf_counter()
        incremental = index_repository(root)
        incremental_seconds = time.perf_counter() - started
        started = time.perf_counter()
        context = context_for(root, "Service0 run", 500)
        retrieval_seconds = time.perf_counter() - started
        return {
            "files": file_count,
            "initial_index_seconds": round(initial_seconds, 6),
            "incremental_index_seconds": round(incremental_seconds, 6),
            "context_retrieval_seconds": round(retrieval_seconds, 6),
            "initial": initial,
            "incremental": incremental,
            "context_estimated_tokens": context["estimated_tokens"],
            "note": "Measured locally for this run; not a cross-repository performance claim.",
        }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Measure GitGraph indexing and context retrieval")
    parser.add_argument("--files", nargs="+", type=int, default=[10, 1000])
    args = parser.parse_args(argv)
    try:
        results = [run_benchmark(count) for count in args.files]
    except (OSError, ValueError, subprocess.CalledProcessError) as exc:
        parser.exit(1, f"gitgraph-benchmark: {exc}\n")
    print(json.dumps(results, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
