"""`version`: which kaiusap this is, for a model (whoami, help), the session map (`version --json`) and a
long-running MCP server that kaiusap was reinstalled under (whoami's warning)."""
from io import StringIO
import json
import subprocess
import sys

import pytest

from kaiusap import build
from kaiusap.cli import main

COMMIT = "34c325b0123456789abcdef0123456789abcdef0"


@pytest.fixture
def record(tmp_path, monkeypatch):
    path = tmp_path / "build.json"
    monkeypatch.setattr(build, "RECORD", path)
    monkeypatch.setattr(build, "_running", None)
    def write(**fields):
        path.write_text(json.dumps({"commit": COMMIT, "commit_date": "2026-11-11T08:46:00-05:00", "dirty": False, **fields}))
    return write


def local(date):
    """A commit date as the label shows it: this machine's local date and time."""
    from datetime import datetime
    return datetime.fromisoformat(date).astimezone().strftime("%Y-%m-%d %H:%M")


def test_the_commit_time_is_shown_in_local_time_and_left_out_when_unreadable():
    from datetime import datetime, timezone
    utc = datetime(2026, 11, 16, 20, 22, tzinfo=timezone.utc)
    assert build.committed("2026-11-16T15:22:00-05:00") == utc.astimezone().strftime("%Y-%m-%d %H:%M")
    assert build.committed("not a date") is None and build.committed(None) is None
    assert build.label({"version": "0.2.0", "commit": "80abb5a" + "0" * 33, "commit_date": "bad", "dirty": False}) == \
        "0.2.0 (80abb5a)"


def run(store, *argv):
    out = StringIO()
    code = main([*argv], store=store, stdin=StringIO(""), stdout=out, stderr=StringIO(), environ={})
    return code, out.getvalue()


def test_version_names_the_commit_its_date_and_a_dirty_build(record, store):
    record()
    code, out = run(store, "version", "--json")
    assert code == 0 and json.loads(out) == {"version": build.version(), "commit": COMMIT,
                                             "commit_date": "2026-11-11T08:46:00-05:00", "dirty": False, "schema": 1}
    assert run(store, "version")[1] == f"kaiusap {build.version()} (34c325b, committed {local('2026-11-11T08:46:00-05:00')})\n"
    build._running = None
    record(dirty=True)
    assert run(store, "version")[1] == f"kaiusap {build.version()} (34c325b+dirty, committed {local('2026-11-11T08:46:00-05:00')})\n"


@pytest.mark.parametrize("text", [None, "{bad", "[]", '{"commit": ""}', '{"commit": 5, "dirty": true}'])
def test_a_build_without_a_readable_record_has_a_null_commit(record, store, tmp_path, text):
    if text is not None:
        build.RECORD.write_text(text)
    code, out = run(store, "version", "--json")
    assert json.loads(out) | {"schema": 1} == {"version": build.version(), "commit": None, "commit_date": None,
                                               "dirty": None, "schema": 1}
    assert run(store, "version")[1] == f"kaiusap {build.version()} (commit unknown)\n"


def test_version_runs_from_a_plain_shell_and_writes_nothing(record, store):
    record()
    assert run(store, "version")[0] == 0 and not store.root.exists()


def test_dash_dash_version_is_the_version_verb(record, store):
    record()
    assert run(store, "--version") == run(store, "version")
    assert run(store, "--version", "--json") == run(store, "version", "--json")


def test_whoami_and_help_carry_the_build(record, store):
    record()
    assert json.loads(run(store, "whoami", "--json")[1])["kaiusap"]["commit"] == COMMIT
    help_json = json.loads(run(store, "help", "--json")[1])
    assert help_json["kaiusap"]["commit"] == COMMIT
    assert run(store, "help")[1].rstrip("\n").endswith(f"kaiusap {build.version()} (34c325b, committed {local('2026-11-11T08:46:00-05:00')})")


def test_a_server_running_an_older_build_than_the_installed_one_says_so(record, store):
    record()
    assert build.running()["commit"] == COMMIT and build.stale_warning() is None
    record(commit="4b1e9a0" + "0" * 33, commit_date="2026-11-12T01:00:00-05:00")  # reinstalled under the server
    assert build.running()["commit"] == COMMIT  # what the process loaded doesn't change
    assert build.stale_warning() == (f"This session's kaiusap server runs {build.version()} (34c325b, committed {local('2026-11-11T08:46:00-05:00')}), but "
                                     f"kaiusap {build.version()} (4b1e9a0, committed {local('2026-11-12T01:00:00-05:00')}) is installed. Tell the user: "
                                     "restarting this session loads the installed one.")


@pytest.mark.parametrize("tool,arguments", [("help", {}), ("who", {}), ("whoami", {}), ("inbox", {"all": "maybe"})])
def test_every_mcp_result_carries_the_notice_after_a_reinstall(record, store, tool, arguments):
    """Every tool's result says so, a refusal included (an unidentified whoami, a bad argument)."""
    from kaiusap.mcp import call, version
    record()
    build.running()
    text, result, failed = call("codex", tool, arguments, {})
    assert "warnings" not in result or not any("restarting this session" in w for w in result["warnings"])
    record(commit="4b1e9a0" + "0" * 33)
    text, result, failed = call("codex", tool, arguments, {})
    assert any("restarting this session loads the installed one" in w for w in result["warnings"])
    assert "Warning: This session's kaiusap server runs" in text
    assert version() == f"{build.version()}+34c325b"  # the server keeps reporting what it runs
