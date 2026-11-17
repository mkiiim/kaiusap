"""The plan: every change, in order, made only
from the checks. A piece already in place is not planned; a key holding something else is left alone and goes
to the closing list. The record decides nothing about what is installed; it only says which restarts a stopped
run still owes, since a restart can't be checked afterwards.
"""
from dataclasses import dataclass, field
import importlib
import os
from pathlib import Path
import shutil

from .checks import label, storeformat
from .context import StepFailed
from . import record as records

PLUGIN_ID = "kaiusap@kaiusap"
MARKETPLACE = "kaiusap"
DAEMON_EFFECTS = ["open Codex terminals reconnect by themselves within a couple of minutes;",
                  "a thread with no open terminal waits until you resume it"]
SERVICE_EFFECTS = ['open OpenCode terminals show "Reconnecting" and reattach;',
                   "terminals inside sessions are closed"]
HOOKS_AGAIN = "Codex will ask you to approve kaiusap's hooks again: they changed."
TITLE_DEFAULTS = ("activity", "thread-name", "project-name")
# Steps after which, in a stopped run, a restart is still owed (the restart itself can't be checked).
DAEMON_TRIGGERS = {"kaiusap", "codex-marketplace", "codex-plugin", "codex-title"}
SERVICE_TRIGGERS = {"kaiusap", "opencode-link", "opencode-mcp"}
COMMAND_LIMIT = None  # a change runs to its end (uv builds the hook program)


def edits():
    """`installer.edits`, the format-preserving TOML and JSON editor (built beside this module)."""
    return importlib.import_module("installer.edits")


@dataclass
class Step:
    id: str
    title: str
    done: str
    action: object  # ctx -> (kind, record, lines printed after the done line)
    details: list = field(default_factory=list)
    runs: str | None = None  # an interrupting step's command
    effects: list = field(default_factory=list)  # what it does to open sessions
    notes: list = field(default_factory=list)  # for the user, in the plan
    name: str = ""  # what the piece is called in "No change needed" and "done earlier"
    warn: bool = False  # flagged like an interrupting step, with no command (putting the store back)
    meta: dict = field(default_factory=dict)  # kept on the change's entry in install.json (an edit's file and key)

    @property
    def interrupts(self):
        return bool(self.runs or self.warn)


@dataclass
class Plan:
    steps: list = field(default_factory=list)
    unchanged: list = field(default_factory=list)  # pieces in place, by name
    closing: list = field(default_factory=list)  # [first line, *more lines] for the closing list
    left: set = field(default_factory=set)  # `setup` items a closing note already covers
    hooks_again: bool = False
    present: set = field(default_factory=set)  # step ids whose piece the checks found in place


def commands_action(*argvs, cwd=None):
    def action(ctx):
        for argv in argvs:
            run_or_fail(ctx, argv, cwd=cwd)
        return "command", {"commands": [list(map(str, a)) for a in argvs]}, []
    return action


def run_or_fail(ctx, argv, cwd=None):
    try:
        code, out = ctx.run(argv, COMMAND_LIMIT, cwd=cwd)
    except OSError as exc:
        raise StepFailed(f"{' '.join(map(str, argv))} couldn't be run ({exc})") from None
    if code != 0:
        raise StepFailed(f"{' '.join(map(str, argv))} exited {code}", out)
    return out


def edit_action(spec):
    def action(ctx):
        module = edits()
        try:
            rec = module.apply(spec, ctx.stamp)
        except module.CannotEdit as exc:
            raise StepFailed(f"{ctx.short(spec.file)} can't be changed by key: {exc.instruction}") from None
        backup = rec.get("backup") if isinstance(rec, dict) else None
        return "edit", rec, [f"backup: {ctx.short(backup)}"] if backup else []
    return action


def link_action(link, target):
    def action(ctx):
        link.parent.mkdir(parents=True, exist_ok=True)
        os.symlink(target, link)
        return "link", {"link": str(link), "target": str(target)}, []
    return action


def relink_action(link, target, old):
    """Points a link that names another checkout's plugin at this checkout's (it may lead nowhere now)."""
    def action(ctx):
        os.unlink(link)
        os.symlink(target, link)
        return "link", {"link": str(link), "target": str(target), "replaced": old}, []
    return action


