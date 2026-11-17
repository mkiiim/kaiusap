"""Provider specifics live in `kaiusap/adapters/`, behind the `Provider` protocol.

The base may not name a provider. In the Python base modules (`kaiusap/*.py`) that means: no provider
name, in any case, in a string constant (display names such as "Claude" included) or an identifier, no
import of an adapter module, and no provider event name (`SessionStart`, ...) in a string constant, such
as a comparison with one. Docstrings and comments may name them: they decide nothing. The native engine
(`native/hook.rs`) and the build (`setup.py`) name neither, comments included, since both are read as
the provider-free half of the native fast path.

KNOWN lists the exceptions, each source line with its reason, and nothing else: a new mention fails here,
and so does a known one that is gone, until it is removed from KNOWN.
"""
import ast
from pathlib import Path
import re

import kaiusap
from kaiusap.adapters import ADAPTERS, adapter_class, display_name

BASE = Path(kaiusap.__file__).parent
REPO = BASE.parent.parent
# The registered providers, and the two the base was written with, so deleting one from the registry
# doesn't hide its name.
PROVIDERS = {"claude", "codex"} | set(ADAPTERS)
DISPLAY_NAMES = {"Claude", "Codex"} | {display_name(name) for name in ADAPTERS}
EVENTS = {"SessionStart", "UserPromptSubmit", "SubagentStart", "PreToolUse"}.union(
    *(adapter_class(name).hook_events for name in ADAPTERS))
NAMED = re.compile("|".join(sorted(PROVIDERS)), re.I)
EVENT = re.compile("|".join(sorted(EVENTS)))

# The exceptions, as {(module, source line): reason, or (reason, count) for a line that occurs more than
# once}. None are left: the PreToolUse screen, the `delivered` line and the start and
# prompt events moved behind the protocol. A new one needs the maintainer's approval.
KNOWN = {}


def allowed(key):
    """How many times a known line may occur: its listed count, else once; 0 when it isn't known."""
    value = KNOWN.get(key)
    return 0 if value is None else value[1] if isinstance(value, tuple) else 1


def reason(value):
    return value[0] if isinstance(value, tuple) else value


def docstrings(tree):
    return {id(node.body[0].value) for node in ast.walk(tree)
            if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
            and node.body and isinstance(node.body[0], ast.Expr) and isinstance(node.body[0].value, ast.Constant)
            and isinstance(node.body[0].value.value, str)}


def identifiers(node):
    if isinstance(node, ast.Name):
        return [node.id]
    if isinstance(node, ast.Attribute):
        return [node.attr]
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        return [node.name]
    if isinstance(node, ast.arg):
        return [node.arg]
    if isinstance(node, ast.keyword) and node.arg:
        return [node.arg]
    if isinstance(node, ast.alias):
        return [node.asname] if node.asname else []
    return []


def provider_mentions(path):
    """(line, what) for each line of a base module that names a provider or a provider event, by rule."""
    text = path.read_text()
    tree = ast.parse(text)
    skip = docstrings(tree)
    found = set()
    for node in ast.walk(tree):
        line = getattr(node, "lineno", None)
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in skip:
            if any(name in node.value for name in DISPLAY_NAMES):
                found.add((line, "display name"))
            elif NAMED.search(node.value):
                found.add((line, "provider name"))
            if EVENT.search(node.value):
                found.add((line, "provider event"))
        elif isinstance(node, ast.ImportFrom) and node.module and NAMED.search(node.module):
            found.add((line, "adapter import"))
        elif isinstance(node, ast.Import) and any(NAMED.search(a.name) for a in node.names):
            found.add((line, "adapter import"))
        elif any(NAMED.search(name) for name in identifiers(node)):
            found.add((line, "provider identifier"))
    lines = text.splitlines()
    return sorted((line, what, lines[line - 1].strip()) for line, what in found)


