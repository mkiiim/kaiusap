"""`kaiusap setup`: every item in both states, read-only, with time limits."""
from io import StringIO
import json
import os
from pathlib import Path
import stat
import subprocess
import sys

import pytest

import kaiusap.setup as setup_mod
from kaiusap.adapters import Identity
from kaiusap.cli import main
from kaiusap.notice import SESSION_LINE
from kaiusap.adapters import claude as claude_mod
from kaiusap.adapters.claude import claude_version
from kaiusap.adapters.codex import HOOK_KEYS
from kaiusap.setup import Setup, direct_url_source
from kaiusap.store import Store

ITEMS = ["kaiusap found by the plugins", "Claude plugin", "Codex plugin", "thread-id in Codex's [tui] terminal_title",
         "Codex daemon", "Claude Code version", "Codex hook approval", "OpenCode plugin",
         "kaiusap's MCP server in OpenCode's config", "OpenCode version", "tmux server", "tmux pane titles"]
# The rig without an `opencode` on the PATH: OpenCode isn't installed, so its items are one skipped line.
SKIPPED_OPENCODE = ITEMS[:7] + ["OpenCode"] + ITEMS[-2:]


def skipped(name, command):
    return {"item": name, "done": True, "detail": f"{command} isn't on the PATH; its items are skipped", "command": None, "note": None}
OPENCODE_CONFIG = json.dumps({"$schema": "https://opencode.ai/config.json",
                              "mcp": {"kaiusap": {"type": "local", "command": ["kaiusap", "mcp", "opencode"]}}})
HOOKS_STATE = "\n".join(f'[hooks.state."{key}"]\ntrusted_hash = "sha256:{label}"\n' for label, key in HOOK_KEYS.items())
FULL_CONFIG = f'''[tui]
terminal_title = ["activity", "thread-name", "thread-id"]

[marketplaces.kaiusap]
source_type = "local"
source = "/repo/kaiusap"

[plugins."kaiusap@kaiusap"]
enabled = true

{HOOKS_STATE}'''


class Rig:
    """A temporary home with fake `claude` and `codex` executables whose behaviour files control."""

    def __init__(self, tmp_path, monkeypatch):
        self.home = Path(os.environ["HOME"])  # conftest's isolated home
        self.claude_dir = self.home / ".claude"
        self.codex_home = self.home / ".codex"
        self.bin = tmp_path / "setup-bin"
        self.bin.mkdir()
        self.control = tmp_path / "control"
        self.control.mkdir()
        self.socket = tmp_path / "daemon.sock"
        self.tmux_tmpdir = tmp_path / "tmux-tmpdir"  # conftest's; no default server until serve_tmux()
        self.env = {"PATH": str(self.bin), "HOME": str(self.home), "TMUX_TMPDIR": str(self.tmux_tmpdir)}
        self.script("claude", f'''c="{self.control}"
[ -e "$c/claude-sleep" ] && /bin/sleep 3
[ -e "$c/claude-fail" ] && exit 1
/bin/cat "$c/claude-version"''')
        (self.control / "claude-version").write_text("2.1.318 (Claude Code)\n")
        self.script("codex", f'''c="{self.control}"
[ -e "$c/codex-sleep" ] && /bin/sleep 3
[ -e "$c/codex-fail" ] && exit 1
/usr/bin/env > "$c/codex-env"
echo '{{"status":"running","socketPath":"{self.socket}","appServerVersion":"0.166.0"}}\'''')
        self.script("kaiusap", "exit 0")
        monkeypatch.setattr(setup_mod, "repo_source", lambda: "/repo/kaiusap")
        self.monkeypatch = monkeypatch

    def serve_tmux(self, body):
        """A default tmux server's socket, and a fake tmux whose show-options does `body`; calls are logged."""
        socket = self.tmux_tmpdir / f"tmux-{os.getuid()}" / "default"
        socket.parent.mkdir(parents=True, exist_ok=True)
        socket.write_text("")
        self.monkeypatch.setattr(setup_mod, "answers", lambda path: True)  # the fake socket stands for a live server
        self.script("tmux", f'echo "$@" >> "{self.control}/tmux-calls"\n{body}')

    def tmux_calls(self):
        path = self.control / "tmux-calls"
        return path.read_text().splitlines() if path.exists() else []

    def script(self, name, body):
        path = self.bin / name
        path.write_text("#!/bin/sh\n" + body + "\n")
        path.chmod(path.stat().st_mode | stat.S_IEXEC)

    def write(self, relative, text):
        path = self.home / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)

    def install_everything(self):
        self.write(".claude/plugins/installed_plugins.json", json.dumps({"version": 2, "plugins": {"kaiusap@kaiusap": [
            {"scope": "user", "installPath": "/x", "version": "0.1.0", "installedAt": "2026-11-05T00:00:00Z"}]}}))
        self.write(".claude/plugins/known_marketplaces.json", json.dumps({"kaiusap": {"source": {"source": "directory", "path": "/repo/kaiusap"}}}))
        self.write(".claude/settings.json", json.dumps({"enabledPlugins": {"kaiusap@kaiusap": True}}))
        self.write(".codex/config.toml", FULL_CONFIG)
        (self.codex_home / "plugins/cache/kaiusap/kaiusap/0.1.0").mkdir(parents=True)
        self.socket.write_text("")
        # OpenCode: the plugin module (the rig's repo isn't on disk, so it is known by
        # its hook command), the MCP entry, and an `opencode` on the PATH.
        self.write(".config/opencode/plugin/kaiusap.ts", 'Bun.spawn(["kaiusap", "hook", "opencode", event])\n')
        self.write(".config/opencode/opencode.json", OPENCODE_CONFIG)
        self.script("opencode", "echo 1.19.5")

    def run(self, store, *flags, **extra):
        out, err = StringIO(), StringIO()
        code = main(["setup", "--json", *flags], store=store, stdin=StringIO(""), stdout=out, stderr=err, environ={**self.env, **extra})
        assert code == 0 and err.getvalue() == "", err.getvalue()
        result = json.loads(out.getvalue())
        assert result["schema"] == 1
        return {item["item"]: item for item in result["items"]}

    def text(self, store, **extra):
        out = StringIO()
        assert main(["setup"], store=store, stdin=StringIO(""), stdout=out, stderr=StringIO(), environ={**self.env, **extra}) == 0
        return out.getvalue().splitlines()