def edit_state(ctx, spec, plan, what, by_hand, item=None):
    """The edit's check: "present", "missing", or None after reporting a conflict or a file it can't change.
    `item` is the `setup` item that checks the same piece: its note is noted in `plan.left`, so the closing list
    doesn't repeat it from `setup`'s report."""
    module = edits()
    try:
        state = module.check(spec)
    except module.CannotEdit as exc:
        reason = str(exc) or f"{ctx.short(spec.file)} can't be changed by key"
        plan.closing.append([f"{what}: {reason}, so I left it. The change, to make by hand:",
                             getattr(exc, "instruction", None) or by_hand])
        plan.left.add(item)
        return None
    if state == "conflict":
        plan.closing.append([f"{what}: {ctx.short(spec.file)} already holds something else there, so I left it.",
                             by_hand])
        plan.left.add(item)
        return None
    return state


def owed(record, triggers, restart):
    """Whether a stopped run made a change in `triggers` with no `restart` after it."""
    owing = False
    for change in records.run_changes(record):
        if change["step"] in triggers:
            owing = True
        elif change["step"] == restart:
            owing = False
    return owing


def files_of(directory):
    out = {}
    for root, _, names in os.walk(directory):
        for name in names:
            path = Path(root) / name
            try:
                out[str(path.relative_to(directory))] = path.read_bytes()
            except OSError:
                pass
    return out


def codex_cache_differs(ctx, source):
    """Whether Codex's copy of the plugin differs from `source`: None when Codex holds no copy
    (Codex's hook approval covers every file in the plugin's folder, so any change asks again)."""
    cache = ctx.home / ".codex" / "plugins" / "cache" / MARKETPLACE / "kaiusap"
    try:
        versions = [p for p in cache.iterdir() if p.is_dir()]
    except OSError:
        return None
    if not versions:
        return None
    wanted = files_of(source)
    return all(files_of(v) != wanted for v in versions)


def opencode_files(directory):
    return [directory / name for name in ("opencode.json", "opencode.jsonc") if (directory / name).exists()]


def mcp_value(ctx, provider):
    if provider == 1:
        return {"type": "local", "command": [str(ctx.kaiusap_bin), "mcp", "opencode"]}
    return {"type": "local", "command": [str(ctx.kaiusap_bin), "mcp", "opencode2"], "codemode": False}


def mcp_path(provider):
    return ("mcp", "kaiusap") if provider == 1 else ("mcp", "servers", "kaiusap")


def title_spec(ctx):
    return edits().TomlListItem(file=str(ctx.home / ".codex" / "config.toml"), table=("tui",), key="terminal_title",
                                item="thread-id", aliases=("session-id",), default=TITLE_DEFAULTS)


# ----- install and upgrade ------------------------------------------------------------------------------------

def build(ctx, survey, opts, mode, stopped=None):
    """The install or upgrade plan: what the checks found missing, in order."""
    plan = Plan()
    repo = ctx.repo
    version = survey.checkout["version"]
    named = label(survey.checkout)  # version and commit: two builds can share a version number
    store_steps(ctx, survey, plan, version)
    if survey.current:
        plan.unchanged.append("kaiusap")
        plan.present.add("kaiusap")
    else:
        title = (f"Upgrade kaiusap to {named}" if mode == "upgrade" else
                 f"Install kaiusap into {ctx.short(ctx.bin_dir)}")
        plan.steps.append(Step("kaiusap", title, f"kaiusap {named} installed",
                               commands_action(["uv", "tool", "install", "--reinstall", repo]), name="kaiusap"))
    reinstalling = any(s.id == "kaiusap" for s in plan.steps) or owed(stopped, {"kaiusap"}, "codex-plugin")
    if survey.claude:
        claude_steps(ctx, survey, plan)
    if survey.codex:
        codex_steps(ctx, survey, plan, named, reinstalling)
    if survey.opencode:
        opencode_steps(ctx, survey, plan)
    restart_steps(ctx, survey, plan, stopped)
    if survey.tmux:
        plan.closing.append(["Turn on pane titles in tmux: add the first line to ~/.tmux.conf, then run the second.",
                             *survey.tmux["command"].splitlines()])
    return plan


