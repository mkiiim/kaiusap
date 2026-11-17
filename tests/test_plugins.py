"""The plugin and marketplace files, word for word."""
import json
import os
from pathlib import Path
import subprocess
import tomllib

import pytest

REPO = Path(__file__).resolve().parent.parent
PLUGIN_FILES = [
    "plugins/claude/.claude-plugin/plugin.json", "plugins/claude/.mcp.json", "plugins/claude/hooks/hooks.json",
    ".claude-plugin/marketplace.json",
    "plugins/codex/.codex-plugin/plugin.json", "plugins/codex/.mcp.json", "plugins/codex/hooks/hooks.json",
    ".agents/plugins/marketplace.json",
]


# Each plugin runs kaiusap through its launcher, never the PATH: Codex
# expands no variable in .mcp.json but joins a relative cwd to the plugin's folder.
MCP_ENTRIES = {"claude": {"command": "${CLAUDE_PLUGIN_ROOT}/bin/kaiusap", "args": ["mcp", "claude"]},
               "codex": {"command": "./bin/kaiusap", "args": ["mcp", "codex"], "cwd": "."}}


def package_version():
    return tomllib.loads((REPO / "pyproject.toml").read_text())["project"]["version"]


# The plugin files, word for word ("<package version>" stands for the version in pyproject.toml).
PLUGIN_TEXTS = {
    "plugins/claude/.claude-plugin/plugin.json": r'''{"name": "kaiusap", "version": "<package version>",
 "description": "Message other Claude Code, Codex, and OpenCode sessions on this machine by handle.",
 "author": {"name": "Mark Emila"}, "license": "Apache-2.0"}''',
    "plugins/claude/.mcp.json": r'''{"mcpServers": {"kaiusap": {"command": "${CLAUDE_PLUGIN_ROOT}/bin/kaiusap", "args": ["mcp", "claude"]}}}''',
    "plugins/claude/hooks/hooks.json": r'''{"hooks": {
  "SessionStart":     [{"hooks": [{"type": "command", "command": "\"${CLAUDE_PLUGIN_ROOT}\"/bin/kaiusap hook claude SessionStart", "timeout": 5}]}],
  "UserPromptSubmit": [{"hooks": [{"type": "command", "command": "\"${CLAUDE_PLUGIN_ROOT}\"/bin/kaiusap hook claude UserPromptSubmit", "timeout": 2}]}],
  "PreToolUse":       [{"matcher": "mcp__plugin_kaiusap_kaiusap__.*",
                        "hooks": [{"type": "command", "command": "\"${CLAUDE_PLUGIN_ROOT}\"/bin/kaiusap hook claude PreToolUse", "timeout": 2}]},
                       {"matcher": "Bash",
                        "hooks": [{"type": "command", "command": "\"${CLAUDE_PLUGIN_ROOT}\"/bin/kaiusap hook claude PreToolUse", "timeout": 2}]}],
  "PermissionRequest": [{"hooks": [{"type": "command", "command": "\"${CLAUDE_PLUGIN_ROOT}\"/bin/kaiusap hook claude PermissionRequest", "timeout": 2}]}],
  "Stop":              [{"hooks": [{"type": "command", "command": "\"${CLAUDE_PLUGIN_ROOT}\"/bin/kaiusap hook claude Stop", "timeout": 2}]}],
  "StopFailure":       [{"hooks": [{"type": "command", "command": "\"${CLAUDE_PLUGIN_ROOT}\"/bin/kaiusap hook claude StopFailure", "timeout": 2}]}],
  "Elicitation":       [{"hooks": [{"type": "command", "command": "\"${CLAUDE_PLUGIN_ROOT}\"/bin/kaiusap hook claude Elicitation", "timeout": 2}]}]}}''',
    ".claude-plugin/marketplace.json": r'''{"name": "kaiusap", "owner": {"name": "kaiusap"},
 "plugins": [{"name": "kaiusap", "source": "./plugins/claude",
              "description": "Message other Claude Code, Codex, and OpenCode sessions on this machine by handle."}]}''',
    "plugins/codex/.codex-plugin/plugin.json": r'''{"name": "kaiusap", "version": "<package version>",
 "description": "Message other Claude Code, Codex, and OpenCode sessions on this machine by handle.",
 "mcpServers": "./.mcp.json", "hooks": "./hooks/hooks.json"}''',
    "plugins/codex/.mcp.json": r'''{"mcpServers": {"kaiusap": {"command": "./bin/kaiusap", "args": ["mcp", "codex"], "cwd": "."}}}''',
    "plugins/codex/hooks/hooks.json": r'''{"description": "kaiusap hooks",
 "hooks": {
  "SessionStart":     [{"hooks": [{"type": "command", "command": "\"${CLAUDE_PLUGIN_ROOT}\"/bin/kaiusap hook codex SessionStart", "timeout": 5}]}],
  "UserPromptSubmit": [{"hooks": [{"type": "command", "command": "\"${CLAUDE_PLUGIN_ROOT}\"/bin/kaiusap hook codex UserPromptSubmit", "timeout": 2}]}],
  "SubagentStart":    [{"hooks": [{"type": "command", "command": "\"${CLAUDE_PLUGIN_ROOT}\"/bin/kaiusap hook codex SubagentStart", "timeout": 2}]}]}}''',
    ".agents/plugins/marketplace.json": r'''{"name": "kaiusap", "interface": {"displayName": "kAIusap"},
 "plugins": [{"name": "kaiusap", "source": "./plugins/codex",
              "policy": {"installation": "AVAILABLE"}}]}''',
}