def test_base_modules_name_no_provider_beyond_the_known_list():
    found = {}
    for path in sorted(BASE.glob("*.py")):
        for line, what, source in provider_mentions(path):
            found.setdefault((path.name, source), {}).setdefault(line, []).append(what)
    problems = []
    for (name, source), lines in sorted(found.items(), key=lambda item: (item[0][0], min(item[1]))):
        extra = sorted(lines)[allowed((name, source)):]
        problems += [f"{name}:{line}: {', '.join(lines[line])}: {source}\n    Put provider code in kaiusap/adapters/, "
                     "behind the Provider protocol, or ask the maintainer for an exception in KNOWN." for line in extra]
    problems += [f"{name}: the known line occurs {len(found.get((name, source), {}))} times, {allowed((name, source))} known; "
                 f"lower or remove it in KNOWN: {source}" for name, source in KNOWN
                 if len(found.get((name, source), {})) < allowed((name, source))]
    assert not problems, "\n".join(problems)


def test_known_exceptions_each_have_a_reason():
    assert all(isinstance(reason(value), str) and reason(value) for value in KNOWN.values())


def test_the_check_sees_literals_imports_names_display_names_and_events(tmp_path):
    sample = tmp_path / "sample.py"
    sample.write_text('''"""A docstring may say Claude, codex and SessionStart."""
from .adapters.codex import X
import kaiusap.adapters.claude
# A comment may say Codex.
if p == 'claude':
    pass
text = f"posted; Codex decides {x}"
if name == 'SessionStart':
    claude_home = None
def f():
    """Codex, in a docstring."""
    return adapters.codex
''')
    assert [(line, what) for line, what, _ in provider_mentions(sample)] == [
        (2, "adapter import"), (3, "adapter import"), (5, "provider name"), (7, "display name"),
        (8, "provider event"), (9, "provider identifier"), (12, "provider identifier")]


# The store paths the base owns under `sessions/`. Every other store path is an adapter's, in its own
# directory, built from `store.adapter_dir(p)`, so a base module names none.
BASE_SESSION_FILES = {"sessions/starts.jsonl", "sessions/prompts.jsonl", "sessions/personas.jsonl"}  # personas: every provider's


def provider_files():
    """The store file names the adapters own: each `.jsonl` name in an adapter's string constants that is not
    the base's, and the terminal map's, so that deleting it from codex.py doesn't hide it."""
    names = {"terminals.jsonl"}
    for path in (BASE / "adapters").glob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                names.update(re.findall(r"[\w.-]+\.jsonl\b", node.value))
    return names - {name.rsplit("/", 1)[-1] for name in BASE_SESSION_FILES} - {"handles.jsonl"}


def store_path_mentions(path, owned):
    """(line, constant) for each string constant of a base module that holds a provider's store path."""
    tree = ast.parse(path.read_text())
    skip = docstrings(tree)
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in skip:
            paths = re.findall(r"sessions/[\w./<>{}-]*", node.value)
            if any(p not in BASE_SESSION_FILES for p in paths) or any(name in node.value for name in owned):
                found.append((node.lineno, node.value))
    return sorted(found)


def test_base_modules_hold_no_provider_store_path():
    # Batch F (2026-11-09): provider-owned files live under `sessions/<p>/` (`store.adapter_dir(p)`), and only
    # the adapter names them. A base module names no such path or file, `store.py` included.
    owned = provider_files()
    problems = [f"{path.name}:{line}: {value!r} is a provider's store path; build it in the adapter from "
                "store.adapter_dir(p)" for path in sorted(BASE.glob("*.py")) for line, value in store_path_mentions(path, owned)]
    assert not problems, "\n".join(problems)


def test_the_store_path_check_sees_provider_paths(tmp_path):
    sample = tmp_path / "sample.py"
    sample.write_text('''"""A docstring may say sessions/terminals.jsonl."""
a = "sessions/starts.jsonl"
b = "sessions/last/x/s.json"
c = root / "sessions/terminals.jsonl"
d = f"sessions/{p}/index"
e = "terminals.jsonl"
f = "sessions/prompts.jsonl"
''')
    assert "terminals.jsonl" in provider_files()
    assert [line for line, _ in store_path_mentions(sample, provider_files())] == [3, 4, 5, 6]