@pytest.fixture
def rig(tmp_path, monkeypatch):
    return Rig(tmp_path, monkeypatch)


def test_everything_missing(rig, store):
    """Claude and Codex on the PATH with nothing of kaiusap's installed; OpenCode isn't installed, so its items are
    one skipped line (`setup` skips a provider that isn't installed)."""
    (rig.bin / "kaiusap").unlink()
    items = rig.run(store)
    assert list(items) == SKIPPED_OPENCODE
    for name in ["kaiusap found by the plugins", "Claude plugin", "Codex plugin", "thread-id in Codex's [tui] terminal_title",
                 "Codex hook approval"]:
        assert items[name]["done"] is False and items[name]["command"], name
    assert items["OpenCode"] == skipped("OpenCode", "opencode")
    assert items["tmux server"] == {"item": "tmux server", "done": True, "detail": "not in tmux", "command": None, "note": None}
    assert items["tmux pane titles"] == {"item": "tmux pane titles", "done": True, "detail": "no default tmux server is running",
                                         "command": None, "note": None}
    assert items["kaiusap found by the plugins"]["command"] == "uv tool install /repo/kaiusap"
    assert items["Claude plugin"]["command"] == "claude plugin marketplace add /repo/kaiusap\nclaude plugin install kaiusap@kaiusap"
    assert items["Codex plugin"]["command"] == "codex plugin marketplace add /repo/kaiusap\ncodex plugin add kaiusap@kaiusap"
    # The plugins run kaiusap through their launcher, not the PATH, so no PATH note.
    assert items["Claude plugin"]["note"] is None and items["Codex plugin"]["note"] is None
    assert items["thread-id in Codex's [tui] terminal_title"]["command"] == 'terminal_title = ["activity", "thread-name", "project-name", "thread-id"]'
    assert items["Codex hook approval"]["detail"] == "no approval recorded for session_start, user_prompt_submit, subagent_start"
    assert items["Codex hook approval"]["command"] == "start Codex and choose Review hooks or Trust all"
    assert not (rig.home / ".kaiusap").exists() and not store.root.exists()


def test_everything_done(rig, store):
    rig.install_everything()
    items = rig.run(store)
    assert list(items) == ITEMS
    for name in ITEMS:
        assert items[name]["done"] is True and items[name]["command"] is None, name
    assert items["kaiusap found by the plugins"]["detail"] == str(rig.bin / "kaiusap")
    assert items["Codex daemon"]["detail"].startswith("running, app-server 0.166.0")
    assert items["Claude Code version"]["detail"] == "2.1.318"
    assert items["Codex hook approval"]["detail"] == "approval recorded for all three hooks"
    assert items["Claude plugin"]["note"] is None and items["Codex plugin"]["note"] is None
    env = (rig.control / "codex-env").read_text()
    assert "CODEX_HOME=" not in env  # the default home's daemon, whatever the caller inherited


def test_text_output_has_each_command_alone_on_its_line(rig, store):
    lines = rig.text(store)
    assert lines[0] == "kaiusap found by the plugins: done (" + str(rig.bin / "kaiusap") + ")"
    index = lines.index("Claude plugin: missing (the marketplace kaiusap isn't added; kaiusap@kaiusap isn't installed)")
    assert lines[index + 1:index + 3] == ["claude plugin marketplace add /repo/kaiusap", "claude plugin install kaiusap@kaiusap"]
    assert not lines[index + 3].startswith("Note: ")
    assert SESSION_LINE not in lines and not any("pointer line" in line for line in lines)
    assert "Codex hook approval: missing (no approval recorded for session_start, user_prompt_submit, subagent_start)" in lines
    assert lines[lines.index("Codex hook approval: missing (no approval recorded for session_start, user_prompt_submit, subagent_start)") + 1] == "start Codex and choose Review hooks or Trust all"
    assert lines[-2:] == ["tmux server: done (not in tmux)", "tmux pane titles: done (no default tmux server is running)"]
    rig.install_everything()
    lines = rig.text(store)
    assert all(": done" in line or line.startswith("Note: ") for line in lines)


def test_setup_takes_no_arguments(rig, store):
    for argv, code in ((["setup", "x"], 64), (["setup", "--all"], 1), (["setup", "--bogus"], 64)):
        err = StringIO()
        assert main(argv, store=store, stdin=StringIO(""), stdout=StringIO(), stderr=err, environ=rig.env) == code
        assert err.getvalue().startswith("kaiusap: ")


# ----- the Claude plugin ------------------------------------------------------

def test_claude_plugin_marketplace_known_but_not_installed(rig, store):
    rig.install_everything()
    rig.write(".claude/plugins/installed_plugins.json", json.dumps({"version": 2, "plugins": {}}))
    item = rig.run(store)["Claude plugin"]
    assert not item["done"] and item["detail"] == "kaiusap@kaiusap isn't installed"
    assert item["command"] == "claude plugin install kaiusap@kaiusap"


def test_claude_plugin_installed_but_marketplace_unknown(rig, store):
    rig.install_everything()
    (rig.claude_dir / "plugins/known_marketplaces.json").unlink()
    item = rig.run(store)["Claude plugin"]
    assert not item["done"] and item["detail"] == "the marketplace kaiusap isn't added"
    assert item["command"] == "claude plugin marketplace add /repo/kaiusap"


