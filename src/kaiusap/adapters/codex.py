"""The Codex adapter, behind the provider adapter contract.

Everything Codex-specific lives here: hook-input identity, the caller
lookup, tmux pane titles and the terminal map, the daemon handshake,
`thread/name/set` mirroring and following a Codex start.
"""
from collections import Counter
from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
import re
import time

from . import SPAWNED_AGENT_TEXT, UNIDENTIFIED_CLOSE, Identity, context_use, tail_records, tmux
from .daemon import Daemon, DaemonError, Deadline, RpcError, TURN_LIMIT, find_daemon, linux_start_time, run_command, time_locale_env
from ..errors import Error
from ..storeformat import NewerFormat
from ..model import binding_history, handles, held, instant, last_directory, load_settings, retirement_name
from ..notice import notice, notice_id
from ..store import decoded, identifier, now

UUID = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
TITLE = re.compile(rf"(?<![0-9a-f-])(?:(?P<full>{UUID})|(?P<prefix>[0-9a-f]{{8}}-[0-9a-f]{{4}}-[0-9a-f]{{4}}-[0-9a-f]{{4}}-[0-9a-f]{{5}})\.\.\.)(?![0-9a-f-])")
ROLLOUT = re.compile(rf"rollout-\d{{4}}-\d{{2}}-\d{{2}}T\d{{2}}-\d{{2}}-\d{{2}}-(?P<id>{UUID})(?:_|\.jsonl$)")
PS_ROW = re.compile(r"^\s*(?P<pid>\d+)\s+(?P<ppid>\d+)\s+(?P<pgid>\d+)\s+(?P<tpgid>-?\d+)\s+(?P<stat>\S+)\s+"
                    r"(?P<lstart>\w{3}\s+\w{3}\s+\d+\s+\d\d:\d\d:\d\d\s+\d{4})\s+(?P<args>.*)$")
PANE_FORMAT = "\t".join(["#{pane_id}", "#{session_name}", "#{session_attached}", "#{pane_pid}", "#{pane_tty}",
                        "#{pid}", "#{start_time}", "#{pane_title}"])
SOURCES = {"startup", "resume", "clear", "compact", "fork"}
TRUST_SLACK = timedelta(seconds=1)
# The terminal map is this adapter's own file, in its store directory. Until 2026-11-09 it was
# OLD_TERMINAL_MAP: the first call that may take the global lock moves it (`move_terminal_map`), and the
# readers still read the old path, for one release, while it holds lines.
TERMINAL_MAP = "terminals.jsonl"
OLD_TERMINAL_MAP = "sessions/terminals.jsonl"
MOVE_WAIT = 1  # the most the move waits for each lock, as the start hook waits for the global lock
# A CLI or MCP call that reaches the daemon has this long in total, from the call's
# start, for the caller lookup, its own daemon requests and the catch-up.
CALL_LIMIT = 20
# The status read before turn/start has its own limit, which doesn't stop the delivery.
STATUS_LIMIT = 2
# ThreadActiveFlag (app-server-protocol v2/thread.rs, read at 0.167.0) -> `waiting_on`'s names.
WAITS = (("waitingOnApproval", "approval"), ("waitingOnUserInput", "user_input"))
NO_THREAD = ("kaiusap can't tell which Codex session is calling: the MCP call carried no _meta.threadId. Check kaiusap setup."
             + UNIDENTIFIED_CLOSE)
# `setup`: its four items, at their places in its order.
SETUP_PLUGIN, SETUP_TITLE, SETUP_DAEMON, SETUP_APPROVAL = 2, 3, 4, 6
HOOK_LABELS = ("session_start", "user_prompt_submit", "subagent_start")
HOOK_KEYS = {label: f"kaiusap@kaiusap:hooks/hooks.json:{label}:0:0" for label in HOOK_LABELS}
DEFAULT_TITLE_ITEMS = ["activity", "thread-name", "project-name"]  # Codex 0.166.0's defaults (tui status_surfaces.rs)
APPROVAL_FIX = "start Codex and choose Review hooks or Trust all"
INSTALL_MARKETPLACE = "codex plugin marketplace add {repo}"
INSTALL_PLUGIN = "codex plugin add kaiusap@kaiusap"
# The unknown caller: neither the records nor the daemon could place the thread.
UNKNOWN = "kaiusap can't tell whether this Codex session is a subagent"
UNKNOWN_FIX = "Install the kaiusap plugin, or start the Codex daemon."

# The Codex terminal classifier: the clap grammar of cli/src/main.rs's
# MultitoolCli and what it flattens, copied as a table from rust-v0.166.0.
VALUE_OPTIONS = {"-c", "--config", "--enable", "--disable", "--remote", "--remote-auth-token-env",
                 "-i", "--image", "-m", "--model", "--local-provider", "-p", "--profile", "-s", "--sandbox",
                 "-C", "--cd", "--add-dir", "-a", "--ask-for-approval"}
MULTI_VALUE_OPTIONS = {"-i", "--image"}
FLAG_OPTIONS = {"--yolo", "--not-so-yolo", "--oss", "--approve-for-me", "--dangerously-bypass-approvals-and-sandbox",
                "--dangerously-bypass-hook-trust", "--worktree", "--strict-config", "--search",
                "--no-alt-screen", "--no-daemon", "--help", "-h", "--version", "-V"}
SUBCOMMANDS = {"help", "agents", "tcp-tunnel", "exec", "e", "review", "login", "git-credential", "logout", "mcp",
               "plugin", "app-server", "remote-control", "app", "completion", "update", "doctor", "sandbox", "debug", "execpolicy",
               "apply", "a", "resume", "queue", "archive", "delete", "migrate-rollouts", "migrate-nfs-db",
               "unarchive", "fork", "cloud", "cloud-tasks", "responses-api-proxy", "stdio-to-uds",
               "exec-server", "features"}
TERMINAL_SUBCOMMANDS = {"resume", "fork", "agents"}

REASONS = {
    "store_failed": "a store failure interrupted the send after the attempt line was written and before the notice was sent",
    "detached": "the session is in a detached tmux session and wake_detached is off",
    "daemon_not_found": "the Codex daemon couldn't be found",
    "protocol_unrecognised": "the Codex daemon's protocol wasn't recognised",
    "thread_not_loaded": "the thread isn't loaded in the Codex daemon",
    "sandboxed": "the socket connection was refused with a permission error, probably by the session's sandbox; use the kaiusap tools instead",
    "timeout": "a time limit was reached before the notice was sent",
    "no_confirmation": "no confirmation after the notice was sent",
}
# The liveness answers that make a suggestion candidate's thread not live.
NOT_LIVE = frozenset({"daemon_not_found", "thread_not_loaded"})
FIXES = {
    "detached": "attach it, or set wake_detached true",
    "daemon_not_found": "start a Codex terminal that uses the daemon",
    "protocol_unrecognised": "check codex app-server daemon version against this kaiusap",
    "thread_not_loaded": "resume the thread in a Codex that uses the daemon",
    "timeout": "try again",
    "sandboxed": "use the kaiusap tools instead",
}


def classify_command(args):
    """Whether a flattened `ps` command line is a Codex terminal."""
    tokens = args.split()
    if not tokens:
        return False
    name = os.path.basename(tokens[0])
    if name != "codex" and not name.startswith("codex-"):
        return False
    index = 1
    while index < len(tokens):
        token = tokens[index]
        if token == "--":
            return True
        if token.startswith("-"):
            option = token.split("=", 1)[0] if token.startswith("--") else token[:2]
            attached = "=" in token if token.startswith("--") else len(token) > 2
            if option in VALUE_OPTIONS:
                index += 1
                if not attached:
                    if index >= len(tokens) or tokens[index].startswith("-"):
                        return False
                    index += 1
                if option in MULTI_VALUE_OPTIONS:
                    while index < len(tokens) and not tokens[index].startswith("-"):
                        index += 1
            elif option in FLAG_OPTIONS and not attached:
                index += 1
            else:
                return False
            continue
        if token in TERMINAL_SUBCOMMANDS:
            return True
        return token not in SUBCOMMANDS
    return True


