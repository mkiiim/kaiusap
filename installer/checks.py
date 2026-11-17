"""Check everything first, change nothing. One pass, every problem in one list.

The provider checks are kaiusap's own `setup` items, called from the checkout (`kaiusap.setup.Setup` and each
adapter's `setup_*` functions), with the installer's runner and environment: the installer and `setup` look at
the same files and run the same probes.
"""
from dataclasses import dataclass, field
import json
import re
import subprocess
import tomllib

from .context import Stop

MIN_PYTHON = (3, 12)
PYTHON_FIX = "install Python 3.12 or later (https://www.python.org/downloads/, or: uv python install 3.12)"
UV_FIX = "install uv: curl -LsSf https://astral.sh/uv/install.sh | sh, then open a new terminal"
RUSTC_FIX = "install Rust from https://rustup.rs, then open a new terminal"
RUSTUP_FIX = "rustup default stable (or install Rust from https://rustup.rs), then open a new terminal"
VERSION_LIMIT = 10
GIT_LIMIT = 10
# The paths the package is built from, as setup.py's build record reads them for `version`.
BUILT_FROM = ("src", "native", "scripts", "setup.py", "pyproject.toml", "MANIFEST.in")


@dataclass
class Line:
    mark: str  # "ok", "bad", "skip", "warn"
    label: str
    detail: str = ""
    notes: list = field(default_factory=list)
    fix: str | None = None

    def render(self):
        mark = {"ok": "✓", "bad": "✗", "skip": "–", "warn": "!"}[self.mark]
        head = f"  {mark} {self.label:<22} {self.detail}".rstrip() if self.detail else f"  {mark} {self.label}"
        lines = [head]
        lines.extend(f"       {note}" for note in self.notes)
        if self.fix:
            fix = self.fix.splitlines()
            lines.append(f"       fix: {fix[0]}")
            lines.extend(f"            {more}" for more in fix[1:])
        return lines


def make_setup(ctx, options=None, repo=None):
    """kaiusap's own `setup` context, over the installer's runner, environment and home (read-only)."""
    from kaiusap.setup import Setup
    from kaiusap.store import Store
    return Setup(Store(ctx.kaiusap_home), ctx.environ, run=ctx.setup_runner, home=ctx.home,
                 repo=str(repo or ctx.repo), options={k: v for k, v in (options or {}).items() if v is not None})


def checkout_build(ctx, repo=None):
    """{version, commit, commit_date, dirty} of the checkout the installer runs from: pyproject's version, git's
    commit and its committer date."""
    repo = repo or ctx.repo
    try:
        with open(repo / "pyproject.toml", "rb") as stream:
            version = tomllib.load(stream)["project"]["version"]
    except (OSError, KeyError, ValueError):
        version = "unknown"
    commit = dirty = date = None
    if ctx.which("git") and (repo / ".git").exists():
        try:
            code, out = ctx.run(["git", "-C", repo, "rev-parse", "HEAD"], GIT_LIMIT)
            commit = out.strip() if code == 0 and out.strip() else None
            if commit:
                code, out = ctx.run(["git", "-C", repo, "status", "--porcelain", "--untracked-files=all", "--",
                                     *BUILT_FROM], GIT_LIMIT)
                dirty = bool(out.strip()) if code == 0 else None
                code, out = ctx.run(["git", "-C", repo, "log", "-1", "--format=%cI", commit], GIT_LIMIT)
                date = out.strip() if code == 0 and out.strip() else None
        except (OSError, ValueError, subprocess.TimeoutExpired):
            commit = dirty = date = None
    return {"version": version, "commit": commit, "commit_date": date, "dirty": dirty}


def installed_build(ctx):
    """{version, commit, commit_date, dirty} of the installed `kaiusap` (`kaiusap version --json`), or None when
    there is none."""
    path = ctx.kaiusap_bin
    if not path.exists():
        return None
    try:
        code, out = ctx.run([path, "version", "--json"], VERSION_LIMIT)
        data = json.loads(out) if code == 0 else None
    except (OSError, ValueError, subprocess.TimeoutExpired):
        data = None
    if not isinstance(data, dict) or not isinstance(data.get("version"), str):
        return {"version": "unknown", "commit": None, "commit_date": None, "dirty": None}
    return {"version": data["version"], "commit": data.get("commit"), "commit_date": data.get("commit_date"),
            "dirty": data.get("dirty")}


