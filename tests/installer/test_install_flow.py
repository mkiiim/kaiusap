"""The installer's flow, driven
end to end through its one runner, one input and its environment, against a fake machine in a temp HOME."""
import json
import os
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[2]
for entry in (str(ROOT), str(ROOT / "src")):
    if entry not in sys.path:
        sys.path.insert(0, entry)
sys.path.insert(0, str(Path(__file__).resolve().parent))

from installer_fakes import COMMIT, OLD_COMMIT, Machine, dump_toml, install_fake_edits  # noqa: E402
from installer.flow import main  # noqa: E402

CLAUDE_CODEX_PLAN = [
    "Install kaiusap into ~/.local/bin",
    "Add the kaiusap marketplace to Claude Code",
    "Install the Claude Code plugin",
    "Add the kaiusap marketplace to Codex",
    "Install the Codex plugin",
    "Add thread-id to Codex's terminal title",
    "⚠ Restart the Codex daemon",
]


@pytest.fixture(autouse=True)
def fake_edits(monkeypatch):
    """`installer.edits` by its contract; KAIUSAP_INSTALLER_REAL_EDITS=1 runs the same tests against the real one."""
    if os.environ.get("KAIUSAP_INSTALLER_REAL_EDITS") == "1":
        import installer.edits
        return installer.edits
    return install_fake_edits(monkeypatch)


@pytest.fixture
def machine(tmp_path):
    return Machine(tmp_path)


def plan_lines(text):
    """The numbered plan lines, without their numbers: from the plan's header to the blank line after it."""
    out, inside = [], False
    for line in text.splitlines():
        if line.endswith(("change:", "changes:", "left:")):
            inside, out = True, []
            continue
        if inside and not line.strip():
            break
        parts = line.strip().split("  ", 1)
        if inside and len(parts) == 2 and parts[0].isdigit():
            out.append(parts[1].split("   runs:")[0])
    return out


def go(machine, *argv, answers=("y",), interactive=True):
    ctx = machine.context(interactive=interactive, answers=answers)
    code = main(list(argv), ctx)
    return code, machine.text()


# ----- a fresh install ----------------------------------------------------------------------------------------

def test_fresh_install_with_claude_and_codex(machine):
    code, text = go(machine)
    assert code == 0, text
    assert "kAIusap installer 0.1.0" in text
    assert "✓ Claude Code 2.1.323" in text and "Codex, app-server 0.169.0, daemon running" in text
    assert "– OpenCode               not found, skipped" in text
    assert "This will make 7 changes:" in text
    assert plan_lines(text) == CLAUDE_CODEX_PLAN
    assert "in ~/.codex/config.toml, under [tui]; the file is backed up first" in text
    assert "open Codex terminals reconnect by themselves within a couple of minutes;" in text
    assert machine.commands() == [
        ["uv", "tool", "install", "--reinstall", str(machine.repo)],
        ["claude", "plugin", "marketplace", "add", str(machine.repo)],
        ["claude", "plugin", "install", "kaiusap@kaiusap"],
        ["codex", "plugin", "marketplace", "add", str(machine.repo)],
        ["codex", "plugin", "add", "kaiusap@kaiusap"],
        ["codex", "app-server", "daemon", "restart"],
    ]
    config = machine.codex_config.read_text()
    assert '"activity", "thread-name", "project-name", "thread-id"' in config
    record = machine.record()
    assert record["version"] == "0.1.0" and record["commit"] == COMMIT
    assert record["finished_at"] and record["planned"] == 7
    assert [c["step"] for c in record["changes"]] == ["kaiusap", "claude-marketplace", "claude-plugin",
                                                     "codex-marketplace", "codex-plugin", "codex-title", "codex-daemon"]
    title = record["changes"][5]
    assert title["kind"] == "edit" and title["file"] == str(machine.codex_config) and title["key"] == "terminal_title"
    assert record["previous"] is None
    for n, done in enumerate([f"kaiusap 0.1.0 ({COMMIT[:7]}) installed", "marketplace added to Claude Code",
                              "kaiusap@kaiusap installed in Claude Code", "marketplace added to Codex",
                              "kaiusap@kaiusap installed in Codex", "terminal_title updated",
                              "Codex daemon restarted"], 1):
        assert f"✓ {n:<2} {done}" in text
    assert "Verifying: kaiusap setup" in text and "→ Codex hook approval" in text
    # No session was open, so there are none to bring back: two things, not three.
    assert "Installed. Two things only you can do:" in text and "Bring open sessions back" not in text
    assert "Start Codex and choose Review hooks or Trust all" in text
    assert '"you are api-cld", "you are web-cdx"' in text
    assert "Undo with: ./install.sh --uninstall" in text


