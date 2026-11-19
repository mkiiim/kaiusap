"""Which kaiusap this is (`version`): the package version and the build record the install wrote.

`setup.py` writes `build.json` beside this module when it builds the package: the git commit, that commit's
date and whether the working tree had uncommitted changes. A copy run from source has no record, and its
commit is null. `running()` is the build this process loaded, read once; `installed()` reads the files on disk
again, so a long-running MCP server can tell that kaiusap was reinstalled under it.
"""
import json
from pathlib import Path

RECORD = Path(__file__).with_name("build.json")
_running = None


def version():
    from importlib.metadata import PackageNotFoundError, version as installed
    try:
        return installed("kaiusap")
    except PackageNotFoundError:
        return "0.3.0"


def installed(record=None):
    """`{version, commit, commit_date, dirty}` as the files on disk say now; unknown fields null."""
    try:
        data = json.loads(Path(record or RECORD).read_text())
    except (OSError, ValueError):
        data = {}
    data = data if isinstance(data, dict) else {}
    commit = data.get("commit") if isinstance(data.get("commit"), str) and data.get("commit") else None
    return {"version": version(), "commit": commit,
            "commit_date": data.get("commit_date") if commit and isinstance(data.get("commit_date"), str) else None,
            "dirty": data.get("dirty") if commit and isinstance(data.get("dirty"), bool) else None}


def running():
    """The build this process loaded: read at the first call and kept for the process's life."""
    global _running
    if _running is None:
        _running = installed()
    return _running


def committed(date):
    """A commit's ISO 8601 date as this machine's local date and time, `2026-11-16 15:22`, or None."""
    from datetime import datetime
    try:
        return datetime.fromisoformat(date).astimezone().strftime("%Y-%m-%d %H:%M")
    except (TypeError, ValueError):
        return None


def label(build):
    """`0.1.0 (34c325b, committed 2026-11-11 08:46)`, `0.1.0 (34c325b+dirty, …)`, or `0.1.0 (commit unknown)`.
    The time is the commit's, in local time: the same for every build of that commit."""
    if not build.get("commit"):
        return f"{build['version']} (commit unknown)"
    commit = build["commit"][:7] + ("+dirty" if build.get("dirty") else "")
    when = committed(build.get("commit_date"))
    return f"{build['version']} ({commit}" + (f", committed {when})" if when else ")")


def stale_warning():
    """For a long-running server: a warning when the installed build differs from the one it runs, else None."""
    now, then = installed(), running()
    if (now["version"], now["commit"], now["dirty"]) == (then["version"], then["commit"], then["dirty"]):
        return None
    return (f"This session's kaiusap server runs {label(then)}, but kaiusap {label(now)} is installed. "
            "Tell the user: restarting this session loads the installed one.")
