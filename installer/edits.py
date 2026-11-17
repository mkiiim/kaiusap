"""Settings edits by key.

The installer changes values in TOML and JSON/JSONC files by key, never by adding lines of text,
keeping each file's comments and layout. Each edit is checked first (`check`), made only when
missing (`apply`, which copies the file to `<file>.kaiusap-backup-<stamp>` first and returns a
record of the edit), and undone only while the value is still the one written (`undo`).

TOML goes through `tomlkit` (an installer-only dependency, imported when a TOML edit runs). JSON and
JSONC go through the small editor below: it tokenizes the file, finds the spans of the objects on
the key's path, and inserts or removes one member's text, leaving every other byte as it was.

This module does not import kaiusap: the installer runs before kaiusap is installed.
"""

import copy
import json
import os
import re
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path

MISSING = object()  # "no value"

_PRESENT, _MISSING, _CONFLICT = "present", "missing", "conflict"


class CannotEdit(Exception):
    """The file can't be changed safely; `instruction` is the change for the user to make by hand."""

    def __init__(self, message, instruction):
        super().__init__(message)
        self.instruction = instruction


@dataclass(frozen=True)
class TomlListItem:
    """An item in a TOML array, e.g. "thread-id" in [tui] terminal_title of ~/.codex/config.toml."""
    file: str
    table: tuple[str, ...]
    key: str
    item: str
    aliases: tuple[str, ...] = ()  # items that count as already present
    default: tuple[str, ...] = ()  # the list to start from when the key is absent


@dataclass(frozen=True)
class JsonKey:
    """A key in a JSON or JSONC object, e.g. mcp.servers.kaiusap in opencode.json."""
    file: str
    path: tuple[str, ...]
    value: object


# --- the public calls ---------------------------------------------------------------------------

def check(spec):
    """"present", "missing" or "conflict"; read-only. A missing file is "missing"; an unreadable one raises CannotEdit."""
    if isinstance(spec, TomlListItem):
        return _toml_check(spec)
    if isinstance(spec, JsonKey):
        return _json_check(spec)
    raise TypeError(f"not an edit: {spec!r}")


def apply(spec, stamp):
    """Make the edit (only when check() is "missing"); returns the JSON-serialisable record of it."""
    _check_stamp(stamp)
    state = check(spec)
    if state != _MISSING:
        raise ValueError(f"{describe(spec)}: not applied, the check found it {state}")
    if isinstance(spec, TomlListItem):
        return _toml_apply(spec, stamp)
    return _json_apply(spec, stamp)


def undo(record, stamp):
    """"removed", "changed" (left as the user has it) or "absent" (already gone)."""
    _check_stamp(stamp)
    kind = record.get("kind")
    if kind == "toml_list_item":
        return _toml_undo(record, stamp)
    if kind == "json_key":
        return _json_undo(record, stamp)
    raise ValueError(f"not an edit record: {record!r}")


def describe(spec):
    """One line for the installer's plan."""
    if isinstance(spec, TomlListItem):
        return f'Add {json.dumps(spec.item)} to [{".".join(spec.table)}] {spec.key} in {_tilde(spec.file)}'
    if isinstance(spec, JsonKey):
        dotted = ".".join(spec.path)
        if isinstance(spec.value, (dict, list)):
            return f"Add {dotted} to {_tilde(spec.file)}"
        return f"Add {dotted} = {json.dumps(spec.value)} to {_tilde(spec.file)}"
    raise TypeError(f"not an edit: {spec!r}")


# --- files --------------------------------------------------------------------------------------

def _path(file):
    return Path(os.path.expanduser(file))


def _tilde(file):
    text = str(_path(file))
    home = str(Path.home())
    if text == home or text.startswith(home.rstrip(os.sep) + os.sep):
        return "~" + text[len(home.rstrip(os.sep)):]
    return text


def _check_stamp(stamp):
    if not isinstance(stamp, str) or not re.fullmatch(r"\d{12}", stamp):
        raise ValueError(f"stamp must be yymmddhhmmss, not {stamp!r}")


def _read(path, instruction):
    """The file's text, exactly (no newline translation), or None when it doesn't exist."""
    try:
        data = path.read_bytes()
    except FileNotFoundError:
        return None
    except OSError as e:
        raise CannotEdit(f"{_tilde(path)} can't be read: {e}", instruction) from e
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as e:
        raise CannotEdit(f"{_tilde(path)} isn't UTF-8", instruction) from e


