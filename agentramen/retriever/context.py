"""Public context retrieval entry point."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .hybrid_engine import retrieve_context


def context_for(root: Path, task: str, budget: int | None = None) -> dict[str, Any]:
    return retrieve_context(root, task, budget)
