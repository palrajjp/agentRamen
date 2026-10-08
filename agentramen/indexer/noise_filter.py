"""Deterministic noise reduction for local embedding input."""

from __future__ import annotations

import re
from collections import Counter
from pathlib import PurePosixPath

_AUTOMATION = re.compile(
    r"(?:generated (?:file|by)|do not edit|automatically generated|"
    r"this file is managed by|copilot instructions|system prompt|"
    r"you are an ai assistant)",
    re.IGNORECASE,
)
_PACKAGE_MAPPING = re.compile(
    r'^\s*["\']?(?:node_modules/|resolved|integrity|'
    r'"?(?:dependencies|devDependencies|peerDependencies)"?\s*:)',
    re.IGNORECASE,
)
_DOC_FILLER = re.compile(
    r"^\s*(?:#{1,6}\s*)?(?:table of contents|installation instructions|"
    r"click here to learn more|for more information,? (?:see|visit)|"
    r"this project is licensed under)\b",
    re.IGNORECASE,
)
_STRUCTURE = re.compile(
    r"^\s*(?:async\s+)?(?:class|def|function|fn|func|interface|trait|"
    r"struct|enum|type|import|from|export|return|if|for|while|match)\b|"
    r"[{}();=]",
)
_CODE_EXTENSIONS = {
    ".c", ".cc", ".cpp", ".cs", ".go", ".h", ".hpp", ".java", ".js",
    ".jsx", ".kt", ".php", ".py", ".rb", ".rs", ".swift", ".ts", ".tsx",
}
_LOCKFILES = {"package-lock.json", "pnpm-lock.yaml", "yarn.lock", "poetry.lock"}


def character_density(text: str) -> float:
    """Return the proportion of non-whitespace characters that are lexical tokens."""
    compact = [character for character in text if not character.isspace()]
    if not compact:
        return 0.0
    token_chars = sum(character.isalnum() or character in "_.$" for character in compact)
    return token_chars / len(compact)


def _is_structural(line: str) -> bool:
    return bool(_STRUCTURE.search(line))


def filter_noise(path: str, text: str) -> str:
    """Remove common generated, instructional, and low-signal text from embeddings."""
    normalized = path.replace("\\", "/")
    basename = PurePosixPath(normalized).name.lower()
    if basename in _LOCKFILES or basename.endswith(".min.js"):
        return ""

    lines = text.splitlines()
    counts = Counter(line.strip() for line in lines if line.strip())
    output: list[str] = []
    for line in lines:
        stripped = line.strip()
        if not stripped or _AUTOMATION.search(stripped):
            continue
        if _PACKAGE_MAPPING.search(line):
            continue
        if _DOC_FILLER.search(line):
            continue
        if counts[stripped] > 3 and not _is_structural(line):
            continue
        if PurePosixPath(normalized).suffix.lower() not in _CODE_EXTENSIONS:
            words = re.findall(r"\w+", stripped)
            if len(words) > 8 and character_density(stripped) < 0.55:
                continue
        output.append(line.rstrip())
    return "\n".join(output).strip()