def test_the_texts_cover_the_eight_files():
    assert list(PLUGIN_TEXTS) == PLUGIN_FILES


@pytest.mark.parametrize("path", PLUGIN_FILES)
def test_plugin_file_is_the_literal_text_with_the_package_version(path):
    expected = PLUGIN_TEXTS[path].replace("<package version>", package_version())
    data = (REPO / path).read_bytes()
    assert data.endswith(b"\n") and not data.endswith(b"\n\n")
    assert data.decode() == expected + "\n"
    json.loads(data)


@pytest.mark.parametrize("path", ["plugins/claude/.claude-plugin/plugin.json", "plugins/codex/.codex-plugin/plugin.json"])
def test_plugin_version_matches_pyproject(path):
    assert json.loads((REPO / path).read_text())["version"] == package_version()
    assert "<package version>" not in (REPO / path).read_text()


def test_plugin_files_register_the_hook_and_mcp_commands():
    """The plugins only register kaiusap's hooks, Claude's record-only activity hooks and the MCP server."""
    for provider in ("claude", "codex"):
        mcp = json.loads((REPO / f"plugins/{provider}/.mcp.json").read_text())
        assert mcp == {"mcpServers": {"kaiusap": MCP_ENTRIES[provider]}}
        hooks = json.loads((REPO / f"plugins/{provider}/hooks/hooks.json").read_text())["hooks"]
        for event, groups in hooks.items():
            for group in groups:
                for handler in group["hooks"]:
                    assert handler["type"] == "command" and type(handler["timeout"]) is int
                    assert handler["command"] == f'"${{CLAUDE_PLUGIN_ROOT}}"/bin/kaiusap hook {provider} {event}'
    assert set(json.loads((REPO / "plugins/claude/hooks/hooks.json").read_text())["hooks"]) == {"SessionStart", "UserPromptSubmit", "PreToolUse",
                                                                                     "PermissionRequest", "Stop", "StopFailure", "Elicitation"}
    assert set(json.loads((REPO / "plugins/codex/hooks/hooks.json").read_text())["hooks"]) == {"SessionStart", "UserPromptSubmit", "SubagentStart"}


# ----- the plugin's launcher -------------------------------------------------

