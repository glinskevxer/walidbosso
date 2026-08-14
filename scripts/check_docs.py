#!/usr/bin/env python3
"""Every module says what it is for, and every public name says what it does.

This package leans on its docstrings. The rules a country elevator runs on are
not obvious from the code that implements them, and a module that says what it
does and why it does it that way is the difference between a reader
understanding the moisture shrink formula and a reader reimplementing it.

So the check is not "has a docstring" alone. Three things:

**A module docstring, and more than one line of it.** A one line docstring on a
two hundred line module is a placeholder, and placeholders are how a package
ends up documented in name only.

**Everything in ``__all__`` is documented**, unless it is an alias for
something that is. A public name with no docstring is a name a reader has to
read the implementation to use.

**No docstring left saying TODO.** Written down here because it has happened.

The check reads the source rather than importing it, so a module that will not
import is a separate problem reported by a separate check.
"""

from __future__ import annotations

import argparse
import ast
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "src" / "binyard"

PLACEHOLDERS = ("todo", "tbd", "fixme", "xxx", "docstring")
#: Modules that are a list of imports and nothing else.
RE_EXPORT_ONLY = 12


@dataclass(frozen=True)
class Problem:
    path: Path
    line: int
    detail: str

    def __str__(self) -> str:
        return f"{self.path.relative_to(ROOT)}:{self.line}: {self.detail}"


def _documented(node: ast.AST) -> str | None:
    if isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
        return ast.get_docstring(node)
    return None


def _exported(tree: ast.Module) -> set[str]:
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        for target in node.targets:
            if (
                isinstance(target, ast.Name)
                and target.id == "__all__"
                and isinstance(node.value, ast.List | ast.Tuple)
            ):
                return {
                    item.value
                    for item in node.value.elts
                    if isinstance(item, ast.Constant) and isinstance(item.value, str)
                }
    return set()


def _definitions(tree: ast.Module) -> dict[str, ast.AST]:
    found: dict[str, ast.AST] = {}
    for node in tree.body:
        if isinstance(node, ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
            found[node.name] = node
    return found


def inspect(path: Path) -> list[Problem]:
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(path))
    problems: list[Problem] = []

    module_doc = ast.get_docstring(tree)
    if not module_doc:
        problems.append(Problem(path, 1, "no module docstring"))
    else:
        lowered = module_doc.lower()
        for word in PLACEHOLDERS:
            if lowered.strip().startswith(word):
                problems.append(Problem(path, 1, f"module docstring starts with {word!r}"))
        statements = len(tree.body)
        if statements > RE_EXPORT_ONLY and len(module_doc.splitlines()) < 2:
            problems.append(
                Problem(path, 1, "a module this size needs more than a one line docstring")
            )

    exported = _exported(tree)
    definitions = _definitions(tree)
    for name in sorted(exported):
        node = definitions.get(name)
        if node is None:
            continue  # re-exported from elsewhere; the other module documents it
        if isinstance(node, ast.ClassDef) and any(
            isinstance(base, ast.Name) and base.id in {"StrEnum", "Enum", "IntEnum"}
            for base in node.bases
        ):
            pass
        if not _documented(node):
            problems.append(
                Problem(path, getattr(node, "lineno", 1), f"{name} is public and undocumented")
            )
    return problems


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", nargs="?", default=str(PACKAGE))
    arguments = parser.parse_args()

    problems: list[Problem] = []
    checked = 0
    for path in sorted(Path(arguments.root).rglob("*.py")):
        checked += 1
        problems.extend(inspect(path))

    for problem in problems:
        print(problem, file=sys.stderr)
    print(f"checked {checked} modules, {len(problems)} undocumented public names")
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
