"""`~/.kaiusap/install.json`: the changes the
installer made, written as each one finishes, atomically. Stopping before the first change writes nothing.

Schema (version 1):

    {"schema": 1,
     "version": "0.1.0", "commit": "<full sha>" | null, "commit_date": "<ISO 8601>" | null,
     "dirty": bool | null,                                                      # what this run installs
     "repo": "<checkout path>",
     "run": "<started_at>/<random>",                                           # this run's id
     "started_at": "<iso>", "finished_at": "<iso>" | null,                     # null while unfinished
     "planned": <changes this run planned, done earlier ones included>,
     "changes": [{"step": "<id>", "description": "<plan line>", "kind": "command" | "edit" | "link" | "store",
                  "record": {...}, "file": "<path>", "key": "<key>"  (edits only),
                  "at": "<iso>", "run": "<id of the run that made it>",
                  "undone_at": "<iso>"  (only once the uninstaller removed it)}],
     "previous": {"version", "commit", "commit_date", "dirty", "store_backup": "<path>" | null, "store_format": int | null,
                  "run", "started_at", "finished_at", "planned": the replaced record's own,
                  "upgraded_at": "<started_at of the upgrade>", "upgrade_run": "<id of the upgrade's run>"}
                 | null,                                                         # set by an upgrade
     "rollback": {"started_at", "done": ["<step id>", ...]} | absent,            # while a rollback runs
     "uninstall": {"started_at", "done": ["<step id>", ...]} | absent}           # while an uninstall runs

A change's `record` is, by kind: `command` {"commands": [[argv...], ...]}; `edit` the record `installer.edits.apply`
returned (with its "backup"); `link` {"link": path, "target": path}; `store` {"backup": path} or {"from": n, "to": m}.
"""
import json
import os

from .context import Stop

NAME = "install.json"
SCHEMA = 1


def path(ctx):
    return ctx.kaiusap_home / NAME


def load(ctx):
    """The record, or None when there is none."""
    try:
        data = json.loads(path(ctx).read_text())
    except FileNotFoundError:
        return None
    except (OSError, ValueError, UnicodeError) as exc:
        raise Stop(f"{ctx.short(path(ctx))} can't be read ({type(exc).__name__}). Move it aside, then run "
                   "./install.sh again. Nothing was changed.") from None
    if not isinstance(data, dict) or not isinstance(data.get("changes"), list):
        raise Stop(f"{ctx.short(path(ctx))} isn't an install record. Move it aside, then run ./install.sh again. "
                   "Nothing was changed.")
    return data


def save(ctx, record):
    """Write the record atomically: a temporary file in the same directory, flushed, then renamed over."""
    target = path(ctx)
    target.parent.mkdir(parents=True, exist_ok=True)
    temp = target.with_name(f".{NAME}.{os.getpid()}.tmp")
    with open(temp, "w") as stream:
        json.dump(record, stream, indent=2, ensure_ascii=False)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temp, target)


def finished(record):
    return bool(record) and record.get("finished_at") is not None


def run_changes(record, run=None):
    """The changes one run made (by default the record's own run), in order."""
    if not record:
        return []
    run = record.get("run") if run is None else run
    return [c for c in record["changes"] if c.get("run") == run]


def live_changes(record):
    """Every change not yet removed by the uninstaller."""
    return [c for c in (record or {}).get("changes", []) if not c.get("undone_at")]


def new(ctx, build, repo, previous=None, earlier=()):
    started = ctx.iso_now()
    return {"schema": SCHEMA, "version": build["version"], "commit": build["commit"],
            "commit_date": build.get("commit_date"), "dirty": build["dirty"],
            "repo": str(repo), "run": f"{started}/{os.urandom(3).hex()}", "started_at": started, "finished_at": None,
            "planned": 0, "changes": list(earlier), "previous": previous}