def engine_mentions(path):
    """Every line of the native engine or the build that names a provider or a provider event, comments included."""
    return [(number, line.strip()) for number, line in enumerate(path.read_text().splitlines(), 1)
            if NAMED.search(line) or EVENT.search(line)]


def test_the_native_engine_and_the_build_name_no_provider():
    # The engine names no provider; the build finds the parts by listing adapters/*.rs.
    problems = [f"{path}:{number}: {line}" for path in ("native/hook.rs", "setup.py")
                for number, line in engine_mentions(REPO / path)]
    assert not problems, "\n".join(problems)


def test_the_engine_check_sees_names_in_any_case_and_events(tmp_path):
    sample = tmp_path / "hook.rs"
    sample.write_text("fn a() {}\n// a CODEX comment\nlet x = \"claude\";\nif e == \"UserPromptSubmit\" {}\n")
    assert [number for number, _ in engine_mentions(sample)] == [2, 3, 4]


def test_the_registry_names_each_adapter_by_its_prefix():
    # One registry line per provider; the base builds its adapters and its provider lists from it.
    from kaiusap.adapters import ADAPTERS, Providers, adapter_class
    from kaiusap.mcp import PROVIDERS
    assert all(adapter_class(name).provider == name for name in ADAPTERS)
    assert list(Providers.default(None).adapters) == list(ADAPTERS)
    assert set(PROVIDERS) == set(ADAPTERS)


def test_identity_asks_each_registered_adapter_for_its_claim(store):
    # The registry reads no provider's variable itself; two claims at once are refused.
    import pytest
    from kaiusap.adapters import ADAPTERS, Providers, UnavailableProvider, adapter_class
    from kaiusap.errors import Error
    assert not UnavailableProvider.claims({"CODEX_THREAD_ID": "t", "CLAUDE_CODE_SESSION_ID": "s"})
    for name in ADAPTERS:
        kind = adapter_class(name)
        assert kind.claims({kind.session_variable: "x"}) and not kind.claims({kind.session_variable: ""})
    providers = Providers({}, store)
    assert providers.identity({}, store).classification == "plain_shell"
    with pytest.raises(Error) as both:
        providers.identity({"CODEX_THREAD_ID": "t", "CLAUDE_CODE_SESSION_ID": "s"}, store)
    assert str(both.value) == ("Both CODEX_THREAD_ID and CLAUDE_CODE_SESSION_ID are set, so kaiusap can't tell which "
                               "session is calling. Run it from the session's own shell, or use the kaiusap tools.")
    # A claim for a provider whose adapter is absent is refused by its stand-in, as before.
    with pytest.raises(Error, match="not implemented"):
        providers.identity({"CODEX_THREAD_ID": "t"}, store)


def test_each_adapter_declares_the_hook_events_its_plugin_runs():
    # `kaiusap hook <p> <event>` handles what the adapter declares, which is what its plugin
    # installs, each as `kaiusap hook <p> <event>`; the plugin's MCP server runs `kaiusap mcp <p>`. A command
    # plugin runs both through its own launcher, `bin/kaiusap`.
    import json
    from kaiusap.adapters import UnavailableProvider
    plugins = REPO / "plugins"
    assert {p.name for p in plugins.iterdir() if p.is_dir()} == set(ADAPTERS), "one plugin folder per registered provider"
    for name in ADAPTERS:
        module = plugins / name / "kaiusap.ts"
        if module.exists():
            check_code_module(name, module.read_text())
            continue
        if (plugins / name / "server.ts").exists():
            check_parted_module(name, plugins / name)
            continue
        hooks = json.loads((plugins / name / "hooks/hooks.json").read_text())["hooks"]
        assert adapter_class(name).hook_events == set(hooks), name
        for event, groups in hooks.items():
            commands = {handler["command"] for group in groups for handler in group["hooks"]}
            assert len(commands) == 1, (name, event, commands)
            assert re.fullmatch(r'"\$\{[A-Z_]+\}"/bin/kaiusap hook ' + re.escape(f"{name} {event}"), *commands), (name, event, commands)
        assert (plugins / name / "bin/kaiusap").is_file(), name
        server = json.loads((plugins / name / ".mcp.json").read_text())["mcpServers"]["kaiusap"]
        assert server["command"].endswith("/bin/kaiusap") and server["args"] == ["mcp", name], name
    assert UnavailableProvider.hook_events == frozenset()