def test_sessions_open_are_counted_and_waited_for(machine):
    machine.claude_sessions = [101, 102, 103]
    machine.processes = ["/usr/local/bin/codex", "codex resume 019a", "/bin/zsh"]

    def close_claude(argv):
        if argv[:2] == ["claude", "agents"] and machine.output and "Checking again" in machine.output[-1]:
            machine.claude_sessions = []
    machine.on_call = close_claude
    code, text = go(machine, answers=["y", "", "continue"])
    assert code == 0, text
    before = text.index("⚠ BEFORE PROCEEDING, CLOSE ALL SESSIONS (recommended):")
    assert before < text.index("This will make 7 changes:") < text.index("Proceed? [y/N]")
    assert "Open now: 3 Claude Code sessions, 2 Codex terminals" in text
    assert "If you leave them open: Claude Code needs /reload-plugins in each;" in text
    assert "Codex terminals reconnect after the daemon restart." in text
    assert "Still open: 2 Codex terminals" in text
    assert 'Close them, then press Enter to check again, or type "continue" to go ahead with them open: ' in text


def test_no_sessions_open_skips_the_block(machine):
    code, text = go(machine)
    assert code == 0
    assert "Before proceeding" not in text and "Checking again for open sessions" not in text


# ----- OpenCode -----------------------------------------------------------------------------------------------

def test_opencode_1_links_the_module_and_adds_mcp_kaiusap(tmp_path):
    machine = Machine(tmp_path, claude=None, codex=False, opencode="1.19.5")
    code, text = go(machine)
    assert code == 0, text
    assert "✓ OpenCode 1.19.5        kaiusap's OpenCode 1 plugin; config in ~/.config/opencode" in text
    assert plan_lines(text) == ["Install kaiusap into ~/.local/bin", "Link kaiusap's plugin into OpenCode",
                                "Add kaiusap's MCP server to OpenCode's config"]
    assert "in ~/.config/opencode/opencode.json, as mcp.kaiusap; backed up first" in text
    link = machine.home / ".config/opencode/plugin/kaiusap.ts"
    assert link.is_symlink() and Path(link.readlink()) == machine.repo / "plugins/opencode/kaiusap.ts"
    config = json.loads((machine.home / ".config/opencode/opencode.json").read_text())
    assert config["mcp"]["kaiusap"] == {"type": "local", "command": [str(machine.kaiusap_bin), "mcp", "opencode"]}
    assert not any("service" in argv for argv in machine.calls)
    assert "Bring open sessions back" not in text  # none was open


def test_an_opencode_mcp_entry_setup_rejects_is_shown_and_listed(tmp_path):
    """The installer writes the entry, but what `setup` reports about it is what the user sees."""
    machine = Machine(tmp_path, claude=None, codex=False, opencode="1.19.5")
    name = "kaiusap's MCP server in OpenCode's config"
    detail = "mcp.kaiusap is disabled in ~/.config/opencode/opencode.json (enabled: false)"
    machine.setup_items = [{"item": "kaiusap on the PATH", "done": True}, {"item": name, "done": False, "detail": detail}]
    code, text = go(machine)
    assert code == 0, text
    assert f"✗ {name}   {detail}" in text and f"✓ {name}" not in text
    assert f"kaiusap setup still reports {name} missing: {detail}" in text


def test_opencode_2_links_the_directory_adds_mcp_servers_and_restarts_its_service(tmp_path):
    machine = Machine(tmp_path, claude=None, codex=False, opencode="2.2.7")
    code, text = go(machine)
    assert code == 0, text
    assert "kaiusap's OpenCode 2 plugin" in text
    assert plan_lines(text) == ["Install kaiusap into ~/.local/bin", "Link kaiusap's plugin into OpenCode",
                                "Add kaiusap's MCP server to OpenCode's config", "⚠ Restart OpenCode's service"]
    assert "runs: opencode service restart" in text
    assert 'open OpenCode terminals show "Reconnecting" and reattach;' in text
    link = machine.home / ".config/opencode/plugins/kaiusap"
    assert link.is_symlink() and Path(link.readlink()) == machine.repo / "plugins/opencode2"
    config = json.loads((machine.home / ".config/opencode/opencode.json").read_text())
    assert config["mcp"]["servers"]["kaiusap"] == {"type": "local", "codemode": False,
                                                   "command": [str(machine.kaiusap_bin), "mcp", "opencode2"]}
    assert machine.commands()[-1] == ["opencode", "service", "restart"]
    assert not (machine.kaiusap_home / "settings.json").exists()


def test_opencode_2_under_another_command_sets_opencode2_command(tmp_path):
    machine = Machine(tmp_path, claude=None, codex=False, opencode=None)
    machine.opencode_version = "2.2.7"
    machine.tool("opencode2")
    config = tmp_path / "oc2config"
    code, text = go(machine, "--opencode", "2", "--opencode-command", "opencode2", "--opencode-config", str(config))
    assert code == 0, text
    assert "Set opencode2_command to opencode2 in kaiusap's settings" in text
    assert json.loads((machine.kaiusap_home / "settings.json").read_text()) == {"opencode2_command": "opencode2"}
    assert (config / "plugins/kaiusap").is_symlink()
    assert ["opencode2", "service", "restart"] in machine.commands()
    setup = [argv for argv in machine.calls if argv[1:3] == ["setup", "--json"]][0]
    assert setup[3:] == ["--opencode", "2", "--opencode-command", "opencode2", "--opencode-config", str(config)]


# ----- changing nothing -----------------------------------------------------------------------------------------

