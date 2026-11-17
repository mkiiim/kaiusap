"""`python -m installer`, as `./install.sh` runs it."""
import sys

from .context import Context, Terminal
from .flow import main


def run():
    terminal = Terminal()
    ctx = Context(ask=terminal.ask, interactive=terminal.interactive)
    return main(sys.argv[1:], ctx)


if __name__ == "__main__":
    sys.exit(run())
