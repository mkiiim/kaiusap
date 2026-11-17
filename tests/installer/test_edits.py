"""installer/edits.py: settings edits by key."""

import json
import os
import stat
import sys
from pathlib import Path

import pytest

tomlkit = pytest.importorskip("tomlkit")

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))  # the repo root: installer/ isn't installed

from installer.edits import MISSING, CannotEdit, JsonKey, TomlListItem, apply, check, describe, undo  # noqa: E402

STAMP = "261113164000"
LATER = "261113170000"
ENTRY = {"type": "local", "command": ["kaiusap", "mcp", "opencode2"], "codemode": False}
TITLE_DEFAULT = ("activity", "thread-name", "project-name")


def title(file):
    return TomlListItem(str(file), ("tui",), "terminal_title", "thread-id", aliases=("session-id",),
                        default=TITLE_DEFAULT)


def mcp(file):
    return JsonKey(str(file), ("mcp", "servers", "kaiusap"), ENTRY)


def roundtrip(record):
    """Records go into install.json: they must survive JSON."""
    return json.loads(json.dumps(record))


# --- TOML ---------------------------------------------------------------------------------------

CODEX = '''# my codex config
model = "gpt-5"  # the model

[tui]
# what the title shows
terminal_title = ["activity", "thread-name"]  # keep short
theme = "dark"

[projects."/x"]
trust_level = "trusted"
'''


def test_toml_append_keeps_comments_and_backs_up(tmp_path):
    f = tmp_path / "config.toml"
    f.write_text(CODEX)
    os.chmod(f, 0o640)
    spec = title(f)
    assert check(spec) == "missing"
    record = apply(spec, STAMP)
    text = f.read_text()
    assert text == CODEX.replace('["activity", "thread-name"]', '["activity", "thread-name", "thread-id"]')
    assert check(spec) == "present"
    assert stat.S_IMODE(f.stat().st_mode) == 0o640
    backup = tmp_path / f"config.toml.kaiusap-backup-{STAMP}"
    assert backup.read_text() == CODEX
    assert record == roundtrip(record)
    assert record["kind"] == "toml_list_item" and record["file"] == str(f)
    assert record["table"] == ["tui"] and record["key"] == "terminal_title" and record["item"] == "thread-id"
    assert record["before"] == ["activity", "thread-name"] and record["before_present"] is True
    assert record["written"] == ["activity", "thread-name", "thread-id"]
    assert record["backup"] == str(backup) and record["created_file"] is False

    assert undo(roundtrip(record), LATER) == "removed"
    assert f.read_text() == CODEX
    assert (tmp_path / f"config.toml.kaiusap-backup-{LATER}").read_text() == text
    assert undo(record, LATER) == "absent"


def test_toml_multiline_array_keeps_its_style(tmp_path):
    f = tmp_path / "config.toml"
    original = '[tui]\nterminal_title = [\n  "activity",  # first\n  "thread-name",\n]\n'
    f.write_text(original)
    record = apply(title(f), STAMP)
    assert f.read_text() == '[tui]\nterminal_title = [\n  "activity",  # first\n  "thread-name",\n  "thread-id",\n]\n'
    assert undo(record, LATER) == "removed"
    assert f.read_text() == original


def test_toml_alias_counts_as_present(tmp_path):
    f = tmp_path / "config.toml"
    f.write_text('[tui]\nterminal_title = ["activity", " Session-ID "]\n')
    assert check(title(f)) == "present"


def test_apply_refuses_unless_missing(tmp_path):
    """The user's own item has no record, so undo can never remove it: apply won't run when it's present."""
    f = tmp_path / "config.toml"
    f.write_text('[tui]\nterminal_title = ["thread-id"]\n')
    with pytest.raises(ValueError):
        apply(title(f), STAMP)
    assert not list(tmp_path.glob("*.kaiusap-backup-*"))
    j = tmp_path / "settings.json"
    j.write_text('{"opencode2_command": "mine"}\n')
    spec = JsonKey(str(j), ("opencode2_command",), "opencode2")
    assert check(spec) == "conflict"
    with pytest.raises(ValueError):
        apply(spec, STAMP)
    assert j.read_text() == '{"opencode2_command": "mine"}\n'