@pytest.mark.parametrize("settings", [{"enabledPlugins": {"kaiusap@kaiusap": False}}, {"enabledPlugins": {}}, {}])
def test_claude_plugin_not_enabled_in_settings(rig, store, settings):
    rig.install_everything()
    rig.write(".claude/settings.json", json.dumps(settings))
    item = rig.run(store)["Claude plugin"]
    assert not item["done"] and item["detail"] == "kaiusap@kaiusap isn't enabled in ~/.claude/settings.json (enabledPlugins)"
    assert item["command"] == "claude plugin enable kaiusap@kaiusap"


def test_claude_plugin_cache_dir_variable_is_honoured(rig, store, tmp_path):
    rig.install_everything()
    assert not rig.run(store, CLAUDE_CODE_PLUGIN_CACHE_DIR=str(tmp_path / "elsewhere"))["Claude plugin"]["done"]


def test_claude_plugin_registry_unreadable_counts_as_missing(rig, store):
    rig.install_everything()
    rig.write(".claude/plugins/installed_plugins.json", "{not json")
    assert not rig.run(store)["Claude plugin"]["done"]


def test_claude_config_dir_is_honoured(rig, store, tmp_path):
    rig.install_everything()
    other = tmp_path / "other-claude"
    items = rig.run(store, CLAUDE_CONFIG_DIR=str(other))
    assert not items["Claude plugin"]["done"]
    assert list(items) == ITEMS


# ----- the Codex plugin ---------------------------------------------------------

def test_codex_plugin_entry_without_cache(rig, store):
    rig.install_everything()
    (rig.codex_home / "plugins/cache/kaiusap/kaiusap/0.1.0").rmdir()
    item = rig.run(store)["Codex plugin"]
    assert not item["done"] and item["detail"] == "kaiusap@kaiusap isn't installed (its plugin cache is missing)"
    assert item["command"] == "codex plugin add kaiusap@kaiusap"


def test_codex_plugin_cache_without_entry(rig, store):
    rig.install_everything()
    rig.write(".codex/config.toml", FULL_CONFIG.replace('[plugins."kaiusap@kaiusap"]\nenabled = true\n', ""))
    item = rig.run(store)["Codex plugin"]
    assert not item["done"] and item["detail"] == "kaiusap@kaiusap isn't installed (its config.toml entry is missing)"
    assert item["command"] == "codex plugin add kaiusap@kaiusap"


def test_codex_plugin_marketplace_missing(rig, store):
    rig.install_everything()
    rig.write(".codex/config.toml", FULL_CONFIG.replace('[marketplaces.kaiusap]\nsource_type = "local"\nsource = "/repo/kaiusap"\n', ""))
    item = rig.run(store)["Codex plugin"]
    assert not item["done"] and item["detail"] == "the marketplace kaiusap isn't added"
    assert item["command"] == "codex plugin marketplace add /repo/kaiusap"


def test_codex_plugin_disabled(rig, store):
    rig.install_everything()
    rig.write(".codex/config.toml", FULL_CONFIG.replace("enabled = true", "enabled = false"))
    item = rig.run(store)["Codex plugin"]
    assert not item["done"] and item["detail"] == "kaiusap@kaiusap is disabled in ~/.codex/config.toml (enabled = false)"
    assert item["command"] == '[plugins."kaiusap@kaiusap"]\nenabled = true'


def test_codex_home_variable_is_ignored(rig, store, tmp_path):
    """`setup` reads the default ~/.codex only, whatever CODEX_HOME says."""
    rig.install_everything()
    items = rig.run(store, CODEX_HOME=str(tmp_path / "elsewhere"))
    assert items["Codex plugin"]["done"] and items["Codex hook approval"]["done"]


# ----- the instruction files, the title, the daemon, the version ---------------

OPENED = []


def record_opens(event, args):
    if event == "open" and OPENED and args and isinstance(args[0], (str, bytes, os.PathLike)):
        OPENED.append(os.fsdecode(args[0]))


sys.addaudithook(record_opens)


@pytest.mark.parametrize("text", ["# Global\n\n" + SESSION_LINE + "\n", "intro\n  - run `kaiusap help` first\n", ""])
def test_setup_never_opens_the_instruction_files(rig, store, tmp_path, text):
    """kaiusap needs no line in CLAUDE.md or AGENTS.md, and setup doesn't read them, whatever they hold."""
    other = tmp_path / "other-claude"
    for relative in (".claude/CLAUDE.md", ".codex/AGENTS.md"):
        rig.write(relative, text)
    other.mkdir()
    (other / "CLAUDE.md").write_text(text)
    rig.install_everything()
    OPENED[:] = ["<recording>"]
    try:
        plain, configured = rig.run(store), rig.run(store, CLAUDE_CONFIG_DIR=str(other))
        rig.text(store)
    finally:
        opened, OPENED[:] = OPENED[1:], []
    assert opened, "the audit hook recorded no opens"
    assert not [path for path in opened if Path(path).name in {"CLAUDE.md", "AGENTS.md"}]
    assert list(plain) == list(configured) == ITEMS
    assert all(item["done"] for item in plain.values())


@pytest.mark.parametrize("config,done,command", [
    ('[tui]\nterminal_title = ["model", "thread-id"]\n', True, None),
    ('[tui]\nterminal_title = ["session-id"]\n', True, None),
    ('[tui]\nterminal_title = ["model", "cwd"]\n', False, 'terminal_title = ["model", "cwd", "thread-id"]'),
    ('[tui]\nterminal_title = []\n', False, 'terminal_title = ["thread-id"]'),
    ("model = \"x\"\n", False, 'terminal_title = ["activity", "thread-name", "project-name", "thread-id"]'),
    ("not = [toml\n", False, 'terminal_title = ["activity", "thread-name", "project-name", "thread-id"]'),
])
def test_title_item(rig, store, config, done, command):
    rig.write(".codex/config.toml", config)
    item = rig.run(store)["thread-id in Codex's [tui] terminal_title"]
    assert item["done"] is done and item["command"] == command
    if not done:
        assert item["detail"] == ("~/.codex/config.toml couldn't be read" if config.startswith("not") else "add under [tui] in ~/.codex/config.toml")


