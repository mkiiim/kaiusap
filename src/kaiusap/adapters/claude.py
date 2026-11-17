"""Claude's provider adapter: its ten provider obligations.

All process and tmux calls use an injectable runner, and inbox sockets an
injectable socket factory. The transcript reads are the bounded continued-in
lookup when a fork starts and `sessions`' context use, from the transcript's end.
"""
from datetime import datetime, timezone
import json
import os
import re
import time

from . import SPAWNED_AGENT_TEXT, UNIDENTIFIED_CLOSE, Identity, context_use, tail_records, tmux
from .shell import bash_verbs
from .daemon import linux_start_time, run_command, time_locale_env
from ..errors import Error
# The store, the model, the notice and pathlib are imported by the methods that use them, so the hook entry
# can ask this class for its events and its PreToolUse screen without importing the store (the
# spawned-agent screen is decided before any store work).

SOURCES = {"startup", "resume", "clear", "compact", "fork"}
# The Claude desktop app: its macOS bundle id, the note on its sessions, and the form of its own session ids,
# which its claude://code/continue link accepts.
DESKTOP_APP_ID = "com.anthropic.claudefordesktop"
DESKTOP_NOTE = "Open in the Claude desktop app"
DESKTOP_SESSION = re.compile(r"^local_[A-Za-z0-9-]{1,64}$")
ACTING = {"send", "reply", "iam", "release", "moot", "unpause"}
# The screen's refusal when it can't decide in time; claude.rs prints the same deny.
SCREEN_TIMEOUT_TEXT = ("kaiusap couldn't check this call in time, so it is refused. You may retry it: a spawned agent "
                       "may only look (whoami, who, inbox, read, help, pause), and those calls are allowed when they "
                       "get through.")
LISTING_ERROR = ("kaiusap can't tell which session is calling: claude agents --json failed; run it by hand to see why."
                 + UNIDENTIFIED_CLOSE)
NO_RECORD = ("kaiusap can't tell which session is calling (no current start record for this Claude process). "
             "Check that the kaiusap plugin is installed and finds kaiusap: kaiusap setup." + UNIDENTIFIED_CLOSE)
REASONS = {
    "listing_failed": "claude agents --json failed, printed something other than a JSON array, or exceeded its 3 s limit",
    "not_live": "no Claude process holds this session",
    "two_processes": "the Claude session is open in two terminals and neither is the process the current binding records",
    "no_socket": "no inbox socket is known for this Claude session: no start record matches the process holding it, its record has no socket, or the socket is gone",
    "detached": "the session is in a detached tmux session and wake_detached is off",
    "connect_failed": "the Claude session's inbox socket refused the connection or was gone",
    "timeout": "a time limit was reached before the notice was sent",
    "no_confirmation": "no confirmation after the notice was sent",
    "sandboxed": "the socket connection was refused with a permission error, probably by the session's sandbox; use the kaiusap tools instead",
}
PS = re.compile(r"^\s*(\w{3}\s+\w{3}\s+\d+\s+\d\d:\d\d:\d\d\s+\d{4})\s+(.*)$")
PANE_FORMAT = "\t".join(["#{pane_id}", "#{session_name}", "#{session_attached}", "#{pid}", "#{start_time}"])
COPY = "This conversation has no handle; if it is a copy of another one, the original keeps its handle."
# `setup`: its two items, at their places in setup's order.
SETUP_PLUGIN, SETUP_VERSION = 1, 5
MIN_CLAUDE = (2, 1, 224)
CLAUDE_VERSION_LIMIT = 5  # seconds for `claude --version`
INSTALL_MARKETPLACE = "claude plugin marketplace add {repo}"
INSTALL_PLUGIN = "claude plugin install kaiusap@kaiusap"
# `activity`: the record-only hooks, whose records the native part writes (claude.rs) and `activity` reads.
ACTIVITY_EVENTS = frozenset({"PermissionRequest", "Stop", "StopFailure", "Elicitation"})
ACTIVITY_FILE = "sessions/claude/activity.jsonl"
ASKS = {"PermissionRequest", "Elicitation"}
STOPS = {"Stop", "StopFailure"}
INTERRUPT = "[Request interrupted by user"
AGENT_ID = re.compile(r"[A-Za-z0-9_-]+")


def claude_version(text):
    """(major, minor, patch) from `claude --version`'s output (`2.1.318 (Claude Code)`), or None."""
    match = re.search(r"(\d+)\.(\d+)\.(\d+)", text or "")
    return tuple(int(part) for part in match.groups()) if match else None


def teammate(args):
    # Deliberately flattened ps text, including its documented ambiguity.
    for word in args.split():
        if word == "--":
            break
        if word == "--agent-id" or word.startswith("--agent-id="):
            return True
    return False


# `sessions`' warning for a resume of a session still running in a terminal (added 2026-11-10 18:09 EST).
COPY_NOTE = "Still open in another terminal: this starts a second copy of the session."

def app_bundle(command):
    """The outermost .app bundle a program path sits in, or None."""
    marker = command.find(".app/")
    return command[:marker + 4] if marker > 0 else None


def bundle_id(path):
    """A macOS app bundle's id from its Info.plist, or None."""
    import plistlib
    try:
        with open(os.path.join(path, "Contents", "Info.plist"), "rb") as f:
            value = plistlib.load(f).get("CFBundleIdentifier")
    except (OSError, ValueError, plistlib.InvalidFileException):
        return None
    return value if isinstance(value, str) and value else None


def transcript_marks(path):
    """`activity`'s reading of a transcript's end: (the newest dated line's time, (the newest top-level user
    line's time, that line) or None), or None when the transcript can't be read or holds no line. A compaction
    summary isn't a prompt: Claude writes it as a user line once compaction ends, with no turn after it."""
    from ..store import instant
    dated, seen = None, False
    for line in tail_records(path):
        seen = True
        try:
            at = instant(line["timestamp"])
        except (KeyError, TypeError, ValueError, AttributeError):
            continue
        dated = dated or at
        if line.get("type") == "user" and not line.get("isSidechain") and not line.get("isCompactSummary"):
            return dated, (at, line)
    return (dated, None) if seen else None


def interrupted(line):
    """A user line that is Claude's interrupt marker, `[Request interrupted by user...`, written for a rejection,
    an Esc or an interrupt, which fire no hook."""
    message = line.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    texts = [content] if isinstance(content, str) else [part.get("text") for part in content
             if isinstance(part, dict) and part.get("type") == "text"] if isinstance(content, list) else []
    return any(isinstance(text, str) and text.startswith(INTERRUPT) for text in texts)