def store_steps(ctx, survey, plan, version):
    """A store in an older format is copied first, then updated."""
    if not survey.store:
        return
    on_disk, current, _ = survey.store
    if on_disk >= current:
        return
    backup = ctx.kaiusap_home.with_name(f"{ctx.kaiusap_home.name}.backup-{ctx.stamp}")

    def back_up(ctx):
        shutil.copytree(ctx.kaiusap_home, backup, symlinks=True)
        return "store", {"backup": str(backup)}, []

    def migrate(ctx):
        storeformat().migrate(ctx.kaiusap_home, to=current)
        return "store", {"from": on_disk, "to": current}, []

    plan.steps.append(Step("store-backup", "Back up your mail and records", f"backed up to {ctx.short(backup)}",
                           back_up, [f"{ctx.short(ctx.kaiusap_home)} → {ctx.short(backup)}"], name="store backup"))
    plan.steps.append(Step("store-migrate", f"Update the store's format from {on_disk} to {current}",
                           f"store updated to format {current}", migrate,
                           [f"{version} reads only format {current}; you can roll back with the backup"],
                           name="store format"))


# ----- where a registered marketplace points ---------------------------------------------------------------------

def same_folder(source, repo):
    try:
        return os.path.realpath(source) == os.path.realpath(repo)
    except (OSError, TypeError, ValueError):
        return False


PLUGIN_LINK_ENDS = {1: ("plugins", "opencode", "kaiusap.ts"), 2: ("plugins", "opencode2")}


def kaiusap_plugin_link(target, provider):
    """Whether a link's target is some checkout's kaiusap OpenCode plugin, by its path's end, whether it's there or
    not: `<checkout>/plugins/opencode/kaiusap.ts` for 1.x, `<checkout>/plugins/opencode2` for 2."""
    ends = PLUGIN_LINK_ENDS[provider]
    return Path(str(target).rstrip("/")).parts[-len(ends):] == ends


def claude_marketplace_source(ctx):
    """Where Claude Code's `kaiusap` marketplace points: a folder path, another source's text, or None when none is
    registered (`known_marketplaces.json`, as `claude plugin marketplace add` writes it)."""
    from kaiusap.setup import read_json
    claude_dir = Path(ctx.environ.get("CLAUDE_CONFIG_DIR") or ctx.home / ".claude")
    plugins_dir = Path(ctx.environ.get("CLAUDE_CODE_PLUGIN_CACHE_DIR") or claude_dir / "plugins")
    known = read_json(plugins_dir / "known_marketplaces.json")
    entry = known.get(MARKETPLACE) if isinstance(known, dict) else None
    source = entry.get("source") if isinstance(entry, dict) else None
    if not isinstance(source, dict):
        return None
    if source.get("source") == "directory" and isinstance(source.get("path"), str):
        return source["path"]
    return " ".join(str(v) for k, v in source.items() if k != "source") or str(source.get("source"))


def codex_marketplace_source(ctx):
    """Where Codex's `kaiusap` marketplace points (`[marketplaces.kaiusap]` in config.toml), or None."""
    from kaiusap.setup import read_toml
    config = read_toml(ctx.home / ".codex" / "config.toml") or {}
    entry = (config.get("marketplaces") or {}).get(MARKETPLACE)
    source = entry.get("source") if isinstance(entry, dict) else None
    return source if isinstance(source, str) else None


def elsewhere(ctx, source):
    """A registered marketplace that points somewhere other than the checkout being installed from."""
    return source is not None and not same_folder(source, ctx.repo)


def move_note(ctx, source):
    shown = ctx.short(Path(source)) if os.path.isabs(str(source)) else source
    return f"it reads kaiusap's plugin from {shown} now; this points it at {ctx.short(ctx.repo)}"