def test_plan_flag_changes_nothing(machine):
    code, text = go(machine, "--plan", answers=())
    assert code == 0
    assert plan_lines(text) == CLAUDE_CODEX_PLAN
    assert "Nothing was changed (--plan)." in text
    assert machine.commands() == [] and not machine.kaiusap_home.exists()


def test_without_a_terminal_it_changes_nothing(machine):
    code, text = go(machine, interactive=False, answers=())
    assert code == 0
    assert "There's no terminal to ask on, so nothing was changed." in text
    assert machine.commands() == [] and not machine.kaiusap_home.exists()


def test_answering_no_changes_nothing(machine):
    code, text = go(machine, answers=["n"])
    assert machine.commands() == [] and not machine.kaiusap_home.exists()
    assert "Nothing was changed." in text


def test_missing_requirements_stop_with_every_problem_and_nothing_changed(machine):
    (machine.bin / "rustc").unlink()
    machine.claude_version = "2.1.200"
    code, text = go(machine)
    assert code == 1
    assert "✗ rustc                  not found" in text and "fix: install Rust from https://rustup.rs" in text
    assert "✗ Claude Code 2.1.200    needs 2.1.224 or later" in text and "fix: claude update" in text
    assert "2 problems. Nothing was changed." in text
    assert "Proceed?" not in text and machine.commands() == [] and not machine.kaiusap_home.exists()


def test_a_rustc_that_doesnt_run_is_a_problem(machine):
    """A rustup shim with no default toolchain is on the PATH but can't build: caught before any change."""
    machine.rustc_broken = True
    code, text = go(machine)
    assert code == 1
    assert "✗ rustc                  found, but rustc --version fails" in text and "fix: rustup default stable" in text
    assert "Proceed?" not in text and not machine.kaiusap_home.exists()


def test_no_provider_stops(tmp_path):
    machine = Machine(tmp_path, claude=None, codex=False)
    code, text = go(machine)
    assert code == 1 and "No provider found" in text and machine.commands() == []


# ----- stopping and running again ---------------------------------------------------------------------------------

def test_a_failure_stops_and_records_what_was_done(machine):
    machine.fail = {("codex", "plugin", "marketplace", "add"): 1}
    code, text = go(machine)
    assert code == 1
    assert "Stopped at change 4 of 7: it failed, so nothing after it was made." in text
    assert "./install.sh --uninstall to undo the 3 made" in text
    record = machine.record()
    assert record["finished_at"] is None and record["planned"] == 7
    assert [c["step"] for c in record["changes"]] == ["kaiusap", "claude-marketplace", "claude-plugin"]
    assert ["codex", "plugin", "add", "kaiusap@kaiusap"] not in machine.commands()


def test_ctrl_c_mid_step_finishes_the_step_then_stops(machine):
    def interrupt(argv):
        if argv[:3] == ["claude", "plugin", "install"]:
            machine.ctx.stop_requested = True  # what the SIGINT handler does while the command runs
    machine.on_call = interrupt
    code, text = go(machine)
    assert code == 130
    assert "✓ 3  kaiusap@kaiusap installed in Claude Code" in text
    assert "Stopped after change 3 of 7. Each change either finished or wasn't started." in text
    assert "Run ./install.sh again to finish, or ./install.sh --uninstall to undo these 3." in text
    assert [c["step"] for c in machine.record()["changes"]] == ["kaiusap", "claude-marketplace", "claude-plugin"]
    assert machine.commands()[-1] == ["claude", "plugin", "install", "kaiusap@kaiusap"]


def test_stopping_before_the_first_change_writes_no_record(machine):
    code, text = go(machine, answers=[])  # EOF at the question
    assert not machine.kaiusap_home.exists()


def stop_after_three(machine):
    def interrupt(argv):
        if argv[:3] == ["claude", "plugin", "install"]:
            machine.ctx.stop_requested = True
    machine.on_call = interrupt
    assert go(machine)[0] == 130
    machine.on_call = None
    machine.output.clear()
    machine.calls.clear()


def test_resume_plans_only_what_is_left(machine):
    stop_after_three(machine)
    code, text = go(machine)
    assert code == 0, text
    assert "You stopped an install at 16:17 today after 3 of 7 changes. Picking up from there." in text
    assert "Checking this machine again. Nothing is changed yet." in text
    assert text.count("done earlier") == 3
    assert "4 changes left:" in text
    assert plan_lines(text) == CLAUDE_CODEX_PLAN[3:]
    assert " 4  Add the kaiusap marketplace to Codex" in text
    assert machine.commands()[0] == ["codex", "plugin", "marketplace", "add", str(machine.repo)]
    record = machine.record()
    assert record["finished_at"] and record["planned"] == 7 and len(record["changes"]) == 7


def test_resume_reinstalls_what_was_removed_by_hand(machine):
    stop_after_three(machine)
    machine.claude_json("plugins/installed_plugins.json", lambda d: d["plugins"].pop("kaiusap@kaiusap"))
    code, text = go(machine)
    assert code == 0, text
    assert "Install the Claude Code plugin" in plan_lines(text)
    assert text.count("done earlier") == 2
    assert ["claude", "plugin", "install", "kaiusap@kaiusap"] in machine.commands()