def declared(source, constant):
    """A code-module plugin's declared list: `const <constant> = [...]` on one line."""
    import json
    match = re.search(rf"^const {constant} = (\[[^\n]*\])$", source, re.M)
    assert match, f"the plugin declares {constant} on one line"
    return json.loads(match.group(1))


def check_code_module(name, source):
    """A plugin that is a code module: its HOOK_EVENTS are the adapter's `hook_events`, its
    SCREENED_TOOLS the adapter's screened tools, and its one hook command `kaiusap hook <p> <event>`."""
    kind = adapter_class(name)
    events = declared(source, "HOOK_EVENTS")
    assert set(events) == kind.hook_events and len(events) == len(set(events)), name
    if kind.screen_events:
        assert set(declared(source, "SCREENED_TOOLS")) == set(kind.screened_tools), name
    # Its first word is the program the plugin's launcher rule resolves.
    assert len(re.findall(r'Bun\.spawn', source)) == 1, name
    commands = re.findall(r'Bun\.spawn\(resolved\(\[([^\]]*)\]\)', source)
    assert commands == [f'"kaiusap", "hook", "{name}", event'], (name, commands)
    # Every event the module hands to the hook entry is a declared one, by name in its own calls.
    called = set(re.findall(r'runHook\("([^"]+)"', source))
    assert called == set(events), (name, called)


def check_parted_module(name, folder):
    """A plugin in parts, one per process OpenCode 2 loads it in: `server.ts` and `tui.ts`, sharing one
    hook runner in `hook.ts`. Together their HOOK_EVENTS are the adapter's `hook_events`, each declared by one
    part and run by that part; the screen's SCREENED_TOOLS are the server part's; the runner's commands are
    `kaiusap hook <p> <event>`."""
    kind = adapter_class(name)
    parts = {part: (folder / f"{part}.ts").read_text() for part in ("server", "tui")}
    declared_events = {part: declared(source, "HOOK_EVENTS") for part, source in parts.items()}
    every = [event for events in declared_events.values() for event in events]
    assert set(every) == kind.hook_events and len(every) == len(set(every)), (name, declared_events)
    for part, source in parts.items():
        assert set(re.findall(r'runHook(?:Now)?\("([^"]+)"', source)) == set(declared_events[part]), (name, part)
        assert "Bun.spawn" not in source, f"{part}.ts runs hooks only through hook.ts"
    if kind.screen_events:
        assert set(declared(parts["server"], "SCREENED_TOOLS")) == set(kind.screened_tools), name
    runner = (folder / "hook.ts").read_text()
    commands = re.findall(r'Bun\.spawn(?:Sync)?\(resolved\(\[([^\]]*)\]\)', runner)
    assert len(commands) == len(re.findall(r'Bun\.spawn', runner)), name
    assert commands and set(commands) == {f'"kaiusap", "hook", "{name}", event'}, (name, commands)


