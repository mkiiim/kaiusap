"""Fakes for the installer's tests: a stand-in for `installer.edits` (its contract, not its code) and a machine
behind the one runner. Nothing here runs a real command; every file lives under the test's temporary directory."""
from dataclasses import dataclass
import json
import os
from pathlib import Path
import shutil
import sys
import tomllib
import types

# ----- installer.edits, by its contract ----------------------------------------------------------------------

MISSING = object()


class CannotEdit(Exception):
    def __init__(self, instruction):
        super().__init__(instruction)
        self.instruction = instruction


@dataclass(frozen=True)
class TomlListItem:
    file: str
    table: tuple
    key: str
    item: str
    aliases: tuple = ()
    default: tuple = ()


@dataclass(frozen=True)
class JsonKey:
    file: str
    path: tuple
    value: object


def dump_toml(data):
    """A small TOML writer for the fakes: nested tables of strings, booleans, numbers and lists."""
    lines = []

    def scalar(value):
        if isinstance(value, bool):
            return "true" if value else "false"
        if isinstance(value, (int, float)):
            return str(value)
        if isinstance(value, list):
            return "[" + ", ".join(scalar(v) for v in value) + "]"
        return json.dumps(value)

    def key(name):
        return name if name.replace("-", "").replace("_", "").isalnum() else json.dumps(name)

    def table(prefix, values):
        plain = {k: v for k, v in values.items() if not isinstance(v, dict)}
        if prefix and (plain or not values):
            lines.append(f"[{'.'.join(key(p) for p in prefix)}]")
        for k, v in plain.items():
            lines.append(f"{key(k)} = {scalar(v)}")
        for k, v in values.items():
            if isinstance(v, dict):
                table(prefix + [k], v)

    table([], data)
    return "\n".join(lines) + "\n"


def read_toml(path):
    try:
        return tomllib.loads(Path(path).read_text())
    except FileNotFoundError:
        return {}
    except tomllib.TOMLDecodeError:
        raise CannotEdit(f"{path} isn't valid TOML") from None


def read_json(path):
    try:
        return json.loads(Path(path).read_text())
    except FileNotFoundError:
        return {}
    except ValueError:
        raise CannotEdit(f"{path} isn't valid JSON") from None


def walk(data, path):
    for part in path:
        if not isinstance(data, dict) or part not in data:
            return MISSING
        data = data[part]
    return data


def check(spec):
    if isinstance(spec, JsonKey):
        found = walk(read_json(spec.file), spec.path)
        return "missing" if found is MISSING else "present" if found == spec.value else "conflict"
    found = walk(read_toml(spec.file), (*spec.table, spec.key))
    if found is MISSING:
        return "missing"
    if not isinstance(found, list):
        return "conflict"
    names = {spec.item, *spec.aliases}
    return "present" if any(isinstance(i, str) and i.strip().lower() in names for i in found) else "missing"


def backup(file, stamp):
    if Path(file).exists():
        target = f"{file}.kaiusap-backup-{stamp}"
        shutil.copy2(file, target)
        return target
    return None


def apply(spec, stamp):
    saved = backup(spec.file, stamp)
    Path(spec.file).parent.mkdir(parents=True, exist_ok=True)
    if isinstance(spec, JsonKey):
        data = read_json(spec.file)
        node = data
        for part in spec.path[:-1]:
            node = node.setdefault(part, {})
        node[spec.path[-1]] = spec.value
        Path(spec.file).write_text(json.dumps(data, indent=2) + "\n")
        return {"type": "json", "file": spec.file, "path": list(spec.path), "before": None, "written": spec.value,
                "backup": saved}
    data = read_toml(spec.file)
    node = data
    for part in spec.table:
        node = node.setdefault(part, {})
    before = node.get(spec.key)
    node[spec.key] = (list(before) if isinstance(before, list) else list(spec.default)) + [spec.item]
    Path(spec.file).write_text(dump_toml(data))
    return {"type": "toml", "file": spec.file, "table": list(spec.table), "key": spec.key, "item": spec.item,
            "before": before, "written": node[spec.key], "backup": saved}