def test_everything_in_place_changes_nothing(machine):
    assert go(machine)[0] == 0
    machine.output.clear()
    machine.calls.clear()
    code, text = go(machine)
    assert code == 0
    assert "is installed, the same as this checkout" in text and "Everything is in place. Nothing to change." in text
    assert machine.commands() == []


def test_a_finished_install_with_kaiusap_removed_by_hand_reinstalls_it(machine):
    assert go(machine)[0] == 0
    machine.kaiusap_bin.unlink()
    machine.installed = None
    machine.output.clear()
    machine.calls.clear()
    code, text = go(machine)
    assert code == 0, text
    assert "was installed from this checkout" in text and "upgrades" not in text
    assert plan_lines(text)[0] == "Install kaiusap into ~/.local/bin"
    assert machine.commands()[0] == ["uv", "tool", "install", "--reinstall", str(machine.repo)]
    assert len(machine.record()["changes"]) > 7  # the earlier run's changes are kept, so uninstall still sees them


def test_a_conflicting_key_is_left_and_listed(machine):
    machine.codex_config.parent.mkdir(parents=True)
    machine.codex_config.write_text('[tui]\nterminal_title = "mine"\n')
    code, text = go(machine)
    assert code == 0, text
    assert "Add thread-id to Codex's terminal title" not in plan_lines(text)
    assert "Codex's terminal title: ~/.codex/config.toml already holds something else there, so I left it." in text
    assert machine.codex_config.read_text().count("mine") == 1


# ----- upgrade --------------------------------------------------------------------------------------------------

def upgrade_machine(tmp_path, store_from=1, store_to=1, monkeypatch=None):
    machine = Machine(tmp_path, version="0.2.0")
    machine.commit = OLD_COMMIT
    machine.versions = {OLD_COMMIT: "0.2.0", COMMIT: "0.3.0"}
    assert go(machine)[0] == 0
    machine.output.clear()
    machine.calls.clear()
    machine.commit = COMMIT
    Machine.make_tree(machine.repo, "0.3.0")
    (machine.repo / "plugins/codex/hooks/hooks.json").write_text('{"hooks": {"changed": true}}\n')
    return machine


def test_upgrade_refreshes_the_codex_plugin_and_restarts(tmp_path):
    machine = upgrade_machine(tmp_path)
    code, text = go(machine)
    assert code == 0, text
    assert "kaiusap 0.2.0 (b7c27e1) is installed. This upgrades it to 0.3.0 (e26fa5c)." in text
    assert plan_lines(text) == ["Upgrade kaiusap to 0.3.0 (e26fa5c)", "Refresh the Codex plugin to 0.3.0 (e26fa5c)",
                                "⚠ Restart the Codex daemon"]
    assert "→ Codex will ask you to approve kaiusap's hooks again: they changed." in text
    assert "No change needed: Claude Code plugin, Codex's terminal title." in text
    assert machine.commands() == [["uv", "tool", "install", "--reinstall", str(machine.repo)],
                                  ["codex", "plugin", "add", "kaiusap@kaiusap"],
                                  ["codex", "app-server", "daemon", "restart"]]
    record = machine.record()
    assert record["version"] == "0.3.0" and record["previous"]["version"] == "0.2.0"
    assert record["previous"]["commit"] == OLD_COMMIT and record["previous"]["store_backup"] is None
    assert len(record["changes"]) == 7 + 3
    assert "Upgraded to 0.3.0." in text and "If something's wrong: ./install.sh --rollback" in text
    assert "you are" not in text
    assert not list(tmp_path.glob("home/.kaiusap.backup-*"))


def test_an_upgrade_names_the_build_open_sessions_keep_running(tmp_path):
    """Two builds can share a version number, so the warning names the commit too."""
    machine = upgrade_machine(tmp_path)
    machine.claude_sessions = [101]
    code, text = go(machine, "--plan")
    assert code == 0, text
    assert "If you leave them open, each keeps running 0.2.0 (b7c27e1) until you restart it." in text


def test_an_upgrade_names_each_build_with_its_commit_time(tmp_path):
    """The commit's time, in local time, beside its hash; a record from before the time was kept shows none,
    and the time this run installs is kept for the next one."""
    from datetime import datetime
    machine = upgrade_machine(tmp_path)
    machine.commit_dates[COMMIT] = "2026-11-16T15:22:00-05:00"
    machine.claude_sessions = [101]
    code, text = go(machine, "--plan")
    assert code == 0, text
    when = datetime.fromisoformat("2026-11-16T15:22:00-05:00").astimezone().strftime("%Y-%m-%d %H:%M")
    assert f"kaiusap 0.2.0 (b7c27e1) is installed. This upgrades it to 0.3.0 (e26fa5c, committed {when})." in text
    assert f"Upgrade kaiusap to 0.3.0 (e26fa5c, committed {when})" in plan_lines(text)
    assert "If you leave them open, each keeps running 0.2.0 (b7c27e1) until you restart it." in text
    machine.claude_sessions = []
    assert go(machine)[0] == 0
    assert machine.record()["commit_date"] == "2026-11-16T15:22:00-05:00"