def test_daemon_item(rig, store, monkeypatch):
    assert rig.run(store)["Codex daemon"]["detail"] == "the daemon's socket is missing"
    rig.socket.write_text("")
    assert rig.run(store)["Codex daemon"]["done"]
    (rig.control / "codex-fail").write_text("")
    item = rig.run(store)["Codex daemon"]
    assert not item["done"] and item["detail"] == "codex app-server daemon version exited 1" and item["command"] == "codex app-server daemon start"
    (rig.control / "codex-fail").unlink()
    (rig.control / "codex-sleep").write_text("")
    monkeypatch.setattr("kaiusap.adapters.daemon.VERSION_LIMIT", 0.3)
    item = rig.run(store)["Codex daemon"]
    assert not item["done"] and item["detail"] == "codex app-server daemon version failed: TimeoutExpired"


def test_claude_version_item(rig, store, monkeypatch):
    assert rig.run(store)["Claude Code version"] == {"item": "Claude Code version", "done": True, "detail": "2.1.318", "command": None, "note": None}
    (rig.control / "claude-version").write_text("2.1.224 (Claude Code)\n")
    assert rig.run(store)["Claude Code version"]["done"]
    (rig.control / "claude-version").write_text("2.1.223 (Claude Code)\n")
    item = rig.run(store)["Claude Code version"]
    assert not item["done"] and item["detail"] == "2.1.223 is older than 2.1.224" and item["command"] == "claude update"
    (rig.control / "claude-version").write_text("garbage\n")
    item = rig.run(store)["Claude Code version"]
    assert not item["done"] and item["detail"] == "claude --version exited 0 without a version"
    (rig.control / "claude-fail").write_text("")
    assert rig.run(store)["Claude Code version"]["detail"] == "claude --version exited 1 without a version"
    (rig.control / "claude-fail").unlink()
    (rig.control / "claude-sleep").write_text("")
    monkeypatch.setattr(claude_mod, "CLAUDE_VERSION_LIMIT", 0.3)
    item = rig.run(store)["Claude Code version"]
    assert not item["done"] and item["detail"] == "claude --version didn't finish in 0.3 s" and item["command"] == "claude update"
    (rig.bin / "claude").unlink()
    assert rig.run(store)["Claude"] == skipped("Claude", "claude")  # nothing of kaiusap's for Claude: skipped
    rig.install_everything()  # the plugin is installed, so its items show without the command
    item = rig.run(store)["Claude Code version"]
    assert item["detail"] == "claude isn't on the PATH" and item["command"].startswith("Install Claude Code 2.1.224 or later")


def test_a_provider_that_isnt_installed_is_skipped(rig, store):
    """A provider whose command isn't on the PATH and of which nothing is in place is one done line."""
    for name in ("claude", "codex"):
        (rig.bin / name).unlink()
    items = rig.run(store)
    assert list(items) == ["kaiusap found by the plugins", "Claude", "Codex", "OpenCode", "tmux server", "tmux pane titles"]
    assert items["Claude"] == skipped("Claude", "claude") and items["Codex"] == skipped("Codex", "codex")


def test_claude_version_parsing():
    assert claude_version("2.1.318 (Claude Code)\n") == (2, 1, 318)
    assert claude_version("10.0.1") == (10, 0, 1)
    assert claude_version("") is None and claude_version("Claude Code") is None


# ----- the hook approval ----------------------------------------------------------

def test_approval_all_three(rig, store):
    rig.write(".codex/config.toml", HOOKS_STATE)
    item = rig.run(store)["Codex hook approval"]
    assert item == {"item": "Codex hook approval", "done": True, "detail": "approval recorded for all three hooks", "command": None, "note": None}


@pytest.mark.parametrize("label", HOOK_KEYS)
def test_approval_one_missing(rig, store, label):
    state = "\n".join(f'[hooks.state."{key}"]\ntrusted_hash = "sha256:x"\n' for l, key in HOOK_KEYS.items() if l != label)
    rig.write(".codex/config.toml", state)
    item = rig.run(store)["Codex hook approval"]
    assert not item["done"] and item["detail"] == f"no approval recorded for {label}"
    assert item["command"] == "start Codex and choose Review hooks or Trust all"


@pytest.mark.parametrize("label", HOOK_KEYS)
def test_approval_one_disabled(rig, store, label):
    state = "\n".join(f'[hooks.state."{key}"]\ntrusted_hash = "sha256:x"\n' + ("enabled = false\n" if l == label else "")
                      for l, key in HOOK_KEYS.items())
    rig.write(".codex/config.toml", state)
    item = rig.run(store)["Codex hook approval"]
    assert not item["done"] and item["detail"] == f"enabled = false for {label}"


def test_approval_ignores_an_empty_or_foreign_hash(rig, store):
    state = HOOKS_STATE.replace('trusted_hash = "sha256:session_start"', 'trusted_hash = ""')
    state = state.replace('trusted_hash = "sha256:subagent_start"', "enabled = true")
    rig.write(".codex/config.toml", state + '\n[hooks.state."other@m:hooks/hooks.json:session_start:0:0"]\ntrusted_hash = "sha256:y"\n')
    item = rig.run(store)["Codex hook approval"]
    assert item["detail"] == "no approval recorded for session_start, subagent_start"


def test_approval_never_compares_the_hash(rig, store):
    rig.write(".codex/config.toml", HOOKS_STATE.replace("sha256:", "stale:"))
    assert rig.run(store)["Codex hook approval"]["done"]


# ----- tmux ----------------------------------------------------------------------

@pytest.mark.parametrize("tmux,done", [
    ("", True), (f"/private/tmp/tmux-{os.getuid()}/default,4321,0", True),
    (f"/private/tmp/tmux-{os.getuid()}/other,4321,0", False), ("/Users/x/custom.sock,4321,1", False),
    (f"/tmp/tmux-{os.getuid() + 1}/default,4321,0", False)])
