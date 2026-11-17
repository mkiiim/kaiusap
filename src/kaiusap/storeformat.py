"""The store's format version: `store.json` at the store root.

`{"format": <int>, "written_by": "<the kaiusap version that last set the format>"}`. A store without the
file is format 1, as every store before it was. kaiusap's commands and hooks never change a store's format:
they write `store.json` only to create it, at format 1, in a store that lacks it, and refuse to write to a
store whose format is newer than `STORE_FORMAT`. Only the installer upgrades a store, through `migrate`.
"""
import fcntl
import json
import os
from pathlib import Path

from .errors import Error

STORE_FORMAT = 1  # the format this kaiusap reads and writes
LEGACY_FORMAT = 1  # the format of a store with no store.json
FILE = "store.json"

# The upgrade to each format from the one before it: MIGRATIONS[n](root) turns a format n-1 store into
# format n, in place. `migrate` writes store.json after the last one; a migration never writes it.
MIGRATIONS = {}


class NewerFormat(Error):
    """A write refused: the store is a newer format than this kaiusap knows. Exit code 4."""

    def __init__(self, found, written_by):
        from .build import version
        if written_by:
            text = (f"This store is format {found}, written by kaiusap {written_by}; this kaiusap ({version()}) "
                    f"only knows format {STORE_FORMAT}. Install kaiusap {written_by} or later.")
        else:
            text = (f"This store is format {found}, written by a newer kaiusap; this kaiusap ({version()}) "
                    f"only knows format {STORE_FORMAT}. Install a newer kaiusap.")
        super().__init__(text, 4)
        self.found, self.written_by = found, written_by


def read_format(root, warnings=None):
    """`(format, written_by)` from `<root>/store.json`. A missing file is `(1, None)`. A malformed one (not
    JSON, not an object, or `format` missing or not a positive integer) is `(1, None)` too, with a warning
    appended to `warnings` when a list is given; it is never rewritten. `written_by` is None unless a string."""
    try:
        text = (Path(root) / FILE).read_text()
    except FileNotFoundError:
        return LEGACY_FORMAT, None
    try:
        data = json.loads(text)
    except (ValueError, UnicodeError):
        data = None
    found = data.get("format") if isinstance(data, dict) else None
    if type(found) is not int or found < 1:
        if warnings is not None:
            warnings.append(f"{FILE} in the store is malformed; kaiusap treats the store as format "
                            f"{LEGACY_FORMAT} and leaves the file as it is.")
        return LEGACY_FORMAT, None
    written_by = data.get("written_by")
    return found, written_by if isinstance(written_by, str) and written_by else None


def check(root, warnings=None):
    """Raise NewerFormat when the store at `root` is a format newer than STORE_FORMAT; else its format."""
    found, written_by = read_format(root, warnings)
    if found > STORE_FORMAT:
        raise NewerFormat(found, written_by)
    return found


def _sync(fd, warn):
    """fsync; with `warn` (the Store's), a failure is its warning, as for the store's other flushes."""
    try:
        os.fsync(fd)
    except OSError as exc:
        if warn is None:
            raise
        warn(exc)


def _sync_dir(path, warn=None):
    fd = os.open(path, os.O_RDONLY)
    try:
        _sync(fd, warn)
    finally:
        os.close(fd)


def _temporary(root, value, warn=None):
    """A synced temporary file in `root` holding `value` as store.json's text; its path."""
    from .store import identifier
    temp = Path(root) / (".tmp-" + FILE + "-" + identifier())
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        data = (json.dumps(value) + "\n").encode()
        while data:
            data = data[os.write(fd, data):]
        _sync(fd, warn)
    finally:
        os.close(fd)
    return temp


def create(root, warn=None):
    """Write store.json at format 1 in a store that lacks it, never over one: a temporary file linked into
    place, so concurrent writers each try once and the first wins. True when this call created it. With
    `warn`, a failed flush is a warning, not an error."""
    from .build import version
    temp = _temporary(root, {"format": LEGACY_FORMAT, "written_by": version()}, warn)
    try:
        try:
            os.link(temp, Path(root) / FILE)
        except FileExistsError:
            return False
        _sync_dir(root, warn)
        return True
    finally:
        temp.unlink(missing_ok=True)


def write_format(root, found):
    """Replace store.json with format `found`, written by this kaiusap: a synced temporary file renamed over it."""
    from .build import version
    temp = _temporary(root, {"format": found, "written_by": version()})
    try:
        os.replace(temp, Path(root) / FILE)
        _sync_dir(root)
    finally:
        temp.unlink(missing_ok=True)


def migrate(root, to=STORE_FORMAT):
    """Upgrade the store at `root` to format `to` (the installer's step, never a command's): each registered
    migration from the current format + 1 to `to`, in order, under the global lock, then store.json. Refuses
    a `to` lower than the store's format, above STORE_FORMAT, or with a migration missing on the way. A store
    already at `to` gets store.json written if it lacks one. Returns `(from, to)`."""
    root = Path(root)
    if type(to) is not int or to < 1 or to > STORE_FORMAT:
        raise Error(f"This kaiusap can't migrate a store to format {to}; it knows formats 1 to {STORE_FORMAT}.")
    if not root.is_dir():
        raise Error(f"No store at {root} to migrate.", 3)
    fd = os.open(root / "lock", os.O_RDWR | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        current, _ = read_format(root)
        if to < current:
            raise Error(f"The store is format {current}; it can't be migrated back to format {to}. "
                        "Restore the store's backup instead.")
        missing = [n for n in range(current + 1, to + 1) if n not in MIGRATIONS]
        if missing:
            raise Error(f"No migration to store format {missing[0]}.")
        for n in range(current + 1, to + 1):
            MIGRATIONS[n](root)
        if to != current or not (root / FILE).exists():
            write_format(root, to)
        return current, to
    finally:
        os.close(fd)
