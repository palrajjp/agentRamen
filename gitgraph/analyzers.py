"""Language-specific extraction kept independent from graph storage."""

from __future__ import annotations

import ast
import posixpath
import re
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Analysis:
    symbols: tuple[str, ...] = ()
    imports: tuple[str, ...] = ()
    calls: tuple[str, ...] = ()


class LanguageAnalyzer:
    language: str

    def analyze(self, path: str, source: str) -> Analysis:
        raise NotImplementedError


class PythonAnalyzer(LanguageAnalyzer):
    language = "python"

    def analyze(self, path: str, source: str) -> Analysis:
        try:
            tree = ast.parse(source, filename=path)
        except (SyntaxError, ValueError):
            return Analysis()
        symbols: set[str] = set()
        imports: set[str] = set()
        calls: set[str] = set()

        class Visitor(ast.NodeVisitor):
            def __init__(self) -> None:
                self.scope: list[str] = []

            def visit_ClassDef(self, node: ast.ClassDef) -> None:
                symbols.add(".".join([*self.scope, node.name]))
                self.scope.append(node.name)
                self.generic_visit(node)
                self.scope.pop()

            def visit_FunctionDef(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
                symbols.add(".".join([*self.scope, node.name]))
                self.scope.append(node.name)
                self.generic_visit(node)
                self.scope.pop()

            visit_AsyncFunctionDef = visit_FunctionDef

            def visit_Import(self, node: ast.Import) -> None:
                imports.update(alias.name for alias in node.names)

            def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
                module = "." * node.level + (node.module or "")
                imports.add(module)
                imports.update(f"{module}.{alias.name}" for alias in node.names)

            def visit_Call(self, node: ast.Call) -> None:
                current = node.func
                parts: list[str] = []
                while isinstance(current, ast.Attribute):
                    parts.append(current.attr)
                    current = current.value
                if isinstance(current, ast.Name):
                    parts.append(current.id)
                if parts:
                    calls.add(".".join(reversed(parts)))
                self.generic_visit(node)

        Visitor().visit(tree)
        return Analysis(tuple(sorted(symbols)), tuple(sorted(imports)), tuple(sorted(calls)))


class RegexAnalyzer(LanguageAnalyzer):
    """Conservative symbol/import extraction for languages without a built-in parser."""

    def __init__(self, language: str):
        self.language = language
        self.symbol_pattern = re.compile(
            r"^\s*(?:export\s+)?(?:async\s+)?(?:class|interface|type|enum|function|def|fn|func|struct|trait)\s+([A-Za-z_$][\w$]*)",
            re.MULTILINE,
        )
        self.import_pattern = re.compile(
            r"""^\s*(?:import\s+(?:.*?\s+from\s+)?|from\s+[\w.]+\s+import\s+|(?:const|let|var)\s+\w+\s*=\s*require\()\s*['"]([^'"]+)['"]""",
            re.MULTILINE,
        )
        self.additional_import_patterns = {
            "java": r"^\s*import\s+(?:static\s+)?([\w.*]+)",
            "kotlin": r"^\s*import\s+([\w.*]+)",
            "csharp": r"^\s*using\s+([\w.]+)",
            "go": r'^\s*import\s+(?:\w+\s+)?["\']([^"\']+)["\']',
            "rust": r"^\s*use\s+([\w:{},*]+)\s*;",
            "c": r'^\s*#\s*include\s+[<"]([^>"]+)[>"]',
            "cpp": r'^\s*#\s*include\s+[<"]([^>"]+)[>"]',
            "ruby": r"""^\s*require(?:_relative)?\s*\(?\s*['"]([^'"]+)['"]""",
            "php": r"^\s*use\s+([\w\\]+)",
            "swift": r"^\s*import\s+([\w.]+)",
        }

    def analyze(self, path: str, source: str) -> Analysis:
        symbols = tuple(sorted(set(self.symbol_pattern.findall(source))))
        imports_found = set(self.import_pattern.findall(source))
        extra_pattern = self.additional_import_patterns.get(self.language)
        if extra_pattern:
            imports_found.update(re.findall(extra_pattern, source, re.MULTILINE))
        imports = tuple(sorted(imports_found))
        return Analysis(symbols=symbols, imports=imports)


ANALYZERS: dict[str, LanguageAnalyzer] = {
    "python": PythonAnalyzer(),
}


def analyze(path: str, source: str, language: str | None = None) -> Analysis:
    language = language or "unknown"
    analyzer = ANALYZERS.get(language, RegexAnalyzer(language))
    return analyzer.analyze(path, source)


def module_candidates(
    import_name: str, source_path: str, paths: set[str] | None = None
) -> set[str]:
    """Resolve an import to known source files without assuming one package layout."""
    if import_name.startswith(("./", "../")):
        module = posixpath.normpath(
            posixpath.join(posixpath.dirname(source_path), import_name)
        )
        candidates = {
            f"{module}{suffix}"
            for suffix in (
                ".py", ".js", ".jsx", ".ts", ".tsx", ".java", ".go", ".rs",
                ".c", ".h", ".cpp", ".hpp",
            )
        }
        candidates.update(f"{module}/index{suffix}" for suffix in (".js", ".ts", ".tsx"))
        return candidates & paths if paths is not None else candidates
    relative_level = len(import_name) - len(import_name.lstrip("."))
    module = import_name.lstrip(".").replace(".", "/")
    if import_name.startswith("."):
        parent = Path(source_path).parent
        if relative_level > 1 and len(parent.parents) >= relative_level - 1:
            parent = parent.parents[relative_level - 2]
        module = (parent / module).as_posix()
    candidates = {
        f"{module}{suffix}"
        for suffix in (".py", ".js", ".jsx", ".ts", ".tsx", ".java", ".go", ".rs")
    }
    candidates.update(f"{module}/__init__.py" for _ in (0,))
    if paths is None:
        return candidates
    candidates.update(
        path
        for path in paths
        if any(path.endswith("/" + module + suffix) for suffix in (
            ".py", ".js", ".jsx", ".ts", ".tsx", ".java", ".go", ".rs"
        ))
    )
    return candidates & paths