def test_upgrade_always_refreshes_codex_even_with_unchanged_files(tmp_path):
    machine = upgrade_machine(tmp_path)
    (machine.repo / "plugins/codex/hooks/hooks.json").write_text('{"hooks": {}}\n')
    for cached in (machine.home / ".codex/plugins/cache/kaiusap/kaiusap").iterdir():
        Machine.make_tree(machine.tmp / "same", "0.3.0")
    code, text = go(machine)
    assert "Refresh the Codex plugin to 0.3.0 (e26fa5c)" in plan_lines(text)


class FormatTwo:
    """A store format module, as `kaiusap.storeformat` will give it."""
    STORE_FORMAT = 2

    def __init__(self):
        self.migrated = []

    @staticmethod
    def read_format(root):
        try:
            return json.loads((Path(root) / "store.json").read_text())["format"], None
        except FileNotFoundError:
            return 1, None

    def migrate(self, root, to=2):
        self.migrated.append((root, to))
        (Path(root) / "store.json").write_text(json.dumps({"format": to}))


def test_upgrade_backs_up_and_migrates_the_store_when_its_format_changes(tmp_path, monkeypatch):
    machine = upgrade_machine(tmp_path)
    module = FormatTwo()
    import installer.checks
    monkeypatch.setattr(installer.checks, "storeformat", lambda: module)
    import installer.plan
    monkeypatch.setattr(installer.plan, "storeformat", lambda: module)
    code, text = go(machine)
    assert code == 0, text
    assert plan_lines(text)[:3] == ["Back up your mail and records", "Update the store's format from 1 to 2",
                                    "Upgrade kaiusap to 0.3.0 (e26fa5c)"]
    backup = machine.home / ".kaiusap.backup-261113161700"
    assert backup.is_dir() and (backup / "install.json").exists()
    assert module.migrated == [(machine.kaiusap_home, 2)]
    assert machine.record()["previous"]["store_backup"] == str(backup)
    assert machine.record()["previous"]["store_format"] == 1


def test_upgrade_leaves_the_store_alone_without_a_format_change(tmp_path, monkeypatch):
    machine = upgrade_machine(tmp_path)
    module = FormatTwo()
    module.STORE_FORMAT = 1
    import installer.checks
    import installer.plan
    monkeypatch.setattr(installer.checks, "storeformat", lambda: module)
    monkeypatch.setattr(installer.plan, "storeformat", lambda: module)
    code, text = go(machine)
    assert "Back up your mail" not in text and module.migrated == []


def test_a_store_newer_than_this_kaiusap_stops(tmp_path, monkeypatch):
    machine = upgrade_machine(tmp_path)
    module = FormatTwo()
    module.STORE_FORMAT = 1
    (machine.kaiusap_home / "store.json").write_text(json.dumps({"format": 3}))
    import installer.checks
    monkeypatch.setattr(installer.checks, "storeformat", lambda: module)
    code, text = go(machine)
    assert code == 1 and "format 3, newer than this kaiusap knows (format 1)" in text
    assert machine.commands() == []


# ----- uninstall --------------------------------------------------------------------------------------------------

def test_uninstall_removes_only_what_was_recorded_and_asks_before_deleting_the_store(machine):
    machine.codex_config.parent.mkdir(parents=True)
    machine.codex_config.write_text(dump_toml({"tui": {"terminal_title": ["mine"]}}))
    assert go(machine)[0] == 0
    machine.output.clear()
    machine.calls.clear()
    code, text = go(machine, "--uninstall", answers=["y", "n"])
    assert code == 0, text
    assert "kAIusap uninstaller 0.1.0" in text
    assert plan_lines(text) == ["Remove the Claude Code plugin and its marketplace",
                                "Remove the Codex plugin and its marketplace",
                                'Remove "thread-id" from [tui] terminal_title in ~/.codex/config.toml',
                                "⚠ Restart the Codex daemon", "Uninstall kaiusap from ~/.local/bin",
                                "Ask about your mail in ~/.kaiusap/"]
    assert machine.commands() == [["claude", "plugin", "uninstall", "kaiusap@kaiusap"],
                                  ["claude", "plugin", "marketplace", "remove", "kaiusap"],
                                  ["codex", "plugin", "remove", "kaiusap@kaiusap"],
                                  ["codex", "plugin", "marketplace", "remove", "kaiusap"],
                                  ["codex", "app-server", "daemon", "restart"],
                                  ["uv", "tool", "uninstall", "kaiusap"]]
    assert 'terminal_title = ["mine"]' in machine.codex_config.read_text()
    assert "Delete it? [y/N] " in text and "Kept ~/.kaiusap/" in text
    assert machine.kaiusap_home.is_dir() and not (machine.kaiusap_home / "install.json").exists()
    assert list(machine.kaiusap_home.glob("install.uninstalled-*.json"))
    assert "Uninstalled. One thing only you can do:" in text
    assert "Restart open Claude Code sessions, or run /reload-plugins in each." in text