def undo(record, stamp):
    if record["type"] == "json":
        data = read_json(record["file"])
        found = walk(data, record["path"])
        if found is MISSING:
            return "absent"
        if found != record["written"]:
            return "changed"
        backup(record["file"], stamp)
        node = walk(data, record["path"][:-1]) if len(record["path"]) > 1 else data
        del node[record["path"][-1]]
        Path(record["file"]).write_text(json.dumps(data, indent=2) + "\n")
        return "removed"
    data = read_toml(record["file"])
    node = walk(data, record["table"])
    items = node.get(record["key"]) if isinstance(node, dict) else None
    if not isinstance(items, list) or record["item"] not in items:
        return "absent"
    backup(record["file"], stamp)
    items.remove(record["item"])
    Path(record["file"]).write_text(dump_toml(data))
    return "removed"


def describe(spec):
    return f"{spec.file}: {getattr(spec, 'key', '.'.join(getattr(spec, 'path', ())))}"


def install_fake_edits(monkeypatch):
    module = types.ModuleType("installer.edits")
    for name in ("MISSING", "CannotEdit", "TomlListItem", "JsonKey", "check", "apply", "undo", "describe"):
        setattr(module, name, globals()[name])
    monkeypatch.setitem(sys.modules, "installer.edits", module)
    import installer
    monkeypatch.setattr(installer, "edits", module, raising=False)
    return module


# ----- the machine behind the runner --------------------------------------------------------------------------

COMMIT = "e26fa5c" + "0" * 33
OLD_COMMIT = "b7c27e1" + "1" * 33


class Interrupt(Exception):
    pass