def test_tmux_warning(rig, store, tmux, done):
    item = rig.run(store, TMUX=tmux)
    item = item["tmux server"]
    assert item["done"] is done and item["command"] is None
    if not done:
        assert item["detail"] == f"this shell's TMUX names {tmux.rsplit(',', 2)[0]}; kaiusap looks at the default tmux server only"
        assert any(line.startswith("tmux server: warning (") for line in rig.text(store, TMUX=tmux))


# ----- tmux pane titles ----------------------------------

ALLOW_SET_TITLE = "-L default show-options -gwv allow-set-title"


def test_a_socket_no_server_answers_on_is_no_server(rig, store, tmp_path):
    """A server that ended leaves its socket; nothing listens, so there is no server to ask."""
    import shutil
    import socket
    import tempfile
    short = Path(tempfile.mkdtemp(dir="/tmp"))  # a Unix socket path has a length limit
    left = short / "s"
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(str(left))
    server.close()  # the socket file stays, with no one listening
    assert left.exists() and not setup_mod.answers(left)
    live = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    live.bind(str(short / "l"))
    live.listen()
    try:
        assert setup_mod.answers(short / "l")
    finally:
        live.close()
        shutil.rmtree(short)
    stale = rig.tmux_tmpdir / f"tmux-{os.getuid()}" / "default"
    stale.parent.mkdir(parents=True, exist_ok=True)
    stale.write_text("")  # not a socket at all: nothing answers either
    rig.script("tmux", f'echo "$@" >> "{rig.control}/tmux-calls"\nexit 1')
    item = rig.run(store)["tmux pane titles"]
    assert item["done"] is True and item["detail"] == "no default tmux server is running" and rig.tmux_calls() == []


def test_pane_titles_on(rig, store):
    rig.serve_tmux("echo on")
    item = rig.run(store)["tmux pane titles"]
    assert item == {"item": "tmux pane titles", "done": True, "detail": "allow-set-title is on", "command": None, "note": None}
    assert rig.tmux_calls() == [ALLOW_SET_TITLE]  # one read, nothing else
    assert "tmux pane titles: done (allow-set-title is on)" in rig.text(store)


def test_pane_titles_off_prints_the_two_line_fix(rig, store):
    rig.serve_tmux("echo off")
    item = rig.run(store)["tmux pane titles"]
    assert not item["done"] and item["command"] == "set -g allow-set-title on\ntmux source-file ~/.tmux.conf"
    assert item["detail"] == ("allow-set-title is off, so a program's title never reaches its pane; "
                              "add the first line to ~/.tmux.conf, then run the second")
    lines = rig.text(store)
    index = lines.index("tmux pane titles: missing (" + item["detail"] + ")")
    assert lines[index + 1:index + 3] == ["set -g allow-set-title on", "tmux source-file ~/.tmux.conf"]
    assert rig.tmux_calls() == [ALLOW_SET_TITLE] * 2


def test_pane_titles_without_a_default_server_runs_no_tmux(rig, store):
    rig.script("tmux", f'echo "$@" >> "{rig.control}/tmux-calls"\necho off')
    item = rig.run(store, TMUX=f"/private/tmp/tmux-{os.getuid()}/default,1,0")["tmux pane titles"]
    assert item["done"] and item["detail"] == "no default tmux server is running" and rig.tmux_calls() == []


@pytest.mark.parametrize("body,detail", [
    ("exit 1", f"tmux {ALLOW_SET_TITLE} exited 1"),
    ("echo maybe", f"tmux {ALLOW_SET_TITLE} printed 'maybe'"),
    # An empty answer, which `-gpv` gave on every server before 2026-11-09, is missing too.
    ("echo", f"tmux {ALLOW_SET_TITLE} printed ''"),
    ("/bin/sleep 3", f"tmux {ALLOW_SET_TITLE} didn't finish in 2 s")])
def test_pane_titles_failed_read_is_missing(rig, store, body, detail):
    rig.serve_tmux(body)
    item = rig.run(store)["tmux pane titles"]
    assert not item["done"] and item["detail"] == detail + "; add the first line to ~/.tmux.conf, then run the second"
    assert item["command"] == "set -g allow-set-title on\ntmux source-file ~/.tmux.conf"


def test_pane_titles_tmux_that_cant_be_run(rig, store):
    rig.serve_tmux("echo on")
    def run(argv, limit, env):
        assert argv[0] == "tmux" and limit == 2
        raise FileNotFoundError(argv[0])
    item = Setup(store, rig.env, run=run).pane_titles()
    assert not item["done"] and item["detail"].startswith(f"tmux {ALLOW_SET_TITLE} couldn't be run (FileNotFoundError)")


# ----- read-only, from any shell ------------------------------------------------

def snapshot(root):
    return {str(p.relative_to(root)): p.read_bytes() for p in root.rglob("*") if p.is_file()} if root.exists() else None


def test_setup_writes_nothing_even_from_a_session_shell(rig, store, tmp_path):
    """No store record, no terminal-map line, no name catch-up, no identity lookup."""
    rig.install_everything()
    store.append("handles.jsonl", {"at": "2026-11-05T12:00:00Z", "id": "20261105T120000Z-aaaaaa", "handle": "alpha", "reason": "iam",
                                   "session_id": "t1", "session_key": "codex:t1", "provider": "codex", "by_session": "t1",
                                   "by_session_key": "codex:t1", "pid": None, "process_started_at": None, "moved_from": None,
                                   "via": None, "also_released": None})
    before = snapshot(store.root)
    assert before and "handles.jsonl" in before
    rig.script("tmux", "echo 'should never run' >&2; exit 1")
    rig.script("ps", "echo 'should never run' >&2; exit 1")
    for extra in ({}, {"CODEX_THREAD_ID": "t1", "CODEX_SESSION_ID": "t1"}, {"CLAUDE_CODE_SESSION_ID": "s", "CLAUDE_PID": "1"},
                  {"CODEX_THREAD_ID": "t1", "CLAUDE_CODE_SESSION_ID": "s"}, {"TMUX": f"/private/tmp/tmux-{os.getuid()}/default,1,0", "TMUX_PANE": "%1"}):
        items = rig.run(store, **extra)
        assert all(item["done"] for item in items.values())
        assert snapshot(store.root) == before
    for name in ("installed_plugins.json", "known_marketplaces.json"):
        assert (rig.claude_dir / "plugins" / name).read_text()  # still there
    assert (rig.codex_home / "config.toml").read_text() == FULL_CONFIG
    assert not (store.root / "sessions/codex/terminals.jsonl").exists()