def same_build(a, b):
    """Whether two builds are the same: version and commit (a short commit matches its long form) and dirtiness.
    Two dirty builds of one commit can't be told apart, so they count as the same."""
    if not a or not b or a.get("version") != b.get("version"):
        return False
    ca, cb = a.get("commit"), b.get("commit")
    if ca and cb:
        if not (ca.startswith(cb) or cb.startswith(ca)):
            return False
    elif ca or cb:
        return False
    return bool(a.get("dirty")) == bool(b.get("dirty"))


def label(build):
    """`0.2.0 (b7c27e1, committed 2026-11-16 15:22)`, as the installer names a build: the commit's time in local
    time, left out when the build doesn't carry it (a record written before it was kept)."""
    from datetime import datetime
    if not build:
        return "unknown"
    commit = build.get("commit")
    if not commit:
        return build.get("version") or "unknown"
    try:
        when = ", committed " + datetime.fromisoformat(build["commit_date"]).astimezone().strftime("%Y-%m-%d %H:%M")
    except (KeyError, TypeError, ValueError):
        when = ""
    return f"{build.get('version')} ({commit[:7]}{'+dirty' if build.get('dirty') else ''}{when})"


def storeformat():
    """The store-format module (`kaiusap.storeformat`, or `kaiusap.store` once it carries STORE_FORMAT), or None
    while it isn't built: then every store counts as format 1 and nothing is migrated."""
    try:
        from kaiusap import storeformat as module
        return module
    except ImportError:
        pass
    try:
        from kaiusap import store as module
        return module if hasattr(module, "STORE_FORMAT") and hasattr(module, "read_format") else None
    except ImportError:
        return None


def store_format(ctx):
    """(format on disk, format this kaiusap writes, written_by), or None when there is no store yet."""
    if not ctx.kaiusap_home.exists():
        return None
    module = storeformat()
    if module is None:
        return 1, 1, None
    on_disk, written_by = module.read_format(ctx.kaiusap_home)
    return on_disk, module.STORE_FORMAT, written_by