def claude_steps(ctx, survey, plan):
    from kaiusap.adapters.claude import ClaudeAdapter
    item = ClaudeAdapter.setup_plugin(survey.claude["setup"])
    source = claude_marketplace_source(ctx)
    if elsewhere(ctx, source):
        # No command changes a marketplace's source, so it is removed and added again, and the plugin installed.
        plan.steps.append(Step("claude-marketplace", "Point Claude Code's kaiusap marketplace at this checkout",
                               "Claude Code's kaiusap marketplace moved",
                               commands_action(["claude", "plugin", "marketplace", "remove", MARKETPLACE],
                                               ["claude", "plugin", "marketplace", "add", ctx.repo],
                                               ["claude", "plugin", "install", PLUGIN_ID]),
                               details=[move_note(ctx, source)], name="kaiusap marketplace in Claude Code"))
        plan.present.add("claude-plugin")
        return
    if item["done"]:
        plan.unchanged.append("Claude Code plugin")
        plan.present.update({"claude-marketplace", "claude-plugin"})
        return
    lines = item["command"].splitlines()
    if any("marketplace add" in line for line in lines):
        plan.steps.append(Step("claude-marketplace", "Add the kaiusap marketplace to Claude Code",
                               "marketplace added to Claude Code",
                               commands_action(["claude", "plugin", "marketplace", "add", ctx.repo]),
                               name="kaiusap marketplace in Claude Code"))
    else:
        plan.present.add("claude-marketplace")
    if any("plugin install" in line for line in lines):
        plan.steps.append(Step("claude-plugin", "Install the Claude Code plugin", f"{PLUGIN_ID} installed in Claude Code",
                               commands_action(["claude", "plugin", "install", PLUGIN_ID]),
                               name=f"{PLUGIN_ID} in Claude Code"))
    elif any("plugin enable" in line for line in lines):
        plan.present.add("claude-plugin")
        plan.steps.append(Step("claude-enable", "Enable the Claude Code plugin", f"{PLUGIN_ID} enabled in Claude Code",
                               commands_action(["claude", "plugin", "enable", PLUGIN_ID]),
                               name=f"{PLUGIN_ID} enabled in Claude Code"))


def codex_steps(ctx, survey, plan, named, reinstalling):
    from kaiusap.adapters.codex import CodexAdapter
    from kaiusap.setup import read_toml
    codex_home = ctx.home / ".codex"
    setup = survey.codex["setup"]
    item = CodexAdapter.setup_plugin(setup, codex_home, read_toml(codex_home / "config.toml"))
    lines = (item["command"] or "").splitlines() if not item["done"] else []
    differs = codex_cache_differs(ctx, ctx.repo / "plugins" / "codex")
    notes = [HOOKS_AGAIN] if differs else []
    moved = False
    source = codex_marketplace_source(ctx)
    if elsewhere(ctx, source):
        # As for Claude: removed, added from this checkout, and the plugin added again from it.
        plan.steps.append(Step("codex-marketplace", "Point Codex's kaiusap marketplace at this checkout",
                               "Codex's kaiusap marketplace moved",
                               commands_action(["codex", "plugin", "marketplace", "remove", MARKETPLACE],
                                               ["codex", "plugin", "marketplace", "add", ctx.repo],
                                               ["codex", "plugin", "add", PLUGIN_ID]),
                               details=[move_note(ctx, source)], notes=notes, name="kaiusap marketplace in Codex"))
        moved = True
    elif any("marketplace add" in line for line in lines):
        plan.steps.append(Step("codex-marketplace", "Add the kaiusap marketplace to Codex", "marketplace added to Codex",
                               commands_action(["codex", "plugin", "marketplace", "add", ctx.repo]),
                               name="kaiusap marketplace in Codex"))
    else:
        plan.present.add("codex-marketplace")
    if moved:
        plan.present.add("codex-plugin")  # added again by the move
    elif any("plugin add" in line for line in lines):
        plan.steps.append(Step("codex-plugin", "Install the Codex plugin", f"{PLUGIN_ID} installed in Codex",
                               commands_action(["codex", "plugin", "add", PLUGIN_ID]), notes=notes,
                               name=f"{PLUGIN_ID} in Codex"))
    elif reinstalling or differs:
        # Codex keeps its own copy and doesn't refresh it by itself.
        plan.present.add("codex-plugin")
        plan.steps.append(Step("codex-plugin", f"Refresh the Codex plugin to {named}",
                               f"Codex plugin refreshed to {named}",
                               commands_action(["codex", "plugin", "add", PLUGIN_ID]), notes=notes,
                               name="Codex plugin"))
    else:
        plan.present.add("codex-plugin")
        plan.unchanged.append("Codex plugin")
    plan.hooks_again = bool(differs)
    if not item["done"] and "is disabled" in (item["detail"] or ""):
        # `enabled = false` is the user's own setting: printed, not edited.
        plan.closing.append([f"Enable the Codex plugin: in {ctx.short(codex_home / 'config.toml')} set",
                             f'[plugins."{PLUGIN_ID}"]', "enabled = true"])
    title = CodexAdapter.setup_title(setup, codex_home, read_toml(codex_home / "config.toml"))
    spec = title_spec(ctx)
    # A title `setup` already accepts is in place, whatever the edit's own check makes of it: no note.
    state = "present" if title["done"] else edit_state(
        ctx, spec, plan, "Codex's terminal title",
        f"add under [tui] in {ctx.short(spec.file)}: {title['command'] or ''}".rstrip(": "), title["item"])
    if state == "missing":
        plan.steps.append(Step("codex-title", "Add thread-id to Codex's terminal title", "terminal_title updated",
                               edit_action(spec),
                               [f"in {ctx.short(spec.file)}, under [tui]; the file is backed up first"],
                               name="Codex's terminal title", meta={"file": spec.file, "key": "terminal_title"}))
    elif state is not None:
        plan.present.add("codex-title")
        plan.unchanged.append("Codex's terminal title")


