"""Dispatch hooks before importing the CLI."""
import sys


def main():
    if sys.argv[1:2] == ["hook"]:
        from .hooks import main as hook_main
        return hook_main(sys.argv[2:])
    if sys.argv[1:2] == ["mcp"]:
        from .mcp import main as mcp_main
        return mcp_main(sys.argv[2:])
    from .cli import main as cli_main
    return cli_main(sys.argv[1:])