def test_setup_through_the_core_runs_no_catch_up(rig, store):
    """Direct use: `Core.run("setup")` ends with `finish(catch_up=False)` for any provider."""
    from kaiusap.adapters import Providers
    from kaiusap.core import Core
    calls = []

    class Adapter:
        def begin(self):
            calls.append("begin")

        def reset(self):
            calls.append("reset")

        def finish(self, catch_up=True):
            calls.append(("finish", catch_up))

    providers = Providers({"codex": Adapter(), "claude": Adapter()}, store)
    result = Core(store, Identity(), providers, rig.env).run("setup", {})
    assert result["schema"] == 1 and [i["item"] for i in result["items"]] == SKIPPED_OPENCODE
    assert ("finish", False) in calls and ("finish", True) not in calls


def test_malformed_settings_are_reported(rig, store):
    store.root.mkdir(parents=True)
    (store.root / "settings.json").write_text("{bad")
    out = StringIO()
    assert main(["setup"], store=store, stdin=StringIO(""), stdout=out, stderr=StringIO(), environ=rig.env) == 0
    assert "Warning: Malformed settings; using defaults." in out.getvalue().splitlines()


def test_setup_as_a_subprocess_from_a_plain_shell(rig, store, tmp_path):
    env = {**rig.env, "PYTHONPATH": "src", "KAIUSAP_HOME": str(tmp_path / "never-created")}
    result = subprocess.run([sys.executable, "-m", "kaiusap", "setup", "--json"], capture_output=True, text=True, env=env)
    assert result.returncode == 0 and result.stderr == ""
    assert [i["item"] for i in json.loads(result.stdout)["items"]] == SKIPPED_OPENCODE
    assert not (tmp_path / "never-created").exists()


def test_after_uninstall_every_piece_is_missing(rig, store):
    """The uninstall commands leave hooks.state and ~/.kaiusap; once hooks.state is removed by hand
    too, every item but the tmux warning reads missing. Nothing is read from CLAUDE.md or AGENTS.md."""
    rig.install_everything()
    # claude plugin uninstall; claude plugin marketplace remove
    rig.write(".claude/plugins/installed_plugins.json", json.dumps({"version": 2, "plugins": {}}))
    rig.write(".claude/plugins/known_marketplaces.json", "{}")
    rig.write(".claude/settings.json", json.dumps({"enabledPlugins": {}}))
    # codex plugin remove; codex plugin marketplace remove (hooks.state stays)
    rig.write(".codex/config.toml", '[tui]\nterminal_title = ["activity", "thread-name", "thread-id"]\n\n' + HOOKS_STATE)
    import shutil
    shutil.rmtree(rig.codex_home / "plugins/cache/kaiusap")
    # rm ~/.config/opencode/plugin/kaiusap.ts; the user removes the mcp.kaiusap entry
    (rig.home / ".config/opencode/plugin/kaiusap.ts").unlink()
    rig.write(".config/opencode/opencode.json", "{}")
    # uv tool uninstall kaiusap
    (rig.bin / "kaiusap").unlink()
    items = rig.run(store)
    assert not items["kaiusap found by the plugins"]["done"] and not items["Claude plugin"]["done"] and not items["Codex plugin"]["done"]
    assert items["Codex hook approval"]["done"]  # hooks.state is left for the user to remove
    # The user removes the hooks.state entries by hand; the instruction files hold nothing of kaiusap's.
    rig.write(".codex/config.toml", "")
    rig.socket.unlink()
    (rig.bin / "claude").unlink()
    (rig.bin / "codex").unlink()
    (rig.bin / "opencode").unlink()
    OPENED[:] = ["<recording>"]
    try:
        items = rig.run(store)
    finally:
        opened, OPENED[:] = OPENED[1:], []
    # Every provider's command is gone too, so each is one skipped line; only the program reads missing.
    assert list(items) == ["kaiusap found by the plugins", "Claude", "Codex", "OpenCode", "tmux server", "tmux pane titles"]
    assert [name for name, item in items.items() if not item["done"]] == ["kaiusap found by the plugins"]
    assert opened and not [path for path in opened if Path(path).name in {"CLAUDE.md", "AGENTS.md"}]


def test_direct_url_source():
    assert direct_url_source('{"url":"file:///home/user/kaiusap","dir_info":{"editable":true}}') == "/home/user/kaiusap"
    assert direct_url_source('{"url":"file:///path/with%20space","dir_info":{}}') == "/path/with space"
    assert direct_url_source('{"url":"https://github.com/x/kaiusap","vcs_info":{"vcs":"git","commit_id":"abc"}}') == "https://github.com/x/kaiusap"
    assert direct_url_source('{"url":"git+ssh://git@github.com/x/kaiusap.git","vcs_info":{"vcs":"git"}}') == "ssh://git@github.com/x/kaiusap.git"
    assert direct_url_source("") is None and direct_url_source("{bad") is None and direct_url_source('{"x":1}') is None


def test_unknown_install_source_prints_a_placeholder(rig, store, monkeypatch):
    monkeypatch.setattr(setup_mod, "repo_source", lambda: None)
    (rig.bin / "kaiusap").unlink()
    assert rig.run(store)["kaiusap found by the plugins"]["command"] == "uv tool install <repo>"