def opencode_steps(ctx, survey, plan):
    found = survey.opencode
    provider, directory, setup, adapter = found["provider"], found["dir"], found["setup"], found["adapter"]
    # The plugin: linked into the config directory, as `setup`'s command does.
    item = adapter.setup_plugin(setup, directory)
    if provider == 1:
        link, target = directory / "plugin" / "kaiusap.ts", ctx.repo / "plugins" / "opencode" / "kaiusap.ts"
    else:
        link, target = directory / "plugins" / "kaiusap", ctx.repo / "plugins" / "opencode2"
    old = os.readlink(link) if os.path.islink(link) else None
    if old is not None and kaiusap_plugin_link(old, provider) and not same_folder(old, target):
        # A link to another checkout's plugin, there or not (a moved or renamed checkout): pointed here, as a
        # marketplace registered elsewhere is.
        plan.steps.append(Step("opencode-link", "Point OpenCode's plugin at this checkout", "plugin link moved",
                               relink_action(link, target, old), [move_note(ctx, old)], name="OpenCode plugin"))
    elif item["done"]:
        plan.present.add("opencode-link")
        plan.unchanged.append("OpenCode plugin")
    elif os.path.lexists(link) or "isn't this kaiusap's" in (item["detail"] or ""):
        plan.closing.append([f"OpenCode's plugin: {ctx.short(link)} isn't kaiusap's plugin, so I left it. "
                             "Move it aside and run ./install.sh again, or link it yourself:", item["command"]])
        plan.left.add(item["item"])
    else:
        plan.steps.append(Step("opencode-link", "Link kaiusap's plugin into OpenCode", "plugin linked",
                               link_action(link, target), [ctx.short(link)], name="OpenCode plugin"))
    # The MCP entry, naming the installed program by its full path.
    key = ".".join(mcp_path(provider))
    files = opencode_files(directory)
    module = edits()
    specs = [module.JsonKey(file=str(f), path=mcp_path(provider), value=mcp_value(ctx, provider))
             for f in (files or [directory / "opencode.json"])]
    by_hand = f"add under \"mcp\" in {ctx.short(specs[0].file)}: {adapter_entry(ctx, provider)}"
    mcp_item = adapter.setup_mcp(setup, directory)
    # An entry `setup` accepts (by name: `kaiusap`, not the full path) is in place too, and gets no note.
    states = [] if mcp_item["done"] else [
        edit_state(ctx, spec, plan, f"kaiusap's MCP server in OpenCode ({key})", by_hand, mcp_item["item"])
        for spec in specs]
    if "present" in states or mcp_item["done"]:
        plan.present.add("opencode-mcp")
        plan.unchanged.append("kaiusap's MCP server in OpenCode's config")
    elif states[0] == "missing":
        where = f"as {key}" if provider == 1 else "under mcp.servers"
        plan.steps.append(Step("opencode-mcp", "Add kaiusap's MCP server to OpenCode's config", f"{key} added",
                               edit_action(specs[0]),
                               [f"in {ctx.short(specs[0].file)}, {where}; backed up first"],
                               name="kaiusap's MCP server in OpenCode's config",
                               meta={"file": specs[0].file, "key": key}))
    # OpenCode 2 under another command: `sessions` prints commands that open it.
    given = found.get("given_command")
    if provider == 2 and given and given != "opencode":
        spec = module.JsonKey(file=str(ctx.kaiusap_home / "settings.json"), path=("opencode2_command",), value=given)
        state = edit_state(ctx, spec, plan, "kaiusap's opencode2_command setting",
                           f'set "opencode2_command": "{given}" in {ctx.short(spec.file)}')
        if state == "missing":
            plan.steps.append(Step("opencode2-command", f"Set opencode2_command to {given} in kaiusap's settings",
                                   "opencode2_command set", edit_action(spec),
                                   [f"in {ctx.short(spec.file)}, so kaiusap sessions prints commands that open it"],
                                   name="opencode2_command", meta={"file": spec.file, "key": "opencode2_command"}))
        elif state == "present":
            plan.present.add("opencode2-command")
            plan.unchanged.append("opencode2_command")