def test_uninstall_leaves_what_was_there_before_and_what_changed_since(tmp_path):
    machine = Machine(tmp_path, claude="2.1.323", codex=False, opencode="1.19.5")
    machine.install_claude_plugin()  # installed by hand before: never recorded, never removed
    assert go(machine)[0] == 0
    config = machine.home / ".config/opencode/opencode.json"
    data = json.loads(config.read_text())
    data["mcp"]["kaiusap"]["enabled"] = True  # the user changed it since
    config.write_text(json.dumps(data))
    machine.output.clear()
    machine.calls.clear()
    code, text = go(machine, "--uninstall", answers=["y", "y"])
    assert code == 0, text
    assert not any(argv[0] == "claude" and "uninstall" in argv for argv in machine.commands())
    assert "mcp.kaiusap in ~/.config/opencode/opencode.json was changed after install, so I left it." in text
    assert json.loads(config.read_text())["mcp"]["kaiusap"]["enabled"] is True
    assert not (machine.home / ".config/opencode/plugin/kaiusap.ts").exists()
    assert not machine.kaiusap_home.exists() and "Deleted ~/.kaiusap/" in text


def test_uninstall_without_a_record_changes_nothing(machine):
    code, text = go(machine, "--uninstall")
    assert code == 1 and "There's no install record" in text and machine.commands() == []
    # The removal commands are printed in full, pointing at nothing the user can't see.
    for line in ("claude plugin uninstall kaiusap@kaiusap", "codex plugin remove kaiusap@kaiusap",
                 "codex app-server daemon restart", "uv tool uninstall kaiusap", "~/.kaiusap/ holds every message"):
        assert line in text
    assert "docs/" not in text


# ----- rollback ---------------------------------------------------------------------------------------------------

def test_rollback_reinstalls_the_previous_commit_and_refreshes_codex(tmp_path):
    machine = upgrade_machine(tmp_path)
    assert go(machine)[0] == 0
    machine.output.clear()
    machine.calls.clear()
    code, text = go(machine, "--rollback")
    assert code == 0, text
    assert ("kaiusap 0.3.0 (e26fa5c) is installed. Rolling back to 0.2.0 (b7c27e1), the version it replaced at "
            "16:17 today.") in text
    assert plan_lines(text) == ["Reinstall kaiusap 0.2.0", "Refresh the Codex plugin to 0.2.0", "⚠ Restart the Codex daemon"]
    commands = machine.commands()
    worktrees = [argv for argv in commands if argv[3:5] == ["worktree", "add"]]
    assert len(worktrees) == 2 and all(argv[-1] == OLD_COMMIT for argv in worktrees)
    tree = worktrees[0][-2]
    assert ["uv", "tool", "install", "--reinstall", tree] in commands
    assert commands[-1] == ["codex", "app-server", "daemon", "restart"]
    assert machine.installed["commit"] == OLD_COMMIT
    cache = machine.home / ".codex/plugins/cache/kaiusap/kaiusap"
    assert [p.name for p in cache.iterdir()] == ["0.2.0"]
    assert tomllib_source(machine) == str(machine.repo)  # Codex's marketplace points at the checkout again
    record = machine.record()
    assert record["version"] == "0.2.0" and record["previous"] is None and "rollback" not in record
    assert len(record["changes"]) == 7
    assert not list(tmp_path.glob("kaiusap-rollback-*"))
    assert "Rolled back to 0.2.0." in text


def tomllib_source(machine):
    import tomllib
    return tomllib.loads(machine.codex_config.read_text())["marketplaces"]["kaiusap"]["source"]


def test_rollback_puts_the_store_back_naming_mail_it_loses(tmp_path, monkeypatch):
    machine = upgrade_machine(tmp_path)
    module = FormatTwo()
    import installer.checks
    import installer.plan
    monkeypatch.setattr(installer.checks, "storeformat", lambda: module)
    monkeypatch.setattr(installer.plan, "storeformat", lambda: module)
    (machine.kaiusap_home / "messages").mkdir()
    (machine.kaiusap_home / "messages" / "20261113T201000Z-aaaaaa.json").write_text(json.dumps(
        {"created": "2026-11-13T20:10:00Z", "from": {"handle": "a"}, "to": {"handle": "b"}, "subject": "old"}))
    assert go(machine)[0] == 0
    (machine.kaiusap_home / "messages" / "20261113T213100Z-bbbbbb.json").write_text(json.dumps(
        {"created": "2026-11-13T21:31:00Z", "from": {"handle": "reviewer"}, "to": {"handle": "builder"},
         "subject": "schema check"}))
    machine.output.clear()
    machine.calls.clear()
    code, text = go(machine, "--rollback")
    assert code == 0, text
    assert plan_lines(text)[0] == "⚠ Put back your mail and records from before the upgrade"
    assert "1 message since the upgrade isn't in it and will be lost:" in text
    assert 'reviewer → builder   "schema check"' in text
    kept = machine.home / ".kaiusap.rolled-back-261113161700"
    assert (kept / "messages" / "20261113T213100Z-bbbbbb.json").exists()
    assert not (machine.kaiusap_home / "messages" / "20261113T213100Z-bbbbbb.json").exists()
    assert (machine.kaiusap_home / "messages" / "20261113T201000Z-aaaaaa.json").exists()
    assert machine.record()["version"] == "0.2.0"