def test_hook_deadline_and_records_come_from_the_adapter(store):
    # The hook entry hands the adapter its total deadline and the native append.
    from kaiusap.adapters import UnavailableProvider, adapter_class
    from kaiusap.adapters.daemon import Deadline
    kinds = {name: adapter_class(name)(store) for name in ("claude", "codex")}
    assert kinds["claude"].hook_deadline(12.5) == 12.5
    assert isinstance(kinds["codex"].hook_deadline(12.5), Deadline) and kinds["codex"].hook_deadline(12.5).at == 12.5
    assert UnavailableProvider().hook_deadline(12.5) == 12.5

    class Recorder:
        def __init__(self, kind):
            self.kind, self.calls = kind, []
        def record(self, event, deadline=None):
            self.calls.append(("record", event.get("prompt_record")))
    def run(name, hook, event, recorded, entry):
        fake = Recorder(name)
        natives = []
        adapter_class(name).hook_record(fake, hook, event, 9.0, recorded=recorded,
                                        native=(lambda: natives.append(hook)) if entry else None)
        return fake.calls, natives
    prompt = {"prompt_record": {"source": "prompt"}, "record": None}
    # Claude: the prompt record is native's (written, or written now); the cwd line is always Python's.
    assert run("claude", "UserPromptSubmit", dict(prompt), True, False) == ([("record", None)], [])
    assert run("claude", "UserPromptSubmit", dict(prompt), False, True) == ([("record", None)], ["UserPromptSubmit"])
    assert run("claude", "UserPromptSubmit", dict(prompt), False, False) == ([("record", {"source": "prompt"})], [])
    # Codex: nothing once native recorded; the constant hooks' records by native when it is there.
    with_record = {"record": {"source": "prompt"}}
    assert run("codex", "UserPromptSubmit", dict(with_record), True, True) == ([], [])
    assert run("codex", "SubagentStart", dict(with_record), False, True) == ([], ["SubagentStart"])
    assert run("codex", "UserPromptSubmit", {"record": None}, False, True) == ([], [])
    assert run("codex", "SessionStart", dict(with_record), False, True) == ([("record", None)], [])
    assert run("codex", "UserPromptSubmit", dict(with_record), False, False) == ([("record", None)], [])


def test_hook_output_and_marker_fields_come_from_the_adapter(store):
    # The base asks the adapter for its output and its marker fields; Codex adds neither,
    # and its record-only SubagentStart stops after the record.
    from kaiusap.adapters import UnavailableProvider, adapter_class
    from kaiusap.hookstate import HookState
    codex, claude = adapter_class("codex")(store), adapter_class("claude")(store)
    assert codex.record_only_events == {"SubagentStart"}
    assert claude.record_only_events == {"PermissionRequest", "Stop", "StopFailure", "Elicitation"}  # activity
    assert UnavailableProvider.record_only_events == frozenset()
    state = HookState(store, "codex", "t")
    event = {"provider": "codex", "session_id": "t", "session_key": "codex:t", "kind": "prompt", "transcript": None}
    assert codex.hook_output(dict(event), state) == {} and codex.marker_fields(state, event) == {}
    assert UnavailableProvider().hook_output(event, state) == {} and UnavailableProvider().marker_fields(state, event) == {}
    state = HookState(store, "claude", "s")
    event = {"provider": "claude", "session_id": "s", "session_key": "claude:s", "kind": "prompt", "transcript": None,
             "session_title": None, "hook_event_name": "UserPromptSubmit"}
    assert claude.hook_output(event, state) == {}
    assert event["history"] == (state.data["ended"], state.data["bindings"]) and event["naming_events"] is state.data["events"]
    assert claude.marker_fields(state, event) == {"index_signature": None, "title_mode": "any", "title": None}
    # The marker's layout is the base's; without the adapter's fields it acknowledges no index, any title.
    import json
    state.save(event)
    assert list(json.loads(state.marker.read_text())) == ["version", "signature", "state_signature", "index_signature",
                                                          "mail_pending", "transcript", "title_mode", "title"]


def test_whoami_directory_comes_from_the_adapter_then_the_records(store):
    # An MCP caller's directory is the adapter's own (Claude: the listing's cwd in this call), else the records.
    from kaiusap.adapters import Identity, UnavailableProvider, adapter_class
    from kaiusap.mcp import directory
    from kaiusap.store import now
    claude = adapter_class("claude")(store)
    claude._listing = [{"pid": 7, "sessionId": "s", "cwd": "/listed"}, {"pid": 8, "sessionId": "s", "cwd": "/other"}]
    caller = Identity("claude", "s", "claude:s", None, "root", pid=7)
    assert claude.directory(caller) == "/listed" and directory(caller, claude, store) == "/listed"
    claude._listing = False
    assert claude.directory(caller) is None and directory(caller, claude, store) is None
    store.start({"at": now(), "provider": "claude", "session_id": "s", "session_key": "claude:s", "source": "startup", "cwd": "/recorded", "transcript": None})
    assert directory(caller, claude, store) == "/recorded"
    codex = adapter_class("codex")(store)
    thread = Identity("codex", "t", "codex:t", None, "root")
    assert codex.directory(thread) is None and UnavailableProvider().directory(thread) is None
    assert directory(Identity(), codex, store) is None


