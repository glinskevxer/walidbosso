#!/usr/bin/env python3
"""Refuse a float literal anywhere near a quantity.

The whole package rests on one rule: money, weights, bushels and percentages
are exact integers underneath, and nothing converts them through a float. The
rule is easy to state and easy to break with one ``0.5`` in a rounding helper,
and the resulting error is a cent here and a hundredth of a bushel there, which
nobody notices until a settlement will not reconcile.

So this walks the package's syntax and refuses a float literal outside the few
places one is legitimate. The allowances are listed rather than pattern
matched, because a pattern would grow to fit whatever was written next.
"""

from __future__ import annotations

import argparse
import ast
import sys
from pathlib import Path

#: Modules where a float is genuinely the right type.
ALLOWED_MODULES = {
    # Histogram bucket edges and elapsed seconds. Neither is money.
    "observability/metrics.py",
}


def offenders(path: Path, source: str) -> list[tuple[int, str]]:
    tree = ast.parse(source, filename=str(path))
    found: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, float):
            found.append((node.lineno, repr(node.value)))
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "float"
        ):
            found.append((node.lineno, "a call to float()"))
    return found


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", nargs="?", default="src/binyard")
    arguments = parser.parse_args()

    problems = 0
    checked = 0
    for path in sorted(Path(arguments.root).rglob("*.py")):
        relative = str(path).split(arguments.root.rstrip("/") + "/", 1)[-1]
        if relative in ALLOWED_MODULES:
            continue
        checked += 1
        for line, what in offenders(path, path.read_text(encoding="utf-8")):
            print(f"{path}:{line}: {what} in a module that has to stay exact", file=sys.stderr)
            problems += 1

    print(f"checked {checked} modules, {problems} floats where there should be none")
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
