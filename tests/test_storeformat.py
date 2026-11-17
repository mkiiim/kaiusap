"""The store's format: store.json, the refusal of a newer format, and the installer's migrate."""
import io
import json
import sys

import pytest

from kaiusap import build, storeformat
from kaiusap.core import Core
from kaiusap.errors import Error
from kaiusap.adapters import Identity
from kaiusap.storeformat import NewerFormat, STORE_FORMAT, migrate, read_format
from kaiusap.store import Store

from conftest import FakeProvider, send


def core(store, key="a"):
    return Core(store, Identity("codex", key, "codex:" + key, "/fixture", "root"), FakeProvider())


def newer(store, written_by="0.9.0"):
    store.root.mkdir(parents=True, exist_ok=True)
    (store.root / "store.json").write_text(json.dumps({"format": STORE_FORMAT + 1, "written_by": written_by}))


def refusal(written_by="0.9.0"):
    return (f"This store is format {STORE_FORMAT + 1}, written by kaiusap {written_by}; this kaiusap "
            f"({build.version()}) only knows format {STORE_FORMAT}. Install kaiusap {written_by} or later.")


def test_a_store_without_store_json_is_format_1_and_gets_it_on_first_write(store):
    assert read_format(store.root) == (1, None)
    assert not (store.root / "store.json").exists()
    list(store.rows("handles.jsonl"))  # a read writes nothing
    assert not store.root.exists()
    core(store).run("iam", {"handle": "alpha"})
    assert json.loads((store.root / "store.json").read_text()) == {"format": 1, "written_by": build.version()}
    assert read_format(store.root) == (1, build.version())
    assert not list(store.root.glob(".tmp-*"))


def test_an_existing_store_json_is_never_rewritten_by_a_write(store):
    store.root.mkdir(parents=True)
    (store.root / "store.json").write_text('{"format": 1, "written_by": "0.0.1"}')
    core(store).run("iam", {"handle": "alpha"})
    assert (store.root / "store.json").read_text() == '{"format": 1, "written_by": "0.0.1"}'


def test_concurrent_creators_leave_one_store_json(store, monkeypatch):
    store.root.mkdir(parents=True)
    assert storeformat.create(store.root) is True
    assert storeformat.create(store.root) is False  # linked, never over an existing one
    assert read_format(store.root)[0] == 1


def test_a_newer_format_refuses_every_write_and_reads_continue(store, pair):
    a, b = pair
    send(a)
    newer(store)
    fresh = Store(store.root)  # a new call, as each command and hook makes
    alpha, beta = core(fresh, "a"), core(fresh, "b")
    writes = [lambda: alpha.run("iam", {"handle": "gamma"}), lambda: alpha.run("release", {}),
              lambda: send(alpha), lambda: alpha.run("pause", {}),
              lambda: fresh.append("handles.jsonl", {"at": "x"}), lambda: fresh.event("moot", message_id="m"),
              lambda: fresh.set_paused(True), lambda: fresh.write_json(fresh.root / "x.json", {}),
              lambda: fresh.save_message({"id": "x"}), lambda: fresh.start({"at": "x"}), fresh.changed]
    for write in writes:
        with pytest.raises(NewerFormat) as caught:
            write()
        assert str(caught.value) == refusal() and caught.value.code == 4 and isinstance(caught.value, Error)
    inbox = beta.run("inbox", {})
    assert len(inbox["messages"]) == 1
    assert alpha.run("who", {})
    assert list(fresh.messages()) and list(fresh.rows("handles.jsonl"))
    assert not (fresh.root / "x.json").exists() and not fresh.paused
    assert json.loads((store.root / "store.json").read_text())["format"] == STORE_FORMAT + 1


def test_a_newer_format_without_written_by_still_says_what_to_do(store):
    store.root.mkdir(parents=True)
    (store.root / "store.json").write_text(json.dumps({"format": STORE_FORMAT + 1}))
    with pytest.raises(NewerFormat, match="written by a newer kaiusap; .* Install a newer kaiusap."):
        store.append("handles.jsonl", {"at": "x"})