def test_toml_absent_key_gets_the_default_list(tmp_path):
    f = tmp_path / "config.toml"
    original = '[tui]\ntheme = "dark"\n\n[other]\nx = 1\n'
    f.write_text(original)
    record = apply(title(f), STAMP)
    doc = tomlkit.parse(f.read_text())
    assert doc["tui"]["terminal_title"] == ["activity", "thread-name", "project-name", "thread-id"]
    assert doc["tui"]["theme"] == "dark" and doc["other"]["x"] == 1
    assert record["before"] is None and record["before_present"] is False
    assert record["written"] == ["activity", "thread-name", "project-name", "thread-id"]
    assert undo(record, LATER) == "removed"
    assert f.read_text() == original


def test_toml_default_list_changed_by_the_user_only_loses_the_item(tmp_path):
    f = tmp_path / "config.toml"
    f.write_text('[tui]\ntheme = "dark"\n')
    record = apply(title(f), STAMP)
    doc = tomlkit.parse(f.read_text())
    doc["tui"]["terminal_title"].append("spinner")
    f.write_text(tomlkit.dumps(doc))
    assert undo(record, LATER) == "removed"
    assert tomlkit.parse(f.read_text())["tui"]["terminal_title"] == ["activity", "thread-name", "project-name", "spinner"]


def test_toml_creates_table_and_file(tmp_path):
    f = tmp_path / "codex" / "config.toml"
    record = apply(title(f), STAMP)
    assert record["created_file"] is True and record["backup"] is None
    assert tomlkit.parse(f.read_text())["tui"]["terminal_title"][-1] == "thread-id"
    assert undo(record, LATER) == "removed"
    assert not f.exists()

    f.parent.mkdir(exist_ok=True)
    f.write_text('model = "x"\n')
    record = apply(title(f), STAMP)
    assert record["created_tables"] == ["tui"]
    assert undo(record, LATER) == "removed"
    assert "tui" not in tomlkit.parse(f.read_text()) and tomlkit.parse(f.read_text())["model"] == "x"


def test_toml_undo_after_the_user_changed_it(tmp_path):
    f = tmp_path / "config.toml"
    f.write_text('[tui]\nterminal_title = ["activity"]\n')
    record = apply(title(f), STAMP)
    changed = '[tui]\nterminal_title = ["activity", "Thread-ID"]\n'
    f.write_text(changed)
    assert undo(record, LATER) == "changed"
    assert f.read_text() == changed
    assert not (tmp_path / f"config.toml.kaiusap-backup-{LATER}").exists()
    f.write_text('[tui]\nterminal_title = "plain"\n')
    assert undo(record, LATER) == "changed"


def test_toml_malformed(tmp_path):
    f = tmp_path / "config.toml"
    f.write_text('[tui\nterminal_title = ["a"\n')
    with pytest.raises(CannotEdit) as e:
        check(title(f))
    assert 'terminal_title = ["activity", "thread-name", "project-name", "thread-id"]' in e.value.instruction
    assert "[tui]" in e.value.instruction
    with pytest.raises(CannotEdit):
        apply(title(f), STAMP)
    assert not list(tmp_path.glob("*.kaiusap-backup-*"))


def test_toml_conflict_when_not_a_list(tmp_path):
    f = tmp_path / "config.toml"
    f.write_text('tui = "nope"\n')
    assert check(title(f)) == "conflict"
    f.write_text('[tui]\nterminal_title = "nope"\n')
    assert check(title(f)) == "conflict"


def test_missing_file_is_missing(tmp_path):
    assert check(title(tmp_path / "none.toml")) == "missing"
    assert check(mcp(tmp_path / "none.json")) == "missing"


# --- JSON and JSONC -----------------------------------------------------------------------------

JSONC = '''// OpenCode config
{
  "$schema": "https://opencode.ai/config.json",
  /* the model */
  "model": "x", // trailing note
  "mcp": {
    "servers": {
      "other": { "type": "local", "command": ["other"] }, // theirs
    },
  },
}
'''