def _backup(path, stamp):
    """Copy the file to <file>.kaiusap-backup-<stamp>; an existing copy of that name is never overwritten."""
    target = Path(f"{path}.kaiusap-backup-{stamp}")
    n = 1
    while target.exists():
        if target.read_bytes() == path.read_bytes():
            return str(target)
        n += 1
        target = Path(f"{path}.kaiusap-backup-{stamp}-{n}")
    shutil.copy2(path, target)
    return str(target)


def _write(path, text):
    """Write atomically (temp file + rename), keeping an existing file's mode; through a symlink to its target."""
    real = Path(os.path.realpath(path))
    real.parent.mkdir(parents=True, exist_ok=True)
    try:
        mode = real.stat().st_mode & 0o7777
    except FileNotFoundError:
        umask = os.umask(0)
        os.umask(umask)
        mode = 0o666 & ~umask
    fd, tmp = tempfile.mkstemp(dir=real.parent, prefix=f".{real.name}.", suffix=".kaiusap-tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, real)
    except BaseException:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise


def _newline(text):
    return "\r\n" if "\r\n" in text else "\n"


def _same(a, b):
    """Equal as JSON values (so 1 and true differ)."""
    return json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True)


# --- TOML ---------------------------------------------------------------------------------------

def _tomlkit():
    try:
        import tomlkit
    except ImportError as e:  # pragma: no cover - the installer runs with `uv run --with tomlkit`
        raise RuntimeError("the installer needs tomlkit (run it with: uv run --with tomlkit ...)") from e
    return tomlkit


def _toml_line(spec, current=None):
    items = list(current) if current is not None else list(spec.default)
    items.append(spec.item)
    return f"{spec.key} = [" + ", ".join(json.dumps(i) for i in items) + "]"


def _toml_instruction(spec, current=None):
    return f"Add under [{'.'.join(spec.table)}] in {_tilde(spec.file)}:\n{_toml_line(spec, current)}"


def _toml_parse(spec, text, instruction=None):
    tomlkit = _tomlkit()
    try:
        return tomlkit.parse(text)
    except Exception as e:
        raise CannotEdit(f"{_tilde(spec.file)} isn't valid TOML: {e}", instruction or _toml_instruction(spec)) from e


def _toml_find(doc, table):
    """(container, created) for the table path: the deepest existing container and how many names are missing,
    or raise _NotTable when a name on the path holds something other than a table."""
    node = doc
    for depth, name in enumerate(table):
        child = node.get(name) if hasattr(node, "get") else None
        if child is None:
            return node, len(table) - depth
        if not isinstance(child, dict):
            raise _NotTable(name)
        node = child
    return node, 0


class _NotTable(Exception):
    pass


def _matches(spec):
    return {s.strip().lower() for s in (spec.item, *spec.aliases)}


def _toml_state(spec, doc):
    """(state, table, array) for the spec in a parsed document."""
    try:
        node, missing = _toml_find(doc, spec.table)
    except _NotTable:
        return _CONFLICT, None, None
    if missing:
        return _MISSING, None, None
    if spec.key not in node:
        return _MISSING, node, None
    value = node[spec.key]
    if not isinstance(value, list):
        return _CONFLICT, node, None
    wanted = _matches(spec)
    if any(isinstance(i, str) and i.strip().lower() in wanted for i in value):
        return _PRESENT, node, value
    return _MISSING, node, value


def _toml_check(spec):
    text = _read(_path(spec.file), _toml_instruction(spec))
    if text is None:
        return _MISSING
    return _toml_state(spec, _toml_parse(spec, text))[0]


def _plain(value):
    return json.loads(json.dumps(value))