LAUNCHERS = ["plugins/claude/bin/kaiusap", "plugins/codex/bin/kaiusap"]
MISSING = "kaiusap isn't installed (looked in ~/.local/bin/kaiusap and on the PATH). Run ./install.sh."
ARGS = ["hook", "claude", "a b", "", "*", "$HOME"]


def test_launchers_are_identical_and_executable():
    first, second = ((REPO / path).read_bytes() for path in LAUNCHERS)
    assert first == second and first.startswith(b"#!/bin/sh\n")
    for path in LAUNCHERS:
        assert os.stat(REPO / path).st_mode & 0o777 == 0o755, path


def fake(folder, name="kaiusap"):
    """An executable that prints its pid, its own path and each argument, each NUL-terminated."""
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / name
    path.write_text('#!/bin/sh\nprintf "%s\\0" "$$" "$0" "$@"\n')
    path.chmod(0o755)
    return path


def launch(tmp_path, args=ARGS, path="", **env):
    """The Claude plugin's launcher with only HOME, PATH and `env` set: (pid, exit code, stdout, stderr)."""
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    environ = {"HOME": str(home), "PATH": path, **env}
    proc = subprocess.Popen([str(REPO / LAUNCHERS[0]), *args], env=environ, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    out, err = proc.communicate(timeout=10)
    return proc.pid, proc.returncode, out.decode(), err.decode()


def ran(result):
    """The fake's own path and arguments, after checking it ran in the launcher's process (exec, no new process)."""
    pid, code, out, err = result
    assert code == 0 and err == ""
    fields = out.split("\0")[:-1]
    assert int(fields[0]) == pid
    return fields[1], fields[2:]


def test_launcher_prefers_kaiusap_bin_then_local_bin_then_path(tmp_path):
    on_path = fake(tmp_path / "path-bin")
    local = fake(tmp_path / "home/.local/bin")
    explicit = fake(tmp_path / "explicit", "my-kaiusap")
    path = f"/usr/bin:/bin:{on_path.parent}"
    assert ran(launch(tmp_path, path=path, KAIUSAP_BIN=str(explicit))) == (str(explicit), ARGS)
    assert ran(launch(tmp_path, path=path)) == (str(local), ARGS)
    explicit.chmod(0o644)  # not executable: skipped
    assert ran(launch(tmp_path, path=path, KAIUSAP_BIN=str(explicit))) == (str(local), ARGS)
    local.unlink()
    assert ran(launch(tmp_path, path=path)) == (str(on_path), ARGS)
    assert ran(launch(tmp_path, path=path, KAIUSAP_BIN="")) == (str(on_path), ARGS)


def test_launcher_skips_itself_and_other_launchers_on_the_path(tmp_path):
    on_path = fake(tmp_path / "path-bin")
    copy = tmp_path / "other-plugin/bin/kaiusap"
    copy.parent.mkdir(parents=True)
    copy.write_bytes((REPO / LAUNCHERS[1]).read_bytes())
    copy.chmod(0o755)
    launchers = f"{REPO / 'plugins/claude/bin'}:{copy.parent}"
    assert ran(launch(tmp_path, path=f"{launchers}:/usr/bin:/bin:{on_path.parent}")) == (str(on_path), ARGS)
    # With only launchers on the PATH it reports the program missing rather than running itself.
    _, code, out, err = launch(tmp_path, ["version"], path=f"{launchers}:/usr/bin:/bin")
    assert (code, out, err) == (1, "", MISSING + "\n")


def test_launcher_without_the_program(tmp_path):
    _, code, out, err = launch(tmp_path, ["hook", "codex", "SessionStart"], path="/usr/bin:/bin")
    assert (code, err) == (0, "") and out == json.dumps({"systemMessage": MISSING}) + "\n"
    for args in (["mcp", "claude"], [], ["version", "hook"]):
        _, code, out, err = launch(tmp_path, args, path="/usr/bin:/bin")
        assert (code, out, err) == (1, "", MISSING + "\n"), args
