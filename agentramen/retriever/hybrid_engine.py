"""Local multi-route retrieval and reciprocal-rank fusion."""

from __future__ import annotations

import json
import fnmatch
import re
from pathlib import Path
from typing import Any

from .. import core


def _route_ranks(root: Path, task: str, paths: set[str]) -> list[list[str]]:
    config = core.load_config(root)
    terms = core._terms(task)
    routes: list[list[str]] = []
    conn = core.connect(root)
    try:
        lexical_scores: dict[str, float] = {}
        if terms:
            for batch in core._batches(terms):
                placeholders = ",".join("?" for _ in batch)
                rows = conn.execute(
                    "SELECT t.path, t.term, COALESCE(d.document_frequency, 0) "
                    "FROM file_search_terms t LEFT JOIN term_document_frequency d "
                    "ON d.term=t.term WHERE t.term IN (" + placeholders + ")",
                    batch,
                )
                for path, term, frequency in rows:
                    if path in paths:
                        lexical_scores[path] = lexical_scores.get(path, 0.0) + 1 / max(
                            1, frequency
                        )
        routes.append(
            sorted(lexical_scores, key=lambda path: (-lexical_scores[path], path))
        )

        semantic_scores: dict[str, float] = {}
        if config.semantic_enabled and task.strip():
            embedding_rows = []
            for batch in core._batches(paths):
                embedding_rows.extend(
                    conn.execute(
                        "SELECT path, vector FROM file_embeddings WHERE model=? "
                        "AND path IN (" + ",".join("?" for _ in batch) + ")",
                        [config.embedding_model, *batch],
                    ).fetchall()
                )
            if embedding_rows:
                query_vector = core._embed_texts(
                    config.embedding_model, ["query: " + task]
                )[0]
                for path, raw_vector in embedding_rows:
                    vector = json.loads(raw_vector)
                    if len(vector) != len(query_vector):
                        continue
                    denominator = sum(value * value for value in query_vector) ** 0.5
                    denominator *= sum(value * value for value in vector) ** 0.5
                    if denominator:
                        semantic_scores[path] = sum(
                            left * right for left, right in zip(query_vector, vector)
                        ) / denominator
        routes.append(
            sorted(semantic_scores, key=lambda path: (-semantic_scores[path], path))
        )

        graph_scores: dict[str, float] = {}
        for batch in core._batches(paths):
            placeholders = ",".join("?" for _ in batch)
            histories = dict(
                conn.execute(
                    "SELECT path, COUNT(*) FROM commit_files WHERE path IN ("
                    + placeholders
                    + ") GROUP BY path",
                    batch,
                ).fetchall()
            )
            relations = dict(
                conn.execute(
                    "SELECT source, SUM(weight) FROM graph_edges "
                    "WHERE type IN ('CO_CHANGED_WITH', 'DEPENDS_ON') AND source IN ("
                    + placeholders
                    + ") GROUP BY source",
                    [f"file:{path}" for path in batch],
                ).fetchall()
            )
            commits = conn.execute(
                "SELECT cf.path, c.subject FROM commit_files cf "
                "JOIN commits c ON c.hash=cf.commit_hash WHERE cf.path IN ("
                + placeholders
                + ") ORDER BY c.committed_at DESC",
                batch,
            ).fetchall()
            commit_relevance: dict[str, float] = {}
            for path, subject in commits:
                overlap = terms & core._terms(subject)
                if overlap:
                    commit_relevance[path] = max(
                        commit_relevance.get(path, 0.0),
                        min(len(overlap), 5) * 0.2,
                    )
            for path in batch:
                graph_scores[path] = (
                    min(histories.get(path, 0), 10) * 0.1
                    + min(relations.get(f"file:{path}", 0), 10) * 0.05
                    + commit_relevance.get(path, 0.0)
                )
        routes.append(
            sorted(
                (path for path, score in graph_scores.items() if score > 0),
                key=lambda path: (-graph_scores[path], path),
            )
        )
        return routes
    finally:
        conn.close()


def rerank_context(root: Path, task: str, base: dict[str, Any]) -> dict[str, Any]:
    """Fuse lexical, semantic, and graph route rankings, then reapply the token budget."""
    files = base.get("files", [])
    if not files:
        return base
    conn = core.connect(root)
    try:
        rules = conn.execute(
            "SELECT pattern, pattern_type FROM context_exclusion_rules WHERE enabled=1"
        ).fetchall()
    finally:
        conn.close()
    included: list[dict[str, Any]] = []
    excluded = 0
    for item in files:
        path = str(item["path"])
        source = ""
        for pattern, pattern_type in rules:
            matched = False
            if pattern_type == "glob":
                matched = fnmatch.fnmatch(path, pattern)
            elif pattern_type == "path":
                matched = path == pattern or path.startswith(pattern.rstrip("/") + "/")
            elif pattern_type == "regex":
                try:
                    matched = re.search(pattern, path) is not None
                except re.error as exc:
                    raise core.AgentRamenError(
                        f"Invalid context exclusion regex '{pattern}': {exc}"
                    ) from exc
            elif pattern_type == "content":
                if not source:
                    try:
                        source = (root / path).read_text(encoding="utf-8", errors="replace")
                    except OSError:
                        source = ""
                matched = pattern.casefold() in source.casefold()
            if matched:
                excluded += 1
                break
        else:
            included.append(item)
    files = included
    base["files_avoided"] = int(base.get("files_avoided", 0)) + excluded
    if not files:
        base["files"] = []
        base["estimated_tokens"] = core._count_context_tokens(
            {"files": []}, core.load_config(root).tokenizer_model
        )
        return base
    paths = {str(item["path"]) for item in files}
    routes = _route_ranks(root, task, paths)
    route_positions = [
        {path: index + 1 for index, path in enumerate(route)} for route in routes
    ]
    fused = {
        path: sum(
            1 / (60 + positions[path])
            for positions in route_positions
            if path in positions
        )
        for path in paths
    }
    original_order = {
        str(item["path"]): index for index, item in enumerate(files)
    }
    ranked = sorted(
        files,
        key=lambda item: (
            -fused[str(item["path"])],
            original_order[str(item["path"])],
            str(item["path"]),
        ),
    )
    config = core.load_config(root)
    budget = int(base["token_budget"])
    selected: list[dict[str, Any]] = []
    for item in ranked:
        candidate = [*selected, item]
        compact_candidate = [
            {key: value for key, value in file.items() if key != "estimated_tokens"}
            for file in candidate
        ]
        if core._count_context_tokens(
            {"files": compact_candidate}, config.tokenizer_model
        ) <= budget:
            selected.append(item)
    base["files"] = selected
    compact_selected = [
        {key: value for key, value in item.items() if key != "estimated_tokens"}
        for item in selected
    ]
    base["estimated_tokens"] = core._count_context_tokens(
        {"files": compact_selected}, config.tokenizer_model
    )
    base["files_avoided"] = int(base.get("files_avoided", 0)) + len(files) - len(selected)
    return base


def retrieve_context(
    root: Path, task: str, budget: int | None = None
) -> dict[str, Any]:
    base = core._context_for_baseline(root, task, budget)
    return rerank_context(root, task, base)