def _toml_apply(spec, stamp):
    tomlkit = _tomlkit()
    path = _path(spec.file)
    text = _read(path, _toml_instruction(spec))
    created_file = text is None
    doc = tomlkit.document() if created_file else _toml_parse(spec, text)
    node, missing = _toml_find(doc, spec.table)
    created_tables = list(spec.table[len(spec.table) - missing:]) if missing else []
    for name in created_tables:
        table = tomlkit.table()
        node.add(name, table)
        node = node[name]
    before_present = spec.key in node
    before = _plain(node[spec.key]) if before_present else None
    if before_present:
        node[spec.key].append(spec.item)
    else:
        node.add(spec.key, list(spec.default) + [spec.item])
    new_text = tomlkit.dumps(doc)
    if not created_file and "\r\n" in text:
        new_text = re.sub(r"(?<!\r)\n", "\r\n", new_text)
    written = _plain(_toml_lookup(_toml_parse(spec, new_text), spec.table, spec.key))
    expected = (before or []) + [spec.item] if before_present else list(spec.default) + [spec.item]
    if written != expected:
        raise CannotEdit(f"{_tilde(spec.file)}: the edit didn't come out as planned", _toml_instruction(spec, before))
    backup = None if created_file else _backup(path, stamp)
    _write(path, new_text)
    return {"kind": "toml_list_item", "file": str(path), "table": list(spec.table), "key": spec.key,
            "item": spec.item, "before": before, "before_present": before_present, "written": written,
            "backup": backup, "created_file": created_file, "created_tables": created_tables}


def _toml_lookup(doc, table, key):
    node = doc
    for name in table:
        node = node[name]
    return node[key]


def _toml_undo(record, stamp):
    tomlkit = _tomlkit()
    path = _path(record["file"])
    spec = TomlListItem(record["file"], tuple(record["table"]), record["key"], record["item"])
    how = f"Remove {json.dumps(spec.item)} from [{'.'.join(spec.table)}] {spec.key} in {_tilde(spec.file)}"
    text = _read(path, how)
    if text is None:
        return "absent"
    doc = _toml_parse(spec, text, how)
    try:
        node, missing = _toml_find(doc, spec.table)
    except _NotTable:
        return "changed"
    if missing or spec.key not in node:
        return "absent"
    array = node[spec.key]
    if not isinstance(array, list):
        return "changed"
    current = _plain(array)
    if spec.item not in current:
        loose = any(isinstance(i, str) and i.strip().lower() == spec.item.strip().lower() for i in current)
        return "changed" if loose else "absent"
    if not record["before_present"] and current == record["written"]:
        del node[spec.key]
        # Tables the installer created, when nothing else is in them now, deepest first.
        created = record.get("created_tables") or []
        for depth in range(len(spec.table), len(spec.table) - len(created), -1):
            parent = doc
            for name in spec.table[:depth - 1]:
                parent = parent[name]
            name = spec.table[depth - 1]
            if len(parent[name]) == 0:
                del parent[name]
            else:
                break
    else:
        del array[current.index(spec.item)]
    new_text = tomlkit.dumps(doc)
    if "\r\n" in text:
        new_text = re.sub(r"(?<!\r)\n", "\r\n", new_text)
    _backup(path, stamp)
    if record.get("created_file") and not new_text.strip():
        path.unlink()
    else:
        _write(path, new_text)
    return "removed"


# --- JSON and JSONC -----------------------------------------------------------------------------

class _Bad(Exception):
    """The text isn't JSON or JSONC this editor can follow."""


_TOKEN = re.compile(r"""
    (?P<ws>[ \t\r\n]+)
  | (?P<line>//[^\n]*)
  | (?P<block>/\*.*?\*/)
  | (?P<string>"(?:[^"\\]|\\.)*")
  | (?P<number>-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?)
  | (?P<literal>true|false|null)
  | (?P<punct>[{}\[\]:,])
""", re.VERBOSE | re.DOTALL)


def _tokens(text, start):
    """(kind, start, end) for each token from `start`, comments and whitespace left out."""
    out = []
    pos = start
    while pos < len(text):
        m = _TOKEN.match(text, pos)
        if not m:
            raise _Bad(f"unexpected {text[pos:pos + 10]!r} at character {pos}")
        kind = m.lastgroup
        if kind == "literal" or kind == "number":
            follow = text[m.end():m.end() + 1]
            if follow and (follow.isalnum() or follow in "_.$"):
                raise _Bad(f"unexpected {text[pos:pos + 10]!r} at character {pos}")
        if kind not in ("ws", "line", "block"):
            out.append((kind if kind != "punct" else m.group(), m.start(), m.end()))
        pos = m.end()
    return out


class _Obj:
    def __init__(self, start):
        self.start, self.end, self.members = start, None, []  # members: [_Member]


class _Arr:
    def __init__(self, start):
        self.start, self.end, self.items = start, None, []


class _Scalar:
    def __init__(self, start, end, value):
        self.start, self.end, self.value = start, end, value


