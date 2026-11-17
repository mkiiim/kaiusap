"""The OpenCode adapter: kaiusap's provider contract for OpenCode 1.x.

OpenCode runs its sessions inside the `opencode` process and has no command hooks. kaiusap's plugin,
`plugins/opencode/kaiusap.ts`, runs `kaiusap hook opencode <event>` for the events in `hook_events`, reports
the facts only the process can see, serves a Unix socket for delivery, names and lookups, and writes the
identity argument into kaiusap's MCP calls. Every rule is here.
"""
from datetime import datetime, timedelta, timezone
import json
import os
import re
import time

from . import SPAWNED_AGENT_TEXT, UNIDENTIFIED_CLOSE, Identity, context_use, tmux
from .daemon import run_command
from .terminal import TerminalProcesses
from .shell import bash_verbs
from ..errors import Error
# The store, the model and the notice are imported by the methods that use them, so the hook entry can ask
# this class for its screen without importing the store (the spawned-agent decision comes before any store work).

PROVIDER = "opencode"
STARTS = "sessions/starts.jsonl"
PROMPTS = "sessions/prompts.jsonl"
ROOT_SOURCES = {"startup", "resume", "clear", "fork"}
ACTING = {"send", "reply", "iam", "release", "moot", "unpause"}
# kaiusap's MCP tools as OpenCode names them, `<server>_<tool>` (`mcp/catalog.ts:119`), and `bash`: the calls
# the plugin hands to the screen (`SCREENED_TOOLS` in the plugin).
TOOLS = ("whoami", "who", "iam", "release", "send", "reply", "inbox", "read", "moot", "pause", "unpause", "help")
TOOL_PREFIX = "kaiusap_"
SCREENED_TOOLS = frozenset({"bash", *(TOOL_PREFIX + tool for tool in TOOLS)})
# The hook input the plugin writes, each key with the JSON types it may have; the native part's
# `clean` accepts the same keys.
INPUT_KEYS = {"session_id": (str,), "agent_id": (str,), "cwd": (str,), "pid": (int,), "socket": (str, type(None)),
              "tmux_pane": (str, type(None)), "title": (str, type(None)), "hook_event_name": (str,),
              "parent_id": (str, type(None)), "created_here": (bool,), "roots_started_here": (int,),
              "prompted_roots": (list,), "prompt": (str,), "tool": (str,), "args": (dict,)}
FORK = re.compile(r"^(?P<title>.*) \(fork #\d+\)$", re.S)
DEFAULT_TITLE = re.compile(r"^(?:New session|Child session) - \d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d+)?Z$")
PROMPT_SLACK = timedelta(seconds=1)  # a prompt record of process P's is at least 1 s after P started

SCREEN_TIMEOUT_TEXT = ("kaiusap couldn't check this call in time, so it is refused. You may retry it: a spawned agent "
                       "may only look (whoami, who, inbox, read, help, pause), and those calls are allowed when they "
                       "get through.")
NO_STAMP = ("kaiusap can't tell which OpenCode session is calling: the MCP call carried no _kaiusap_session kaiusap "
            "could match to this OpenCode process. Check that kaiusap's OpenCode plugin is loaded: kaiusap setup."
            + UNIDENTIFIED_CLOSE)
UNKNOWN = "kaiusap can't tell whether this OpenCode session is a subagent"
UNKNOWN_FIX = "Check that kaiusap's OpenCode plugin is loaded: kaiusap setup."
COPY = "This conversation has no handle; if it is a copy of another one, the original keeps its handle."
# `sessions`' warning for a resume of a session still open in a running OpenCode process.
COPY_NOTE = "Still open in another OpenCode process: this opens it in a second one."
REASONS = {
    "not_live": "no OpenCode process has opened this session since it started",
    "no_socket": "kaiusap's OpenCode plugin isn't serving this process",
    "detached": "the session is in a detached tmux session and wake_detached is off",
    "connect_failed": "the OpenCode plugin's socket refused the connection or was gone",
    "provider_refused": "OpenCode refused the prompt",
    "timeout": "a time limit was reached before the notice was sent",
    "no_confirmation": "no confirmation after the notice was sent",
    "sandboxed": "the socket connection was refused with a permission error, probably by the session's sandbox; use the kaiusap tools instead",
    "store_failed": "a store failure interrupted the send after the attempt line was written and before the notice was sent",
}
FIXES = {
    "not_live": "open it and send it a prompt",
    "no_socket": "check kaiusap setup and restart OpenCode",
    "detached": "attach it, or set wake_detached true",
    "timeout": "try again",
    "sandboxed": "use the kaiusap tools instead",
}
# The time limits, in seconds, for a line to the plugin's socket.
CONNECT_LIMIT, WRITE_LIMIT, ANSWER_LIMIT, DELIVERY_LIMIT = 2, 5, 10, 20
LOOKUP_LIMIT = 2  # a `session` or `context` line outside a delivery: best effort

# `setup`: the three items, at their places in its order.
SETUP_PLUGIN, SETUP_MCP, SETUP_VERSION = 7, 8, 9
MIN_OPENCODE = (1, 19, 5)
# OpenCode 2 rewrote the plugin API and runs one shared service per user (read at v2.2.7): this plugin doesn't load
# there, and the process model this adapter assumes doesn't hold, so 2.0 and later go to the OpenCode 2 adapter
# (opencode2.py).
UNSUPPORTED_OPENCODE = (2, 0, 0)
VERSION_LIMIT = 3
PLUGIN_NOTE = "OpenCode loads plugins when it starts; restart open OpenCode sessions."
MCP_COMMAND = ["kaiusap", "mcp", PROVIDER]
MCP_ENTRY = '"kaiusap": {"type": "local", "command": ["kaiusap", "mcp", "opencode"]}'