def test_rollback_undoes_settings_edits_the_upgrade_added(tmp_path):
    machine = upgrade_machine(tmp_path)
    machine.codex_toml(lambda d: d["tui"].update(terminal_title=["activity"]))  # thread-id gone by hand
    assert go(machine)[0] == 0  # the upgrade adds it again
    assert "thread-id" in machine.codex_config.read_text()
    machine.output.clear()
    machine.calls.clear()
    code, text = go(machine, "--rollback")
    assert code == 0, text
    assert 'Remove "thread-id" from [tui] terminal_title in ~/.codex/config.toml' in plan_lines(text)
    assert "thread-id" not in machine.codex_config.read_text()


def test_rollback_without_an_upgrade_changes_nothing(machine):
    assert go(machine)[0] == 0
    machine.calls.clear()
    code, text = go(machine, "--rollback")
    assert code == 1 and "nothing to roll back to" in text and machine.commands() == []


def test_commands_get_the_callers_pythonpath_not_the_checkout(tmp_path):
    """install.sh puts the checkout on PYTHONPATH for the installer only; an installed kaiusap run with it would import
    the checkout's code and report no commit, so commands get the caller's PYTHONPATH back (or none)."""
    from installer.context import Context
    ctx = Context(environ={"PYTHONPATH": "/repo:/repo/src", "KAIUSAP_INSTALLER_PYTHONPATH": "", "PATH": "/bin"})
    assert "PYTHONPATH" not in ctx.environ and "KAIUSAP_INSTALLER_PYTHONPATH" not in ctx.environ
    ctx = Context(environ={"PYTHONPATH": "/repo:/repo/src:/mine", "KAIUSAP_INSTALLER_PYTHONPATH": "/mine", "PATH": "/bin"})
    assert ctx.environ["PYTHONPATH"] == "/mine"


def test_uninstall_removes_a_link_recorded_twice_once(tmp_path):
    """A repair run records the OpenCode link again; uninstall removes it once (found in the Linux run)."""
    machine = Machine(tmp_path, claude=None, codex=False, opencode="1.19.5")
    assert go(machine)[0] == 0
    link = machine.home / ".config/opencode/plugin/kaiusap.ts"
    link.unlink()
    machine.output.clear()
    assert go(machine)[0] == 0 and link.is_symlink()  # the repair run relinks it and records it again
    machine.output.clear()
    code, text = go(machine, "--uninstall", answers=["y", "n"])
    assert code == 0, text
    assert plan_lines(text).count("Remove kaiusap's plugin link from OpenCode") == 1
    assert not link.exists() and not link.is_symlink()


def test_only_the_kinds_of_session_that_were_open_are_listed_to_bring_back(machine):
    machine.claude_sessions = [4242]
    code, text = go(machine, answers=["y", "continue"])
    assert code == 0, text
    assert "Bring open sessions back." in text and "Claude Code: restart it, or run /reload-plugins in it" in text
    assert "Codex: open terminals reconnect" not in text


def test_marketplaces_pointing_at_another_checkout_are_moved_to_this_one(machine, tmp_path):
    """Both marketplaces named another clone of the repo while the install ran from this one; Codex's refresh
    would have copied the other clone's plugin. Each is removed, added from this checkout and its plugin installed
    again, named in the plan."""
    other = tmp_path / "other-clone"
    other.mkdir()
    machine.install_claude_plugin(marketplace=other)
    machine.install_codex_plugin(marketplace=other)
    code, text = go(machine, "--plan")
    assert code == 0, text
    lines = plan_lines(text)
    assert "Point Claude Code's kaiusap marketplace at this checkout" in lines
    assert "Point Codex's kaiusap marketplace at this checkout" in lines
    assert "Refresh the Codex plugin to 0.1.0" not in lines and "Install the Codex plugin" not in lines
    assert f"it reads kaiusap's plugin from {other}" in text
    machine.output.clear()
    machine.calls.clear()
    code, text = go(machine, answers=["y"])
    assert code == 0, text
    commands = machine.commands()
    for provider, add in (("claude", "install"), ("codex", "add")):
        start = commands.index([provider, "plugin", "marketplace", "remove", "kaiusap"])
        assert commands[start + 1] == [provider, "plugin", "marketplace", "add", str(machine.repo)]
        assert commands[start + 2] == [provider, "plugin", add, "kaiusap@kaiusap"]
    machine.output.clear()
    assert "Everything is in place" in go(machine, "--plan")[1]  # pointing here now, so nothing moves again


def test_a_marketplace_already_pointing_here_is_left_alone(machine):
    machine.install_claude_plugin()
    code, text = go(machine, "--plan")
    assert code == 0 and "Point Claude Code's kaiusap marketplace" not in text


# ----- a checkout moved or renamed after kaiusap was installed from it ------------------------------------------