def test_jsonc_insert_keeps_comments_and_trailing_commas(tmp_path):
    f = tmp_path / "opencode.jsonc"
    f.write_text(JSONC)
    spec = mcp(f)
    assert check(spec) == "missing"
    record = apply(spec, STAMP)
    text = f.read_text()
    assert text == JSONC.replace(
        '      "other": { "type": "local", "command": ["other"] }, // theirs\n',
        '      "other": { "type": "local", "command": ["other"] }, // theirs\n'
        '      "kaiusap": {\n'
        '        "type": "local",\n'
        '        "command": [\n'
        '          "kaiusap",\n'
        '          "mcp",\n'
        '          "opencode2"\n'
        '        ],\n'
        '        "codemode": false\n'
        '      },\n')
    assert check(spec) == "present"
    assert record["kind"] == "json_key" and record["path"] == ["mcp", "servers", "kaiusap"]
    assert record["key"] is None and record["value"] == ENTRY and record["written"] == ENTRY
    assert record["before"] is None and record["before_present"] is False
    assert record["created_parents"] == []
    assert (tmp_path / f"opencode.jsonc.kaiusap-backup-{STAMP}").read_text() == JSONC
    assert record == roundtrip(record)
    assert undo(roundtrip(record), LATER) == "removed"
    assert f.read_text() == JSONC


def test_jsonc_last_member_without_comma(tmp_path):
    f = tmp_path / "opencode.jsonc"
    original = '{\n  "model": "x" // note\n}\n'
    f.write_text(original)
    record = apply(JsonKey(str(f), ("opencode2_command",), "opencode2"), STAMP)
    assert f.read_text() == '{\n  "model": "x", // note\n  "opencode2_command": "opencode2"\n}\n'
    assert undo(record, LATER) == "removed"
    assert f.read_text() == '{\n  "model": "x" // note\n}\n'


def test_json_creates_parents_and_removes_them(tmp_path):
    f = tmp_path / "opencode.json"
    original = '{\n    "model": "x"\n}\n'
    f.write_text(original)
    record = apply(mcp(f), STAMP)
    text = f.read_text()
    assert json.loads(text) == {"model": "x", "mcp": {"servers": {"kaiusap": ENTRY}}}
    assert '\n    "mcp": {\n        "servers": {\n            "kaiusap": {\n' in text  # the file's own 4-space indent
    assert record["created_parents"] == ["mcp", "servers"]
    assert undo(record, LATER) == "removed"
    assert f.read_text() == original


def test_json_parent_the_user_added_to_is_kept(tmp_path):
    f = tmp_path / "opencode.json"
    f.write_text('{"model": "x"}')
    record = apply(mcp(f), STAMP)
    assert f.read_text() == ('{"model": "x", "mcp": {"servers": {"kaiusap": {"type": "local", "command": '
                             '["kaiusap", "mcp", "opencode2"], "codemode": false}}}}')
    data = json.loads(f.read_text())
    data["mcp"]["theirs"] = {"type": "remote"}
    f.write_text(json.dumps(data, indent=2))
    assert undo(record, LATER) == "removed"
    assert json.loads(f.read_text()) == {"model": "x", "mcp": {"theirs": {"type": "remote"}}}


def test_json_stays_valid_json(tmp_path):
    f = tmp_path / "opencode.json"
    cases = ['{}', '{}\n', '{\n}\n', '{"a": 1}', '{\n  "a": 1\n}', '{\n  "a": 1,\n  "mcp": {}\n}\n',
             '{\n  "mcp": {\n    "servers": {}\n  }\n}\n', '{"mcp": {"servers": {"x": 1}}}',
             '﻿{"a": [1, {"b": null}]}', '{\r\n  "a": 1\r\n}\r\n']
    for original in cases:
        f.write_bytes(original.encode())
        record = apply(mcp(f), STAMP)
        text = f.read_bytes().decode()
        data = json.loads(text.lstrip("﻿"))
        assert data["mcp"]["servers"]["kaiusap"] == ENTRY, original
        if "\r\n" in original:
            assert "\n" not in text.replace("\r\n", ""), "mixed newlines"
        assert undo(record, LATER) == "removed"
        assert json.loads(f.read_bytes().decode().lstrip("﻿")) == json.loads(original.lstrip("﻿")), original
        for b in tmp_path.glob("*.kaiusap-backup-*"):
            b.unlink()