def root_of(session_key):
    return (session_key or "").split(":", 1)[-1].split("/", 1)[0] or None


def default_title(title):
    """OpenCode's own title for a session not yet named (`session/session.ts:51`): it counts as no name."""
    return not isinstance(title, str) or not title or bool(DEFAULT_TITLE.match(title))


def parse_version(text):
    match = re.search(r"(\d+)\.(\d+)\.(\d+)", text or "")
    return tuple(int(part) for part in match.groups()) if match else None


def iso_ms(value):
    """An OpenCode message time (milliseconds since the epoch) as the store's UTC instant, or None."""
    if type(value) not in (int, float) or value <= 0:
        return None
    return datetime.fromtimestamp(value / 1000, timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class OpenCodeAdapter(TerminalProcesses):
    process_label = "OpenCode"  # in log lines (adapters/terminal.py)
    provider = PROVIDER
    delivers = True
    harness_maker = "opencode"  # `sessions`
    session_variable = "KAIUSAP_OPENCODE_SESSION"
    hook_events = frozenset({"session.start", "chat.message", "tool.execute.before"})
    record_only_events = frozenset()  # a child's `session.start` is record-only by its kind, `subagent`
    start_events = frozenset({"session.start"})
    prompt_events = frozenset({"chat.message"})
    screen_events = frozenset({"tool.execute.before"})
    screened_tools = SCREENED_TOOLS
    setup_flags = frozenset({"opencode", "opencode_config", "opencode_command"})  # choosing the OpenCode provider

    @classmethod
    def claims(cls, environment):
        """Whether a shell environment is one of this provider's sessions: the plugin's variable."""
        return bool(environment.get(cls.session_variable))

    # ----- `setup`: read-only --------------------------------------

    @classmethod
    def setup_command(cls, setup):
        from .opencode_choice import command
        return command(setup)

    @classmethod
    def setup_items(cls, setup):
        """These items only when `setup` shows this provider."""
        from .opencode_choice import choice, suggestion_note
        found = choice(setup)
        if found["shown"] != 1:
            return []
        config_dir = cls.config_dir(setup)
        plugin = cls.setup_plugin(setup, config_dir)
        if note := suggestion_note(found):
            plugin["note"] = f"{plugin['note']} {note}" if plugin["note"] else note
        return [(SETUP_PLUGIN, plugin),
                (SETUP_MCP, cls.setup_mcp(setup, config_dir)),
                (SETUP_VERSION, cls.setup_version(setup))]

    @staticmethod
    def config_dir(setup):
        from .opencode_choice import config_dir
        return config_dir(setup)

    @staticmethod
    def setup_plugin(setup, config_dir):
        """Done when plugin/kaiusap.ts or plugins/kaiusap.ts holds the installed package's plugin module."""
        from pathlib import Path
        from ..setup import short
        try:
            wanted = (Path(setup.repo) / "plugins/opencode/kaiusap.ts").read_bytes()
        except (OSError, TypeError, ValueError):
            wanted = None  # an install from a URL: the module is recognised by its hook command instead
        from .opencode_choice import plugin_command
        command = plugin_command(setup, 1)
        for folder in ("plugin", "plugins"):
            path = config_dir / folder / "kaiusap.ts"
            try:
                found = path.read_bytes()
            except OSError:
                continue
            if found == wanted or (wanted is None and b'"kaiusap", "hook", "opencode"' in found):
                return setup.item("OpenCode plugin", True, f"kaiusap.ts is loaded from {short(path.parent)}", note=PLUGIN_NOTE)
            return setup.item("OpenCode plugin", False, f"{short(path)} isn't this kaiusap's plugin", command, PLUGIN_NOTE)
        return setup.item("OpenCode plugin", False, f"no kaiusap.ts in {short(config_dir / 'plugin')}", command, PLUGIN_NOTE)

    @staticmethod
    def setup_mcp(setup, config_dir):
        """Done when opencode.json or opencode.jsonc has mcp.kaiusap, local, running `kaiusap mcp opencode`, not disabled."""
        from ..setup import short
        from .opencode_choice import strip_jsonc
        name = "kaiusap's MCP server in OpenCode's config"
        found = []
        for file in ("opencode.json", "opencode.jsonc"):
            path = config_dir / file
            try:
                text = path.read_text()
            except FileNotFoundError:
                continue
            except OSError:
                return setup.item(name, False, f"{short(path)} couldn't be read", MCP_ENTRY)
            try:
                config = json.loads(strip_jsonc(text))
            except ValueError:
                return setup.item(name, False, f"{short(path)} couldn't be read", MCP_ENTRY)
            found.append((path, config if isinstance(config, dict) else {}))
        for path, config in found:
            mcp = config.get("mcp") if isinstance(config.get("mcp"), dict) else {}
            entry = mcp.get("kaiusap")
            if not isinstance(entry, dict):
                continue
            from .opencode_choice import runs_kaiusap_mcp  # by name or by full path
            if entry.get("type") != "local" or not runs_kaiusap_mcp(entry.get("command"), PROVIDER):
                return setup.item(name, False, f"mcp.kaiusap in {short(path)} doesn't run kaiusap mcp opencode", MCP_ENTRY)
            if entry.get("enabled") is False:
                return setup.item(name, False, f"mcp.kaiusap is disabled in {short(path)} (enabled: false)", MCP_ENTRY)
            return setup.item(name, True, f"mcp.kaiusap in {short(path)}")
        where = short(found[0][0]) if found else short(config_dir / "opencode.json")
        return setup.item(name, False, f"no mcp.kaiusap in {where}; add under \"mcp\"", MCP_ENTRY)

    @staticmethod
    def setup_version(setup):
        """`opencode --version` (3 s), only when opencode is on the PATH; done for 1.19.5 up to 2.0."""
        from .opencode_choice import choice, probe
        found = probe(setup)
        version = found["version"]
        if version is None:
            return setup.item("OpenCode version", False, found["detail"], found["command"])
        text = ".".join(map(str, version))
        if version < MIN_OPENCODE:
            return setup.item("OpenCode version", False, f"{text}; kaiusap needs 1.19.5 or later", "opencode upgrade")
        if version >= UNSUPPORTED_OPENCODE:
            if 1 in choice(setup)["installed"]:
                from .opencode_choice import plugin_command
                return setup.item("OpenCode version", False,
                                  f"{text}; kaiusap's OpenCode 1 plugin is installed, which doesn't load in OpenCode {text}",
                                  plugin_command(setup, 2))
            return setup.item("OpenCode version", False,
                              f"{text}; kaiusap supports OpenCode 1.19.5 up to 2.0: OpenCode 2 doesn't load its plugin",
                              "npm install -g opencode-ai@1")
        return setup.item("OpenCode version", True, text)

    # ----- one call ----------------------------------------------------------

    def __init__(self, store, run=None, environ=None, clock=None, socket_factory=None, parent_pid=None):
        self.store = store
        self.run = run or run_command
        self.environ = os.environ if environ is None else environ
        self.clock = clock or time.monotonic
        self.socket_factory = socket_factory
        self.parent_pid = parent_pid  # the MCP server's parent, for tests; else os.getppid()
        self._deadline = None
        self.reset()

    def begin(self):
        self.reset()

    def reset(self):
        self._processes, self._panes, self._parents, self._contexts = {}, None, None, {}

    def finish(self, catch_up=True):
        self.reset()
        self._deadline = None

    def hook_deadline(self, at):
        return at

    def limit(self, seconds, deadline=None):
        end = self._deadline if deadline is None else deadline
        limit = seconds if end is None else min(seconds, end - self.clock())
        if limit <= 0:
            raise TimeoutError()
        return limit

    # ----- processes and panes: adapters/terminal.py, and these ---------------------

    def running(self, record):
        """Whether the record's process is still the one running (pid and start time)."""
        started = record.get("process_started_at")
        return bool(started) and self.started_at(record.get("pid")) == started

    def ancestors(self):
        """The MCP server's nearest ancestors, at most three steps up."""
        import subprocess
        result, pid = [], os.getppid() if self.parent_pid is None else self.parent_pid
        for _ in range(3):
            if not pid or pid in result:
                break
            result.append(pid)
            try:
                code, out = self.run(["ps", "-o", "ppid=", "-p", str(pid)], self.limit(2), None)
                pid = int(out.strip()) if not code else None
            except (OSError, subprocess.TimeoutExpired, ValueError):
                break
        return result

    # ----- records ------------------------------------------------------------

    def records(self, session_id, child=False):
        """Every start record of the session, newest first: its root records, or with `child` its subagent records."""
        rows = []
        for row in self.store.rows(STARTS, reverse=True):
            if row.get("provider") != PROVIDER:
                continue
            if child:
                if row.get("source") == "subagent" and row.get("agent_id") == session_id:
                    rows.append(row)
            elif row.get("session_id") == session_id and row.get("source") in ROOT_SOURCES:
                rows.append(row)
        return rows

    def process_records(self, pid, started):
        """The root start records of one process, newest first."""
        return [r for r in self.store.rows(STARTS, reverse=True) if r.get("provider") == PROVIDER and
                r.get("source") in ROOT_SOURCES and r.get("pid") == pid and r.get("process_started_at") == started]

    def live_record(self, session_id):
        """The session's start record with the greatest `at` whose process is running, or None."""
        best = None
        for row in self.records(session_id):
            if self.running(row) and (best is None or row["at"] > best["at"]):
                best = row
        return best

    # ----- identity ----------------------------------------------

    def identity(self, environment, store):
        """A shell command's caller: `KAIUSAP_OPENCODE_SESSION` with `OPENCODE_PID`, under the process test."""
        session = environment.get(self.session_variable)
        cwd = environment.get("PWD") or os.getcwd()
        try:
            pid = int(environment.get("OPENCODE_PID", ""))
        except (TypeError, ValueError):
            pid = None
        if not session:
            return Identity()
        found = self.place(session, [pid] if pid else [], cwd)
        if found:
            return found
        return Identity(PROVIDER, session, None, cwd, "unknown",
                        reason=f"no start record of session {session} names this OpenCode process")

    def mcp_identity(self, meta, stated=None):
        """An MCP caller: the identity argument the plugin wrote, under the process test; otherwise unidentified."""
        if not isinstance(stated, str) or not stated:
            raise Error(NO_STAMP)
        found = self.place(stated, self.ancestors(), None)
        if not found or found.classification == "unknown":
            raise Error(NO_STAMP)
        return found

    def place(self, session, pids, cwd):
        """The lookup for an id, given the processes the call may come from: a start record of the session
        naming one of them places it (step 1). Otherwise that process's plugin sockets (step 2) may place it as a
        child, and only under a root whose start record names the process, so the process test still holds for the
        session that acts; an id the plugin calls a root without such a record isn't placed. Else None."""
        for pid in pids:
            started = self.started_at(pid)
            if not started:
                continue
            for child in (False, True):
                for row in self.records(session, child):
                    if row.get("pid") == pid and row.get("process_started_at") == started:
                        return self.identified(row, session, cwd, pid, started)
            asked = set()
            for row in self.process_records(pid, started):
                path = row.get("socket")
                if not path or path in asked:
                    continue
                asked.add(path)  # one plugin per directory instance, each with its own socket
                answer = self.ask(path, {"type": "session", "session_id": session}, LOOKUP_LIMIT)
                root = answer.get("root") if answer and answer.get("ok") else None
                if isinstance(root, str) and root != session and any(
                        r.get("pid") == pid and r.get("process_started_at") == started for r in self.records(root)):
                    return Identity(PROVIDER, root, f"{PROVIDER}:{root}/{session}", cwd, "spawned", f"{PROVIDER}:{root}",
                                    pid=pid, process_started_at=started, agent_id=session)
        return None

    @staticmethod
    def identified(row, session, cwd, pid, started):
        cwd = cwd or row.get("cwd")
        if row.get("source") == "subagent":
            root = row["session_id"]
            return Identity(PROVIDER, root, f"{PROVIDER}:{root}/{session}", cwd, "spawned", f"{PROVIDER}:{root}",
                            pid=pid, process_started_at=started, agent_id=session)
        return Identity(PROVIDER, session, f"{PROVIDER}:{session}", cwd, "root", pid=pid, process_started_at=started)

    def directory(self, identity):
        return None

    def refuse_before_acting(self, identity):
        if identity.classification == "spawned":
            raise Error(SPAWNED_AGENT_TEXT)
        if identity.classification == "unknown":
            raise Error(f"{UNKNOWN} ({identity.reason}). {UNKNOWN_FIX}")

    def unidentified(self, identity):
        return {"can_be_woken": False, "why_not": UNKNOWN + (f" ({identity.reason})" if identity.reason else ""),
                "reason_code": "unknown_caller", "fix": UNKNOWN_FIX}

    # ----- the screen ---------------------------------------------

    @classmethod
    def screened(cls, payload):
        """Whether the screen decides this call: a spawned agent's kaiusap tool call, or its bash command
        mentioning `kaiusap`. Malformed bash input is decided, so it fails closed."""
        if not isinstance(payload, dict) or not payload.get("agent_id"):
            return False
        tool = payload.get("tool")
        if tool == "bash":
            args = payload.get("args")
            command = args.get("command") if isinstance(args, dict) else None
            return not isinstance(command, str) or "kaiusap" in command
        return isinstance(tool, str) and tool.startswith(TOOL_PREFIX)

    @classmethod
    def pre_tool(cls, payload):
        """The spawned-agent screen before any store work: None when there is nothing to decide, else the deny or `{}`."""
        if not cls.screened(payload):
            return None
        tool = payload.get("tool")
        if tool == "bash":
            command = (payload.get("args") or {}).get("command")
            denied = not isinstance(command, str) or any(verb in ACTING for verb in bash_verbs(command))
        else:
            denied = tool[len(TOOL_PREFIX):] in ACTING
        return {"hookSpecificOutput": {"deny": SPAWNED_AGENT_TEXT}} if denied else {}

    @classmethod
    def screen_timeout(cls, event, payload):
        if event not in cls.screen_events or not cls.screened(payload):
            return None
        return {"hookSpecificOutput": {"deny": SCREEN_TIMEOUT_TEXT}}

    # ----- events --------------------------------------------

    @staticmethod
    def clean_input(payload):
        """Only the plugin's own keys, each of its type; anything else is not ours."""
        if not isinstance(payload, dict):
            return False
        for key, value in payload.items():
            kinds = INPUT_KEYS.get(key)
            if kinds is None or type(value) not in kinds:
                return False
        return True

    @staticmethod
    def source(payload):
        """`source` from the plugin's facts."""
        if payload.get("parent_id"):
            return "subagent"
        if not payload.get("created_here"):
            return "resume"
        title = payload.get("title")
        if isinstance(title, str) and FORK.match(title):
            return "fork"
        return "clear" if (payload.get("roots_started_here") or 0) > 0 else "startup"

    def events(self, payload, deadline=None):
        from ..notice import notice_id
        from ..store import now
        if not self.clean_input(payload):
            return None
        root, agent = payload.get("session_id"), payload.get("agent_id")
        if not isinstance(root, str) or not root or (agent is not None and (not isinstance(agent, str) or not agent)):
            return None
        name = payload.get("hook_event_name")
        at = now()
        self._deadline = deadline
        key = f"{PROVIDER}:{root}" + (f"/{agent}" if agent else "")
        identity = Identity(PROVIDER, root, key, payload.get("cwd"), "spawned" if agent else "root",
                            f"{PROVIDER}:{root}" if agent else None, agent_id=agent)
        pid = payload.get("pid") if type(payload.get("pid")) is int else None
        title = payload.get("title")
        event = {"provider": PROVIDER, "identity": identity, "session_id": root, "session_key": key, "agent_id": agent,
                 "cwd": payload.get("cwd"), "at": at, "pid": pid, "title": None if default_title(title) else title,
                 "raw_title": title, "socket": payload.get("socket"), "record": None, "prompt_record": None,
                 "prompt": None, "hook_event_name": name}
        if name == "session.start":
            source = self.source(payload)
            if (source == "subagent") != bool(agent):
                return None  # a child is named by both, or by neither: anything else isn't the plugin's input
            started = self.started_at(pid)
            if not started:
                self.store.log(f"OpenCode start {key}: no start time for process {pid}; the session can't be live there")
            event.update(kind=source, source=source, process_started_at=started, tmux_pane=None, tmux_session=None,
                         prompted_roots=payload.get("prompted_roots") or [])
            raw_pane = payload.get("tmux_pane")
            if raw_pane and self.environ.get("TMUX"):
                self.pane_key(event, raw_pane)
            record = {"at": at, "provider": PROVIDER, "session_id": root, "session_key": key, "source": source}
            if source == "subagent":
                record["agent_id"] = agent
            record.update(cwd=event["cwd"], pid=pid, process_started_at=started, socket=event["socket"],
                          tmux_pane=event["tmux_pane"])
            event["record"] = record
            return event
        if name == "chat.message":
            if agent:
                return None  # a child session's prompts write nothing and get no note
            prompt = payload.get("prompt")
            event.update(kind="prompt", prompt=prompt)
            if notice_id(prompt if isinstance(prompt, str) else "") is None:
                event["prompt_record"] = {"at": at, "provider": PROVIDER, "session_id": root, "session_key": key,
                                          "source": "prompt", "cwd": event["cwd"], "pid": pid}
            return event
        return None

    def hook_record(self, name, event, deadline, recorded=False, native=None):
        """The prompt record is the native part's fixed-shape append: written already when `recorded`, else
        by `native()` when the native entry is there; the start records are always written here."""
        if event.get("prompt_record") and (recorded or native):
            if not recorded:
                native()
            event["prompt_record"] = None
        if event.get("prompt_record"):
            self.store.append(PROMPTS, event["prompt_record"], deadline)
        if event.get("record"):
            self.store.start(event["record"], deadline)

    def tell(self, text, event="chat.message"):
        """A text the plugin adds to the prompt as a part the model reads and the TUI doesn't show."""
        return {"hookSpecificOutput": {"tell": text}}

    # ----- naming ---------------------------------------------------

    def hook_output(self, event, state):
        event["history"] = (state.data["ended"], state.data["bindings"])
        event["naming_events"] = state.data["events"]
        return self.mirror_output(event)

    def marker_fields(self, state, event):
        """The marker's OpenCode fields, read back by the native part's `clean`: the title
        rule for the next prompt."""
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
        return {"title_mode": title_mode, "title": title}

    def named(self, binding, name, kind, event, retry=False):
        from contextlib import nullcontext
        budget = self.store.hook_budget
        with budget.operation() if budget else nullcontext():
            self.store.event("named", handle=binding["handle"], session_id=binding["session_id"], session_key=binding["session_key"],
                             binding=binding["id"], name=name, kind=kind, retry=retry,
                             by_session=event["session_id"], by_session_key=event["session_key"], deadline=event.get("deadline"))
            if budget and name is not None:
                # The name is committed with its event: the hook's output carries it even past the deadline.
                budget.output.setdefault("hookSpecificOutput", {}).update(title=name)

    def mirror_output(self, event):
        """The hook naming rules, as for Claude's hooks, with the input's `title` (a default title is no name) as the
        current name; the output's `title` is set with `session.update`. The tmux session name follows the name
        applied, or at start a current name equal to the handle's."""
        from ..model import binding_history, held, retirement_name
        if event.get("agent_id") or event.get("kind") not in {"startup", "resume", "fork", "prompt"}:
            return {}
        ended, bindings = event.get("history") or binding_history(self.store)
        key, title = event["session_key"], event.get("title")
        ended = [(binding, at) for binding, at in ended if binding["session_key"] == key]
        handle = held(bindings, key)
        output = None
        if handle:
            binding = bindings[handle]
            named = [e for e in (event["naming_events"] if "naming_events" in event else self.store.events(binding["at"]))
                     if e.get("event") == "named" and e.get("binding") == binding["id"] and e.get("kind") == "handle"]
            if not named:
                allowed = binding["reason"] != "fresh-start" or title is None or title == handle
                output = handle if allowed else None
                self.named(binding, output, "handle", event)
            elif event["kind"] == "prompt" and not any(e.get("retry") for e in named):
                if title is None and not any(e.get("name") is None for e in named):
                    output = handle
                    self.named(binding, output, "handle", event, retry=True)
                elif title is not None and title != handle:
                    self.named(binding, None, "handle", event, retry=True)
        retirement_events = event.get("naming_events")
        if retirement_events is None:
            retirement_events = self.store.events(min(b["at"] for b, _ in ended)) if ended else []
        done = {(e.get("binding"), e.get("kind")) for e in retirement_events if e.get("event") == "named"}
        for binding, left_at in ended:
            if (binding["id"], "retired") in done:
                continue
            name = retirement_name(binding["handle"], left_at) if not handle and title == binding["handle"] else None
            self.named(binding, name, "retired", event)
            if name:
                output = name
        follow = handle if output is None and event["kind"] in {"startup", "resume"} and handle and title == handle else output
        if follow is not None:
            self.hook_session_name(event, follow)
        return {"hookSpecificOutput": {"title": output}} if output is not None else {}

    def hook_session_name(self, event, name):
        """The tmux session name from a hook that applies `name`: the pane in the live process's start record."""
        from ..model import load_settings
        try:
            if load_settings(self.store)["tmux_session_names"] != "follow":
                return
            if self.store.hook_budget:
                self.store.hook_budget.check()
            record = event.get("record") or self.live_record(event["session_id"])
            if record:
                self.follow_session_name(record, name)
        except (OSError, TimeoutError) as exc:
            self.store.log(f"tmux session name for {event.get('session_key')} skipped: {type(exc).__name__}: {exc}")

    def follow_session_name(self, record, name):
        from ..model import load_settings
        if not record or not record.get("tmux_pane") or load_settings(self.store)["tmux_session_names"] != "follow":
            return []
        warning = tmux.rename_session(self.run, self.limit, self.store.log, record["tmux_pane"], name, self.holds(record.get("pid")))
        return [warning] if warning else []

    def tmux_start(self, event):
        """The tmux window name isn't applied; the tmux session name's start rule runs with the hook's name step (`mirror_output`)."""
        return None

    def mirror(self, binding):
        """At `iam`, rename the session at once over the live process's plugin socket; an `{"ok":true}` answer is
        the `named` event, so the hooks don't apply it again. Then the tmux session name follows the name applied."""
        if binding.get("reason") != "iam" or not binding.get("session_key"):
            return []
        try:
            record = self.live_record(binding["session_id"])
        except OSError as exc:
            self.store.log(f"OpenCode rename at iam skipped: {exc}")
            return []
        if not record or not record.get("socket"):
            return []
        handle = binding["handle"]
        if not self.rename(record["socket"], binding["session_id"], handle):
            return []
        try:
            self.named(binding, handle, "handle", binding)
        except OSError as exc:
            self.store.warn(exc)
        return self.follow_session_name(record, handle)

    def retire(self, binding, left_at, cause=None):
        """At once only when `iam` or `release` ends the binding and the session is live with the handle's name:
        the `rename` line with the retirement name. Otherwise the hooks retire it."""
        from ..model import handles, held, retirement_name
        if cause not in {"iam", "release"}:
            return []
        try:
            if held(handles(self.store), binding["session_key"]):
                return []
            record = self.live_record(binding["session_id"])
            if not record or not record.get("socket"):
                return []
            answer = self.ask(record["socket"], {"type": "session", "session_id": binding["session_id"]}, LOOKUP_LIMIT)
            if not answer or not answer.get("ok") or answer.get("title") != binding["handle"]:
                return []
            name = retirement_name(binding["handle"], left_at)
            if not self.rename(record["socket"], binding["session_id"], name):
                return []
            try:
                self.named(binding, name, "retired", binding)
            except OSError as exc:
                self.store.warn(exc)
            return self.follow_session_name(record, name)
        except (OSError, ValueError, TimeoutError) as exc:
            self.store.log(f"OpenCode retirement at once skipped: {type(exc).__name__}: {exc}")
            return []

    def rename(self, path, session_id, name):
        answer = self.ask(path, {"type": "rename", "session_id": session_id, "name": name}, LOOKUP_LIMIT)
        if answer and answer.get("ok") is True:
            return True
        self.store.log(f"OpenCode rename to {name} not applied: {answer}")
        return False

    # ----- the plugin's socket ----------------------------------------

    def exchange(self, path, request, sent, answer_limit=ANSWER_LIMIT):
        """One request line to the plugin's socket and its one answer line, under the socket limits: connecting 2 s,
        writing 5 s, the answer `answer_limit`, each capped by the time left. `sent[0]` counts bytes written.
        Returns the parsed answer; raises OSError, TimeoutError or ValueError."""
        import socket
        data = (json.dumps(request, ensure_ascii=False, separators=(",", ":")) + "\n").encode()
        connection = (self.socket_factory or socket.socket)(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            connection.settimeout(self.limit(CONNECT_LIMIT))
            connection.connect(path)
            end = self.clock() + self.limit(WRITE_LIMIT)
            while sent[0] < len(data):
                connection.settimeout(self.limit(WRITE_LIMIT, end))
                count = connection.send(data[sent[0]:])
                if count <= 0:
                    raise OSError("socket closed while writing")
                sent[0] += count
            end = self.clock() + self.limit(answer_limit)
            answer = b""
            while b"\n" not in answer:
                connection.settimeout(self.limit(answer_limit, end))
                chunk = connection.recv(65536)
                if not chunk:
                    break
                answer += chunk
                if len(answer) > 1 << 20:
                    raise ValueError("answer too long")
            line = answer.split(b"\n", 1)[0]
            value = json.loads(line.decode())
            if not isinstance(value, dict):
                raise ValueError("answer isn't an object")
            return value
        except socket.timeout as exc:
            raise TimeoutError(str(exc)) from None
        finally:
            try:
                connection.close()
            except OSError:
                pass

    def ask(self, path, request, limit):
        """A lookup line (`session`, `rename`, `context`): its answer, or None when the plugin couldn't be asked."""
        if not path:
            return None
        saved = self._deadline
        try:
            self._deadline = min(saved, self.clock() + limit) if saved is not None else self.clock() + limit
            return self.exchange(path, request, [0], limit)
        except (OSError, TimeoutError, ValueError) as exc:
            self.store.log(f"OpenCode plugin socket {request.get('type')}: {type(exc).__name__}: {exc}")
            return None
        finally:
            self._deadline = saved

    # ----- liveness --------------------------------------------------

    def status(self, session_key, wake_detached=None):
        from pathlib import Path
        from ..model import load_settings
        session = root_of(session_key)
        record = self.live_record(session) if session else None
        result = {"reason_code": None, "record": record, "panes": [], "detached": False}
        if not record:
            return {**result, "reason_code": "not_live"}
        pane = self.record_pane(record)
        if pane:
            result["panes"] = [pane]
            result["detached"] = not pane["attached"]
        if not record.get("socket") or not Path(record["socket"]).exists():
            return {**result, "reason_code": "no_socket"}
        if result["detached"] and not (load_settings(self.store)["wake_detached"] if wake_detached is None else wake_detached):
            result["reason_code"] = "detached"
        return result

    def liveness(self, session_key):
        status = self.status(session_key)
        code = status["reason_code"]
        # For the handle suggestion: not live only when no running process recorded the session; a process that runs
        # with its socket gone counts as live, so nothing is suggested where kaiusap can't tell.
        return {"can_be_woken": code is None, "reason_code": code, "why_not": REASONS.get(code), "fix": FIXES.get(code),
                "detached": status["detached"], "live": code != "not_live"}

    # ----- deliver -----------------------------------------------------------

    def deliver(self, store, message, deadline=None):
        from ..notice import notice
        self._deadline = min(deadline, self.clock() + DELIVERY_LIMIT) if deadline is not None else self.clock() + DELIVERY_LIMIT
        sent = [0]
        try:
            try:
                status = self.status(message["to"]["session_key"])
            except (OSError, ValueError) as exc:
                store.warn(exc)
                return {**self.outcome("stored", "store_failed"), "store_failed_before_handover": True}
            if status["reason_code"]:
                return self.outcome("stored", status["reason_code"])
            request = {"type": "notice", "session_id": message["to"]["session_id"], "text": notice(message)}
            answer = self.exchange(status["record"]["socket"], request, sent)
        except (TimeoutError, ValueError):
            return self.outcome("uncertain", "no_confirmation") if sent[0] else self.outcome("stored", "timeout")
        except PermissionError:
            return self.outcome("uncertain", "no_confirmation") if sent[0] else self.outcome("stored", "sandboxed")
        except OSError:
            return self.outcome("uncertain", "no_confirmation") if sent[0] else self.outcome("stored", "connect_failed")
        if answer.get("ok") is True:
            return self.outcome("woken")
        if answer.get("code") == "not_live":
            return self.outcome("stored", "not_live")
        if answer.get("code") == "refused":
            detail = answer.get("message") if isinstance(answer.get("message"), str) else "no message"
            return {**self.outcome("stored", "provider_refused"), "reason": f"OpenCode refused the prompt: {detail}"}
        return self.outcome("uncertain", "no_confirmation")

    @staticmethod
    def outcome(outcome, code=None):
        return {"outcome": outcome, "reason_code": code, "reason": REASONS.get(code), "provider_version": None, "warnings": []}

    # ----- list and where ---------------------------------------------------

    def where(self, session_key):
        import shlex
        from pathlib import Path
        from ..model import last_directory
        session = root_of(session_key)
        empty = {"cwd": None, "name": None, "where": "unknown", "tmux_panes": [], "command": None, "trusted": None,
                 "detached": False, "note": None}
        if not session:
            return empty
        status = self.status(session_key)
        record = status["record"]
        cwd = (record or {}).get("cwd") or last_directory(self.store, f"{PROVIDER}:{session}")
        row = {**empty, "cwd": cwd, "detached": status["detached"]}
        resume = f"opencode -s {shlex.quote(session)}"
        resume = f"cd {shlex.quote(cwd)} && {resume}" if cwd else resume
        if not record:
            return {**row, "where": "not_running", "command": resume}
        if not record.get("socket") or not Path(record["socket"]).exists():
            return {**row, "where": "unknown"}  # the process may still show it
        answer = self.ask(record["socket"], {"type": "session", "session_id": session}, LOOKUP_LIMIT)
        title = answer.get("title") if answer and answer.get("ok") else None
        row["name"] = None if default_title(title) else title
        if status["panes"]:
            panes = [{k: v for k, v in p.items() if k != "pane_pid"} for p in status["panes"]]
            return {**row, "where": "tmux", "tmux_panes": panes, "command": tmux.attach_command(tmux.attach_pane(panes)["tmux_session"])}
        return {**row, "where": "not_in_tmux", "command": resume, "note": COPY_NOTE}

    def list(self):
        from ..model import handles
        keys, seen = [], set()
        for row in self.store.rows(STARTS, reverse=True):
            session = row.get("session_id")
            if row.get("provider") != PROVIDER or row.get("source") not in ROOT_SOURCES or session in seen:
                continue
            seen.add(session)
            if self.live_record(session):
                keys.append(f"{PROVIDER}:{session}")
        keys.extend(b["session_key"] for b in handles(self.store).values() if b and b["provider"] == PROVIDER)
        return [{"provider": PROVIDER, "session_id": root_of(key), "session_key": key, **self.where(key)}
                for key in dict.fromkeys(keys)]

    # ----- context use ------------------------------------------------------------

    def harness(self, session_key):
        """`sessions`' harness_maker: who made this provider's harness, whatever model it runs."""
        return self.harness_maker

    def context_answer(self, session):
        """The plugin's `context` answer for a live session, asked once per call (`context` and `harness_version`
        share it), or None."""
        if not session:
            return None
        if session not in self._contexts:
            record = self.live_record(session)
            answer = None
            if record and record.get("socket"):
                answer = self.ask(record["socket"], {"type": "context", "session_id": session}, LOOKUP_LIMIT)
            self._contexts[session] = answer if answer and answer.get("ok") else None
        return self._contexts[session]

    def activity(self, session_key):
        """None: OpenCode 1.x's plugin events could give it, but not until setting and clearing it on every path is proven."""
        return None

    def harness_version(self, session_key):
        """The running OpenCode process's version, which the plugin reads from its own process
        (`GET /global/health`) and carries in its `context` answer. None when not live, or a plugin too old to say."""
        answer = self.context_answer(root_of(session_key))
        version = answer.get("version") if answer else None
        return version if isinstance(version, str) and version else None

    def context(self, session_key):
        """From the live process's plugin socket, the newest assistant message with output tokens: the sum of
        its input, output, reasoning, cache read and cache write tokens, as OpenCode's sidebar counts them
        (`tui/src/feature-plugins/sidebar/context.tsx:19-34`), against the model's `limit.context`."""
        answer = self.context_answer(root_of(session_key))
        if not answer or not isinstance(answer.get("messages"), list):
            return None
        limits = answer.get("limits") if isinstance(answer.get("limits"), dict) else {}
        for message in reversed(answer["messages"]):
            tokens = message.get("tokens") if isinstance(message, dict) else None
            if not isinstance(tokens, dict) or not (type(tokens.get("output")) is int and tokens["output"] > 0):
                continue
            cache = tokens.get("cache") if isinstance(tokens.get("cache"), dict) else {}
            counts = [tokens.get("input", 0), tokens.get("output", 0), tokens.get("reasoning", 0),
                      cache.get("read", 0), cache.get("write", 0)]
            if not all(type(c) is int and c >= 0 for c in counts):
                continue
            model = message.get("model") if isinstance(message.get("model"), str) else None
            provider = message.get("provider") if isinstance(message.get("provider"), str) else None
            limit = limits.get(f"{provider}/{model}") if provider and model else None
            return context_use(sum(counts), limit, model, iso_ms(message.get("at")), provider)
        return None

    # ----- follow a start -----------------------------------------------

    def follow_start(self, event, store):
        self._deadline = event.get("deadline")
        kind = event.get("kind")
        proposal = {"tmux_session": event.get("tmux_session")}
        if kind == "fork":
            return self.step_fork(event, store)
        if kind == "clear":
            try:
                return {**proposal, **(self.step_clear(event, store) or {})}
            except (OSError, ValueError, TimeoutError) as exc:
                store.log(f"start {event['session_key']}: couldn't follow: {type(exc).__name__}: {exc}")
                return {**proposal, "suggest": False}
        return proposal

    def step_fork(self, event, store):
        from ..model import handles
        match = FORK.match(event.get("raw_title") or "")
        bindings = handles(store)
        original = match.group("title") if match else None
        binding = bindings.get(original) if original else None
        if binding and binding.get("provider") == PROVIDER:
            record = self.live_record(root_of(binding["session_key"]))
            if record and (record.get("pid"), record.get("process_started_at")) == (event.get("pid"), event.get("process_started_at")):
                return {"text": f"This conversation is a copy of the one holding {original}, which keeps it; you have no handle.",
                        "suggest": False}
        return {"text": COPY, "suggest": False}

    def step_clear(self, event, store):
        """The predecessor is the root in `prompted_roots` with the newest prompt record of this
        process's (its `pid`, at least 1 s after the process started)."""
        from ..model import handles, held
        from ..store import instant
        pid, started = event.get("pid"), event.get("process_started_at")
        if not pid or not started:
            return None
        roots = {r.get("session_id") for r in event.get("prompted_roots") or [] if isinstance(r, dict)} - {event["session_id"]}
        roots.discard(None)
        if not roots:
            return None
        floor = instant(started) + PROMPT_SLACK
        # Append order can differ from `at` order by a hook's budget: the scan goes 3 s past the floor,
        # and past the first match, keeping the greatest `at`.
        predecessor, best, stop = None, None, floor - timedelta(seconds=3)
        for row in self.store.rows(PROMPTS, reverse=True):
            try:
                at = instant(row["at"])
            except (KeyError, ValueError, TypeError):
                continue
            if at < stop:
                break
            if at < floor or row.get("provider") != PROVIDER or row.get("pid") != pid or row.get("session_id") not in roots:
                continue
            if best is None or at > best:
                predecessor, best = row["session_id"], at
                stop = max(stop, at - timedelta(seconds=3))
        if not predecessor:
            return None
        bindings = handles(store)
        name = held(bindings, f"{PROVIDER}:{predecessor}")
        if not name:
            # The predecessor holds no handle, but /new still continues its persona, on the move's
            # own condition: its latest live start record is this process's.
            live = self.live_record(predecessor)
            if live and (live.get("pid"), live.get("process_started_at")) == (pid, started):
                return {"persona": {"continues": f"{PROVIDER}:{predecessor}", "via": "fresh-start"}}
            return None
        live = self.live_record(predecessor)
        if not live or (live.get("pid"), live.get("process_started_at")) != (pid, started):
            return None  # another running process receives its mail: no move, and on to step 4
        return {"move": {"handle": name, "from": f"{PROVIDER}:{predecessor}", "to": event["session_key"],
                         "reason": "fresh-start", "via": None},
                "text": f"You are {name}; the handle followed this conversation from session {predecessor}, which it replaced in this terminal.",
                "failed": f"You may be {name}: this conversation replaced its session in this terminal, but kaiusap couldn't follow it in time. Ask the user.",
                "suggest": False}

    def current(self, event, proposal):
        """Currency: the hook's own record is the newest, alone, among its process's root records."""
        records = self.process_records(event.get("pid"), event.get("process_started_at"))
        if not event.get("pid") or not event.get("process_started_at") or not records:
            return False
        latest = max(r["at"] for r in records)
        return event["at"] == latest and sum(r["at"] == latest for r in records) == 1
