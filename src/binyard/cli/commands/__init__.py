"""One module per subcommand, each registering a parser and a handler."""

from binyard.cli.commands import bins, migrate, receive

__all__ = ["bins", "migrate", "receive"]