def test_json_creates_the_file_and_its_directory(tmp_path):
    f = tmp_path / ".kaiusap" / "settings.json"
    spec = JsonKey(str(f), ("opencode2_command",), "opencode2")
    record = apply(spec, STAMP)
    assert json.loads(f.read_text()) == {"opencode2_command": "opencode2"}
    assert record["created_file"] is True and record["backup"] is None
    assert record["created_parents"] == []
    assert undo(record, LATER) == "removed"
    assert not f.exists()
    assert f.parent.exists()  # the directory is left: ~/.kaiusap holds the user's mail


def test_json_created_file_kept_when_the_user_added_to_it(tmp_path):
    f = tmp_path / "settings.json"
    record = apply(JsonKey(str(f), ("opencode2_command",), "opencode2"), STAMP)
    f.write_text('{"opencode2_command": "opencode2", "mine": true}')
    assert undo(record, LATER) == "removed"
    assert json.loads(f.read_text()) == {"mine": True}


def test_json_undo_after_the_user_changed_it(tmp_path):
    f = tmp_path / "settings.json"
    f.write_text('{"a": 1}\n')
    record = apply(JsonKey(str(f), ("opencode2_command",), "opencode2"), STAMP)
    changed = '{"a": 1, "opencode2_command": "opencode-beta"}\n'
    f.write_text(changed)
    assert undo(record, LATER) == "changed"
    assert f.read_text() == changed
    assert not (tmp_path / f"settings.json.kaiusap-backup-{LATER}").exists()
    f.write_text('{"a": 1}\n')
    assert undo(record, LATER) == "absent"


def test_json_conflict_and_present(tmp_path):
    f = tmp_path / "opencode.json"
    f.write_text(json.dumps({"mcp": {"servers": {"kaiusap": {"type": "local", "command": ["other"]}}}}))
    assert check(mcp(f)) == "conflict"
    f.write_text(json.dumps({"mcp": {"servers": {"kaiusap": ENTRY}}}))
    assert check(mcp(f)) == "present"
    f.write_text(json.dumps({"mcp": {"servers": "nope"}}))
    assert check(mcp(f)) == "conflict"
    f.write_text(json.dumps({"opencode2_command": 1}))
    assert check(JsonKey(str(f), ("opencode2_command",), True)) == "conflict"


@pytest.mark.parametrize("text", ['{"a": 1,,}', '{"a": }', '[1, 2]', '{"a": 1} {"b": 2}', '{"a": tru}',
                                  '{"a": 1 /* open', '', '{"mcp": {}, "mcp": {}}', "{'a': 1}"])
def test_json_malformed(tmp_path, text):
    f = tmp_path / "opencode.jsonc"
    f.write_text(text)
    with pytest.raises(CannotEdit) as e:
        apply(mcp(f), STAMP)
    assert e.value.instruction.startswith(f"Add this to {f}, merged into what is there:\n")
    assert json.loads(e.value.instruction.split("\n", 1)[1]) == {"mcp": {"servers": {"kaiusap": ENTRY}}}
    assert f.read_text() == text
    assert not list(tmp_path.glob("*.kaiusap-backup-*"))


def test_backup_never_overwritten(tmp_path):
    f = tmp_path / "settings.json"
    f.write_text('{"a": 1}')
    (tmp_path / f"settings.json.kaiusap-backup-{STAMP}").write_text("older")
    record = apply(JsonKey(str(f), ("b",), 2), STAMP)
    assert (tmp_path / f"settings.json.kaiusap-backup-{STAMP}").read_text() == "older"
    assert record["backup"] == f"{f}.kaiusap-backup-{STAMP}-2"
    assert Path(record["backup"]).read_text() == '{"a": 1}'