def test_display_names_come_from_the_registry():
    # Notices, who, sessions and whoami show the registry's display name, and
    # building the notice's pattern imports no adapter (the hook path imports notice.py).
    import subprocess
    import sys
    from kaiusap.adapters import ADAPTERS, display_name
    from kaiusap.notice import NOTICE, PROVIDER_NAMES, notice, notice_id
    assert PROVIDER_NAMES == {name: display_name(name) for name in ADAPTERS} == {"codex": "Codex", "claude": "Claude", "opencode": "OpenCode", "opencode2": "OpenCode 2"}
    assert display_name("gemini") == "Gemini"  # a provider no longer registered, as before
    assert r"(?:(?:Claude|Codex|OpenCode|OpenCode\ 2), )?" in NOTICE.pattern
    message = {"id": "20261109T000000Z-abcdef", "in_reply_to": None, "reply_needed": False, "subject": "S"}
    for name, shown in PROVIDER_NAMES.items():
        text = notice({**message, "from": {"handle": "alpha", "provider": name}})
        assert text.startswith(f"kaiusap: message from alpha ({shown}, a peer assistant") and notice_id(text) == message["id"]
    old = notice({**message, "from": {"handle": "alpha"}})  # before 2026-11-05: no provider
    assert old.startswith("kaiusap: message from alpha (a peer assistant") and notice_id(old) == message["id"]
    assert notice_id(old.replace("(a peer", "(Gemini, a peer")) is None
    loaded = subprocess.run([sys.executable, "-I", "-c", "import sys, kaiusap.notice, kaiusap.adapters; "
                             "print(sorted(m for m in sys.modules if m.startswith('kaiusap.adapters.')))"],
                            capture_output=True, text=True, check=True)
    assert loaded.stdout.strip() == "[]"


def test_an_adapter_keeps_its_derived_files_in_its_own_store_directory(store):
    # The store gives each adapter a directory; Claude's transcript index lives
    # in it, at the path native's claude.rs reads, and is written under the starts lock right after its record.
    from kaiusap.adapters import adapter_class
    claude = adapter_class("claude")(store)
    assert store.adapter_dir("claude") == store.root / "sessions/claude"
    assert claude.index_path("s") == store.root / "sessions/claude/last/s.json"
    record = {"at": "2026-11-09T00:00:00Z", "provider": "claude", "session_id": "s", "session_key": "claude:s",
              "source": "cwd", "cwd": "/c", "transcript": "/t"}
    held = []
    store.start(dict(record, session_id="other", session_key="claude:other"))
    assert not store.adapter_dir("claude").exists()  # the base derives nothing itself
    def update(row):
        import fcntl
        with open(store.root / "sessions/starts.jsonl") as stream:
            try:
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                held.append(True)
        claude.update_index(row)
    store.start(record, then=update)
    assert held == [True]
    assert (store.root / "sessions/claude/last/s.json").read_text() == '{"at": "2026-11-09T00:00:00Z", "cwd": "/c", "transcript": "/t"}'
    claude.record({"record": dict(record, at="2026-11-09T00:00:01Z", cwd="/d")})
    assert claude.index("s")["cwd"] == "/d"


