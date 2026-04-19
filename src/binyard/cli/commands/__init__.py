"""One module per subcommand, each registering a parser and a handler."""

from binyard.cli.commands import (
    bins,
    blend,
    dryer,
    feed,
    migrate,
    receive,
    report,
    settle,
    ship,
)

__all__ = [
    "bins",
    "blend",
    "dryer",
    "feed",
    "migrate",
    "receive",
    "report",
    "settle",
    "ship",
]