class Machine:
    """A temp HOME with fake provider commands. `run` is the installer's runner; every call is recorded."""

    def __init__(self, tmp_path, claude="2.1.323", codex=True, opencode=None, daemon=True, version="0.1.0"):
        self.tmp = tmp_path
        self.home = tmp_path / "home"
        self.home.mkdir(exist_ok=True)
        self.bin = tmp_path / "bin"
        self.bin.mkdir(exist_ok=True)
        self.repo = tmp_path / "repo"
        self.make_tree(self.repo, version)
        (self.repo / ".git").mkdir(exist_ok=True)
        self.commit = COMMIT
        self.commit_dates = {}  # commit -> its committer date (ISO 8601); none by default
        self.trees = {}  # git worktree path -> commit
        self.versions = {COMMIT: version}
        self.installed = None  # {version, commit, dirty} once `uv tool install` ran
        self.calls = []
        self.fail = {}  # a command prefix (tuple) -> exit code
        self.on_call = None  # called with argv before the command runs
        self.claude_version = claude
        self.claude_sessions = []
        self.processes = []  # `ps -axww -o args=` lines
        self.daemon = daemon
        self.opencode_version = opencode
        self.setup_items = None  # what the installed `kaiusap setup --json` reports; None: computed
        self.socket = tmp_path / "codex.sock"
        self.socket.write_text("")
        for name in ("uv", "rustc", "git"):
            self.tool(name)
        if claude:
            self.tool("claude")
        if codex:
            self.tool("codex")
        if opencode:
            self.tool("opencode")
        self.ask_answers = []
        self.output = []
        self.rustc_broken = False

    @staticmethod
    def make_tree(root, version):
        (root / "plugins" / "codex" / "hooks").mkdir(parents=True, exist_ok=True)
        (root / "plugins" / "codex" / "hooks" / "hooks.json").write_text('{"hooks": {}}\n')
        (root / "plugins" / "codex" / ".codex-plugin").mkdir(exist_ok=True)
        (root / "plugins" / "codex" / ".codex-plugin" / "plugin.json").write_text(json.dumps({"version": version}))
        (root / "plugins" / "opencode").mkdir(parents=True, exist_ok=True)
        (root / "plugins" / "opencode" / "kaiusap.ts").write_text('spawn(["kaiusap", "hook", "opencode"])\n')
        (root / "plugins" / "opencode2").mkdir(parents=True, exist_ok=True)
        for name in ("server.ts", "tui.ts", "hook.ts"):
            (root / "plugins" / "opencode2" / name).write_text(f'// {name}\n["kaiusap", "hook", "opencode2"]\n')
        (root / "pyproject.toml").write_text(f'[project]\nname = "kaiusap"\nversion = "{version}"\n')

    def tool(self, name):
        path = self.bin / name
        path.write_text("#!/bin/sh\nexit 99\n")  # never run: the runner answers for it
        path.chmod(0o755)
        return path

    # ----- the context ----------------------------------------------------------------------------------

    @property
    def environ(self):
        return {"HOME": str(self.home), "PATH": str(self.bin), "KAIUSAP_HOME": str(self.home / ".kaiusap"),
                "TMUX_TMPDIR": str(self.tmp / "tmux"), "TMPDIR": str(self.tmp)}

    def context(self, interactive=True, answers=None):
        from installer.context import Context
        from datetime import datetime
        self.ask_answers = list(answers or [])

        def ask(prompt):
            self.output.append(prompt)
            if not self.ask_answers:
                raise EOFError
            return self.ask_answers.pop(0)

        self.ctx = Context(environ=self.environ, run=self.run, ask=ask, write=self.output.append,
                           interactive=interactive, repo=self.repo, now=lambda: datetime(2026, 11, 13, 16, 17, 0),
                           python_version=(3, 14, 0))
        return self.ctx

    def text(self):
        return "\n".join(self.output)

    def commands(self):
        """The commands that change something, in order (reads and probes left out)."""
        reads = (("git", "-C"), ("rustc", "--version"), ("claude", "--version"), ("claude", "agents"), ("codex", "app-server", "daemon", "version"),
                 ("ps",), ("opencode", "--version"))
        out = []
        for argv in self.calls:
            if argv[0] == str(self.kaiusap_bin) and argv[1] in {"version", "setup"}:
                continue
            if argv[:2] == ["git", "-C"] and len(argv) > 3 and argv[3] == "worktree":
                out.append(argv)
                continue
            if any(tuple(argv[:len(r)]) == r for r in reads):
                continue
            out.append(argv)
        return out

    # ----- paths ------------------------------------------------------------------------------------------

    @property
    def kaiusap_home(self):
        return self.home / ".kaiusap"

    @property
    def kaiusap_bin(self):
        return self.home / ".local" / "bin" / "kaiusap"

    @property
    def codex_config(self):
        return self.home / ".codex" / "config.toml"

    @property
    def claude_dir(self):
        return self.home / ".claude"

    def record(self):
        return json.loads((self.kaiusap_home / "install.json").read_text())

    # ----- Claude's and Codex's files, as their plugin commands write them --------------------------------

    def claude_json(self, name, update):
        path = self.claude_dir / name
        path.parent.mkdir(parents=True, exist_ok=True)
        data = json.loads(path.read_text()) if path.exists() else {}
        update(data)
        path.write_text(json.dumps(data))

    def codex_toml(self, update):
        self.codex_config.parent.mkdir(parents=True, exist_ok=True)
        data = tomllib.loads(self.codex_config.read_text()) if self.codex_config.exists() else {}
        update(data)
        self.codex_config.write_text(dump_toml(data))

    def install_claude_plugin(self, marketplace=None):
        where = str(marketplace or self.repo)  # as `claude plugin marketplace add <folder>` records it
        self.claude_json("plugins/known_marketplaces.json", lambda d: d.update(
            kaiusap={"source": {"source": "directory", "path": where}, "installLocation": where}))
        self.claude_json("plugins/installed_plugins.json", lambda d: d.setdefault("plugins", {}).update(
            {"kaiusap@kaiusap": [{"scope": "user"}]}))
        self.claude_json("settings.json", lambda d: d.setdefault("enabledPlugins", {}).update({"kaiusap@kaiusap": True}))

    def install_codex_plugin(self, source=None, version="0.1.0", marketplace=None):
        self.codex_toml(lambda d: d.setdefault("marketplaces", {}).update(
            kaiusap={"source_type": "local", "source": str(marketplace or self.repo)}))
        self.codex_toml(lambda d: d.setdefault("plugins", {}).update({"kaiusap@kaiusap": {"enabled": True}}))
        cache = self.home / ".codex" / "plugins" / "cache" / "kaiusap" / "kaiusap" / version
        shutil.rmtree(cache, ignore_errors=True)
        shutil.copytree(source or self.repo / "plugins" / "codex", cache)

    # ----- the runner -------------------------------------------------------------------------------------

    def run(self, argv, limit=None, env=None, cwd=None):
        argv = [str(a) for a in argv]
        self.calls.append(argv)
        if self.on_call:
            self.on_call(argv)
        for prefix, code in self.fail.items():
            if tuple(argv[:len(prefix)]) == prefix:
                return code, "it went wrong\n"
        name = os.path.basename(argv[0])
        handler = getattr(self, "cmd_" + name.replace("-", "_"), None)
        if argv[0] == str(self.kaiusap_bin):
            handler = self.cmd_kaiusap
        if handler is None:
            raise AssertionError(f"unexpected command: {argv}")
        return handler(argv[1:])

    def cmd_git(self, args):
        if args[2:4] == ["rev-parse", "HEAD"]:
            return 0, self.trees.get(args[1], self.commit) + "\n"
        if args[2] == "status":
            return 0, ""
        if args[2:5] == ["log", "-1", "--format=%cI"]:
            return 0, self.commit_dates.get(args[5], "") + "\n"
        if args[2:5] == ["worktree", "add", "--detach"]:
            tree, commit = Path(args[5]), args[6]
            self.make_tree(tree, self.versions[commit])
            self.trees[str(tree)] = commit
            return 0, ""
        if args[2:4] == ["worktree", "remove"]:
            shutil.rmtree(args[-1], ignore_errors=True)
            return 0, ""
        raise AssertionError(f"unexpected git {args}")

    def cmd_rustc(self, args):
        assert args == ["--version"], args
        return (1, "error: rustup could not choose a version of rustc to run\n") if self.rustc_broken else (0, "rustc 1.96.1\n")

    def cmd_claude(self, args):
        if args == ["--version"]:
            return 0, f"{self.claude_version} (Claude Code)\n"
        if args == ["agents", "--json"]:
            return 0, json.dumps([{"pid": p, "kind": "interactive"} for p in self.claude_sessions])
        if args[:3] == ["plugin", "marketplace", "add"]:
            self.claude_json("plugins/known_marketplaces.json", lambda d: d.update(
                kaiusap={"source": {"source": "directory", "path": args[3]}, "installLocation": args[3]}))
            return 0, ""
        if args == ["plugin", "install", "kaiusap@kaiusap"]:
            self.claude_json("plugins/installed_plugins.json", lambda d: d.setdefault("plugins", {}).update(
                {"kaiusap@kaiusap": [{"scope": "user"}]}))
            self.claude_json("settings.json", lambda d: d.setdefault("enabledPlugins", {}).update({"kaiusap@kaiusap": True}))
            return 0, ""
        if args == ["plugin", "enable", "kaiusap@kaiusap"]:
            self.claude_json("settings.json", lambda d: d.setdefault("enabledPlugins", {}).update({"kaiusap@kaiusap": True}))
            return 0, ""
        if args == ["plugin", "uninstall", "kaiusap@kaiusap"]:
            self.claude_json("plugins/installed_plugins.json", lambda d: d.get("plugins", {}).pop("kaiusap@kaiusap", None))
            self.claude_json("settings.json", lambda d: d.get("enabledPlugins", {}).pop("kaiusap@kaiusap", None))
            return 0, ""
        if args == ["plugin", "marketplace", "remove", "kaiusap"]:
            self.claude_json("plugins/known_marketplaces.json", lambda d: d.pop("kaiusap", None))
            return 0, ""
        raise AssertionError(f"unexpected claude {args}")

    def cmd_codex(self, args):
        if args == ["app-server", "daemon", "version"]:
            if not self.daemon:
                return 1, ""
            return 0, json.dumps({"socketPath": str(self.socket), "appServerVersion": "0.169.0"})
        if args[:2] == ["app-server", "daemon"] and args[2] in {"start", "restart"}:
            self.daemon = True
            return 0, ""
        if args[:3] == ["plugin", "marketplace", "add"]:
            self.codex_toml(lambda d: d.setdefault("marketplaces", {}).update(kaiusap={"source": args[3]}))
            self.codex_market = args[3]
            return 0, ""
        if args == ["plugin", "marketplace", "remove", "kaiusap"]:
            self.codex_toml(lambda d: d.get("marketplaces", {}).pop("kaiusap", None))
            return 0, ""
        if args == ["plugin", "add", "kaiusap@kaiusap"]:
            source = Path(tomllib.loads(self.codex_config.read_text())["marketplaces"]["kaiusap"]["source"])
            version = json.loads((source / "plugins/codex/.codex-plugin/plugin.json").read_text())["version"]
            cache = self.home / ".codex" / "plugins" / "cache" / "kaiusap" / "kaiusap"
            shutil.rmtree(cache, ignore_errors=True)
            shutil.copytree(source / "plugins" / "codex", cache / version)
            self.codex_toml(lambda d: d.setdefault("plugins", {}).update({"kaiusap@kaiusap": {"enabled": True}}))
            return 0, ""
        if args == ["plugin", "remove", "kaiusap@kaiusap"]:
            shutil.rmtree(self.home / ".codex" / "plugins" / "cache" / "kaiusap", ignore_errors=True)
            self.codex_toml(lambda d: d.get("plugins", {}).pop("kaiusap@kaiusap", None))
            return 0, ""
        raise AssertionError(f"unexpected codex {args}")

    def cmd_opencode(self, args):
        if args == ["--version"]:
            return 0, f"{self.opencode_version}\n"
        if args == ["service", "restart"]:
            return 0, ""
        raise AssertionError(f"unexpected opencode {args}")

    cmd_opencode2 = cmd_opencode

    def cmd_ps(self, args):
        assert args == ["-axww", "-o", "args="]
        return 0, "".join(line + "\n" for line in self.processes)

    def cmd_uv(self, args):
        if args[:3] == ["tool", "install", "--reinstall"]:
            source = args[3]
            commit = self.trees.get(source, self.commit)
            self.kaiusap_bin.parent.mkdir(parents=True, exist_ok=True)
            self.kaiusap_bin.write_text("#!/bin/sh\nexit 99\n")
            self.installed = {"version": self.versions[commit], "commit": commit, "dirty": False}
            return 0, ""
        if args == ["tool", "uninstall", "kaiusap"]:
            self.kaiusap_bin.unlink()
            self.installed = None
            return 0, ""
        raise AssertionError(f"unexpected uv {args}")

    def cmd_kaiusap(self, args):
        if args == ["version", "--json"]:
            return 0, json.dumps(self.installed or {"version": "0.0.1", "commit": None, "dirty": None})
        if args[:2] == ["setup", "--json"]:
            items = self.setup_items or [{"item": "kaiusap on the PATH", "done": True},
                                         {"item": "Claude plugin", "done": True},
                                         {"item": "Codex plugin", "done": True},
                                         {"item": "Codex hook approval", "done": False, "detail": "no approval",
                                          "command": "start Codex and choose Review hooks or Trust all"}]
            return 0, json.dumps({"items": items})
        raise AssertionError(f"unexpected kaiusap {args}")