def test_the_cli_reports_the_refusal_with_exit_code_4(store):
    from io import StringIO
    from kaiusap.cli import main
    newer(store)
    out, err = StringIO(), StringIO()
    code = main(["pause"], store=store, identity=Identity(), provider=FakeProvider(),
                stdin=StringIO(""), stdout=out, stderr=err, environ={})
    assert code == 4 and err.getvalue() == "kaiusap: " + refusal() + "\n"


@pytest.mark.parametrize("text", ["not json", "[]", "{}", '{"format": "2"}', '{"format": true}',
                                  '{"format": 0}', '{"format": 2.0}'])
def test_a_malformed_store_json_is_format_1_with_a_warning_and_kept(store, text):
    store.root.mkdir(parents=True)
    (store.root / "store.json").write_text(text)
    warnings = []
    assert read_format(store.root, warnings) == (1, None) and len(warnings) == 1
    store.append("handles.jsonl", {"at": "x"})
    store.event("moot", message_id="m")
    assert store.warnings == ["store.json in the store is malformed; kaiusap treats the store as format 1 "
                              "and leaves the file as it is."]
    assert (store.root / "store.json").read_text() == text


def test_a_hook_logs_the_refusal_and_exits_quietly(store, monkeypatch, capsys):
    import kaiusap.hooks
    for name in ("KAIUSAP_HOOK_END", "KAIUSAP_HOOK_HARD_END", "KAIUSAP_HOOK_START", "KAIUSAP_NATIVE_ENTRY",
                 "KAIUSAP_NATIVE_RECORDED"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("KAIUSAP_HOME", str(store.root))
    newer(store)
    sid = "019a0000-0000-7000-8000-000000000000"
    data = dict(session_id=sid, turn_id="t", transcript_path=f"/fixture/rollout-{sid}.jsonl", cwd="/fixture",
                hook_event_name="UserPromptSubmit", model="m", permission_mode="default", prompt="Hello")
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(data)))
    assert kaiusap.hooks.main(["codex", "UserPromptSubmit"]) == 0
    assert capsys.readouterr().out == ""
    log = (store.root / "hook-errors.log").read_text()
    assert "NewerFormat: " + refusal() in log
    assert sorted(p.name for p in store.root.iterdir()) == ["hook-errors.log", "store.json"]


def test_migrate_runs_each_migration_in_order_then_writes_store_json(store, monkeypatch):
    store.root.mkdir(parents=True)
    ran = []
    monkeypatch.setattr(storeformat, "STORE_FORMAT", 4)
    monkeypatch.setattr(storeformat, "MIGRATIONS", {
        4: lambda root: ran.append((4, read_format(root)[0])),
        2: lambda root: ran.append((2, read_format(root)[0])),
        3: lambda root: ran.append((3, read_format(root)[0]))})
    assert migrate(store.root, to=3) == (1, 3)
    assert ran == [(2, 1), (3, 1)]  # store.json changes once, after the last
    assert read_format(store.root) == (3, build.version())
    assert migrate(store.root, to=4) == (3, 4) and ran[-1] == (4, 3)
    assert read_format(store.root)[0] == 4
    with pytest.raises(Error, match="can't be migrated back to format 2"):
        migrate(store.root, to=2)
    assert read_format(store.root)[0] == 4 and len(ran) == 3


def test_migrate_refuses_a_format_it_has_no_migration_for_or_doesnt_know(store, monkeypatch):
    store.root.mkdir(parents=True)
    with pytest.raises(Error, match="knows formats 1 to"):
        migrate(store.root, to=STORE_FORMAT + 1)
    monkeypatch.setattr(storeformat, "STORE_FORMAT", 3)
    monkeypatch.setattr(storeformat, "MIGRATIONS", {3: lambda root: None})
    with pytest.raises(Error, match="No migration to store format 2"):
        migrate(store.root, to=3)
    assert not (store.root / "store.json").exists()


def test_migrate_to_the_current_format_writes_a_missing_store_json(store):
    store.root.mkdir(parents=True)
    assert migrate(store.root) == (1, STORE_FORMAT)
    assert read_format(store.root) == (STORE_FORMAT, build.version())


def test_the_native_engine_knows_the_same_format():
    # native/hook.rs refuses its appends to a newer store with its own copy of the constant.
    from pathlib import Path
    source = (Path(__file__).resolve().parents[1] / "native" / "hook.rs").read_text()
    assert f"const STORE_FORMAT: i64 = {STORE_FORMAT};" in source