def test_program_found_by_the_launchers_rule(rig, store, tmp_path):
    """KAIUSAP_BIN, then ~/.local/bin/kaiusap, then the PATH, skipping a plugin's launcher."""
    name = "kaiusap found by the plugins"
    (rig.bin / "kaiusap").unlink()
    launcher_dir = tmp_path / "plugin-bin"
    launcher_dir.mkdir()
    launcher = launcher_dir / "kaiusap"
    launcher.write_bytes((Path(__file__).resolve().parents[1] / "plugins/claude/bin/kaiusap").read_bytes())
    launcher.chmod(0o755)
    rig.env["PATH"] = f"{launcher_dir}:{rig.bin}"
    item = rig.run(store)[name]
    assert (item["done"], item["detail"], item["command"]) == (False, "not found", "uv tool install /repo/kaiusap")
    rig.script("kaiusap", "exit 0")
    assert rig.run(store)[name]["detail"] == str(rig.bin / "kaiusap")
    local = rig.home / ".local/bin/kaiusap"
    local.parent.mkdir(parents=True, exist_ok=True)
    local.write_text("#!/bin/sh\n")
    local.chmod(0o755)
    assert rig.run(store)[name]["detail"] == str(local)
    explicit = tmp_path / "explicit-kaiusap"
    explicit.write_text("#!/bin/sh\n")
    assert rig.run(store, KAIUSAP_BIN=str(explicit))[name]["detail"] == str(local)  # not executable: skipped
    explicit.chmod(0o755)
    assert rig.run(store, KAIUSAP_BIN=str(explicit))[name]["detail"] == str(explicit)


def test_start_hooks_give_the_session_line_word_for_word():
    """The start hooks give exactly this session line."""
    assert SESSION_LINE == ("To message other assistant sessions, or when the user gives you a handle (\"you are "
                            "<handle>\"), use kaiusap's tools (search for kaiusap if they aren't loaded; `kaiusap help` "
                            "prints the rules).")


def test_each_adapter_gives_its_own_items_in_order(rig, store):
    """`setup` keeps the shared items (PATH, tmux, pane titles); each registered adapter gives its
    own, each in its place in the order, from the class alone (no adapter is built, nothing is written)."""
    from kaiusap.adapters import ADAPTERS, adapter_class
    setup = Setup(store, rig.env)
    places = {name: [(place, item["item"]) for place, item in adapter_class(name).setup_items(setup)] for name in ADAPTERS}
    assert places == {"claude": [(1, "Claude plugin"), (5, "Claude Code version")],
                      "codex": [(2, "Codex plugin"), (3, "thread-id in Codex's [tui] terminal_title"),
                                (4, "Codex daemon"), (6, "Codex hook approval")],
                      "opencode": [(7, "OpenCode plugin"), (8, "kaiusap's MCP server in OpenCode's config"), (9, "OpenCode version")],
                      "opencode2": []}  # setup shows one OpenCode provider's items, here 1.x's (no opencode found)
    assert [item["item"] for item in setup.report()["items"]] == SKIPPED_OPENCODE  # no opencode: skipped
    assert not store.root.exists()


@pytest.mark.parametrize("output,done", [("1.19.5", True), ("1.19.9", True)])
def test_opencode_1_versions_show_the_1x_items(rig, store, output, done):
    rig.script("opencode", f"echo '{output}'")
    found = rig.run(store)
    assert found["OpenCode version"]["done"] is done and "OpenCode plugin" in found and "OpenCode 2 plugin" not in found


# ----- choosing the OpenCode provider: detection suggests, the user chooses -------------------

PLUGIN_2_COMMAND = "mkdir -p ~/.config/opencode/plugins && ln -s /repo/kaiusap/plugins/opencode2 ~/.config/opencode/plugins/kaiusap"


def test_opencode_2_found_suggests_the_opencode_2_provider(rig, store):
    rig.script("opencode", "echo 'opencode v2.2.7'")
    found = rig.run(store)
    assert "OpenCode plugin" not in found
    plugin = found["OpenCode 2 plugin"]
    assert plugin["done"] is False and plugin["command"] == PLUGIN_2_COMMAND
    assert plugin["note"].endswith("kaiusap suggests its OpenCode 2 provider for OpenCode 2.2.7; for the other, run "
                                   "kaiusap setup --opencode 1")
    assert (found["OpenCode version"]["done"], found["OpenCode version"]["detail"]) == (True, "2.2.7")
    assert found["kaiusap's MCP server in OpenCode's config"]["command"] == (
        '"mcp": {"servers": {"kaiusap": {"type": "local", "command": ["kaiusap", "mcp", "opencode2"], "codemode": false}}}')


def test_opencode_2_before_2_2_7_is_not_supported(rig, store):
    rig.script("opencode", "echo 2.0.0")
    item = rig.run(store)["OpenCode version"]
    assert (item["done"], item["detail"]) == (False, "2.0.0; kaiusap supports OpenCode 2.2.7 up to 3.0")


def test_an_installed_1x_plugin_keeps_the_choice_and_says_it_doesn_t_load(rig, store):
    rig.write(".config/opencode/plugin/kaiusap.ts", 'Bun.spawn(["kaiusap", "hook", "opencode", event])\n')
    rig.script("opencode", "echo 2.2.7")
    found = rig.run(store)
    assert found["OpenCode plugin"]["done"] is True and "OpenCode 2 plugin" not in found
    item = found["OpenCode version"]
    assert item["done"] is False and item["detail"] == (
        "2.2.7; kaiusap's OpenCode 1 plugin is installed, which doesn't load in OpenCode 2.2.7")
    assert item["command"] == PLUGIN_2_COMMAND