class _Member:
    def __init__(self, key, key_start, value):
        self.key, self.key_start, self.value, self.comma = key, key_start, value, None


def _parse(text):
    """The root object's tree of spans."""
    offset = 1 if text.startswith("﻿") else 0
    toks = _tokens(text, offset)
    i = 0

    def take(kind=None):
        nonlocal i
        if i >= len(toks):
            raise _Bad("unexpected end of file")
        tok = toks[i]
        if kind is not None and tok[0] != kind:
            raise _Bad(f"expected {kind!r} at character {tok[1]}")
        i += 1
        return tok

    def scalar(tok):
        try:
            return _Scalar(tok[1], tok[2], json.loads(text[tok[1]:tok[2]]))
        except ValueError as e:
            raise _Bad(f"bad value at character {tok[1]}") from e

    def value(depth=0):
        if depth > 200:
            raise _Bad("nested too deeply")
        tok = take()
        if tok[0] == "{":
            node = _Obj(tok[1])
            while True:
                if toks[i:i + 1] and toks[i][0] == "}":
                    node.end = take()[2]
                    return node
                key_tok = take("string")
                key = scalar(key_tok).value
                take(":")
                member = _Member(key, key_tok[1], value(depth + 1))
                node.members.append(member)
                nxt = take()
                if nxt[0] == ",":
                    member.comma = nxt[1]
                elif nxt[0] == "}":
                    node.end = nxt[2]
                    return node
                else:
                    raise _Bad(f"expected ',' or '}}' at character {nxt[1]}")
        if tok[0] == "[":
            node = _Arr(tok[1])
            while True:
                if toks[i:i + 1] and toks[i][0] == "]":
                    node.end = take()[2]
                    return node
                node.items.append(value(depth + 1))
                nxt = take()
                if nxt[0] == "]":
                    node.end = nxt[2]
                    return node
                if nxt[0] != ",":
                    raise _Bad(f"expected ',' or ']' at character {nxt[1]}")
        if tok[0] in ("string", "number", "literal"):
            return scalar(tok)
        raise _Bad(f"unexpected {tok[0]!r} at character {tok[1]}")

    if not toks:
        raise _Bad("the file is empty")
    root = value()
    if i != len(toks):
        raise _Bad(f"more after the end, at character {toks[i][1]}")
    if not isinstance(root, _Obj):
        raise _Bad("the top level isn't an object")
    return root


def _py(node):
    if isinstance(node, _Obj):
        return {m.key: _py(m.value) for m in node.members}
    if isinstance(node, _Arr):
        return [_py(n) for n in node.items]
    return node.value


def _member(obj, key):
    found = [m for m in obj.members if m.key == key]
    if len(found) > 1:
        raise _Bad(f"the key {key!r} appears more than once")
    return found[0] if found else None


def _walk(root, path):
    """(state, deepest object on the path, depth reached, member at the end or None).
    state: "present"-ish lookups are left to the caller; "blocked" when a name on the path isn't an object."""
    node = root
    for depth, name in enumerate(path[:-1]):
        m = _member(node, name)
        if m is None:
            return "open", node, depth, None
        if not isinstance(m.value, _Obj):
            return "blocked", node, depth, m
        node = m.value
    m = _member(node, path[-1])
    return ("found" if m else "open"), node, len(path) - 1, m


def _nested(path, value):
    for name in reversed(path):
        value = {name: value}
    return value


def _json_instruction(spec):
    return (f"Add this to {_tilde(spec.file)}, merged into what is there:\n"
            + json.dumps(_nested(spec.path, spec.value), indent=2))


def _json_load(spec, text, instruction=None):
    """The parsed root, with the key's path checked to be unambiguous (no name on it given twice)."""
    try:
        root = _parse(text)
        _walk(root, spec.path)
        return root
    except _Bad as e:
        raise CannotEdit(f"{_tilde(spec.file)} can't be read as JSON: {e}",
                         instruction or _json_instruction(spec)) from e


def _json_check(spec):
    if not spec.path:
        raise ValueError("a JsonKey needs a path")
    text = _read(_path(spec.file), _json_instruction(spec))
    if text is None:
        return _MISSING
    state, _, _, member = _walk(_json_load(spec, text), spec.path)
    if state == "blocked":
        return _CONFLICT
    if state == "open":
        return _MISSING
    return _PRESENT if _same(_py(member.value), spec.value) else _CONFLICT