def test_an_opencode_plugin_link_to_another_checkout_is_pointed_here_even_when_it_leads_nowhere(tmp_path):
    """The link named the old checkout's plugins/opencode/kaiusap.ts, which no longer existed; the installer left it
    "in the way" and suggested an ln -s that the dead link would refuse. It's kaiusap's link, so it's moved here."""
    machine = Machine(tmp_path, claude=None, codex=False, opencode="1.19.5")
    link = machine.home / ".config/opencode/plugin/kaiusap.ts"
    link.parent.mkdir(parents=True)
    old = tmp_path / "old-checkout" / "plugins" / "opencode" / "kaiusap.ts"
    link.symlink_to(old)  # the old checkout is gone: the link leads nowhere
    code, text = go(machine, "--plan")
    assert code == 0, text
    assert "Point OpenCode's plugin at this checkout" in plan_lines(text)
    assert f"it reads kaiusap's plugin from {old} now; this points it at" in text
    assert "so I left it" not in text
    machine.output.clear()
    code, text = go(machine)
    assert code == 0, text
    assert link.is_symlink() and Path(os.readlink(link)) == machine.repo / "plugins/opencode/kaiusap.ts"
    change = next(c for c in machine.record()["changes"] if c["step"] == "opencode-link")
    assert change["record"]["replaced"] == str(old)
    machine.output.clear()
    assert "Point OpenCode's plugin" not in go(machine, "--plan")[1]  # pointing here now, so nothing moves again


def test_an_opencode_2_plugin_link_to_another_checkout_is_pointed_here(tmp_path):
    machine = Machine(tmp_path, claude=None, codex=False, opencode="2.2.7")
    link = machine.home / ".config/opencode/plugins/kaiusap"
    link.parent.mkdir(parents=True)
    link.symlink_to(tmp_path / "old-checkout" / "plugins" / "opencode2")
    code, text = go(machine)
    assert code == 0, text
    assert "Point OpenCode's plugin at this checkout" in plan_lines(text)
    assert Path(os.readlink(link)) == machine.repo / "plugins/opencode2"


def test_a_file_that_isnt_kaiusaps_plugin_is_still_left_with_one_note(tmp_path):
    machine = Machine(tmp_path, claude=None, codex=False, opencode="1.19.5")
    plugin = machine.home / ".config/opencode/plugin/kaiusap.ts"
    plugin.parent.mkdir(parents=True)
    plugin.write_text("// someone else's kaiusap.ts\n")
    machine.setup_items = [{"item": "OpenCode plugin", "done": False, "detail": "not this kaiusap's",
                            "command": "mkdir -p x && ln -s y z"}]
    code, text = go(machine)
    assert code == 0, text
    assert "OpenCode's plugin: ~/.config/opencode/plugin/kaiusap.ts isn't kaiusap's plugin, so I left it." in text
    assert "kaiusap setup still reports OpenCode plugin missing" not in text  # the note above covers it
    assert plugin.read_text() == "// someone else's kaiusap.ts\n"


def test_an_mcp_entry_naming_kaiusap_without_its_full_path_is_in_place_with_no_note(tmp_path):
    """`setup` accepts {"command": ["kaiusap", "mcp", "opencode"]}; the installer left it, but also listed it as
    holding "something else"."""
    machine = Machine(tmp_path, claude=None, codex=False, opencode="1.19.5")
    config = machine.home / ".config/opencode/opencode.json"
    config.parent.mkdir(parents=True)
    config.write_text(json.dumps({"mcp": {"kaiusap": {"type": "local", "command": ["kaiusap", "mcp", "opencode"]}}}))
    code, text = go(machine)
    assert code == 0, text
    assert "Add kaiusap's MCP server to OpenCode's config" not in plan_lines(text)
    assert "already holds something else" not in text
    assert json.loads(config.read_text())["mcp"]["kaiusap"]["command"] == ["kaiusap", "mcp", "opencode"]


def test_plan_flag_shows_what_it_will_leave_for_you(machine):
    machine.codex_config.parent.mkdir(parents=True)
    machine.codex_config.write_text('[tui]\nterminal_title = "mine"\n')
    code, text = go(machine, "--plan")
    assert code == 0, text
    assert "I'll leave these for you (they're in the closing list too):" in text
    assert "Codex's terminal title: ~/.codex/config.toml already holds something else there, so I left it." in text
    assert text.index("I'll leave these for you") < text.index("Nothing was changed (--plan).")


def test_a_left_key_is_listed_once_not_again_from_setups_report(machine):
    machine.codex_config.parent.mkdir(parents=True)
    machine.codex_config.write_text('[tui]\nterminal_title = "mine"\n')
    machine.setup_items = [{"item": "thread-id in Codex's [tui] terminal_title", "done": False,
                            "detail": "terminal_title has no thread-id", "command": "add it"}]
    code, text = go(machine)
    assert code == 0, text
    closing = text[text.index("Installed."):]
    assert closing.count("Codex's terminal title") == 1
    assert "kaiusap setup still reports thread-id" not in closing


def test_a_kaiusap_installed_before_the_installer_isnt_told_to_name_its_sessions(machine):
    """No install record (kaiusap was installed by hand), but the store already has handles: "say who it is" is
    noise there."""
    machine.kaiusap_home.mkdir(parents=True, exist_ok=True)
    (machine.kaiusap_home / "handles.jsonl").write_text('{"handle": "api-cld"}\n')
    code, text = go(machine)
    assert code == 0, text
    assert "say who it is" not in text