class ClaudeAdapter:
    provider = "claude"
    delivers = True
    harness_maker = "anthropic"  # as `sessions` shows it
    session_variable = "CLAUDE_CODE_SESSION_ID"
    hook_events = frozenset({"SessionStart", "UserPromptSubmit", "PreToolUse"}) | ACTIVITY_EVENTS
    record_only_events = ACTIVITY_EVENTS  # written by the native part alone; `events` maps them to nothing
    start_events = frozenset({"SessionStart"})
    prompt_events = frozenset({"UserPromptSubmit"})
    screen_events = frozenset({"PreToolUse"})  # the spawned-agent tool-call screen
    setup_flags = frozenset()

    @classmethod
    def claims(cls, environment):
        """Whether a shell environment is one of this provider's sessions."""
        return bool(environment.get(cls.session_variable))

    # ----- `setup`: read-only ---------------------------------------------

    @classmethod
    def setup_command(cls, setup):
        return "claude"

    @classmethod
    def setup_items(cls, setup):
        """The Claude plugin and the Claude Code version, each with its place in setup's order."""
        return [(SETUP_PLUGIN, cls.setup_plugin(setup)), (SETUP_VERSION, cls.setup_version(setup))]

    @staticmethod
    def setup_plugin(setup):
        """Installed when `installed_plugins.json` lists kaiusap@kaiusap, `known_marketplaces.json` has the
        marketplace, and the user settings enable the plugin, as `claude plugin install` writes them (2.1.318)."""
        from pathlib import Path
        from ..setup import MARKETPLACE, PLUGIN_ID, read_json, short
        claude_dir = Path(setup.environ.get("CLAUDE_CONFIG_DIR") or setup.home / ".claude")
        plugins_dir = Path(setup.environ.get("CLAUDE_CODE_PLUGIN_CACHE_DIR") or claude_dir / "plugins")
        plugins = read_json(plugins_dir / "installed_plugins.json")
        entries = (plugins.get("plugins") or {}).get(PLUGIN_ID) if isinstance(plugins, dict) else None
        installed = bool(entries) if isinstance(entries, (list, dict)) else False
        marketplaces = read_json(plugins_dir / "known_marketplaces.json")
        known = isinstance(marketplaces, dict) and isinstance(marketplaces.get(MARKETPLACE), dict)
        settings = read_json(claude_dir / "settings.json")
        enabled = (settings.get("enabledPlugins") or {}).get(PLUGIN_ID) if isinstance(settings, dict) else None
        if installed and known and enabled is True:
            return setup.item("Claude plugin", True, f"{PLUGIN_ID} is installed in {short(claude_dir)}")
        problems, steps = [], []
        if not known:
            problems.append(f"the marketplace {MARKETPLACE} isn't added")
            steps.append(INSTALL_MARKETPLACE)
        if not installed:
            problems.append(f"{PLUGIN_ID} isn't installed")
            steps.append(INSTALL_PLUGIN)
        elif enabled is not True:
            problems.append(f"{PLUGIN_ID} isn't enabled in {short(claude_dir / 'settings.json')} (enabledPlugins)")
        return setup.item("Claude plugin", False, "; ".join(problems), setup.command(*steps) or "claude plugin enable kaiusap@kaiusap")

    @staticmethod
    def setup_version(setup):
        """`claude --version` 2.1.224 or later, 5 s."""
        import shutil
        import subprocess
        absent = setup.item("Claude Code version", False, "claude isn't on the PATH",
                            "Install Claude Code 2.1.224 or later (https://claude.ai/install.sh) and put claude on the PATH.")
        if not shutil.which("claude", path=setup.environ.get("PATH", "")):
            return absent
        try:
            code, out = setup.run(["claude", "--version"], CLAUDE_VERSION_LIMIT, setup.environ)
        except subprocess.TimeoutExpired:
            return setup.item("Claude Code version", False, f"claude --version didn't finish in {CLAUDE_VERSION_LIMIT} s", "claude update")
        except (OSError, ValueError):
            return absent
        version = claude_version(out) if code == 0 else None
        if version is None:
            return setup.item("Claude Code version", False, f"claude --version exited {code} without a version", "claude update")
        text = ".".join(map(str, version))
        if version < MIN_CLAUDE:
            return setup.item("Claude Code version", False, f"{text} is older than 2.1.224", "claude update")
        return setup.item("Claude Code version", True, text)

    def __init__(self, store, run=None, environ=None, clock=None, sleep=None, socket_factory=None):
        self.store = store
        self.run = run or run_command
        self.environ = os.environ if environ is None else environ
        self.clock = clock or time.monotonic
        self.sleep = sleep or time.sleep
        self.socket_factory = socket_factory
        self._identity_snapshot = False
        self.reset()

    def begin(self):
        """A CLI or MCP call starts: nothing is kept from an earlier call but the identity snapshot."""
        self.reset()

    def reset(self):
        # Identity and the verb share one listing for a single MCP call.
        if self._identity_snapshot:
            self._identity_snapshot = False
        else:
            self._listing = None
        self._processes = {}
        self._panes = None
        self._deadline = None

    def finish(self, catch_up=True):
        self._identity_snapshot = False
        self.reset()

    def limit(self, seconds, deadline=None):
        end = self._deadline if deadline is None else deadline
        limit = seconds if end is None else min(seconds, end - self.clock())
        if limit <= 0:
            raise TimeoutError()
        return limit

    def listing(self, refresh=False):
        import subprocess
        if self._listing is None or refresh:
            try:
                code, out = self.run(["claude", "agents", "--json"], self.limit(3), self.environ)
                rows = json.loads(out)
                if code or not isinstance(rows, list):
                    raise ValueError()
                self._listing = [r for r in rows if isinstance(r, dict)]
            except (OSError, subprocess.TimeoutExpired, ValueError):
                self._listing = False
        return self._listing

    def directory(self, identity):
        """An MCP caller's directory: the listing's `cwd` for the calling process, read in this call."""
        for row in self.listing() or []:
            if row.get("pid") == identity.pid and row.get("sessionId") == identity.session_id and row.get("cwd"):
                return row["cwd"]
        return None

    def process(self, pid, refresh=False):
        import subprocess
        if type(pid) is not int or pid <= 0:
            return None
        if pid not in self._processes or refresh:
            self._processes[pid] = None
            try:
                code, out = self.run(["ps", "-ww", "-o", "lstart=,args=", "-p", str(pid)], self.limit(2), time_locale_env())
                match = PS.match(out.strip()) if not code else None
                if match:
                    started = linux_start_time(pid, self.run)  # /proc on Linux, else `lstart`
                    if not started:
                        local = datetime.strptime(" ".join(match[1].split()), "%a %b %d %H:%M:%S %Y").astimezone()
                        started = local.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
                    self._processes[pid] = (started, match[2])
            except (OSError, subprocess.TimeoutExpired, ValueError):
                pass
        return self._processes[pid]

    def starts(self):
        return [r for r in self.store.rows("sessions/starts.jsonl")
                if r.get("provider") == "claude" and r.get("source") in SOURCES]

    def latest_process(self, pid, started):
        return self.store.latest("sessions/starts.jsonl", lambda r:
            r.get("provider") == "claude" and r.get("source") in SOURCES and
            r.get("pid") == pid and r.get("process_started_at") == started, started)

    def identity(self, environment, store):
        if environment.get("CODEX_THREAD_ID") and environment.get("CLAUDE_CODE_SESSION_ID"):
            raise Error("Both CODEX_THREAD_ID and CLAUDE_CODE_SESSION_ID are set, so kaiusap can't tell which session is calling. Run it from the session's own shell, or use the kaiusap tools.")
        try:
            pid = int(environment.get("CLAUDE_PID", ""))
        except (TypeError, ValueError):
            return Identity()
        sid = environment.get("CLAUDE_CODE_SESSION_ID")
        if not sid or pid <= 0:
            return Identity()
        process = self.process(pid, refresh=True)
        if not process:
            # The session's own variable, but its process isn't visible from here. Claude Code's Linux
            # sandbox runs Bash in its own process namespace, so this is a sandboxed shell, not a plain one.
            return Identity("claude", sid, f"claude:{sid}", environment.get("PWD") or os.getcwd(),
                            "sandboxed", pid=pid, reason="this shell can't see its Claude process")
        return Identity("claude", sid, f"claude:{sid}", environment.get("PWD") or os.getcwd(),
                        "root", pid=pid, process_started_at=process[0])

    def mcp_identity(self, meta=None, stated=None, cwd=None, parent_pid=None, deadline=None):
        """The ancestor rule; the identity argument (`stated`) is ignored."""
        import subprocess
        self.finish()
        self._deadline = deadline
        ancestors = []
        pid = os.getppid() if parent_pid is None else parent_pid
        for _ in range(3):
            if not pid or pid in ancestors:
                break
            ancestors.append(pid)
            try:
                code, out = self.run(["ps", "-o", "ppid=", "-p", str(pid)], self.limit(2), None)
                pid = int(out.strip()) if not code else None
            except (OSError, subprocess.TimeoutExpired, ValueError):
                break
        listing = self.listing(refresh=True)
        if listing is False:
            raise Error(LISTING_ERROR)
        missing = None
        for pid in ancestors:
            process = self.process(pid)
            rows = [r for r in listing if r.get("pid") == pid]
            record = self.latest_process(pid, process[0]) if process else None
            if record and (any(r.get("sessionId") == record["session_id"] for r in rows) or
                           (not rows and record.get("teammate"))):
                self._identity_snapshot = True
                return Identity("claude", record["session_id"], record["session_key"], cwd or record.get("cwd"),
                                "root", pid=pid, process_started_at=process[0])
            if record is None and missing is None:
                missing = next((r for r in rows if r.get("sessionId")), None)
        if missing:
            raise Error("kaiusap can't tell which session is calling: this session started before kaiusap's start hook ran; "
                        f"quit and run claude --resume {missing['sessionId']} so it runs (for a background session, "
                        f"claude stop {missing.get('id', '<job>')} first), and check kaiusap setup." + UNIDENTIFIED_CLOSE)
        raise Error(NO_RECORD)

    def hook_identity(self, payload):
        sid = payload.get("session_id")
        if not isinstance(sid, str) or not sid:
            return None
        agent = payload.get("agent_id")
        return Identity("claude", sid, f"claude:{sid}" + (f"/{agent}" if agent else ""), payload.get("cwd"),
                        "spawned" if agent else "root", f"claude:{sid}" if agent else None, agent_id=agent)

    def events(self, payload, deadline=None):
        from ..notice import NOTICE
        from ..store import now
        at = now()  # entry time, before any process or tmux read
        entered = self.clock()
        identity = self.hook_identity(payload)
        if identity is None or identity.agent_id:
            return None
        event_name = payload.get("hook_event_name")
        if event_name not in {"SessionStart", "UserPromptSubmit"}:
            return None  # PreToolUse is a screen; the activity hooks' records are the native part's alone
        self._deadline = deadline
        event = {"provider": "claude", "session_id": identity.session_id, "session_key": identity.session_key,
                 "identity": identity, "cwd": payload.get("cwd"), "at": at, "entered": entered,
                 "transcript": payload.get("transcript_path") or None, "session_title": payload.get("session_title"),
                 "prompt": payload.get("prompt"), "record": None, "file": None, "prompt_record": None,
                 "hook_event_name": event_name}
        if event_name == "UserPromptSubmit":
            event["kind"] = "prompt"
            prompt = event["prompt"]
            if not NOTICE.search(prompt if isinstance(prompt, str) else ""):
                # A prompt holding a kaiusap notice anywhere (Claude may wrap a post) writes no
                # record, so a woken session keeps its chain (amended 2026-11-06).
                event["prompt_record"] = {"at": at, "provider": "claude", "session_id": identity.session_id,
                                          "session_key": identity.session_key, "source": "prompt"}
            index = self.index(identity.session_id)
            if index is None or index["transcript"] != event["transcript"]:
                event["record"] = {k: event[k] for k in ("provider", "session_id", "session_key", "cwd", "at", "transcript")}
                event["record"]["source"] = "cwd"
                event["file"] = "sessions/starts.jsonl"
            return event
        import subprocess
        source = payload.get("source")
        if not isinstance(source, str) or source not in SOURCES:
            # An unrecognised source: identified, so the hook gives the session line, but no record.
            event.update(kind="unrecognised", source=source)
            return event
        try:
            pid = int(self.environ.get("CLAUDE_PID", ""))
        except (ValueError, TypeError):
            pid = None
        process = self.process(pid, refresh=True)
        event.update(kind=source, source=source, pid=pid, process_started_at=process[0] if process else None,
                     socket=self.environ.get("CLAUDE_CODE_MESSAGING_SOCKET") or None,
                     teammate=teammate(process[1]) if process else False, tmux_pane=None, tmux_session=None,
                     # Where the session was started: "cli" in a terminal, "claude-desktop" in the Claude desktop
                     # app, which also gives its own id for the session and its version.
                     entrypoint=self.environ.get("CLAUDE_CODE_ENTRYPOINT") or None,
                     host_session_id=self.environ.get("CLAUDE_CODE_HOST_SESSION_ID") or None,
                     host_version=self.environ.get("CLAUDE_CODE_DESKTOP_APP_VERSION") or None)
        if self.environ.get("TMUX_PANE") and self.environ.get("TMUX"):
            try:
                code, out = self.run(["tmux", "-L", "default", "display-message", "-p", "-t", self.environ["TMUX_PANE"],
                                      "#{pid} #{start_time} #{session_name}"], self.limit(2), None)
                server, started, session = out.strip().split(" ", 2)
                if code or server != self.environ["TMUX"].rsplit(",", 2)[1]:
                    raise ValueError("non-default tmux server or failed display-message")
                event.update(tmux_pane=f"{server}@{started}:{self.environ['TMUX_PANE']}", tmux_session=session)
            except (OSError, subprocess.TimeoutExpired, ValueError, IndexError) as exc:
                self.store.log(f"Claude start pane unavailable: {exc}")
        event["record"] = {k: event[k] for k in ("provider", "session_id", "session_key", "source", "cwd", "at", "pid",
                                                       "process_started_at", "socket", "teammate", "tmux_pane", "transcript",
                                                       "entrypoint", "host_session_id", "host_version")}
        event["file"] = "sessions/starts.jsonl"
        return event

    def hook_deadline(self, at):
        """The deadline this adapter's own calls use in a hook: the hook's total, as a clock reading."""
        return at

    def hook_record(self, name, event, deadline, recorded=False, native=None):
        """Append the hook's records. The prompt record is native's fixed-shape append: already
        written when `recorded`, else by `native()` when the native entry is there. The cwd line stays here."""
        if event.get("prompt_record") and (recorded or native):
            if not recorded:
                native()
            event["prompt_record"] = None
        self.record(event, deadline)

    def record(self, event, deadline=None):
        if event and event.get("prompt_record"):
            self.store.append("sessions/prompts.jsonl", event["prompt_record"], deadline)
        if event and event.get("record"):
            self.store.start(event["record"], deadline, then=self.update_index)

    # ----- the transcript index, sessions/claude/last/<session id>.json -----

    def index_path(self, session_id):
        from pathlib import Path
        if not session_id or Path(session_id).name != session_id or session_id in {".", ".."}:
            raise Error("Invalid session id for transcript index.")
        return self.store.adapter_dir(self.provider) / "last" / (session_id + ".json")

    def index(self, session_id):
        from ..store import instant
        try:
            value = json.loads(self.index_path(session_id).read_text())
            instant(value["at"])
            value["cwd"], value["transcript"]
            return value
        except (OSError, ValueError, KeyError, TypeError):
            return None

    def update_index(self, record):
        """Called under the starts.jsonl append lock, right after the record's append."""
        from ..store import instant
        path = self.index_path(record["session_id"])
        previous = self.index(record["session_id"])
        if previous and instant(previous["at"]) >= instant(record["at"]):
            return
        self.store.write_json(path, {k: record[k] for k in ("at", "cwd", "transcript")})

    def rebuild_index(self, session_id, deadline=None):
        record = self.store.latest("sessions/starts.jsonl", lambda r:
                                   r.get("provider") == "claude" and r.get("session_id") == session_id)
        if record:
            with self.store.locked("sessions/starts.jsonl", deadline):
                self.update_index(record)
            return self.index(session_id)
        return None

    def panes(self):
        import subprocess
        if self._panes is None:
            self._panes = {}
            try:
                code, out = self.run(["tmux", "-L", "default", "list-panes", "-a", "-F", PANE_FORMAT], self.limit(2), None)
                if not code:
                    for line in out.splitlines():
                        fields = line.split("\t")
                        if len(fields) == 5:
                            pane, session, attached, pid, started = fields
                            key = f"{pid}@{started}:{pane}"
                            self._panes[key] = {"pane": key, "tmux_session": session, "attached": attached not in {"", "0"}, "trusted": None}
            except (OSError, subprocess.TimeoutExpired, ValueError):
                pass
        return self._panes

    def unlisted(self, listing):
        listed_pids = {r.get("pid") for r in listing}
        latest = {}
        for row in self.starts():
            pid, started = row.get("pid"), row.get("process_started_at")
            key = (pid, started)
            if pid not in listed_pids and (key not in latest or row["at"] > latest[key]["at"]):
                latest[key] = row
        result = []
        for (pid, started), row in latest.items():
            if row.get("teammate") and (process := self.process(pid)) and process[0] == started:
                result.append(row)
        return result

    def status(self, session_key):
        from pathlib import Path
        from ..model import handles, load_settings
        sid = session_key.split(":", 1)[1].split("/", 1)[0]
        listing = self.listing()
        result = {"reason_code": None, "record": None, "entry": None, "detached": False, "panes": []}
        if listing is False:
            return {**result, "reason_code": "listing_failed"}
        entries = [r for r in listing if r.get("sessionId") == sid and type(r.get("pid")) is int]
        records = {}
        if not entries:
            fallback = [r for r in self.unlisted(listing) if r["session_id"] == sid]
            entries = [{"sessionId": sid, "pid": r["pid"], "cwd": r.get("cwd"), "kind": "interactive"} for r in fallback]
            records = {r["pid"]: r for r in fallback}
        entries = list({r["pid"]: r for r in entries}.values())
        if not entries:
            return {**result, "reason_code": "not_live"}
        binding = next((b for b in handles(self.store).values() if b and b["session_key"] == session_key), None)
        if len(entries) > 1:
            entries = [r for r in entries if binding and r["pid"] == binding.get("pid")
                       and (process := self.process(r["pid"]))
                       and process[0] == binding.get("process_started_at")]
            if not entries:
                return {**result, "reason_code": "two_processes"}
        entry = entries[0]
        pid = entry["pid"]
        process = self.process(pid)
        if process and pid not in records:
            record = self.store.latest("sessions/starts.jsonl", lambda r:
                r.get("provider") == "claude" and r.get("source") in SOURCES and r.get("session_id") == sid and
                r.get("pid") == pid and r.get("process_started_at") == process[0], process[0])
        else:
            record = records.get(pid)
        result.update(entry=entry, record=record)
        if entry.get("kind") != "background" and record and record.get("tmux_pane"):
            pane = self.panes().get(record["tmux_pane"])
            if pane:
                result["panes"] = [pane]
                result["detached"] = not pane["attached"]
        if not record or not record.get("socket") or not Path(record["socket"]).exists():
            return {**result, "reason_code": "no_socket"}
        if result["detached"] and not load_settings(self.store)["wake_detached"]:
            result["reason_code"] = "detached"
        return result

    def liveness(self, session_key):
        from ..model import handles, held
        status = self.status(session_key)
        code, sid = status["reason_code"], session_key.split(":", 1)[1]
        fixes = {"listing_failed": "run `claude agents --json` by hand to see why", "not_live": "resume it",
                 "two_processes": f"say 'you are {held(handles(self.store), session_key) or '<handle>'}' in the terminal that should receive mail",
                 "detached": "attach it, or set wake_detached true"}
        job = (status.get("entry") or {}).get("id", "<job>")
        fixes["no_socket"] = ("this session started before its start hook could record the socket, or its inbox was bound later; "
                              f"quit and run `claude --resume {sid}` so the start hook runs (for a background session, "
                              f"`claude stop {job}` first), and check `kaiusap setup`")
        return {"can_be_woken": code is None, "reason_code": code, "why_not": REASONS.get(code),
                "fix": fixes.get(code), "detached": status["detached"],
                "live": code != "not_live"}

    def unidentified(self, identity):
        """A Claude caller is identified or refused, never "unknown"; were one passed, it isn't reachable."""
        return {"can_be_woken": False, "why_not": identity.reason, "reason_code": "unknown_caller", "fix": None}

    def deliver(self, store, message, deadline=None):
        from ..notice import notice
        import subprocess
        self._deadline = min(deadline, self.clock() + 20) if deadline is not None else self.clock() + 20
        sent = [0]
        try:
            try:
                status = self.status(message["to"]["session_key"])
            except (OSError, ValueError) as exc:
                store.warn(exc)
                return {"outcome": "stored", "reason_code": "store_failed",
                        "reason": "a store failure interrupted the send after the attempt line was written and before the notice was sent",
                        "provider_version": None, "warnings": [], "store_failed_before_handover": True}
            self.limit(20)
            if status["reason_code"]:
                return self.outcome("stored", status["reason_code"])
            frame = {"type": "user", "message": {"role": "user", "content": notice(message)},
                     "session_id": message["to"]["session_id"]}
            self.post_line(status["record"]["socket"], frame, sent)
            return self.outcome("delivered")
        except (TimeoutError, subprocess.TimeoutExpired):
            return self.outcome("uncertain", "no_confirmation") if sent[0] else self.outcome("stored", "timeout")
        except PermissionError:
            return self.outcome("uncertain", "no_confirmation") if sent[0] else self.outcome("stored", "sandboxed")
        except OSError:
            return self.outcome("uncertain", "no_confirmation") if sent[0] else self.outcome("stored", "connect_failed")

    def post_line(self, path, frame, sent):
        """Write one frame as one line to an inbox socket, with no auth line, and close it: connecting
        2 s, writing the line and closing 5 s, each capped by the time left. `sent[0]` counts the bytes
        written, so the caller can tell a failure before any of the line from one after part of it."""
        import socket
        data = (json.dumps(frame, ensure_ascii=False, separators=(",", ":")) + "\n").encode()
        connection = (self.socket_factory or socket.socket)(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            connection.settimeout(self.limit(2))
            connection.connect(path)
            end = self.clock() + self.limit(5)
            while sent[0] < len(data):
                connection.settimeout(self.limit(5, end))
                count = connection.send(data[sent[0]:])
                if count <= 0:
                    raise OSError("inbox closed while writing")
                sent[0] += count
            self.limit(5, end)
            connection.close()
            connection = None
            self.limit(5, end)
        finally:
            if connection is not None:
                try:
                    connection.close()
                except OSError:
                    pass

    def rename(self, path, session_id, name):
        """The inbox `rename` control line, sent the way a notice is. True only when the whole
        line was written and the connection closed cleanly; nothing confirms that Claude applied it."""
        import subprocess
        frame = {"type": "control", "action": "rename", "name": name, "session_id": session_id}
        try:
            self.post_line(path, frame, [0])
            return True
        except (OSError, TimeoutError, subprocess.TimeoutExpired) as exc:  # PermissionError: a sandbox
            self.store.log(f"Claude rename to {name} not sent on the inbox socket: {type(exc).__name__}: {exc}")
            return False

    @staticmethod
    def outcome(outcome, code=None):
        return {"outcome": outcome, "reason_code": code, "reason": REASONS.get(code), "provider_version": None, "warnings": []}

    def where(self, session_key):
        from ..model import last_directory
        import shlex
        sid = session_key.split(":", 1)[1]
        status = self.status(session_key)
        entry, record = status["entry"] or {}, status["record"] or {}
        panes = list(status["panes"])
        # Discovery includes every process holding the conversation; a binding
        # selects the one receiving mail, not which terminals sessions shows.
        listing = self.listing()
        entries = [] if listing is False else [r for r in listing if r.get("sessionId") == sid and type(r.get("pid")) is int]
        for candidate in entries:
            if candidate.get("kind") == "background":
                continue
            process = self.process(candidate["pid"])
            latest = self.latest_process(candidate["pid"], process[0]) if process else None
            if latest and latest["session_key"] == session_key and latest.get("tmux_pane"):
                pane = self.panes().get(latest["tmux_pane"])
                if pane and pane not in panes:
                    panes.append(pane)
        if not entry:
            entry = next(iter(entries), {})
        where = "unknown" if status["reason_code"] == "listing_failed" else (
            "background" if entry.get("kind") == "background" else "tmux" if panes else "not_in_tmux" if entry else "not_running")
        cwd = entry.get("cwd") or record.get("cwd") or last_directory(self.store, session_key)
        hosts = [] if where in {"unknown", "background"} else self.hosts(sid, record if entry else None, entry.get("pid"))
        if hosts:
            # A terminal resume would take the session out of the app that hosts it.
            return {"cwd": cwd, "name": entry.get("name"), "where": "hosted" if entry else "not_running",
                    "tmux_panes": panes, "hosts": hosts, "command": None, "trusted": None,
                    "detached": status["detached"], "note": DESKTOP_NOTE}
        resume = "claude --resume " + shlex.quote(sid)
        command = tmux.attach_command(tmux.attach_pane(panes)["tmux_session"]) if panes else (
            (f"cd {shlex.quote(cwd)} && {resume}" if cwd else resume) if where in {"not_running", "not_in_tmux"} else
            "claude attach " + shlex.quote(entry["id"]) if where == "background" and entry.get("id") else None)
        return {"cwd": cwd,
                "name": entry.get("name"), "where": where, "tmux_panes": panes, "hosts": [], "command": command,
                "trusted": None, "detached": status["detached"], "note": COPY_NOTE if where == "not_in_tmux" else None}

    def hosts(self, sid, record, pid):
        """The apps hosting a session: one entry when its newest start record says the Claude desktop app started
        it, else none. `record` is the live process's start record, or None for a session not running, whose
        newest start record counts; `pid` the live session's process, from which the app's own is found."""
        import sys
        if record is None:
            record = self.store.latest("sessions/starts.jsonl", lambda r:
                r.get("provider") == "claude" and r.get("source") in SOURCES and r.get("session_id") == sid)
        if not record or record.get("entrypoint") != "claude-desktop":
            return []
        app = self.app_process(pid) if pid else None
        app_pid, path = app if app else (None, None)
        app_id = (bundle_id(path) if path else None) or (DESKTOP_APP_ID if sys.platform == "darwin" else None)
        host_id = record.get("host_session_id")
        link = (f"claude://code/continue?session={host_id}&source=desktop_action"
                if app_id and isinstance(host_id, str) and DESKTOP_SESSION.match(host_id) else None)
        return [{"kind": "app", "app": "claude-desktop", "name": "Claude", "version": record.get("host_version"),
                 "app_id": app_id, "path": path, "pid": app_pid, "session_id": host_id, "link": link,
                 "link_opens": "session" if link else None, "window": None, "archived": None}]

    def app_process(self, pid):
        """The app a session's process runs under: (its pid, its bundle's path), or None. Walks up from the
        session's parent to the first process inside an app bundle, then on up through that bundle's own
        processes (a helper's parent is the app), from one `ps` call. The session's own process is skipped:
        Claude's binary sits in a bundle of its own."""
        import subprocess
        try:
            code, out = self.run(["ps", "-A", "-o", "pid=,ppid=,comm="], self.limit(2), None)
        except (OSError, subprocess.TimeoutExpired):
            return None
        if code:
            return None
        rows = {}
        for line in out.splitlines():
            parts = line.strip().split(None, 2)
            if len(parts) == 3 and parts[0].isdigit() and parts[1].isdigit():
                rows[int(parts[0])] = (int(parts[1]), parts[2])
        found, current = None, rows.get(pid, (None,))[0]
        for _ in range(64):
            if current is None or current <= 1 or current not in rows:
                break
            parent, command = rows[current]
            bundle = app_bundle(command)
            if found and bundle != found[1]:
                break
            if bundle:
                found = (current, bundle)
            current = parent
        return found

    def list(self):
        from ..model import handles
        listing = self.listing()
        keys = []
        if listing is False:
            self.store.warnings.append("listing_failed: " + REASONS["listing_failed"])
        else:
            keys.extend(f"claude:{r['sessionId']}" for r in listing if r.get("sessionId") and type(r.get("pid")) is int)
            keys.extend(r["session_key"] for r in self.unlisted(listing))
        keys.extend(b["session_key"] for b in handles(self.store).values() if b and b["provider"] == "claude")
        return [{"provider": "claude", "session_id": key.split(":", 1)[1], "session_key": key, **self.where(key)}
                for key in dict.fromkeys(keys)]

    def tell(self, text, event="UserPromptSubmit"):
        return {"hookSpecificOutput": {"hookEventName": event, "additionalContext": text}}

    def binding_record(self, session_id, pid, started):
        """The latest start record of the process (pid and start time) for the session, or None."""
        if not pid or not started:
            return None
        return self.store.latest("sessions/starts.jsonl", lambda r:
            r.get("provider") == "claude" and r.get("source") in SOURCES and r.get("session_id") == session_id and
            r.get("pid") == pid and r.get("process_started_at") == started, started)

    def mirror(self, binding):
        """At `iam`, rename the session at once with the inbox `rename` line, to the socket in the binding's start
        record; a line written in full is the `named` event, so the hooks don't apply it again. Then
        the tmux session rename for the name just applied and the window name for the record's pane. A line not written leaves the name to the
        hooks. Hook moves are named by the hooks' `sessionTitle` (mirror_output), so they do nothing here."""
        if binding.get("reason") != "iam" or not binding.get("session_key"):
            return []
        warnings = []
        try:
            record = self.binding_record(binding["session_id"], binding.get("pid"), binding.get("process_started_at"))
        except OSError as exc:
            self.store.log(f"Claude rename at iam skipped: the start records couldn't be read: {exc}")
            return []
        if record is None:
            return []
        handle, pane = binding["handle"], record.get("tmux_pane")
        if record.get("socket") and self.rename(record["socket"], binding["session_id"], handle):
            try:
                self.named(binding, handle, "handle", binding)
            except OSError as exc:
                self.store.warn(exc)
            warnings.extend(self.follow_session_name(pane, handle, record.get("pid")))
        self.name_window(pane, record.get("pid"))
        return warnings

    def retire(self, binding, left_at, cause=None):
        """At once only when `iam` or `release` ends the binding and the listing shows its session held by a
        running process named the handle: the `rename` line with the retirement name, to that process's
        socket; written in full, the `retired` event. Otherwise the hooks retire it, as before."""
        from ..model import handles, held, retirement_name
        import subprocess
        if cause not in {"iam", "release"}:
            return []
        try:
            if held(handles(self.store), binding["session_key"]):
                return []  # it holds another handle now: the mirror wins, at its next hook
            listing = self.listing()
            if listing is False:
                return []
            sid = binding["session_id"]
            entries = [r for r in listing if r.get("sessionId") == sid and type(r.get("pid")) is int]
            if len(entries) > 1:
                entries = [r for r in entries if r["pid"] == binding.get("pid")]
            if len(entries) != 1 or entries[0].get("name") != binding["handle"]:
                return []
            entry = entries[0]
            process = self.process(entry["pid"])
            record = self.binding_record(sid, entry["pid"], process[0]) if process else None
            if not record or not record.get("socket"):
                return []
            name = retirement_name(binding["handle"], left_at)
            if not self.rename(record["socket"], sid, name):
                return []
            try:
                self.named(binding, name, "retired", binding)
            except OSError as exc:
                self.store.warn(exc)
            # A session the listing marks background ignores its record's pane.
            return self.follow_session_name(record.get("tmux_pane") if entry.get("kind") != "background" else None, name,
                                            entry["pid"])
        except (OSError, ValueError, TimeoutError, subprocess.TimeoutExpired) as exc:
            self.store.log(f"Claude retirement at once skipped: {type(exc).__name__}: {exc}")
            return []

    def holds(self, pid):
        """The pane check for a Claude process: a function of the pane's process id, true only when that
        process is `pid` or one of its ancestors, from one `ps -A -o pid=,ppid=` call. A start record's pane
        comes from TMUX_PANE, which a background worker can inherit from the shell that dispatched it.
        With no pid, no pane holds it."""
        def check(pane_pid):
            if type(pid) is not int or pid <= 0:
                return False
            code, out = self.run(["ps", "-A", "-o", "pid=,ppid="], self.limit(2), None)
            if code:
                raise ValueError(f"ps exited {code}")
            parents = {}
            for line in out.splitlines():
                parts = line.split()
                if len(parts) == 2 and parts[0].isdigit() and parts[1].isdigit():
                    parents[int(parts[0])] = int(parts[1])
            target, current = int(pane_pid), pid
            for _ in range(64):
                if current == target:
                    return True
                if current not in parents or current <= 1:
                    return False
                current = parents[current]
            return False
        return check

    def name_window(self, pane, pid):
        """The window name for the pane in a start record, when the pane holds the process `pid`; never fails anything."""
        if pane:
            tmux.name_window(self.run, self.limit, self.store.log, pane, self.holds(pid))

    def follow_session_name(self, pane, name, pid):
        """With `tmux_session_names` "follow", rename the pane's single-pane tmux session to the name just
        applied, when the pane holds the process `pid`. Returns `iam`'s collision warning, if any."""
        from ..model import load_settings
        if not pane or load_settings(self.store)["tmux_session_names"] != "follow":
            return []
        warning = tmux.rename_session(self.run, self.limit, self.store.log, pane, name, self.holds(pid))
        return [warning] if warning else []

    # The usage fields whose sum is the context a reply was given.
    CONTEXT_FIELDS = ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")

    def harness(self, session_key):
        """`sessions`' harness_maker: who made this provider's harness, whatever model it runs."""
        return self.harness_maker

    def activity(self, session_key):
        """What a live session is doing, from its activity records since its process started and from the
        transcripts. `blocked` while an asker's transcript (the session's, or a subagent's beside it) has no dated
        line newer than its newest ask; `error` when the newest record is a StopFailure with no user line after it;
        `working` when the newest user line, not an interrupt, is newer than the newest Stop or StopFailure and the
        process start; else `idle`. None for a spawned agent's key, a session that isn't live, no record since the
        process started (its hooks aren't loaded, or no turn has ended yet), or a transcript that can't be read."""
        from pathlib import Path
        from ..store import instant
        from . import activity_reading
        if not session_key or "/" in session_key:
            return None
        sid = session_key.split(":", 1)[-1]
        status = self.status(f"claude:{sid}")
        if status["reason_code"] in {"not_live", "listing_failed", "two_processes"} or not status.get("entry"):
            return None
        process = self.process(status["entry"].get("pid"))
        if not process or not process[0]:
            return None
        started = instant(process[0])
        record = status.get("record") if (status.get("record") or {}).get("transcript") else self.store.latest(
            "sessions/starts.jsonl", lambda r: r.get("provider") == "claude" and r.get("session_id") == sid
            and isinstance(r.get("transcript"), str))
        transcript = (record or {}).get("transcript")
        if not isinstance(transcript, str) or not transcript:
            return None
        asks, stop, newest = {}, None, None
        for row in self.store.rows(ACTIVITY_FILE, reverse=True):
            try:
                at = instant(row["at"])
            except (KeyError, TypeError, ValueError, AttributeError):
                continue
            if at < started:
                break
            if row.get("session_id") != sid:
                continue
            newest = newest or (row, at)
            if row.get("kind") in ASKS:
                asks.setdefault(row.get("agent_id"), (row, at))
            elif row.get("kind") in STOPS and row.get("agent_id") is None and stop is None:
                stop = (row, at)
        if newest is None:
            return None
        main = transcript_marks(transcript)
        if main is None:
            return None
        waits = set()
        for agent, (row, at) in asks.items():
            if agent is None:
                marks = main
            elif isinstance(agent, str) and AGENT_ID.fullmatch(agent):
                marks = transcript_marks(str(Path(transcript).with_suffix("")) + f"/subagents/agent-{agent}.jsonl")
            else:
                return None
            if marks is None:
                return None
            if marks[0] is None or marks[0] <= at:
                waits.add("user_input" if row.get("kind") == "Elicitation" or row.get("tool") == "AskUserQuestion"
                          else "approval")
        if waits:
            return activity_reading("blocked", waits)
        user = main[1]
        if newest[0].get("kind") == "StopFailure" and (user is None or user[0] <= newest[1]):
            return activity_reading("error")
        if user and user[0] > max(started, stop[1] if stop else started) and not interrupted(user[1]):
            return activity_reading("working")
        return activity_reading("idle")

    def harness_version(self, session_key):
        """Claude Code's version running a live session, from the `version` on its transcript's newest line
        that has one: the running process writes it, so after an upgrade it is the old binary's until the session
        restarts. None when the session isn't live, or no line names a version."""
        session_id = (session_key or "").split(":", 1)[-1].split("/", 1)[0]
        if not session_id or not self.liveness(f"claude:{session_id}").get("live") or "/" in (session_key or ""):
            return None
        record = self.store.latest("sessions/starts.jsonl", lambda r: r.get("provider") == "claude"
                                   and r.get("session_id") == session_id and isinstance(r.get("transcript"), str))
        if not record:
            return None
        for line in tail_records(record["transcript"]):
            version = line.get("version")
            if isinstance(version, str) and version:
                return version
        return None

    def context(self, session_key):
        """The context use of the session's latest reply, from the end of the transcript its latest start
        record names: the sum of CONTEXT_FIELDS in that reply's `message.usage`. The transcript has no limit,
        so `limit` is null; none is inferred from the model. None when no reply with usage is found."""
        session_id = (session_key or "").split(":", 1)[-1].split("/", 1)[0]
        record = self.store.latest("sessions/starts.jsonl", lambda r: r.get("provider") == "claude"
                                   and r.get("session_id") == session_id and isinstance(r.get("transcript"), str))
        if not record:
            return None
        for line in tail_records(record["transcript"]):
            message = line.get("message")
            if line.get("type") != "assistant" or line.get("isSidechain") or not isinstance(message, dict):
                continue
            usage = message.get("usage")
            if message.get("model") == "<synthetic>" or not isinstance(usage, dict):
                continue
            counts = [usage.get(field, 0) for field in self.CONTEXT_FIELDS]
            if all(type(count) is int and count >= 0 for count in counts) and sum(counts):
                return context_use(sum(counts), None, message.get("model"), line.get("timestamp"))
        return None

    def tmux_start(self, event):
        """The start hook's window name write, on every source but `fork`, for the pane in its start record."""
        if event.get("kind") in SOURCES - {"fork"}:
            self.name_window(event.get("tmux_pane"), event.get("pid"))

    def hook_session_name(self, event, name):
        """The tmux session rename from a hook that applies `name`: the pane in the start record (the start hook's own, or the
        session's latest). In the prompt hook it starts only while the hook's budget allows new work;
        otherwise the next start hook renames it."""
        from ..model import load_settings
        try:
            if load_settings(self.store)["tmux_session_names"] != "follow":
                return
            if self.store.hook_budget:
                self.store.hook_budget.check()
            if "tmux_pane" in event:
                pane, pid = event["tmux_pane"], event.get("pid")
            else:
                record = self.store.latest("sessions/starts.jsonl", lambda r: r.get("provider") == "claude" and
                                           r.get("source") in SOURCES and r.get("session_id") == event["session_id"])
                pane, pid = (record.get("tmux_pane"), record.get("pid")) if record else (None, None)
            self.follow_session_name(pane, name, pid)
        except (OSError, TimeoutError) as exc:
            self.store.log(f"tmux session name for claude:{event.get('session_id')} skipped: {type(exc).__name__}: {exc}")

    def named(self, binding, name, kind, event, retry=False):
        from contextlib import nullcontext
        budget = self.store.hook_budget
        with budget.operation() if budget else nullcontext():
            self.store.event("named", handle=binding["handle"], session_id=binding["session_id"], session_key=binding["session_key"],
                             binding=binding["id"], name=name, kind=kind, retry=retry,
                             by_session=event["session_id"], by_session_key=event["session_key"], deadline=event.get("deadline"))
            if budget and name is not None:
                # The name is committed with its event: the hook's output carries it even past the deadline.
                budget.output.setdefault("hookSpecificOutput", {}).update(
                    hookEventName=event.get("hook_event_name", "UserPromptSubmit"), sessionTitle=name)

    def hook_output(self, event, state):
        """The hook's output for this event : the name it sets through `sessionTitle`,
        decided from the hook state's bindings and name events."""
        event["history"] = (state.data["ended"], state.data["bindings"])
        event["naming_events"] = state.data["events"]
        return self.mirror_output(event)

    def marker_fields(self, state, event):
        """The marker's Claude fields, read back by `claude.rs`'s `clean`: the title rule
        for the next prompt, and the fingerprint of the transcript index this hook verified."""
        title_mode, title = "any", None
        events, binding = state.data["events"], state.binding
        if binding:
            named = [e for e in events if e.get("event") == "named" and e.get("binding") == binding["id"] and e.get("kind") == "handle"]
            if not named:
                title_mode = "work"
            elif not any(e.get("retry") or e.get("name") is None for e in named):
                title_mode, title = "equals", state.handle
        retired = {e.get("binding") for e in events if e.get("event") == "named" and e.get("kind") == "retired"}
        if any(b["id"] not in retired for b, _ in state.data["ended"]):
            title_mode = "work"
        index_signature = None
        try:
            st = self.index_path(event["session_id"]).stat()
            candidate = f"{st.st_ino}:{st.st_size}:{st.st_mtime_ns}"
            index = self.index(event["session_id"])
            if index is not None and index["transcript"] == event.get("transcript"):
                index_signature = candidate
        except OSError:
            pass
        return {"index_signature": index_signature, "title_mode": title_mode, "title": title}

    def mirror_output(self, event):
        """Record the title decision before returning output; the only call it makes is the tmux session
        rename for the name it applies, or at start for a current name equal to the handle's."""
        from ..model import binding_history, held, retirement_name
        if event.get("identity") and event["identity"].agent_id:
            return {}
        if event.get("kind") not in {"startup", "resume", "fork", "prompt"}:
            return {}
        ended, bindings = event.get("history") or binding_history(self.store)
        key, title = event["session_key"], event.get("session_title")
        ended = [(binding, at) for binding, at in ended if binding["session_key"] == key]
        handle = held(bindings, key)
        output = None
        if handle:
            binding = bindings[handle]
            named = [e for e in (event["naming_events"] if "naming_events" in event else self.store.events(binding["at"])) if e.get("event") == "named" and
                     e.get("binding") == binding["id"] and e.get("kind") == "handle"]
            if not named:
                allowed = binding["reason"] != "fresh-start" or title is None or title == handle
                output = handle if allowed else None
                self.named(binding, output, "handle", event)
            elif event["kind"] == "prompt" and not any(e.get("retry") for e in named):
                if title is None and not any(e.get("name") is None for e in named):
                    output = handle
                    self.named(binding, output, "handle", event, retry=True)
                elif title is not None and title != handle:
                    # A user rename spends the retry without applying a title.
                    # A subsequent cleared name must remain cleared.
                    self.named(binding, None, "handle", event, retry=True)
        retirement_events = event.get("naming_events")
        if retirement_events is None:
            retirement_events = self.store.events(min(b["at"] for b, _ in ended)) if ended else []
        done = {(e.get("binding"), e.get("kind")) for e in retirement_events if e.get("event") == "named"}
        for binding, left_at in ended:
            if binding["session_key"] != key or (binding["id"], "retired") in done:
                continue
            retired = retirement_name(binding['handle'], left_at)
            name = retired if not handle and title == binding["handle"] else None
            self.named(binding, name, "retired", event)
            if name:
                output = name
        if output is None and event["kind"] in {"startup", "resume"} and handle and title == handle:
            follow = handle  # at start: the current name is the handle's
        else:
            follow = output
        if follow is not None:
            self.hook_session_name(event, follow)
        return {"hookSpecificOutput": {"hookEventName": event.get("hook_event_name", "UserPromptSubmit" if event["kind"] == "prompt" else "SessionStart"),
                                        "sessionTitle": output}} if output is not None else {}

    def refuse_before_acting(self, identity):
        if identity.classification == "spawned":
            raise Error(SPAWNED_AGENT_TEXT)

    @classmethod
    def pre_tool(cls, payload):
        """The spawned-agent screen at the hook entry, before any store work and without an adapter: None for
        a call with no `agent_id` or a Bash command without `kaiusap`; otherwise the deny, or `{}` to allow."""
        if not cls.screened(payload):
            return None
        return cls.pre_tool_use(None, payload)

    @classmethod
    def screen_timeout(cls, event, payload):
        """The screen's output when it can't be decided in time, or fails: the deny, for a call the
        screen would decide, so it fails closed. None for anything else; a root caller is never refused."""
        if event not in cls.screen_events or not cls.screened(payload):
            return None
        return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                        "permissionDecisionReason": SCREEN_TIMEOUT_TEXT}}

    @staticmethod
    def screened(payload):
        """Whether the screen decides this call: a spawned agent's, unless it is a Bash command that doesn't
        mention `kaiusap` (claude.rs's `screen`). Malformed Bash input is decided, so it fails closed."""
        if not isinstance(payload, dict) or payload.get("agent_id") in (None, ""):
            return False
        if payload.get("tool_name") != "Bash":
            return True
        tool_input = payload.get("tool_input")
        command = tool_input.get("command") if isinstance(tool_input, dict) else None
        return not isinstance(command, str) or "kaiusap" in command

    def pre_tool_use(self, payload):
        if not payload.get("agent_id"):
            return {}
        name = payload.get("tool_name", "")
        if name == "Bash":
            command = payload.get("tool_input", {}).get("command", "")
            denied = "kaiusap" in command and any(verb in ACTING for verb in bash_verbs(command))
        else:
            prefix = "mcp__plugin_kaiusap_kaiusap__"
            denied = name.startswith(prefix) and name[len(prefix):] in ACTING
        return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                        "permissionDecisionReason": SPAWNED_AGENT_TEXT}} if denied else {}

    def follow_start(self, event, store):
        from ..model import handles, held
        from ..store import instant
        self._deadline = event.get("deadline")
        self._listing = None
        bindings = handles(store)
        key = event["session_key"]
        proposal = {"tmux_session": event.get("tmux_session")}
        if event["kind"] == "fork":
            if held(bindings, key):
                return {"suggest": False}
            previous = self.previous(event)
            holders = [b for b in bindings.values() if b and b["provider"] == "claude"]
            if not previous and holders:
                end = min(event.get("entered", self.clock()) + 1.5, self._deadline or float("inf"))
                while True:
                    for binding in holders:
                        index = self.index(binding["session_id"]) or self.rebuild_index(binding["session_id"], self._deadline)
                        if index and index.get("transcript") and self.continued(index["transcript"], event["session_id"]):
                            return self.propose(event, binding, "moved-background")
                    if self.clock() >= end:
                        break
                    self.sleep(min(.05, end - self.clock()))
                # A `continued-in` record later than the wait would leave a real move looking like
                # a copy. Holders whose session just ended in this directory are offered instead.
                late = self.late_background(event, holders)
                if late:
                    # The late offer's text, exactly.
                    if len(late) == 1:
                        name = late[0]["handle"]
                        text = (f"You may be {name}: this conversation started as {name}'s session ended. Ask the user "
                                f"whether this is {name} continuing in the background.")
                    else:
                        names = [b["handle"] for b in late]
                        text = (f"You may be {' or '.join(names)}: this conversation started as "
                                f"{' or '.join(n + chr(39) + 's' for n in names)} session ended. Ask the user whether this "
                                "is one of them continuing in the background.")
                    return {"text": text,
                            "offers": [{"handle": b["handle"], "from": b["session_key"]} for b in late], "suggest": False}
            return {"text": COPY, "suggest": False}
        if event["kind"] == "clear":
            earlier = self.previous(event)
            via = []
            next_record = event
            for row in earlier:
                name = held(bindings, row["session_key"])
                if name:
                    binding = bindings[name]
                    # Another live receiving terminal keeps the handle.
                    if binding.get("pid") and (binding["pid"], binding.get("process_started_at")) != (event.get("pid"), event.get("process_started_at")):
                        process = self.process(binding["pid"])
                        if process and process[0] == binding.get("process_started_at"):
                            listing = self.listing()
                            if listing is False:
                                store.log("Claude process test: listing_failed")
                                failed = self.propose(event, binding, "fresh-start", via)["failed"]
                                # Offered, so "you are <handle>" continues the persona, as when a move fails.
                                return {**proposal, "text": failed, "suggest": False, "told": True,
                                        "offers": [{"handle": binding["handle"], "from": binding["session_key"]}]}
                            rows = [r for r in listing if r.get("pid") == binding["pid"]]
                            latest = self.latest_process(binding["pid"], process[0])
                            if any(r.get("sessionId") == binding["session_id"] for r in rows) or (
                                not rows and latest and latest.get("teammate") and latest["session_key"] == binding["session_key"]):
                                return {**proposal, "suggest": False}
                    return {**proposal, **self.propose(event, binding, "fresh-start", via)}
                if row["source"] != "clear" or (instant(next_record["at"]) - instant(row["at"])).total_seconds() > 3:
                    break
                via.append(row["session_key"])
                next_record = row
            # No handle followed, but the /clear still continues the persona of the session
            # this process held just before, when no other process holds that session now.
            if earlier and not self.held_elsewhere(earlier[0]["session_id"], event.get("pid")):
                return {**proposal, "persona": {"continues": earlier[0]["session_key"], "via": "fresh-start"}}
        if event["kind"] == "resume":
            candidates = []
            for binding in store.rows("handles.jsonl"):
                current = bindings.get(binding.get("handle"))
                if current and current["session_key"] == binding.get("session_key") and binding["reason"] == "fresh-start" and binding.get("pid") == event.get("pid") and \
                        binding.get("process_started_at") == event.get("process_started_at") and (
                        (binding.get("moved_from") or {}).get("session_key") == key or key in (binding.get("via") or [])):
                    candidates.append(binding)
            if candidates:
                binding = bindings[max(candidates, key=lambda b: b["at"])["handle"]]
                return {**proposal, **self.propose(event, binding, "rewind")}
        return proposal

    def held_elsewhere(self, session_id, pid):
        """Whether the listing shows another process holding the session; a failed listing counts as yes."""
        listing = self.listing()
        if listing is False:
            return True
        return any(r.get("sessionId") == session_id and r.get("pid") != pid for r in listing)

    LATE_WINDOW = 60  # seconds: the late background offer

    def late_background(self, event, holders):
        """The late background offer: holders no process holds now, last in this directory, whose transcript changed within
        the last 60 s (its time stamp only)."""
        from ..model import last_directory
        found = []
        if self.listing() is False:
            self.store.log("Claude late background offer: listing_failed")
            return found
        for binding in holders:
            if self.held_elsewhere(binding["session_id"], None):
                continue
            if os.path.normpath(last_directory(self.store, binding["session_key"]) or "") != os.path.normpath(event.get("cwd") or "/"):
                continue
            index = self.index(binding["session_id"])
            try:
                changed = os.stat(index["transcript"]).st_mtime if index and index.get("transcript") else None
            except OSError:
                changed = None
            if changed is not None and time.time() - changed <= self.LATE_WINDOW:
                found.append(binding)
        return found

    def previous(self, event):
        return sorted((r for r in self.starts() if r.get("pid") == event.get("pid") and
                       r.get("process_started_at") == event.get("process_started_at") and r["at"] < event["at"]),
                      key=lambda r: r["at"], reverse=True)

    @staticmethod
    def continued(path, session_id):
        try:
            with open(path, "rb") as stream:
                size = stream.seek(0, os.SEEK_END)
                stream.seek(max(0, size - 65536))
                tail = stream.read(65536)
            for line in tail.splitlines():
                if b'"continued-in"' not in line:
                    continue
                try:
                    value = json.loads(line)
                    if isinstance(value, dict) and value.get("type") == "continued-in" and value.get("continuedInSessionId") == session_id:
                        return True
                except (ValueError, UnicodeError):
                    pass
        except OSError:
            pass
        return False

    @staticmethod
    def propose(event, binding, reason, via=None):
        handle, sid = binding["handle"], binding["session_id"]
        if reason == "moved-background":
            text = f"You are {handle}; the handle followed this conversation from session {sid}."
            failed = f"You may be {handle}: this conversation continued its session in the background, but kaiusap couldn't follow it in time. Ask the user."
        elif reason == "rewind":
            text = f"You are {handle}; the handle came back from session {sid}, which this conversation replaced."
            failed = f"You may be {handle}: this conversation is the one before a /clear in this terminal, but kaiusap couldn't move the handle back in time. Ask the user."
        else:
            text = f"You are {handle}; the handle followed this conversation from session {sid}, which it replaced in this terminal."
            failed = f"You may be {handle}: this conversation replaced its session in this terminal, but kaiusap couldn't follow it in time. Ask the user."
        return {"move": {"handle": handle, "from": binding["session_key"], "to": event["session_key"], "reason": reason,
                         "via": via or None}, "text": text, "failed": failed, "suggest": False}

    def current(self, event, proposal):
        self._deadline = event.get("deadline") or self._deadline or self.clock() + 3
        records = [r for r in self.starts() if r.get("pid") == event.get("pid") and
                   r.get("process_started_at") == event.get("process_started_at")]
        if not event.get("pid") or not event.get("process_started_at") or not records:
            return False
        latest = max(r["at"] for r in records)
        if event["at"] != latest or sum(r["at"] == latest for r in records) != 1:
            return False
        first = True
        while True:
            listing = self.listing(refresh=True)
            # Start records append independently of the global handle lock.
            # A newer start may land while the registry is catching up.
            records = [r for r in self.starts() if r.get("pid") == event.get("pid") and
                       r.get("process_started_at") == event.get("process_started_at")]
            latest = max((r["at"] for r in records), default=None)
            if latest != event["at"] or sum(r["at"] == latest for r in records) != 1:
                return False
            if listing is False:
                return False
            rows = [r for r in listing if r.get("pid") == event["pid"]]
            if not rows and first:
                return True
            if any(r.get("sessionId") == event["session_id"] for r in rows):
                return True
            first = False
            try:
                wait = min(.05, self.limit(.05))
            except TimeoutError:
                self.store.log("Claude start currency: listing still names another session")
                return False
            self.sleep(wait)