def _line_start(text, pos):
    return text.rfind("\n", 0, pos) + 1


def _first_on_line(text, pos):
    return text[_line_start(text, pos):pos].strip() == ""


def _indent(text, pos):
    start = _line_start(text, pos)
    end = start
    while end < len(text) and text[end] in " \t":
        end += 1
    return text[start:end]


def _unit(text, root):
    """The file's indent step, from the first object whose members sit on their own lines; else two spaces."""
    stack = [root]
    while stack:
        node = stack.pop(0)
        if isinstance(node, _Obj) and node.members and _first_on_line(text, node.members[0].key_start):
            outer, inner = _indent(text, node.start), _indent(text, node.members[0].key_start)
            if inner.startswith(outer) and len(inner) > len(outer):
                return inner[len(outer):]
        if isinstance(node, _Obj):
            stack.extend(m.value for m in node.members)
        elif isinstance(node, _Arr):
            stack.extend(node.items)
    return "  "


def _apply_edits(text, edits):
    for start, end, new in sorted(edits, key=lambda e: (e[0], e[1]), reverse=True):
        text = text[:start] + new + text[end:]
    return text


def _member_text(key, value, indent, unit, nl):
    lines = json.dumps(value, indent=unit).split("\n")
    return json.dumps(key) + ": " + (nl + indent).join(lines)


def _insert(text, obj, key, value, unit, nl):
    """`text` with the member key: value added at the end of `obj`, in the object's own layout."""
    close = obj.end - 1  # the "}"
    if obj.members:
        last = obj.members[-1]
        if _first_on_line(text, obj.members[0].key_start):
            indent = _indent(text, obj.members[0].key_start)
            member = _member_text(key, value, indent, unit, nl)
            trailing = "," if last.comma is not None else ""
            edits = [] if last.comma is not None else [(last.value.end, last.value.end, ",")]
            if _first_on_line(text, close):
                pos = _line_start(text, close)
                edits.append((pos, pos, indent + member + trailing + nl))
            else:
                edits.append((close, close, nl + indent + member + trailing))
            return _apply_edits(text, edits)
        member = json.dumps(key) + ": " + json.dumps(value)
        if last.comma is None:
            return _apply_edits(text, [(last.value.end, last.value.end, ", " + member)])
        return _apply_edits(text, [(last.comma + 1, last.comma + 1, " " + member + ",")])
    base = _indent(text, obj.start)
    indent = base + unit
    member = _member_text(key, value, indent, unit, nl)
    inner = text[obj.start + 1:close]
    if inner.strip() == "":
        return _apply_edits(text, [(obj.start + 1, close, nl + indent + member + nl + base)])
    if _first_on_line(text, close):
        pos = _line_start(text, close)
        return _apply_edits(text, [(pos, pos, indent + member + nl)])
    return _apply_edits(text, [(close, close, nl + indent + member + nl + base)])


def _remove(text, obj, member):
    """`text` with `member` taken out of `obj`, its neighbours and comments left as they were."""
    members = obj.members
    idx = members.index(member)
    after = member.comma + 1 if member.comma is not None else member.value.end
    if len(members) == 1:
        rest = text[obj.start + 1:member.key_start] + text[after:obj.end - 1]
        if rest.strip() == "":
            return _apply_edits(text, [(obj.start + 1, obj.end - 1, "")])
    if member.comma is not None:
        end = member.comma + 1
        eol = text.find("\n", end)
        if _first_on_line(text, member.key_start) and eol != -1 and text[end:eol].strip() == "":
            return _apply_edits(text, [(_line_start(text, member.key_start), eol + 1, "")])
        if idx > 0:  # inline, after another member: the space before it goes, the one after stays
            start = member.key_start
            while start > members[idx - 1].comma + 1 and text[start - 1] in " \t":
                start -= 1
            return _apply_edits(text, [(start, end, "")])
        while end < len(text) and text[end] in " \t":
            end += 1
        return _apply_edits(text, [(member.key_start, end, "")])
    # The last member, with no comma after it: the comma before it goes too.
    edits = []
    floor = obj.start + 1
    if idx > 0:
        prev = members[idx - 1]
        edits.append((prev.comma, prev.comma + 1, ""))
        floor = prev.comma + 1
    start = member.key_start
    while start > floor and text[start - 1] in " \t\r\n":
        start -= 1
    edits.append((start, member.value.end, ""))
    return _apply_edits(text, edits)