class Survey:
    """Everything the plan is made from: one pass over the machine, read-only."""

    def __init__(self, ctx, opts, requirements=True):
        self.ctx, self.opts = ctx, opts
        self.lines = []
        self.problems = 0
        self.claude = self.codex = self.opencode = None
        self.tmux = None  # the pane-titles item when it isn't done
        self.checkout = checkout_build(ctx)
        self.installed = installed_build(ctx)
        self.current = same_build(self.installed, self.checkout)
        self.store = None
        if requirements:
            self.check_requirements()
        self.check_claude()
        self.check_codex()
        self.check_opencode()
        self.check_tmux()
        if requirements:
            self.check_store()

    def add(self, line):
        self.lines.append(line)
        if line.mark == "bad":
            self.problems += 1

    @property
    def providers(self):
        return [name for name in ("claude", "codex", "opencode") if getattr(self, name)]

    # ----- requirements ---------------------------------------------------------------------------------

    def check_requirements(self):
        ctx = self.ctx
        version = ".".join(map(str, ctx.python_version[:2]))
        if ctx.python_version[:2] >= MIN_PYTHON:
            self.add(Line("ok", f"Python {version}", "needs 3.12 or later"))
        else:
            self.add(Line("bad", f"Python {version}", "needs 3.12 or later", fix=PYTHON_FIX))
        if ctx.which("uv"):
            self.add(Line("ok", "uv", "installs kaiusap"))
        else:
            self.add(Line("bad", "uv", "not found", ["uv installs kaiusap."], UV_FIX))
        if not ctx.which("rustc"):
            self.add(Line("bad", "rustc", "not found", ["kaiusap's hook program is built with Rust."], RUSTC_FIX))
        elif self.rustc_runs():
            self.add(Line("ok", "rustc", "builds kaiusap's hook program"))
        else:
            # Found but unusable, e.g. rustup with no default toolchain: the build would fail at the first change.
            self.add(Line("bad", "rustc", "found, but rustc --version fails",
                          ["kaiusap's hook program is built with Rust."], RUSTUP_FIX))

    def rustc_runs(self):
        try:
            code, _ = self.ctx.run(["rustc", "--version"], VERSION_LIMIT)
        except Exception:  # a hung or unstartable rustc counts as not working
            return False
        return code == 0

    # ----- providers, by their command on the PATH --------------------------------------------------------

    def check_claude(self):
        if not self.ctx.which("claude"):
            self.add(Line("skip", "Claude Code", "not found, skipped"))
            return
        from kaiusap.adapters.claude import ClaudeAdapter
        setup = make_setup(self.ctx)
        item = ClaudeAdapter.setup_version(setup)
        self.claude = {"setup": setup}
        found = re.match(r"(\d+\.\d+\.\d+)", item["detail"] or "")
        name = f"Claude Code {found[1]}" if found else "Claude Code"
        if item["done"]:
            self.add(Line("ok", name, "needs 2.1.224 or later"))
        else:
            self.add(Line("bad", name, "needs 2.1.224 or later" if found else item["detail"], fix=item["command"]))

    def check_codex(self):
        if not self.ctx.which("codex"):
            self.add(Line("skip", "Codex", "not found, skipped"))
            return
        from kaiusap.adapters.daemon import DaemonError, Deadline, find_daemon
        try:
            socket, version = find_daemon(self.ctx.setup_runner, Deadline(), self.ctx.environ)
            daemon = {"socket": socket, "version": version}
        except DaemonError:
            daemon = None
        self.codex = {"setup": make_setup(self.ctx), "daemon": daemon}
        if daemon:
            self.add(Line("ok", f"Codex, app-server {daemon['version'] or 'unknown version'}, daemon running"))
        else:
            self.add(Line("ok", "Codex, daemon not running; the install starts it"))

    def check_opencode(self):
        """OpenCode by its command and config directory as `setup` finds them; its version picks kaiusap's OpenCode
        1 or 2 plugin, unless `--opencode` chooses."""
        from kaiusap.adapters import opencode_choice
        opts, ctx = self.opts, self.ctx
        command = opts.opencode_command or "opencode"
        flags = {"opencode_command": opts.opencode_command, "opencode_config": opts.opencode_config}
        probe_setup = make_setup(ctx, flags)
        asked = bool(opts.opencode or opts.opencode_command or opts.opencode_config)
        if not ctx.which(opencode_choice.command(probe_setup)):
            if asked:
                self.add(Line("bad", "OpenCode", f"{command} isn't on the PATH",
                              fix=f"install OpenCode, or name its command with --opencode-command"))
            else:
                self.add(Line("skip", "OpenCode", "not found, skipped"))
            return
        found = opencode_choice.probe(probe_setup)
        version = found["version"]
        if version is None and not opts.opencode:
            self.add(Line("bad", "OpenCode", found["detail"], fix=found["command"] or "opencode upgrade"))
            return
        provider = int(opts.opencode) if opts.opencode else (2 if version >= opencode_choice.V2 else 1)
        setup = make_setup(ctx, {**flags, "opencode": str(provider)})
        if provider == 1:
            from kaiusap.adapters.opencode import OpenCodeAdapter as adapter
        else:
            from kaiusap.adapters.opencode2 import OpenCode2Adapter as adapter
        item = adapter.setup_version(setup)
        directory = opencode_choice.config_dir(setup)
        text = ".".join(map(str, version)) if version else "unknown version"
        self.opencode = {"provider": provider, "version": version, "command": opencode_choice.command(setup),
                         "dir": directory, "setup": setup, "adapter": adapter, "given_command": opts.opencode_command}
        if item["done"]:
            self.add(Line("ok", f"OpenCode {text}",
                          f"kaiusap's OpenCode {provider} plugin; config in {ctx.short(directory)}"))
        else:
            self.add(Line("bad", f"OpenCode {text}", item["detail"], fix=item["command"]))

    def check_tmux(self):
        """tmux and `allow-set-title`: printed for the user when off, never edited."""
        if not self.ctx.which("tmux"):
            self.add(Line("skip", "tmux", "not found, skipped"))
            return
        item = make_setup(self.ctx).pane_titles()
        if item["done"]:
            running = "no default tmux server" not in (item["detail"] or "")
            self.add(Line("ok", "tmux, pane titles on" if running else "tmux, no default server running"))
        else:
            self.tmux = item
            self.add(Line("warn", "tmux, pane titles off", "turn them on yourself; the line is at the end"))

    def check_store(self):
        try:
            self.store = store_format(self.ctx)
        except (OSError, ValueError) as exc:
            self.add(Line("bad", "kaiusap's store", f"{self.ctx.short(self.ctx.kaiusap_home)} can't be read ({exc})"))
            return
        if self.store and self.store[0] > self.store[1]:
            on_disk, current, by = self.store
            self.add(Line("bad", "kaiusap's store",
                          f"format {on_disk}, newer than this kaiusap knows (format {current})",
                          fix=f"install the kaiusap that wrote it{f' ({by})' if by else ''}, or a newer one"))


def stop_without_provider(survey):
    if not survey.providers:
        raise Stop("No provider found: none of claude, codex or opencode is on the PATH. Install one, then run "
                   "./install.sh again. Nothing was changed.")