def test_an_installed_opencode_2_plugin_with_its_mcp_entry_is_done(rig, store):
    rig.write(".config/opencode/plugins/kaiusap/server.ts", "")
    rig.write(".config/opencode/plugins/kaiusap/tui.ts", "")
    rig.write(".config/opencode/plugins/kaiusap/hook.ts", 'Bun.spawn(["kaiusap", "hook", "opencode2", event])\n')
    rig.write(".config/opencode/opencode.json", json.dumps({"mcp": {"servers": {"kaiusap": {
        "type": "local", "command": ["kaiusap", "mcp", "opencode2"], "codemode": False}}}}))
    rig.script("opencode", "echo 2.2.7")
    found = rig.run(store)
    assert [found[name]["done"] for name in ("OpenCode 2 plugin", "kaiusap's MCP server in OpenCode's config",
                                             "OpenCode version")] == [True, True, True]
    assert found["kaiusap's MCP server in OpenCode's config"]["note"] is None
    rig.write(".config/opencode/opencode.json", json.dumps({"mcp": {"servers": {"kaiusap": {
        "type": "local", "command": ["kaiusap", "mcp", "opencode2"]}}}}))
    note = rig.run(store)["kaiusap's MCP server in OpenCode's config"]["note"]
    assert note.endswith('add "codemode": false to mcp.servers.kaiusap in ~/.config/opencode/opencode.json')
    assert "§" not in note
    rig.write(".config/opencode/opencode.json", json.dumps({"mcp": {"kaiusap": {"type": "local", "command": ["kaiusap", "mcp", "opencode"]}}}))
    item = rig.run(store)["kaiusap's MCP server in OpenCode's config"]
    assert item["done"] is False and item["detail"].endswith("doesn't run kaiusap mcp opencode2")


def test_the_opencode_flag_shows_that_provider_whatever_is_found(rig, store):
    rig.script("opencode", "echo 2.2.7")
    found = rig.run(store, "--opencode", "1")
    item = found["OpenCode version"]
    assert "OpenCode plugin" in found and found["OpenCode plugin"]["note"] == (
        "OpenCode loads plugins when it starts; restart open OpenCode sessions.")
    assert "OpenCode 2 doesn't load its plugin" in item["detail"] and item["command"] == "npm install -g opencode-ai@1"
    rig.script("opencode", "echo 1.19.5")
    found = rig.run(store, "--opencode=2")
    assert "OpenCode 2 plugin" in found and found["OpenCode version"]["detail"] == (
        "1.19.5; kaiusap's OpenCode 2 provider needs OpenCode 2.2.7 or later")


def test_the_opencode_flags_are_checked_and_only_setup_takes_them(rig, store, tmp_path):
    from kaiusap.cli import parse
    from kaiusap.errors import Error
    with pytest.raises(Error, match="--opencode must be 1 or 2"):
        Setup(store, rig.env, options={"opencode": "3"}).report()
    with pytest.raises(Error, match="Unknown flag --opencode"):
        parse(["who", "--opencode", "1"], StringIO(""))
    other = tmp_path / "other-config"
    (other / "plugins/kaiusap").mkdir(parents=True)
    (other / "plugins/kaiusap/server.ts").write_text("")
    rig.script("opencode", "echo 1.19.5")
    assert "OpenCode 2 plugin" in rig.run(store, "--opencode-config", str(other))


def test_both_versions_side_by_side_are_checked_in_their_own_directory_and_command(rig, store, tmp_path):
    """From the Linux test: with 1.x as `opencode` and OpenCode 2 run through
    its own launcher and config directory, `--opencode-command` reads that launcher's version and the plugin
    command installs into `--opencode-config`'s directory, not `~/.config/opencode`."""
    rig.script("opencode", "echo 1.19.5")
    rig.script("opencode2", "echo 'opencode v2.2.7'")
    config = rig.home / ".opencode2/xdg/config/opencode"
    found = rig.run(store, "--opencode", "2", "--opencode-command", "opencode2", "--opencode-config", str(config))
    assert (found["OpenCode version"]["done"], found["OpenCode version"]["detail"]) == (True, "2.2.7")
    assert found["OpenCode 2 plugin"]["command"] == (
        "mkdir -p ~/.opencode2/xdg/config/opencode/plugins && ln -s /repo/kaiusap/plugins/opencode2 "
        "~/.opencode2/xdg/config/opencode/plugins/kaiusap")
    assert found["OpenCode 2 plugin"]["detail"] == "no kaiusap plugin directory in ~/.opencode2/xdg/config/opencode/plugins"
    # Without the command flag the `opencode` on the PATH is 1.x's, as the Linux test found.
    assert rig.run(store, "--opencode", "2", "--opencode-config", str(config))["OpenCode version"]["done"] is False
    # 1.x's own items take the directory too; one outside the home is shown whole, and quoted when it must be.
    other = tmp_path / "odd {dir}"
    found = rig.run(store, "--opencode", "1", "--opencode-config", str(other))
    assert found["OpenCode plugin"]["command"] == (
        f"mkdir -p '{other}/plugin' && ln -s /repo/kaiusap/plugins/opencode/kaiusap.ts '{other}/plugin'/kaiusap.ts")


def test_the_opencode_command_is_named_when_missing_and_taken_as_a_path(rig, store):
    assert rig.run(store, "--opencode-command", "opencode2")["OpenCode"] == skipped("OpenCode", "opencode2")
    rig.script("opencode2", "echo 'opencode v2.2.7'")
    found = rig.run(store, "--opencode-command", str(rig.bin / "opencode2"))
    assert "OpenCode 2 plugin" in found and found["OpenCode version"]["detail"] == "2.2.7"  # the version suggests 2


@pytest.mark.parametrize("command,done", [(["/home/u/.local/bin/kaiusap", "mcp", "opencode"], True),
                                          (["kaiusap", "mcp", "opencode"], True),
                                          (["/home/u/.local/bin/kaiusap", "mcp", "opencode2"], False),
                                          (["/home/u/bin/not-kaiusap", "mcp", "opencode"], False)])
def test_the_opencode_mcp_entry_may_name_kaiusap_by_its_full_path(rig, store, command, done):
    """The plugin's launcher: the installer writes the entry with the installed program's full path."""
    rig.install_everything()
    rig.write(".config/opencode/opencode.json", json.dumps({"mcp": {"kaiusap": {"type": "local", "command": command}}}))
    assert rig.run(store)["kaiusap's MCP server in OpenCode's config"]["done"] is done