def _strict(text):
    try:
        json.loads(text[1:] if text.startswith("﻿") else text)
        return True
    except ValueError:
        return False


def _set_in(tree, path, value):
    for name in path[:-1]:
        tree = tree.setdefault(name, {})
    tree[path[-1]] = value


def _json_apply(spec, stamp):
    path = _path(spec.file)
    text = _read(path, _json_instruction(spec))
    created_file = text is None
    if created_file:
        new_text = json.dumps(_nested(spec.path, spec.value), indent=2) + "\n"
        created_parents = list(spec.path[:-1])
    else:
        root = _json_load(spec, text)
        _, obj, depth, _ = _walk(root, spec.path)
        created_parents = list(spec.path[depth:-1])
        new_text = _insert(text, obj, spec.path[depth], _nested(spec.path[depth + 1:], spec.value),
                           _unit(text, root), _newline(text))
        # Check the result: everything else unchanged, the key there, and a .json file still JSON.
        expected = _py(root)
        _set_in(expected, spec.path, copy.deepcopy(spec.value))
        try:
            ok = _same(_py(_parse(new_text)), expected)
        except _Bad:
            ok = False
        if ok and path.suffix.lower() == ".json" and _strict(text) and not _strict(new_text):
            ok = False
        if not ok:
            raise CannotEdit(f"{_tilde(spec.file)}: the edit didn't come out as planned", _json_instruction(spec))
    backup = None if created_file else _backup(path, stamp)
    _write(path, new_text)
    return {"kind": "json_key", "file": str(path), "path": list(spec.path), "key": None,
            "value": _plain(spec.value), "before": None, "before_present": False,
            "written": _plain(spec.value), "backup": backup, "created_file": created_file,
            "created_parents": created_parents}


def _json_undo(record, stamp):
    path = _path(record["file"])
    keys = tuple(record["path"])
    spec = JsonKey(record["file"], keys, record["written"])
    how = f"Remove {'.'.join(keys)} from {_tilde(record['file'])}"
    text = _read(path, how)
    if text is None:
        return "absent"
    original = text
    root = _json_load(spec, text, how)
    state, obj, _, member = _walk(root, keys)
    if state == "blocked":
        return "changed"
    if state == "open":
        return "absent"
    if not _same(_py(member.value), record["written"]):
        return "changed"
    text = _remove(text, obj, member)
    # Parent objects the installer created, when empty now (no members, no comments), deepest first.
    created = record.get("created_parents") or []
    for depth in range(len(keys) - 1, len(keys) - 1 - len(created), -1):
        root = _json_load(spec, text, how)
        state, obj, reached, m = _walk(root, keys[:depth])
        if state != "found" or not isinstance(m.value, _Obj) or m.value.members:
            break
        if text[m.value.start + 1:m.value.end - 1].strip() != "":
            break
        text = _remove(text, obj, m)
    # Check the result: the key gone, and with it none, some or all of the empty created parents.
    before = _py(_json_load(spec, original, how))
    try:
        after = _py(_parse(text))
        ok = any(_same(after, _drop(copy.deepcopy(before), keys, k)) for k in range(len(created) + 1))
    except _Bad:
        ok = False
    if not ok or (path.suffix.lower() == ".json" and _strict(original) and not _strict(text)):
        raise CannotEdit(f"{_tilde(record['file'])}: removing {'.'.join(keys)} didn't come out as planned", how)
    _backup(path, stamp)
    if record.get("created_file") and not _parse(text).members and _only_space(text, _parse(text)):
        path.unlink()
    else:
        _write(path, text)
    return "removed"


def _only_space(text, root):
    return (text[:root.start] + text[root.start + 1:root.end - 1] + text[root.end:]).strip("﻿ \t\r\n") == ""


def _drop(tree, keys, parents):
    """`tree` without keys[-1] and its `parents` nearest parents (None if one of them isn't empty then)."""
    chain = [tree]
    for name in keys[:-1]:
        chain.append(chain[-1][name])
    del chain[-1][keys[-1]]
    for depth in range(len(keys) - 1, len(keys) - 1 - parents, -1):
        if chain[depth]:
            return None
        del chain[depth - 1][keys[depth - 1]]
    return tree
