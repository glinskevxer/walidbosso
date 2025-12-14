#!/usr/bin/env python3
"""Catch the test mistakes a linter does not.

Three of them, all of which have shipped in this repository at least once:

**A focused test.** ``pytest.mark.only`` or a stray ``-k`` in the addopts means
CI runs a handful of tests and reports green. There is no way to notice this by
reading the summary line, because the summary line is green.

**A test with no assertion.** A test that calls the code and asserts nothing is
a smoke test wearing a test's name. Some of those are deliberate, and they say
so with ``# smoke`` on the ``def`` line.

**A test that asserts a bare local.** ``assert result`` passes for a non empty
list, a non zero number and an object with no ``__bool__``, which is almost
never what was meant. An attribute is left alone: ``assert ticket.is_open``
reads a named boolean and says exactly what it means.

The check runs on the source, not by importing, so it cannot be defeated by an
import that fails and it costs nothing.
"""

from __future__ import annotations

import argparse
import ast
import sys
from dataclasses import dataclass
from pathlib import Path

FORBIDDEN_MARKS = {"only", "focus", "wip"}


@dataclass(frozen=True)
class Finding:
    path: Path
    line: int
    problem: str

    def __str__(self) -> str:
        return f"{self.path}:{self.line}: {self.problem}"


def _is_test(node: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    return node.name.startswith("test")


def _marks(node: ast.FunctionDef | ast.AsyncFunctionDef) -> set[str]:
    found: set[str] = set()
    for decorator in node.decorator_list:
        target = decorator.func if isinstance(decorator, ast.Call) else decorator
        parts: list[str] = []
        while isinstance(target, ast.Attribute):
            parts.append(target.attr)
            target = target.value
        if isinstance(target, ast.Name):
            parts.append(target.id)
        found.update(parts)
    return found


def _asserts(node: ast.AST) -> list[ast.Assert]:
    return [child for child in ast.walk(node) if isinstance(child, ast.Assert)]


def _calls_raises(node: ast.AST) -> bool:
    for child in ast.walk(node):
        if isinstance(child, ast.Attribute) and child.attr in {"raises", "warns", "approx"}:
            return True
    return False


def _bare_truthy(test: ast.expr) -> bool:
    return isinstance(test, ast.Name)


def inspect(path: Path, source: str) -> list[Finding]:
    findings: list[Finding] = []
    try:
        tree = ast.parse(source, filename=str(path))
    except SyntaxError as error:
        return [Finding(path, error.lineno or 0, f"will not parse: {error.msg}")]

    lines = source.splitlines()
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        if not _is_test(node):
            continue

        focused = _marks(node) & FORBIDDEN_MARKS
        if focused:
            findings.append(
                Finding(path, node.lineno, f"focused with {', '.join(sorted(focused))}")
            )

        declaration = lines[node.lineno - 1] if node.lineno <= len(lines) else ""
        deliberate = "# smoke" in declaration
        statements = _asserts(node)
        if not statements and not _calls_raises(node) and not deliberate:
            findings.append(Finding(path, node.lineno, f"{node.name} asserts nothing"))
        for statement in statements:
            if _bare_truthy(statement.test):
                findings.append(
                    Finding(
                        path,
                        statement.lineno,
                        f"asserts the bare local {ast.unparse(statement.test)}",
                    )
                )
    return findings


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("paths", nargs="*", default=["tests"])
    arguments = parser.parse_args()

    files: list[Path] = []
    for raw in arguments.paths:
        path = Path(raw)
        files.extend(sorted(path.rglob("test_*.py")) if path.is_dir() else [path])

    findings: list[Finding] = []
    for path in files:
        findings.extend(inspect(path, path.read_text(encoding="utf-8")))

    for finding in findings:
        print(finding, file=sys.stderr)
    print(f"checked {len(files)} test modules, {len(findings)} problems")
    return 1 if findings else 0


if __name__ == "__main__":
    raise SystemExit(main())