def test_symlinked_file_is_written_through(tmp_path):
    real = tmp_path / "dotfiles" / "opencode.json"
    real.parent.mkdir()
    real.write_text('{"a": 1}')
    link = tmp_path / "opencode.json"
    link.symlink_to(real)
    apply(JsonKey(str(link), ("b",), 2), STAMP)
    assert link.is_symlink()
    assert json.loads(real.read_text()) == {"a": 1, "b": 2}


def test_stamp_and_sentinel():
    assert MISSING is not None and MISSING is not MISSING.__class__
    with pytest.raises(ValueError):
        apply(JsonKey("/nonexistent/x.json", ("a",), 1), "2611131640")


def test_describe(tmp_path):
    home = Path.home()
    assert describe(title(home / ".codex" / "config.toml")) == \
        'Add "thread-id" to [tui] terminal_title in ~/.codex/config.toml'
    assert describe(mcp(home / ".config" / "opencode" / "opencode.json")) == \
        "Add mcp.servers.kaiusap to ~/.config/opencode/opencode.json"
    assert describe(JsonKey("~/.kaiusap/settings.json", ("opencode2_command",), "opencode2")) == \
        'Add opencode2_command = "opencode2" to ~/.kaiusap/settings.json'
    assert describe(mcp(tmp_path.parent / "elsewhere.json")).endswith(str(tmp_path.parent / "elsewhere.json"))


@pytest.mark.parametrize("original", [
    '{\n\t"model": "x"\n}\n',                                  # tabs
    '{\n  "mcp": { // servers below\n  }\n}\n',                 # an empty object holding a comment
    '{\n  "mcp": {\n    "servers": {\n      // none yet\n    }\n  }\n}\n',
    '{"mcp": {"servers": {"a": 1,}}, /* end */}',             # inline, trailing commas
    '/* head */ {"a": 1} // tail\n',
    '{\n  "a": "x // not a comment", "b": "/* nor this */"\n}',
])
def test_jsonc_layouts_round_trip(tmp_path, original):
    f = tmp_path / "opencode.jsonc"
    f.write_text(original)
    record = apply(mcp(f), STAMP)
    assert check(mcp(f)) == "present"
    edited = f.read_text()
    for comment in ("// servers below", "// none yet", "/* end */", "/* head */", "// tail",
                    "x // not a comment", "/* nor this */"):
        assert (comment in edited) == (comment in original)
    assert undo(record, LATER) == "removed"
    assert check(mcp(f)) == "missing"
    assert f.read_text() == original


def test_tabs_are_kept(tmp_path):
    f = tmp_path / "opencode.json"
    f.write_text('{\n\t"model": "x"\n}\n')
    apply(JsonKey(str(f), ("mcp", "kaiusap"), {"type": "local"}), STAMP)
    assert f.read_text() == '{\n\t"model": "x",\n\t"mcp": {\n\t\t"kaiusap": {\n\t\t\t"type": "local"\n\t\t}\n\t}\n}\n'


def test_toml_crlf_is_kept(tmp_path):
    f = tmp_path / "config.toml"
    original = '[tui]\r\nterminal_title = ["activity"]\r\n'
    f.write_bytes(original.encode())
    record = apply(title(f), STAMP)
    assert f.read_bytes().decode() == '[tui]\r\nterminal_title = ["activity", "thread-id"]\r\n'
    assert undo(record, LATER) == "removed"
    assert f.read_bytes().decode() == original


def test_undo_of_a_file_broken_since(tmp_path):
    f = tmp_path / "opencode.json"
    f.write_text('{"a": 1}')
    record = apply(mcp(f), STAMP)
    f.write_text('{"a": 1,')
    with pytest.raises(CannotEdit) as e:
        undo(record, LATER)
    assert e.value.instruction == f"Remove mcp.servers.kaiusap from {f}"
    t = tmp_path / "config.toml"
    t.write_text('[tui]\nterminal_title = ["a"]\n')
    record = apply(title(t), STAMP)
    t.write_text("[tui\n")
    with pytest.raises(CannotEdit) as e:
        undo(record, LATER)
    assert e.value.instruction == f'Remove "thread-id" from [tui] terminal_title in {t}'