def thread_in_title(title):
    """(full id or None, 29-character prefix or None) from a pane title."""
    match = TITLE.search(title or "")
    if not match:
        return None, None
    full = match.group("full")
    return full, (full[:29] if full else match.group("prefix"))


def thread_in_transcript(path):
    match = ROLLOUT.search(os.path.basename(path or ""))
    return match.group("id") if match else None


def parse_lstart(text):
    local = datetime.strptime(" ".join(text.split()), "%a %b %d %H:%M:%S %Y").astimezone()
    return local.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def lines_of(data):
    """The non-empty lines of a terminal-map file's bytes, without their newlines."""
    return [line for line in data.split(b"\n") if line.strip()]


def merged_lines(old, new):
    """One history from the old terminal map's lines and the new one's: the old lines the new file already
    holds (a move interrupted after its rename, or read while it ran) are dropped, and the rest are sorted
    by `at`, stably, old before new. A line without a readable `at` keeps the place of the line before it."""
    present = Counter(new)
    kept = []
    for line in old:
        if present[line]:
            present[line] -= 1
        else:
            kept.append(line)
    combined = kept + new
    earliest = datetime.min.replace(tzinfo=timezone.utc)
    keys, last = [], earliest
    for line in combined:
        try:
            stamp = instant(decoded(line)["at"])
            last = stamp if stamp.tzinfo else stamp.replace(tzinfo=timezone.utc)
        except (KeyError, ValueError, TypeError, AttributeError):
            pass
        keys.append(last)
    return [combined[i] for i in sorted(range(len(combined)), key=keys.__getitem__)]


# `sessions`' warnings for a resume that may not be the only terminal on the thread (added 2026-11-10 18:09 EST).
JOIN_NOTE = "Still open elsewhere: this joins the same thread as a second terminal, not a copy."
UNKNOWN_NOTE = "kaiusap can't tell whether this thread is open somewhere: this could start a second copy."

class Pane:
    def __init__(self, fields):
        (self.id, self.session, attached, pane_pid, self.tty, server_pid, server_start, self.title) = fields
        self.attached = attached not in {"", "0"}
        self.pane_pid = int(pane_pid)
        self.key = f"{server_pid}@{server_start}:{self.id}"
        self.thread, self.prefix = thread_in_title(self.title)
        self.process = None  # (pid, started_at) once looked up

    def shows(self, thread_id):
        return bool(thread_id) and (self.thread == thread_id or (self.thread is None and self.prefix == thread_id[:29]))