def adapter_entry(ctx, provider):
    import json
    value = json.dumps(mcp_value(ctx, provider))
    return f'"kaiusap": {value}' if provider == 1 else f'"servers": {{"kaiusap": {value}}}'


def restart_steps(ctx, survey, plan, stopped):
    planned = {s.id for s in plan.steps}
    if survey.codex:
        if not survey.codex["daemon"]:
            plan.steps.append(Step("codex-daemon", "Start the Codex daemon", "Codex daemon started",
                                   commands_action(["codex", "app-server", "daemon", "start"]),
                                   [f"runs: codex app-server daemon start"], name="Codex daemon"))
        elif planned & DAEMON_TRIGGERS or owed(stopped, DAEMON_TRIGGERS, "codex-daemon"):
            plan.steps.append(Step("codex-daemon", "Restart the Codex daemon", "Codex daemon restarted",
                                   commands_action(["codex", "app-server", "daemon", "restart"]),
                                   runs="codex app-server daemon restart", effects=DAEMON_EFFECTS,
                                   name="Codex daemon"))
    if survey.opencode and survey.opencode["provider"] == 2:
        if planned & SERVICE_TRIGGERS or owed(stopped, SERVICE_TRIGGERS, "opencode-service"):
            command = survey.opencode["command"]
            plan.steps.append(Step("opencode-service", "Restart OpenCode's service", "OpenCode's service restarted",
                                   commands_action([command, "service", "restart"]),
                                   runs=f"{command} service restart", effects=SERVICE_EFFECTS,
                                   name="OpenCode's service"))


# ----- uninstall ----------------------------------------------------------------------------------------------

EDIT_UNDO = {
    "codex-title": ('Remove "thread-id" from [tui] terminal_title in {file}', "terminal_title",
                    '"thread-id" removed from terminal_title; the rest of the list kept'),
    "opencode-mcp": ("Remove {key} from {file}", "{key}", "{key} removed"),
    "opencode2-command": ("Remove opencode2_command from {file}", "opencode2_command", "opencode2_command removed"),
}


def uninstall(ctx, survey, rec):
    """What the record says the installer added, still in place, removed in the mock's order."""
    plan = Plan()
    live = records.live_changes(rec)
    by_step = {}
    for change in live:
        by_step.setdefault(change["step"], []).append(change)
    claude_removal(ctx, survey, plan, by_step)
    codex_removal(ctx, survey, plan, by_step)
    for step_id in ("codex-title", "opencode-mcp", "opencode2-command"):
        for change in by_step.get(step_id, []):
            plan.steps.append(undo_edit_step(ctx, change))
    links = set()  # a repair run records the same link again: remove it once
    for change in by_step.get("opencode-link", []):
        link, target = Path(change["record"]["link"]), change["record"]["target"]
        if (link, target) in links:
            continue
        links.add((link, target))
        if os.path.islink(link) and os.readlink(link) == target:
            plan.steps.append(Step("undo-opencode-link", "Remove kaiusap's plugin link from OpenCode",
                                   "OpenCode plugin link removed", unlink_action(link), [ctx.short(link)]))
    removing_codex = any(s.id == "undo-codex" for s in plan.steps)
    if survey.codex and survey.codex["daemon"] and (removing_codex or "kaiusap" in by_step):
        plan.steps.append(Step("codex-daemon", "Restart the Codex daemon", "Codex daemon restarted",
                               commands_action(["codex", "app-server", "daemon", "restart"]),
                               runs="codex app-server daemon restart",
                               effects=["stops kaiusap's server; open Codex terminals reconnect within a couple of "
                                        "minutes"]))
    if "kaiusap" in by_step and ctx.kaiusap_bin.exists():
        plan.steps.append(Step("undo-kaiusap", f"Uninstall kaiusap from {ctx.short(ctx.bin_dir)}", "kaiusap uninstalled",
                               commands_action(["uv", "tool", "uninstall", "kaiusap"])))
    return plan


