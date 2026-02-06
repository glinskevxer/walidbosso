"""One module per subcommand, each registering a parser and a handler."""

from binyard.cli.commands import bins, blend, feed, migrate, receive, report, ship

__all__ = ["bins", "blend", "feed", "migrate", "receive", "report", "ship"]