class CodexAdapter:
    delivers = True
    harness_maker = "openai"  # `sessions`
    provider = "codex"
    session_variable = "CODEX_THREAD_ID"
    hook_events = frozenset({"SessionStart", "UserPromptSubmit", "SubagentStart"})
    record_only_events = frozenset({"SubagentStart"})  # its hook writes the record and nothing else
    start_events = frozenset({"SessionStart"})
    prompt_events = frozenset({"UserPromptSubmit"})
    screen_events = frozenset()  # no tool-call screen: the caller lookup refuses at call time
    setup_flags = frozenset()

    @classmethod
    def claims(cls, environment):
        """Whether a shell environment is one of this provider's sessions."""
        return bool(environment.get(cls.session_variable))

    @classmethod
    def pre_tool(cls, payload):
        return None

    @classmethod
    def screen_timeout(cls, event, payload):
        return None  # no screens

    # ----- `setup`: read-only, in the default ~/.codex -----

    @classmethod
    def setup_command(cls, setup):
        return "codex"

    @classmethod
    def setup_items(cls, setup):
        """The Codex plugin, `thread-id` in the title, the daemon and the hook approval, each with its
        place in `setup`'s order; `config.toml` is read once."""
        from ..setup import read_toml
        codex_home = setup.home / ".codex"
        config = read_toml(codex_home / "config.toml")
        return [(SETUP_PLUGIN, cls.setup_plugin(setup, codex_home, config)),
                (SETUP_TITLE, cls.setup_title(setup, codex_home, config)),
                (SETUP_DAEMON, cls.setup_daemon(setup)),
                (SETUP_APPROVAL, cls.setup_approval(setup, config))]

    @staticmethod
    def setup_plugin(setup, codex_home, config):
        """Installed when config.toml has [plugins."kaiusap@kaiusap"] and the plugin cache holds a version."""
        from ..setup import MARKETPLACE, PLUGIN_ID, short
        config = config or {}
        marketplaces = config.get("marketplaces") if isinstance(config.get("marketplaces"), dict) else {}
        known = isinstance(marketplaces.get(MARKETPLACE), dict)
        plugins = config.get("plugins") if isinstance(config.get("plugins"), dict) else {}
        entry = plugins.get(PLUGIN_ID)
        configured = isinstance(entry, dict)
        enabled = entry.get("enabled", True) if configured else None
        cache = codex_home / "plugins/cache" / MARKETPLACE / "kaiusap"
        try:
            cached = any(p.is_dir() and re.fullmatch(r"[A-Za-z0-9._+-]+", p.name) and p.name not in {".", ".."} for p in cache.iterdir())
        except OSError:
            cached = False
        installed = configured and cached
        if installed and known and enabled is not False:
            return setup.item("Codex plugin", True, f"{PLUGIN_ID} is installed in {short(codex_home)}")
        problems, steps = [], []
        if not known:
            problems.append(f"the marketplace {MARKETPLACE} isn't added")
            steps.append(INSTALL_MARKETPLACE)
        if not installed:
            problems.append(f"{PLUGIN_ID} isn't installed" + ("" if configured == cached else
                            " (its config.toml entry is missing)" if cached else " (its plugin cache is missing)"))
            steps.append(INSTALL_PLUGIN)
        if enabled is False:
            problems.append(f"{PLUGIN_ID} is disabled in {short(codex_home / 'config.toml')} (enabled = false)")
        return setup.item("Codex plugin", False, "; ".join(problems), setup.command(*steps) or f'[plugins."{PLUGIN_ID}"]\nenabled = true')

    @staticmethod
    def setup_title(setup, codex_home, config):
        """`thread-id` (or `session-id`) among Codex's [tui] terminal_title items."""
        import json
        from ..setup import short
        tui = config.get("tui") if isinstance(config, dict) else None
        items = tui.get("terminal_title") if isinstance(tui, dict) else None
        current = [i for i in items if isinstance(i, str)] if isinstance(items, list) else None
        done = any(i.strip().lower() in {"thread-id", "session-id"} for i in current or [])
        wanted = (current if current is not None else DEFAULT_TITLE_ITEMS) + ["thread-id"]
        line = "terminal_title = [" + ", ".join(json.dumps(i) for i in wanted) + "]"
        detail = None if done else (
            f"{short(codex_home / 'config.toml')} couldn't be read" if config is None else
            f"add under [tui] in {short(codex_home / 'config.toml')}")
        return setup.item("thread-id in Codex's [tui] terminal_title", done, detail, line)

    @staticmethod
    def setup_daemon(setup):
        """`codex app-server daemon version` with CODEX_HOME removed, 3 s."""
        import shutil
        from ..setup import short
        if not shutil.which("codex", path=setup.environ.get("PATH", "")):
            return setup.item("Codex daemon", False, "codex isn't on the PATH", "codex app-server daemon start")
        try:
            path, version = find_daemon(setup.run, Deadline(), setup.environ)
        except DaemonError as exc:
            return setup.item("Codex daemon", False, exc.detail, "codex app-server daemon start")
        return setup.item("Codex daemon", True, f"running, app-server {version or 'unknown version'}, {short(path)}")

    @staticmethod
    def setup_approval(setup, config):
        """The three hooks.state keys, each with a trusted_hash and none with enabled = false."""
        hooks = config.get("hooks") if isinstance(config, dict) else None
        state = hooks.get("state") if isinstance(hooks, dict) else None
        state = state if isinstance(state, dict) else {}
        missing, disabled = [], []
        for label, key in HOOK_KEYS.items():
            entry = state.get(key)
            if not isinstance(entry, dict) or not isinstance(entry.get("trusted_hash"), str) or not entry["trusted_hash"]:
                missing.append(label)
            elif entry.get("enabled") is False:
                disabled.append(label)
        if not missing and not disabled:
            return setup.item("Codex hook approval", True, "approval recorded for all three hooks")
        problems = []
        if missing:
            problems.append("no approval recorded for " + ", ".join(missing))
        if disabled:
            problems.append("enabled = false for " + ", ".join(disabled))
        return setup.item("Codex hook approval", False, "; ".join(problems), APPROVAL_FIX)

    def __init__(self, store, run=None, codex_home=None, environ=None, clock=None, version="0.1.0", sleep=None):
        self.store = store
        self.run = run or run_command
        self.codex_home = Path(codex_home or Path.home() / ".codex")  # the default home only
        self.environ = environ
        self.clock = clock or time.monotonic
        self.sleep = sleep or time.sleep
        self.version = version
        self._daemon = None
        self._call, self._reached, self._deadline = False, False, None
        self.reset()

    # ----- one call: begin, reset, finish -----------

    def begin(self):
        """A CLI or MCP call starts, before its identity lookup: one deadline for the lookup,
        the verb and the catch-up. Idempotent within a call, so `Core.run` may call it again."""
        if self._call:
            return
        self.reset()
        self._call, self._reached, self._deadline = True, False, Deadline(CALL_LIMIT)
        self.move_terminal_map()

    def reset(self):
        """Drop this call's caches and close its connection; the call itself (its deadline and
        whether it reached the daemon) continues until `finish`."""
        self._close()
        self._observed = False
        self._daemon_error, self._info = None, None
        self._panes, self._processes, self._loaded, self._threads, self._all_processes = None, {}, None, {}, None

    def _close(self):
        if self._daemon is not None:
            self._daemon.close()
            self._daemon = None

    def finish(self, catch_up=True):
        """The call ends: the pending-name catch-up runs once, only when this call reached the
        daemon and time is left, then everything is closed and the call's deadline dropped."""
        try:
            if catch_up and self._reached and not self.deadline().expired():
                self.catch_up()
        except Exception as exc:
            self.store.warnings.append(f"Pending Codex names couldn't be applied: {exc}")
        finally:
            self.reset()
            self._call, self._reached, self._deadline = False, False, None

    # ----- identity -----------------------------------

    def identity(self, environment, store):
        thread = environment.get("CODEX_THREAD_ID")
        cwd = environment.get("PWD") or os.getcwd()
        return self.classify(thread, environment.get("CODEX_SESSION_ID"), cwd)

    def mcp_identity(self, meta, stated=None, cwd=None):
        """Identity from a tool call's `_meta`; without `threadId` the caller is unidentified, never a plain shell.
        The identity argument (`stated`) is ignored: Codex's `_meta` names the caller."""
        meta = meta or {}
        thread = meta.get("threadId")
        if not isinstance(thread, str) or not thread:
            raise Error(NO_THREAD)
        session_id = meta.get("sessionId")
        return self.classify(thread, session_id if isinstance(session_id, str) else None, cwd)

    def directory(self, identity):
        """An MCP caller's directory: Codex has none of its own, so the base's records answer."""
        return None

    def hook_identity(self, payload):
        """Identity from hook input, or None when unidentified (transcript_path null)."""
        if not isinstance(payload, dict) or payload.get("transcript_path") is None:
            return None
        root = payload.get("session_id")
        if not isinstance(root, str) or not root:
            return None
        agent = payload.get("agent_id")
        if not isinstance(agent, str) or not agent:
            in_transcript = thread_in_transcript(payload["transcript_path"])
            agent = in_transcript if in_transcript and in_transcript != root else None
        cwd = payload.get("cwd")
        if agent:
            return Identity("codex", root, f"codex:{root}/{agent}", cwd, "spawned", f"codex:{root}", agent_id=agent)
        return Identity("codex", root, f"codex:{root}", cwd, "root")

    def classify(self, thread, session_id=None, cwd=None):
        """The caller lookup: the call's ids, then start records, then the daemon, else unknown.

        The lookup writes nothing and closes the connection it opened; that it reached the
        daemon is remembered, so the call's catch-up still runs after the verb.
        """
        if not thread:
            return Identity()
        if session_id and session_id != thread:
            return Identity("codex", session_id, f"codex:{session_id}/{thread}", cwd, "spawned", f"codex:{session_id}", agent_id=thread)
        record = self.store.latest("sessions/starts.jsonl", lambda r: r.get("provider") == "codex" and (
            (r.get("session_id") == thread and r.get("source") != "subagent") or
            (r.get("source") == "subagent" and r.get("agent_id") == thread)))
        if record:
            if record.get("source") == "subagent":
                root = record["session_id"]
                return Identity("codex", root, f"codex:{root}/{thread}", cwd, "spawned", f"codex:{root}", agent_id=thread)
            return Identity("codex", thread, f"codex:{thread}", cwd, "root")
        try:
            root = self.root_of(thread)
        except (DaemonError, RpcError) as exc:
            reason = f"no start record for thread {thread} and " + (
                f"the Codex daemon couldn't be asked: {exc.detail}" if isinstance(exc, DaemonError) else f"thread/read failed: {exc.message}")
            return Identity("codex", thread, None, cwd, "unknown", reason=reason)
        finally:
            self._close()
        if root == thread:
            return Identity("codex", thread, f"codex:{thread}", cwd, "root")
        return Identity("codex", root, f"codex:{root}/{thread}", cwd, "spawned", f"codex:{root}", agent_id=thread)

    def root_of(self, thread):
        seen = []
        while thread not in seen and len(seen) < 64:
            seen.append(thread)
            parent = self.thread(thread).get("parentThreadId")
            if not parent:
                return thread
            thread = parent
        raise DaemonError("protocol_unrecognised", "parentThreadId never reached a root")

    def refuse_before_acting(self, identity):
        if identity.classification == "spawned":
            raise Error(SPAWNED_AGENT_TEXT)
        if identity.classification == "unknown":
            raise Error(f"{UNKNOWN} ({identity.reason}). {UNKNOWN_FIX}")

    # ----- events ---------------------------------------------------

    def events(self, payload, deadline=None):
        """Map hook input to the base vocabulary and the record to append; None when nothing is written.
        Nothing here waits, so the hook's deadline isn't needed."""
        identity = self.hook_identity(payload)
        if identity is None:
            return None
        name = payload.get("hook_event_name")
        at = now()
        event = {"provider": "codex", "identity": identity, "session_id": identity.session_id,
                 "session_key": identity.session_key, "agent_id": identity.agent_id, "cwd": payload.get("cwd"),
                 "at": at, "transcript": payload.get("transcript_path"), "record": None, "file": None, "prompt": None, "notice_id": None}
        if name == "SubagentStart":
            if not identity.agent_id:
                return None
            event.update(kind="subagent", file="sessions/starts.jsonl", record={
                "at": at, "provider": "codex", "session_id": identity.session_id, "session_key": identity.session_key,
                "source": "subagent", "agent_id": identity.agent_id, "cwd": event["cwd"]})
            return event
        if identity.classification != "root":
            return None
        if name == "SessionStart":
            source = payload.get("source")
            if not isinstance(source, str) or source not in SOURCES:
                # An unrecognised source: identified, so the hook gives the session line, but no record.
                event.update(kind="unrecognised", source=source)
                return event
            event.update(kind=source, source=source, file="sessions/starts.jsonl", record={
                "at": at, "provider": "codex", "session_id": identity.session_id, "session_key": identity.session_key,
                "source": source, "cwd": event["cwd"]})
            return event
        if name == "UserPromptSubmit":
            prompt = payload.get("prompt")
            event.update(kind="prompt", prompt=prompt, notice_id=notice_id(prompt if isinstance(prompt, str) else ""))
            if event["notice_id"] is None:
                event.update(file="sessions/prompts.jsonl", record={
                    "at": at, "provider": "codex", "session_id": identity.session_id,
                    "session_key": identity.session_key, "source": "prompt", "cwd": event["cwd"]})
            return event
        return None

    def hook_deadline(self, at):
        """The deadline this adapter's own calls use in a hook: the hook's total, as a `Deadline`."""
        return Deadline(at=at)

    def hook_record(self, name, event, deadline, recorded=False, native=None):
        """Append the hook's records. The constant hooks' records are native's fixed-shape appends:
        already written when `recorded`, else by `native()` when the native entry is there."""
        if recorded:
            return
        if native and name in {"UserPromptSubmit", "SubagentStart"}:
            if event.get("record"):
                native()
        else:
            self.record(event, deadline)

    def record(self, event, deadline=None):
        """Append the event's record ("record first"), and for a prompt the pane lines it shows in."""
        if not event or not event.get("record"):
            return
        if event["file"] == "sessions/starts.jsonl":
            self.store.start(event["record"], deadline)
        else:
            self.store.append(event["file"], event["record"], deadline)
        if event["kind"] == "prompt":
            thread = event["session_id"]
            for pane in self.panes(Deadline(at=deadline)):
                if pane.shows(thread):
                    self.store.append(self.terminal_map, {
                        "at": event["at"], "pane": pane.key, "codex_pid": None, "codex_started_at": None,
                        "thread_prefix": thread[:29], "thread_id": thread, "source": "prompt",
                        "session_key": event["session_key"]}, deadline)

    def tell(self, text, event="UserPromptSubmit"):
        return {"hookSpecificOutput": {"hookEventName": event, "additionalContext": text}}

    def hook_output(self, event, state):
        """Codex sets no name through a hook (it uses `thread/name/set`, at once), so a hook adds nothing of its own."""
        return {}

    def marker_fields(self, state, event):
        """Nothing beyond the shared marker: no transcript index, and any title will do."""
        return {}

    # ----- where it runs: tmux and the terminal map -------------

    def deadline(self):
        return self._deadline or Deadline()

    @property
    def terminal_map(self):
        """The terminal map's store path, in this adapter's own directory."""
        return (self.store.adapter_dir(self.provider) / TERMINAL_MAP).relative_to(self.store.root).as_posix()

    def old_terminal_lines(self):
        try:
            return lines_of((self.store.root / OLD_TERMINAL_MAP).read_bytes())
        except FileNotFoundError:
            return []

    def terminal_rows(self, reverse=False):
        """The terminal map's rows, oldest first or newest first. While the old path still holds lines, the
        rows are one history merged from both (`merged_lines`). The old file is read before the new one: a
        move that empties it has renamed the merged file into place first, so no line is missed, and a line
        read from both is dropped once."""
        old = self.old_terminal_lines()
        if not old:
            yield from self.store.rows(self.terminal_map, reverse=reverse)
            return
        try:
            data = (self.store.root / self.terminal_map).read_bytes()
        except FileNotFoundError:
            data = b""
        rows = [row for row in map(decoded, merged_lines(old, lines_of(data))) if row is not None]
        yield from (reversed(rows) if reverse else rows)

    def move_terminal_map(self, wait=MOVE_WAIT):
        """Move the terminal map from its old path into this adapter's directory, once.

        Under the global lock, and the old file's append lock, so no older writer appends meanwhile: the old
        lines and the new file's are merged into a temporary file without the new file's lock; then, under
        that lock, the lines native and Python appended meanwhile are added at its end and it is renamed over
        the new file. A writer that waited for the replaced file's lock finds it replaced and locks the new
        one (`Store.locked`, and the native engine's append). Last, the old file is emptied, not removed, so
        a writer still waiting for its lock appends where the readers look. Without the locks in time, or on
        a store error, nothing changes: the readers keep merging both files, and a later call moves them."""
        store = self.store
        if store is None or "lock" in store.holding:
            return
        old = store.root / OLD_TERMINAL_MAP
        try:
            if not old.stat().st_size:
                return
        except OSError:
            return
        temp = None
        try:
            with store.locked("lock", time.monotonic() + wait), \
                    store.locked(OLD_TERMINAL_MAP, time.monotonic() + wait) as old_fd:
                size = os.fstat(old_fd).st_size
                lines = lines_of(os.pread(old_fd, size, 0)) if size else []
                if lines:
                    new = store.root / self.terminal_map
                    store._mkdir(new.parent)
                    try:
                        before = new.read_bytes()
                    except FileNotFoundError:
                        before = b""
                    cut = before.rfind(b"\n") + 1  # whole lines only; the rest is read under the lock
                    temp = new.with_name(".tmp-" + identifier())
                    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                    try:
                        store._write(fd, b"".join(line + b"\n" for line in merged_lines(lines, lines_of(before[:cut]))))
                        os.fsync(fd)
                        with store.locked(self.terminal_map, time.monotonic() + wait) as new_fd:
                            end = os.fstat(new_fd).st_size
                            tail = os.pread(new_fd, end - cut, cut) if end > cut else b""
                            if tail and not tail.endswith(b"\n"):
                                tail += b"\n"
                            store._write(fd, tail)
                            os.fsync(fd)
                            os.replace(temp, new)
                            temp = None
                            store._sync_dir(new.parent)
                    finally:
                        os.close(fd)
                os.ftruncate(old_fd, 0)
                os.fsync(old_fd)
        except TimeoutError as exc:
            if type(exc) is not TimeoutError:
                raise  # a hook's hard limit is not a lock wait: it still stops the hook
        except (OSError, NewerFormat) as exc:
            # A store of a newer format refuses the move; a read verb that began it still runs.
            store.log(f"terminal map move failed: {exc}")
        finally:
            if temp is not None:
                temp.unlink(missing_ok=True)

    def panes(self, deadline=None):
        import subprocess
        if (deadline or self.deadline()).expired():
            raise DaemonError("timeout", "no time left to read panes")
        if self._panes is None:
            self._panes = []
            limit = (deadline or self.deadline()).limit(2)
            try:
                code, out = self.run(["tmux", "-L", "default", "list-panes", "-a", "-F", PANE_FORMAT], limit, None)
            except (OSError, subprocess.TimeoutExpired, ValueError):
                return self._panes
            if code != 0:
                return self._panes
            for line in out.splitlines():
                fields = line.split("\t", 7)
                if len(fields) == 8 and fields[3].isdigit():
                    self._panes.append(Pane(fields))
        return self._panes

    def process_of(self, pane, deadline=None):
        """The pane's Codex terminal process: (pid, started_at) or None, by the classifier."""
        if pane.tty not in self._processes:
            self._processes[pane.tty] = self._lookup_process(pane, deadline)
        pane.process = self._processes[pane.tty]
        return pane.process

    def _lookup_process(self, pane, deadline):
        import subprocess
        tty = pane.tty.removeprefix("/dev/")
        if (deadline or self.deadline()).expired():
            raise DaemonError("timeout", "no time left to read processes")
        try:
            code, out = self.run(["ps", "-t", tty, "-ww", "-o", "pid=,ppid=,pgid=,tpgid=,stat=,lstart=,args="],
                                 (deadline or self.deadline()).limit(2), time_locale_env())
        except (OSError, subprocess.TimeoutExpired, ValueError):
            return None
        if code != 0:
            return None
        rows = {}
        for line in out.splitlines():
            match = PS_ROW.match(line)
            if match:
                rows[int(match["pid"])] = match
        for pid, row in rows.items():
            ancestor, hops = pid, 0
            while ancestor != pane.pane_pid and ancestor in rows and hops < 64:
                ancestor, hops = int(rows[ancestor]["ppid"]), hops + 1
            if ancestor != pane.pane_pid:
                continue
            foreground = "+" in row["stat"] or row["tpgid"] == row["pgid"]
            if foreground and classify_command(row["args"]):
                try:
                    return pid, linux_start_time(pid, self.run) or parse_lstart(row["lstart"])
                except ValueError:
                    return None
        return None

    def terminal_running(self):
        """Whether any Codex terminal process runs on the machine; None when ps can't say."""
        import subprocess
        if self.deadline().expired():
            raise DaemonError("timeout", "no time left to read processes")
        if self._all_processes is None:
            try:
                code, out = self.run(["ps", "-axww", "-o", "args="], self.deadline().limit(3), None)
                self._all_processes = None if code != 0 else [line for line in out.splitlines() if classify_command(line)]
            except (OSError, subprocess.TimeoutExpired, ValueError):
                self._all_processes = None
            if self._all_processes is None:
                return None
        return bool(self._all_processes)

    def observe(self, panes, deadline=None):
        """Record the complete pane snapshot once per call, resolving only loaded ids."""
        if self._observed:
            return
        deadline = deadline or self.deadline()
        for pane in panes:
            process = self.process_of(pane, deadline)
            thread = self.pane_thread(pane, deadline)
            line = {"pane": pane.key, "codex_pid": process[0] if process else None,
                    "codex_started_at": process[1] if process else None,
                    "thread_prefix": pane.prefix, "thread_id": thread, "source": "read",
                    "session_key": f"codex:{thread}" if thread else None}
            latest = self.store.latest_of(self.terminal_rows(reverse=True), lambda r: r.get("pane") == pane.key)
            if latest and all(latest.get(k) == v for k, v in line.items() if k not in {"session_key", "source"}):
                continue
            try:
                self.store.append(self.terminal_map, {"at": now(), **line}, deadline=deadline.at)
            except OSError as exc:
                self.store.warn(exc)
        self._observed = True

    def resolve(self, prefix, deadline=None):
        """The unique loaded thread matching this full id or prefix, else None."""
        if not prefix:
            return None
        try:
            candidates = {t for t in self.loaded(deadline) if t.startswith(prefix)}
        except DaemonError:
            return None
        return candidates.pop() if len(candidates) == 1 else None

    def pane_thread(self, pane, deadline=None):
        return self.resolve(pane.thread or pane.prefix, deadline)

    def pane_lines(self, pane, process, deadline=None):
        """The pane's terminal-map lines written in its current Codex process, newest first."""
        started = instant(process[1])
        lines = []
        for row in self.terminal_rows(reverse=True):
            try:
                at = instant(row["at"])
            except (KeyError, ValueError, TypeError):
                continue
            if at < started - timedelta(seconds=3):
                break
            if row.get("pane") != pane.key:
                continue
            if row.get("source") == "read" and (row.get("codex_pid"), row.get("codex_started_at")) == process:
                lines.append(row)
            elif row.get("source") == "prompt" and at >= started + TRUST_SLACK:
                lines.append(row)
        return sorted(lines, key=lambda row: instant(row["at"]), reverse=True)

    def trusted(self, pane, thread, deadline=None):
        """The trust test: a non-compact start or a prompt record at least 1 s newer than the pane's process."""
        process = self.process_of(pane, deadline)
        if not process:
            return False
        floor = instant(process[1]) + TRUST_SLACK
        record = self.store.latest("sessions/starts.jsonl", lambda r: r.get("provider") == "codex" and r.get("session_id") == thread
                                   and r.get("source") not in {"compact", "subagent"}, process_started_at=process[1])
        if record and instant(record["at"]) >= floor:
            return True
        prompt = self.store.latest("sessions/prompts.jsonl", lambda r: r.get("provider") == "codex" and r.get("session_id") == thread,
                                   process_started_at=process[1])
        return bool(prompt and instant(prompt["at"]) >= floor)

    def prompted_in(self, thread, process):
        floor = instant(process[1]) + TRUST_SLACK
        prompt = self.store.latest("sessions/prompts.jsonl", lambda r: r.get("provider") == "codex" and r.get("session_id") == thread,
                                   process_started_at=process[1])
        return bool(prompt and instant(prompt["at"]) >= floor)

    # ----- the daemon -------------------------------------------------

    def daemon(self, deadline=None):
        """A connected, initialized daemon for this call, or raise DaemonError; the failure is remembered for the call."""
        if self._daemon:
            return self._daemon
        if self._daemon_error:
            raise self._daemon_error
        try:
            self._daemon = self.open(deadline or self.deadline())
        except DaemonError as exc:
            self._daemon_error = exc
            raise
        self._reached = True
        return self._daemon

    def open(self, deadline):
        path, version = find_daemon(self.run, deadline, self.environ)
        self._info = (path, version)
        daemon = Daemon(path, version, deadline, self.version)
        try:
            daemon.connect()
            daemon.initialize()
        except Exception:
            daemon.close()
            raise
        return daemon

    def loaded(self, deadline=None):
        if self._loaded is None:
            self._loaded = self.daemon(deadline).loaded_threads()
        return self._loaded

    def thread(self, thread_id, deadline=None):
        if thread_id not in self._threads:
            self._threads[thread_id] = self.daemon(deadline).thread(thread_id)
        return self._threads[thread_id]

    def daemon_version(self):
        return self._info[1] if self._info else None

    # ----- liveness --------------------------------------

    @staticmethod
    def thread_of(session_key):
        return (session_key or "").split(":", 1)[-1].split("/", 1)[0] or None

    def status(self, thread, deadline=None, record=True, read_panes=True, wake_detached=None):
        """The first failing test of whoami's order, or live; with the panes and trusted panes seen.

        A thread is live while the default daemon has it loaded, whether or not a pane shows it
        (amended 2026-11-07). Panes decide only the detached hold, and `who`/`sessions` show them;
        a send passes `read_panes=False` when `wake_detached` is on, so it reads no titles.
        """
        snapshot = self.panes(deadline) if read_panes else []
        panes = [p for p in snapshot if p.shows(thread)]
        if record and read_panes:
            self.observe(snapshot, deadline)
        # Trust needs only the records; the daemon then narrows it to panes whose
        # title resolves to this thread alone.
        trusted = [p for p in panes if self.trusted(p, thread, deadline)]
        result = {"panes": panes, "trusted": trusted, "detached": False, "version": None}
        try:
            loaded = thread in self.loaded(deadline)
        except DaemonError as exc:
            return {**result, "reason_code": exc.code, "why_not": REASONS[exc.code], "fix": FIXES[exc.code], "detail": exc.detail}
        result["version"] = self.daemon_version()
        if not loaded:
            return {**result, "reason_code": "thread_not_loaded", "why_not": REASONS["thread_not_loaded"], "fix": FIXES["thread_not_loaded"]}
        trusted = [p for p in trusted if self.pane_thread(p, deadline) == thread]
        result["trusted"] = trusted
        # Held only when trusted panes show the thread and every one is detached; a thread
        # no pane shows is never held, as a Claude background session isn't.
        result["detached"] = bool(trusted) and all(not p.attached for p in trusted)
        if result["detached"] and not (load_settings(self.store)["wake_detached"] if wake_detached is None else wake_detached):
            return {**result, "reason_code": "detached", "why_not": "Its tmux session is detached and wake_detached is off", "fix": FIXES["detached"]}
        return {**result, "reason_code": None, "why_not": None, "fix": None}

    def liveness(self, session_key):
        thread = self.thread_of(session_key)
        if not thread:
            return {"can_be_woken": False, "why_not": UNKNOWN, "reason_code": "unknown_caller", "fix": UNKNOWN_FIX, "detached": False,
                    "live": True}
        status = self.status(thread)
        # For suggestions: not live only when the daemon isn't found or hasn't the thread
        # loaded; any other answer, a protocol not recognised, a timeout or a sandbox among them, counts as live.
        return {"can_be_woken": status["reason_code"] is None, "why_not": status["why_not"],
                "reason_code": status["reason_code"], "fix": status["fix"], "detached": status["detached"],
                "live": status["reason_code"] not in NOT_LIVE}

    def unidentified(self, identity):
        """`whoami` for the lookup's unknown caller: not reachable, with the lookup's reason."""
        return {"can_be_woken": False, "why_not": UNKNOWN + (f" ({identity.reason})" if identity.reason else ""),
                "reason_code": "unknown_caller", "fix": UNKNOWN_FIX}

    # ----- deliver ------------------------------------------

    def deliver(self, store, message, deadline=None):
        """Hand the notice to the thread with turn/start; never raises. The base writes the attempt and delivery lines."""
        thread = self.thread_of(message["to"]["session_key"])
        self.reset()
        self._deadline = Deadline(at=deadline) if deadline is not None else Deadline(20)
        try:
            try:
                # Pane titles are read only for the detached hold, when wake_detached is off (amended 2026-11-07).
                wake_detached = load_settings(self.store)["wake_detached"]
                status = self.status(thread, self._deadline, read_panes=not wake_detached, wake_detached=wake_detached)
            except (OSError, ValueError) as exc:
                # No notice has been handed over. Keep the attempt without an
                # outcome, as this store failure requires.
                store.warn(exc)
                return {**self.stored("store_failed", REASONS["store_failed"]),
                        "store_failed_before_handover": True}
            if status["reason_code"]:
                return self.stored(status["reason_code"], REASONS[status["reason_code"]], status.get("detail"))
            daemon = self._daemon
            waiting_on = self.waiting_on(daemon, thread)
            try:
                result = daemon.request("turn/start", {"threadId": thread, "input": [{"type": "text", "text": notice(message)}]}, TURN_LIMIT)
            except RpcError as exc:
                if "thread not found" in exc.message or "not loaded" in exc.message:
                    return self.stored("thread_not_loaded", REASONS["thread_not_loaded"], exc.message)
                return self.stored("codex_refused", "Codex refused the turn: " + exc.message)
            except DaemonError as exc:
                observed = self.uncertain(exc.detail) if exc.sent else self.stored(exc.code, REASONS[exc.code], exc.detail)
                daemon.close()
                self._daemon = None
                return observed
            turn = result.get("turn") if isinstance(result, dict) else None
            if not isinstance(turn, dict) or not isinstance(turn.get("id"), str):
                return self.uncertain("the answer to turn/start carried no turn id")
            return {"outcome": "woken", "reason_code": None, "reason": None, "provider_version": self.daemon_version(),
                    "turn_id": turn["id"], "waiting_on": waiting_on, "warnings": []}
        except DaemonError as exc:
            observed = self.stored(exc.code, REASONS[exc.code], exc.detail)
            self.reset()
            return observed
        except Exception:
            self.reset()
            raise
        # The caller records the delivery outcome, then calls finish() to apply
        # pending names within the remaining deadline and close this connection.

    @staticmethod
    def waiting_on(daemon, thread):
        """What the thread's turn already waits on before the wake, or None.

        `thread/read` without turns, within 2 s. A JSON-RPC error, a slow answer or one without a
        readable status reports nothing and doesn't stop the delivery; a late answer is skipped
        by id when `turn/start`'s comes. A connection that breaks here can't carry `turn/start`,
        so that is raised as a failure before `turn/start` is sent (`stored`), not left to look
        like a turn that may have been submitted.
        """
        try:
            result = daemon.request("thread/read", {"threadId": thread, "includeTurns": False}, STATUS_LIMIT)
        except RpcError:
            return None
        except DaemonError as exc:
            if daemon.usable():
                return None
            raise DaemonError(exc.code, "the connection broke reading the thread's status: " + exc.detail) from None
        info = result.get("thread") if isinstance(result, dict) else None
        status = info.get("status") if isinstance(info, dict) else None
        if not isinstance(status, dict) or status.get("type") != "active" or not isinstance(status.get("activeFlags"), list):
            return None
        waits = [name for flag, name in WAITS if flag in status["activeFlags"]]
        return waits or None

    def stored(self, code, reason, detail=None):
        return {"outcome": "stored", "reason_code": code, "reason": reason, "provider_version": self.daemon_version(),
                "warnings": [], "detail": detail}

    def uncertain(self, detail):
        return {"outcome": "uncertain", "reason_code": "no_confirmation", "reason": REASONS["no_confirmation"],
                "provider_version": self.daemon_version(), "warnings": [], "detail": detail}

    # ----- list and where ------------------------------------------

    def where(self, session_key):
        thread = self.thread_of(session_key)
        if not thread:
            return {"cwd": None, "name": None, "where": "unknown", "tmux_panes": [], "command": None, "trusted": None, "detached": False, "note": None}
        return self.row(thread)

    def row(self, thread):
        key = f"codex:{thread}"
        status = self.status(thread)
        info = None
        try:
            info = self.thread(thread)
        except (DaemonError, RpcError):
            pass
        cwd = last_directory(self.store, key) or (info.get("cwd") if info else None)
        name = info.get("name") if info else None
        panes = [{"tmux_session": p.session, "attached": p.attached, "pane": p.key, "trusted": p in status["trusted"]}
                 for p in status["panes"] if self.pane_thread(p) == thread]
        row = {"provider": "codex", "session_id": thread, "session_key": key, "cwd": cwd, "name": name,
               "tmux_panes": panes, "trusted": (any(p["trusted"] for p in panes) if panes else None),
               "detached": status["detached"], "command": None, "note": None}
        if panes:
            row["where"] = "tmux"
            # A trusted pane first; an untrusted one still gets the line, since attaching opens no copy.
            pane = tmux.attach_pane([p for p in panes if p["trusted"]] or panes)
            row["command"] = tmux.attach_command(pane["tmux_session"])
            return row
        loaded = None
        try:
            loaded = thread in self.loaded()
        except DaemonError as exc:
            loaded = False if exc.code == "daemon_not_found" else None
        if loaded:
            # Loaded and no pane shows it: running, perhaps with no terminal at all. A resume joins the loaded
            # thread as another client, not a copy.
            row["where"] = "not_in_tmux"
            return self.resume_line(row, cwd, thread, JOIN_NOTE)
        if loaded is None or self.terminal_running() is not False:
            row["where"] = "unknown"
            return self.resume_line(row, cwd, thread, UNKNOWN_NOTE)
        row["where"] = "not_running"
        return self.resume_line(row, cwd, thread, None)

    @staticmethod
    def resume_line(row, cwd, thread, warning):
        """`sessions`' resume command and note: a `cd` when the directory is known, else the note says where."""
        import shlex
        row["command"] = f"cd {shlex.quote(cwd)} && codex resume {shlex.quote(thread)}" if cwd else f"codex resume {shlex.quote(thread)}"
        notes = [warning] if warning else []
        if not cwd:
            notes.append("(run it in the thread's directory)")
        row["note"] = " ".join(notes) or None
        return row

    def list(self):
        threads = []
        try:
            for thread_id in self.loaded():
                try:
                    info = self.thread(thread_id)
                except (DaemonError, RpcError):
                    continue
                if not info.get("parentThreadId"):
                    threads.append(thread_id)
        except DaemonError:
            pass
        self.observe(self.panes())
        for pane in self.panes():
            thread = self.pane_thread(pane)
            if thread and thread not in threads:
                threads.append(thread)
        return [self.row(thread) for thread in threads]

    # ----- mirror the name ---------------------------------------

    def mirror(self, binding):
        """Name the thread after its handle: always after iam, after a hook move only when it has no name.
        At `iam`, also the window rule for a trusted pane showing the thread.

        Other pending names wait for the call's one catch-up, run from `finish`.
        """
        warnings = self._mirror(binding)
        if binding.get("reason") == "iam":
            try:
                pane = self.trusted_pane(self.thread_of(binding["session_key"]))
                if pane:
                    tmux.name_window(self.run, self.tmux_limit, self.store.log, pane.key)
            except (DaemonError, OSError) as exc:
                self.store.log(f"tmux window name for {binding['session_key']}: {exc}")
        return warnings

    def _mirror(self, binding):
        thread = self.thread_of(binding["session_key"])
        try:
            current = self.thread(thread).get("name")
            applied = binding["reason"] == "iam" or not current
            if applied:
                self.daemon().set_name(thread, binding["handle"])
                name = binding["handle"]
            else:
                name = current
        except DaemonError as exc:
            return [f"The Codex thread name wasn't set ({REASONS[exc.code]}); it will be at the next kaiusap call that reaches the daemon."]
        except RpcError as exc:
            name, applied = None, False
            self.store.log(f"thread/name/set for {thread} failed: {exc.message}")
        self.named(binding, name, "handle")
        return self.follow_session_name(thread, name) if applied else []

    def retire(self, binding, left_at, cause=None):
        return self._retire(binding, left_at)

    def _retire(self, binding, left_at):
        thread = self.thread_of(binding["session_key"])
        retired = retirement_name(binding["handle"], left_at)
        try:
            if held(handles(self.store), binding["session_key"]):
                name = None  # the mirror wins: a session holding a handle is never given a retired name
            elif self.thread(thread).get("name") == binding["handle"]:
                self.daemon().set_name(thread, retired)
                name = retired
            else:
                name = None
        except DaemonError as exc:
            return [f"The Codex thread wasn't renamed {retired} ({REASONS[exc.code]}); it will be at the next kaiusap call that reaches the daemon."]
        except RpcError as exc:
            name = None
            self.store.log(f"thread/name/set for {thread} failed: {exc.message}")
        self.named(binding, name, "retired")
        return self.follow_session_name(thread, name) if name else []

    # ----- tmux names: the window rule and the session rename ------

    def tmux_limit(self, seconds):
        return self.deadline().limit(seconds)

    def trusted_pane(self, thread):
        """The first pane whose title shows the thread and is trusted to, or None."""
        if not thread:
            return None
        return next((p for p in self.panes() if p.shows(thread) and self.trusted(p, thread)), None)

    def follow_session_name(self, thread, name):
        """With `tmux_session_names` "follow", rename the single-pane tmux session of a trusted pane showing
        the thread to the name just applied; with no trusted pane, nothing. Returns `iam`'s warning."""
        if not name or load_settings(self.store)["tmux_session_names"] != "follow":
            return []
        try:
            pane = self.trusted_pane(thread)
        except (DaemonError, OSError) as exc:
            self.store.log(f"tmux session name for codex:{thread}: {exc}")
            return []
        warning = tmux.rename_session(self.run, self.tmux_limit, self.store.log, pane.key, name) if pane else None
        return [warning] if warning else []

    def harness(self, session_key):
        """`sessions`' harness_maker: who made this provider's harness, whatever model it runs."""
        return self.harness_maker

    def harness_version(self, session_key):
        """The Codex daemon's app-server version, for a thread the daemon has loaded: a live thread is one it
        runs. None otherwise, or when the daemon can't be asked."""
        thread = self.thread_of(session_key)
        if not thread:
            return None
        try:
            loaded = self.loaded()
        except (DaemonError, RpcError, OSError, ValueError, TimeoutError, Error):
            return None
        return self.daemon_version() if thread in (loaded or ()) else None

    def activity(self, session_key):
        """The thread's `status` in the `thread/read` `context` also makes (one read per call): `idle` is
        idle; `active` is working, or blocked with its known wait flags; `systemError` is error; `notLoaded`, any
        other answer, or a failed read is None."""
        from . import activity_reading
        thread = self.thread_of(session_key)
        if not thread:
            return None
        try:
            info = self.thread(thread)
        except (DaemonError, RpcError, OSError, ValueError, TimeoutError, Error):
            return None
        status = info.get("status") if isinstance(info, dict) else None
        kind = status.get("type") if isinstance(status, dict) else None
        if kind == "idle":
            return activity_reading("idle")
        if kind == "systemError":
            return activity_reading("error")
        if kind == "active" and isinstance(status.get("activeFlags"), list):
            waits = [name for flag, name in WAITS if flag in status["activeFlags"]]
            return activity_reading("blocked", waits) if waits else activity_reading("working")
        return None

    def context(self, session_key):
        """The context use of the thread's latest turn, from the end of its rollout file (`thread/read`'s
        `path`): the latest `token_count` event's `last_token_usage.total_tokens` (Codex's own
        `tokens_in_context_window`) and `model_context_window`, the limit; the model from `thread/read`, else
        the latest `turn_context`. None when the daemon can't be read or no `token_count` with usage is found."""
        thread = self.thread_of(session_key)
        try:
            info = self.thread(thread)
        except (DaemonError, RpcError):
            return None
        path, model = info.get("path"), info.get("model")
        if not isinstance(path, str):
            return None
        found = None
        for line in tail_records(path):
            payload = line.get("payload") if isinstance(line.get("payload"), dict) else {}
            if found is None and line.get("type") == "event_msg" and payload.get("type") == "token_count":
                usage = payload.get("info") if isinstance(payload.get("info"), dict) else {}
                last = usage.get("last_token_usage") if isinstance(usage.get("last_token_usage"), dict) else {}
                if type(last.get("total_tokens")) is int:
                    found = (last["total_tokens"], usage.get("model_context_window"), line.get("timestamp"))
            elif line.get("type") == "turn_context" and not isinstance(model, str):
                model = payload.get("model")
            if found and isinstance(model, str):
                break
        return context_use(found[0], found[1], model, found[2]) if found else None

    def tmux_start(self, event):
        """The start hook's tmux work, on every source: the window rule for a trusted pane showing the thread; on
        `startup` and `resume`, the session rename for a thread holding a handle whose name (`thread/read`) is the handle's."""
        if event.get("kind") not in SOURCES:
            return
        thread = event["session_id"]
        try:
            pane = self.trusted_pane(thread)
            if pane:
                tmux.name_window(self.run, self.tmux_limit, self.store.log, pane.key)
            if (pane and event["kind"] in {"startup", "resume"}
                    and load_settings(self.store)["tmux_session_names"] == "follow"):
                handle = held(handles(self.store), event["session_key"])
                if handle and self.thread(thread).get("name") == handle:
                    self.follow_session_name(thread, handle)
        except (DaemonError, RpcError, OSError) as exc:
            self.store.log(f"tmux names at the start of codex:{thread}: {exc}")

    def named(self, binding, name, kind):
        try:
            self.store.event("named", handle=binding["handle"], session_id=self.thread_of(binding["session_key"]),
                             session_key=binding["session_key"], binding=binding["id"], name=name, kind=kind, retry=False,
                             by_session=self.thread_of(binding["session_key"]), by_session_key=binding["session_key"],
                             deadline=self.deadline().at)
        except OSError as exc:
            self.store.warn(exc)

    def catch_up(self):
        """Apply mirrors and retirements that found no daemon when they were due; best effort.

        Runs once per call, from `finish`, after the verb and its permission check; every
        request is capped by the time the call has left, and nothing starts once it has none.
        """
        try:
            self.daemon()
        except DaemonError as exc:
            self.store.warnings.append(f"Pending Codex names couldn't be applied: {exc}")
            return
        try:
            ended, current = binding_history(self.store)
            done = {(e.get("binding"), e.get("kind")) for e in self.store.events() if e.get("event") == "named"}
        except OSError as exc:
            self.store.warn(exc)
            return
        # Retirements first, as in one call's mirror, so a retired tmux session frees its name.
        for binding, left_at in ended:
            if self.deadline().expired():
                return
            if binding["provider"] == "codex" and (binding["id"], "retired") not in done:
                self.store.warnings.extend(self._retire(binding, left_at))
        for binding in current.values():
            if self.deadline().expired():
                return
            if binding and binding["provider"] == "codex" and (binding["id"], "handle") not in done:
                self.store.warnings.extend(self._mirror(binding))

    # ----- follow a start: Codex steps 1–4 ---------------------------

    def follow_start(self, event, store):
        """A proposed move or a told text for a start, or None; the base applies the lock, guards and currency."""
        kind = event.get("kind")
        self._deadline = Deadline(at=event["deadline"]) if event.get("deadline") else Deadline(3)
        self.move_terminal_map()
        thread, key = event["session_id"], event["session_key"]
        if kind == "fork":
            return self.step_fork(event, thread, key)
        if kind in {"startup", "clear"}:
            return self.step_fresh(event, thread, key)
        try:
            panes = self.panes()
            self.observe(panes)
        except (DaemonError, RpcError) as exc:
            return self.not_followed(key, exc)
        showing = [p for p in panes if p.shows(thread)]
        return {"tmux_session": showing[0].session if showing else None}

    def not_followed(self, key, exc):
        """Record first: a nearing deadline or a failed read while following, before any holder is
        identified, ends the following with nothing moved and a logged line; nothing is said ("Couldn't
        follow"), and nothing is suggested."""
        self.store.log(f"start {key}: couldn't follow: {type(exc).__name__}: {exc}")
        return {"suggest": False}

    def step_fork(self, event, thread, key):
        ask = {"text": "This conversation was copied or moved from another one. Ask the user whether it should hold a handle.", "suggest": False}
        bindings = handles(self.store)
        seen, current = set(), thread
        try:
            while True:
                if self.deadline().expired():
                    return ask
                parent = self.thread(current).get("forkedFromId")
                if not parent or parent in seen or not isinstance(parent, str):
                    if parent:
                        return ask
                    return {"text": "This conversation is a copy of another one, which held no handle; you have no handle.", "suggest": False}
                seen.add(parent)
                holder = held(bindings, f"codex:{parent}")
                if holder:
                    break
                if self.store.latest("sessions/starts.jsonl", lambda r: r.get("provider") == "codex" and r.get("session_id") == parent
                                     and r.get("source") != "subagent"):
                    return {"text": "This conversation is a copy of another one, which held no handle; you have no handle.", "suggest": False}
                current = parent
            directory = last_directory(self.store, f"codex:{parent}") or self.thread(parent).get("cwd")
        except (DaemonError, RpcError):
            return ask
        if not directory:
            return ask
        if self.same_directory(directory, event.get("cwd")):
            return {"text": f"This conversation is a copy of the one holding {holder}, which keeps it; you have no handle.", "suggest": False}
        # Codex reports /cd, a continued worktree and /fork into a worktree alike, and a clone never
        # takes the handle, so nothing moves into another directory: the session is asked, and that is an offer.
        return {"text": f"You may be {holder}: this conversation started from {holder}'s thread {parent}, in another "
                        f"directory. Ask the user whether this is {holder} moving here (/cd, or continuing in a worktree) "
                        "or a copy (/fork).",
                "offers": [{"handle": holder, "from": f"codex:{parent}"}], "suggest": False}

    @staticmethod
    def same_directory(a, b):
        return bool(a and b) and os.path.normpath(a) == os.path.normpath(b)

    def step_fresh(self, event, thread, key):
        found = []  # per pane with a predecessor: (pane, holder, predecessor, via), holder None when none is found
        try:
            # The pane and process reads raise DaemonError once the deadline has passed.
            panes = self.panes_showing(thread)
            self.observe(self.panes())
            bindings = handles(self.store)
            for pane in panes:
                process = self.process_of(pane)
                if not process:
                    continue
                lines = self.pane_lines(pane, process)
                timestamps = [instant(line["at"]) for line in lines]
                if len(timestamps) != len(set(timestamps)):
                    return {"suggest": False}
                previous = next((l for l in lines if not self.line_names(l, thread)), None)
                if previous:
                    found.append((pane, *self.walk_back(bindings, thread, process, lines, previous)))
        except (DaemonError, RpcError) as exc:
            return self.not_followed(key, exc)
        result = {"suggest": True, "tmux_session": panes[0].session if panes else None}
        holders = list(dict.fromkeys(holder for _, holder, _, _ in found if holder))
        if not holders:
            # No pane resolves to a holder: no move, nothing told. The thread this pane showed just before still
            # continues its persona, when no other trusted pane shows that thread.
            persona = self.continued_without_handle(panes, thread)
            return {**result, "persona": persona} if persona else result
        if len(holders) > 1 or any(not holder for _, holder, _, _ in found):
            # The panes disagree: the holders found are named, and none moves.
            text = (f"You may be {holders[0]}: this terminal showed it before this conversation, so kaiusap didn't move it. "
                    "Tell the user.") if len(holders) == 1 else (
                    f"You may be {' or '.join(holders)}: this terminal showed each before this conversation, so kaiusap "
                    "moved neither. Tell the user.")
            return {**result, "suggest": False, "told": True, "text": text}
        pane, holder, predecessor, via = found[0]
        directory = last_directory(self.store, f"codex:{predecessor}")
        if not directory:
            try:
                directory = self.thread(predecessor).get("cwd")
            except (DaemonError, RpcError):
                directory = None
        if not directory:
            return {**result, "suggest": False, "told": True,
                    "text": f"You may be {holder}: this conversation replaced its thread in this terminal, but kaiusap couldn't find that thread's directory. Tell the user."}
        if not self.same_directory(directory, event.get("cwd")):
            # The handle stays in its directory, and the thread is told so; no suggestion.
            return {**result, "suggest": False, "told": True,
                    "text": f"This terminal showed {holder} before, in {directory}. This conversation started in "
                            f"{event.get('cwd')}, so kaiusap left the handle there. Tell the user."}
        return {**result, "pane": pane.key, "suggest": False,
                "move": {"handle": holder, "from": f"codex:{predecessor}", "to": key, "reason": "fresh-start", "via": via or None},
                "text": f"You are {holder}; the handle followed this conversation from thread {predecessor}, which it replaced in this terminal.",
                "failed": f"You may be {holder}: this conversation replaced its thread in this terminal, but kaiusap couldn't follow it in time. Ask the user."}

    def continued_without_handle(self, panes, thread):
        """The persona line for a /clear or /new no handle followed: the one pane's previous thread, if no
        other trusted pane shows it now. None when the panes don't agree on one predecessor."""
        before = set()
        for pane in panes:
            process = self.process_of(pane)
            if not process:
                continue
            previous = next((l for l in self.pane_lines(pane, process) if not self.line_names(l, thread)), None)
            predecessor = self.line_thread(previous) if previous else None
            if predecessor:
                before.add((pane.key, predecessor))
        if len({p for _, p in before}) != 1:
            return None
        predecessor = next(iter(before))[1]
        shown = {key for key, _ in before}
        if any(p.key not in shown and p.shows(predecessor) and self.trusted(p, predecessor) for p in self.panes()):
            return None
        return {"continues": f"codex:{predecessor}", "via": "fresh-start"}

    def walk_back(self, bindings, thread, process, lines, previous):
        """One pane's predecessor resolved to its holder: `(holder, predecessor, via)`, walking back
        past threads that held nothing and were never prompted in this process; holder None when
        the walk finds none, or a thread the user prompted in this process stops it."""
        predecessor = self.line_thread(previous)
        if not predecessor:
            return None, None, []
        holder = held(bindings, f"codex:{predecessor}")
        if holder:
            return holder, predecessor, []
        if self.prompted_in(predecessor, process):
            return None, None, []
        passed = [predecessor]
        for line in lines[lines.index(previous) + 1:]:
            candidate = self.line_thread(line)
            if not candidate or candidate == thread or candidate in passed:
                continue
            holder = held(bindings, f"codex:{candidate}")
            if holder:
                return holder, candidate, [f"codex:{t}" for t in passed]
            if self.prompted_in(candidate, process):
                return None, None, []
            passed.append(candidate)
        return None, None, []

    def panes_showing(self, thread):
        """Panes whose title shows the thread, re-read for up to 2 s while the title catches up."""
        end = self.clock() + 2
        while True:
            showing = [p for p in self.panes() if p.shows(thread)]
            if showing or self.clock() >= end or self.deadline().limit(1) <= 0.3:
                return showing
            self.sleep(0.2)
            self._panes = None
            self._observed = False

    def line_thread(self, line):
        return line.get("thread_id") or (self.resolve(line["thread_prefix"]) if line.get("thread_prefix") else None)

    def line_names(self, line, thread):
        return self.line_thread(line) == thread

    def current(self, event, proposal):
        """The currency check: no newer record for the thread; the pane's latest line still names it."""
        thread, at = event["session_id"], instant(event["at"])
        ties = 0
        for row in self.store.rows("sessions/starts.jsonl", reverse=True):
            if instant(row["at"]) < at - timedelta(seconds=3):
                break
            if row.get("provider") != "codex" or row.get("session_id") != thread or row.get("source") in {"subagent", "cwd", "prompt"}:
                continue
            stamp = instant(row["at"])
            if stamp > at:
                return False
            if stamp == at:
                ties += 1
        if ties > 1:  # The hook's own record is the one allowed equal timestamp.
            return False
        newest, counts = {}, {}
        evidence = bool(proposal.get("pane"))
        for row in self.terminal_rows(reverse=True):
            if self.deadline().expired():
                raise TimeoutError("terminal currency deadline reached")
            stamp = instant(row["at"])
            # Append order can differ by at most one hook budget. Once a
            # matching latest answer is older than that window, it is final.
            if any(self.line_names(r, thread) and counts[p] == 1 and
                   stamp < instant(r["at"]) - timedelta(seconds=3) for p, r in newest.items()):
                return True
            pane = row.get("pane")
            evidence |= self.line_names(row, thread)
            previous = newest.get(pane)
            if previous is None or stamp > instant(previous["at"]):
                newest[pane], counts[pane] = row, 1
            elif stamp == instant(previous["at"]):
                counts[pane] += 1
        if not evidence:
            return True  # A lineage move without pane evidence uses the binding guard.
        return any(self.line_names(row, thread) and counts[pane] == 1 for pane, row in newest.items())