def test_every_adapter_and_the_stand_in_implement_the_whole_protocol():
    # Step 10 of the provider-boundary plan: the base calls each obligation directly, never through getattr.
    import inspect
    from kaiusap.adapters import ADAPTERS, Provider, UnavailableProvider, adapter_class
    declared = {name for name, value in vars(Provider).items() if not name.startswith("_") and (
        callable(value) or isinstance(value, classmethod))} | set(Provider.__annotations__)
    assert {"begin", "reset", "finish", "delivers", "mcp_identity", "directory", "tmux_start", "hook_output",
            "marker_fields", "record_only_events", "unidentified", "setup_items", "start_events", "prompt_events",
            "screen_events", "pre_tool"} <= declared
    for kind in [UnavailableProvider, *(adapter_class(name) for name in ADAPTERS)]:
        missing = sorted(name for name in declared - {"provider"} if not hasattr(kind, name)) if kind is UnavailableProvider else \
            sorted(name for name in declared if not hasattr(kind, name))
        assert not missing, f"{kind.__name__} lacks {missing}"
    for path in BASE.glob("*.py"):
        tree = ast.parse(path.read_text())
        # `missing` is the registry's (which providers have no adapter here), not an obligation: Core takes either.
        lookups = [node.lineno for node in ast.walk(tree) if isinstance(node, ast.Call) and getattr(node.func, "id", None) in {"getattr", "hasattr"}
                   and node.args and getattr(node.args[0], "attr", getattr(node.args[0], "id", None)) in {"provider", "adapter"}
                   and not (len(node.args) > 1 and getattr(node.args[1], "value", None) == "missing")]
        assert not lookups, f"{path.name}:{lookups} looks up a provider method; declare it in Provider instead"
    assert inspect.signature(adapter_class("claude").begin).parameters.keys() == {"self"}


def test_an_unknown_caller_is_described_by_its_adapter(store):
    # core.py names no provider: the unknown Codex caller's text is the adapter's.
    from kaiusap.adapters import Identity, Providers, UnavailableProvider, adapter_class
    unknown = Identity("codex", "t", None, "/f", "unknown", reason="no daemon")
    codex = adapter_class("codex")(store)
    expected = {"can_be_woken": False, "why_not": "kaiusap can't tell whether this Codex session is a subagent (no daemon)",
                "reason_code": "unknown_caller", "fix": "Install the kaiusap plugin, or start the Codex daemon."}
    assert codex.unidentified(unknown) == expected
    assert Providers({"codex": codex}, store).unidentified(unknown) == expected
    assert codex.unidentified(Identity("codex", "t", None, "/f", "unknown"))["why_not"] == "kaiusap can't tell whether this Codex session is a subagent"
    assert UnavailableProvider().unidentified(unknown) == {"can_be_woken": False, "why_not": "no daemon", "reason_code": "unknown_caller", "fix": None}


def adapter_imports(path, modules):
    """(line, module) for each import in an adapter module of another registered adapter's module."""
    own = path.stem
    found = []
    for node in ast.walk(ast.parse(path.read_text())):
        names = []
        if isinstance(node, ast.ImportFrom):
            base = (node.module or "").rsplit(".", 1)[-1] if node.level or (node.module or "").startswith("kaiusap.adapters") else None
            names = [base] if base else []
            if node.level and not node.module:  # `from . import x`
                names = [alias.name for alias in node.names]
        elif isinstance(node, ast.Import):
            names = [a.name.rsplit(".", 1)[-1] for a in node.names if a.name.startswith("kaiusap.adapters.")]
        found += [(node.lineno, name) for name in names if name in modules and name != own]
    return found


def test_no_adapter_reads_another_adapter():
    """An adapter reads no other adapter. What two share lives in a module no adapter
    owns (adapters/terminal.py, tmux.py, shell.py, daemon.py; opencode_choice.py for the two OpenCode providers)."""
    modules = {ADAPTERS[name][0].rsplit(".", 1)[-1] for name in ADAPTERS}
    problems = [f"{name}.py:{line}: imports the {other} adapter's module" for name in sorted(modules)
                for line, other in adapter_imports(BASE / "adapters" / f"{name}.py", modules)]
    assert not problems, "\n".join(problems)


def test_the_adapter_import_check_sees_each_form(tmp_path):
    path = tmp_path / "alpha.py"
    path.write_text("from .beta import x\nfrom . import beta\nimport kaiusap.adapters.beta\nfrom kaiusap.adapters.beta import y\n"
                    "from .tmux import z\nfrom . import tmux\nfrom .alpha import w\n")
    assert adapter_imports(path, {"alpha", "beta"}) == [(1, "beta"), (2, "beta"), (3, "beta"), (4, "beta")]


def test_the_shared_terminal_module_names_no_provider():
    assert provider_mentions(BASE / "adapters" / "terminal.py") == []
