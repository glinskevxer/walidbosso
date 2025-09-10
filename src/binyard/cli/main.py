"""The command line, on argparse.

Subcommands live in :mod:`binyard.cli.commands` and each one registers itself
with a parser and a handler. The handler takes a context and the parsed
arguments and returns an exit code, so nothing in a command calls
:func:`sys.exit` and every command is callable from a test.

Errors from the domain are caught here, once, and rendered as one line on
stderr with the machine code. A traceback is only printed under ``--debug``,
because a scale operator seeing a Python traceback learns nothing and phones
somebody.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from datetime import date
from typing import TextIO

from binyard import __version__
from binyard.cli.commands import bins, migrate
from binyard.cli.context import Context, OutputFormat, build_context
from binyard.errors import BinyardError

__all__ = ["build_parser", "run", "main"]

_COMMANDS = (migrate, bins)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="binyard",
        description="Grain accounting for a country elevator.",
    )
    parser.add_argument("--version", action="version", version=f"binyard {__version__}")
    parser.add_argument(
        "--database",
        help="sqlite url to work against, overriding BINYARD_DATABASE_URL",
    )
    parser.add_argument(
        "--format",
        dest="output_format",
        choices=[item.value for item in OutputFormat],
        default=OutputFormat.TEXT.value,
        help="how to render tables",
    )
    parser.add_argument(
        "--on",
        type=date.fromisoformat,
        help="the date to work as of, defaulting to today",
    )
    parser.add_argument("--quiet", action="store_true", help="drop the notes")
    parser.add_argument("--debug", action="store_true", help="show tracebacks")

    subparsers = parser.add_subparsers(dest="command", required=True)
    for module in _COMMANDS:
        module.register(subparsers)
    return parser


def run(
    argv: Sequence[str] | None = None,
    *,
    out: TextIO | None = None,
    err: TextIO | None = None,
    environment: dict[str, str] | None = None,
) -> int:
    """Parse and dispatch. Returns the exit code rather than raising it."""
    parser = build_parser()
    arguments = parser.parse_args(argv)
    stream_out = out or sys.stdout
    stream_err = err or sys.stderr

    context: Context | None = None
    try:
        context = build_context(
            out=stream_out,
            err=stream_err,
            database=arguments.database,
            output_format=OutputFormat(arguments.output_format),
            environment=environment,
            today=arguments.on,
            quiet=arguments.quiet,
        )
        return int(arguments.handler(context, arguments))
    except BinyardError as error:
        stream_err.write(f"{error.code}: {error}\n")
        if arguments.debug:
            raise
        return 2
    except KeyboardInterrupt:  # pragma: no cover - only reachable interactively
        stream_err.write("interrupted\n")
        return 130
    finally:
        if context is not None:
            context.close()


def main() -> None:  # pragma: no cover - the console entry point
    sys.exit(run())