def claude_removal(ctx, survey, plan, by_step):
    recorded = {s for s in ("claude-plugin", "claude-marketplace", "claude-enable") if s in by_step}
    if not recorded or not survey.claude:
        return
    from kaiusap.adapters.claude import ClaudeAdapter
    item = ClaudeAdapter.setup_plugin(survey.claude["setup"])
    lines = (item["command"] or "").splitlines() if not item["done"] else []
    installed = not any("plugin install" in line for line in lines)
    known = not any("marketplace add" in line for line in lines)
    argvs = []
    if "claude-plugin" in recorded and installed:
        argvs.append(["claude", "plugin", "uninstall", PLUGIN_ID])
    elif "claude-enable" in recorded and installed:
        argvs.append(["claude", "plugin", "disable", PLUGIN_ID])
    if "claude-marketplace" in recorded and known:
        argvs.append(["claude", "plugin", "marketplace", "remove", MARKETPLACE])
    if argvs:
        both = len(argvs) == 2 and argvs[-1][2] == "marketplace"
        title = ("Remove the Claude Code plugin and its marketplace" if both else
                 "Remove the kaiusap marketplace from Claude Code" if argvs[0][2] == "marketplace" else
                 "Disable the Claude Code plugin" if argvs[0][2] == "disable" else "Remove the Claude Code plugin")
        done = ("Claude Code plugin and marketplace removed" if both else
                "marketplace removed from Claude Code" if argvs[0][2] == "marketplace" else
                "Claude Code plugin disabled" if argvs[0][2] == "disable" else "Claude Code plugin removed")
        plan.steps.append(Step("undo-claude", title, done, commands_action(*argvs)))


def codex_removal(ctx, survey, plan, by_step):
    recorded = {s for s in ("codex-plugin", "codex-marketplace") if s in by_step}
    if not recorded or not survey.codex:
        return
    from kaiusap.adapters.codex import CodexAdapter
    from kaiusap.setup import read_toml
    codex_home = ctx.home / ".codex"
    item = CodexAdapter.setup_plugin(survey.codex["setup"], codex_home, read_toml(codex_home / "config.toml"))
    lines = (item["command"] or "").splitlines() if not item["done"] else []
    installed = not any("plugin add" in line for line in lines)
    known = not any("marketplace add" in line for line in lines)
    argvs = []
    # `codex plugin marketplace remove` leaves plugins alone, so the plugin goes first.
    if "codex-plugin" in recorded and installed:
        argvs.append(["codex", "plugin", "remove", PLUGIN_ID])
    if "codex-marketplace" in recorded and known:
        argvs.append(["codex", "plugin", "marketplace", "remove", MARKETPLACE])
    if argvs:
        both = len(argvs) == 2
        title = ("Remove the Codex plugin and its marketplace" if both else
                 "Remove the kaiusap marketplace from Codex" if argvs[0][2] == "marketplace" else "Remove the Codex plugin")
        done = ("Codex plugin and marketplace removed" if both else
                "marketplace removed from Codex" if argvs[0][2] == "marketplace" else "Codex plugin removed")
        plan.steps.append(Step("undo-codex", title, done, commands_action(*argvs)))


def undo_edit_step(ctx, change):
    """The uninstaller's rule: removed only while the value is still the one written."""
    rec = change["record"]
    path = change.get("file") or ""
    file, key = ctx.short(path), change.get("key") or "the entry"
    title, key_name, done = (part.format(file=file, key=key) for part in EDIT_UNDO[change["step"]])

    def action(ctx):
        state = edits().undo(rec, ctx.stamp)
        backup = Path(f"{path}.kaiusap-backup-{ctx.stamp}")
        if state == "changed":
            return "edit", {"undo": state}, [f"! {key_name} in {file} was changed after install, so I left it."]
        if state == "absent":
            return "edit", {"undo": state}, ["it was already gone"]
        return "edit", {"undo": state}, [f"backup: {ctx.short(backup)}"] if path and backup.exists() else []

    return Step(f"undo-{change['step']}", title, done, action, ["the file is backed up first; earlier backups are kept"])


def unlink_action(link):
    def action(ctx):
        try:
            os.unlink(link)
        except FileNotFoundError:  # already gone: nothing left to remove
            pass
        return "link", {"removed": str(link)}, []
    return action
