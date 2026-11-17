"""OpenCode 2's adapter: the provider `opencode2`, apart from OpenCode 1.x's `opencode`.

OpenCode 2 runs every session in one shared service; each terminal (TUI) is a client of it. kaiusap's plugin,
`plugins/opencode2/`, has a part in the service (`server.ts`: the start, prompt and screen hooks, and the told
text) and a part in each terminal (`tui.ts`: which session the terminal shows). Both run `kaiusap hook opencode2
<event>` and carry out the answer; every rule is here. kaiusap speaks to the service over HTTP with Basic auth:
the service's password is read from its registration for each request and is never written, logged, shown or
stored; nothing this module raises or returns carries it.
"""
from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone

from . import SPAWNED_AGENT_TEXT, UNIDENTIFIED_CLOSE, Identity, context_use, tmux
from .daemon import run_command
from .terminal import TerminalProcesses
from .shell import bash_verbs
from ..errors import Error
# The store, the model and the notice are imported by the methods that use them, so the hook entry can ask
# this class for its screen without importing the store (the spawned-agent decision comes before any store work). The HTTP
# modules are imported by the requests that use them, so a prompt hook that makes none doesn't pay for them
# within its 50 ms.

PROVIDER = "opencode2"
USERNAME = "opencode"  # the service's Basic-auth user (`server/src/auth.ts`)
REGISTRATION = "service.json"  # under `$XDG_STATE_HOME/opencode/` for the release channels
REQUEST_LIMIT = 2.0  # seconds, for a request outside a delivery, which has its own limit
ID_CHARS = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"


class ServiceError(Exception):
    """A request to the service that didn't succeed. `code` is the delivery reason code it maps to:
    `connect_failed` (no registration, or the connection refused), `provider_refused` (an error status),
    `timeout` (no answer in time) or `conflict` (409: the message id was already admitted). `status` is the
    HTTP status, when there was one. The text never carries the password."""

    def __init__(self, code, text, status=None):
        super().__init__(text)
        self.code = code
        self.status = status


def registration_path(environ=None):
    """`$XDG_STATE_HOME/opencode/service.json`, else `~/.local/state/opencode/service.json`."""
    environ = os.environ if environ is None else environ
    state = environ.get("XDG_STATE_HOME") or os.path.join(environ.get("HOME") or os.path.expanduser("~"), ".local", "state")
    return os.path.join(state, "opencode", REGISTRATION)


def message_id(message):
    """The `msg_` id kaiusap gives a notice for kaiusap message `message`, the same for each attempt, so the
    service refuses a repeat (409) rather than run it twice. It has the form OpenCode's own
    ids have (`schema/src/identifier.ts`): 12 hex digits of the message's creation time, then 14 characters, here
    from a hash of kaiusap's id. The service orders messages by sequence, not id, so the time is only the form."""
    created = message.get("created")
    try:
        at = datetime.fromisoformat(str(created).replace("Z", "+00:00"))
        if at.tzinfo is None:
            at = at.replace(tzinfo=timezone.utc)
        ms = int(at.timestamp() * 1000)
    except (TypeError, ValueError):
        ms = 0
    import hashlib
    value = (ms * 0x1000 + 1) & 0xFFFFFFFFFFFF
    digest = hashlib.sha256(f"kaiusap:{message['id']}".encode()).digest()
    return "msg_" + f"{value:012x}" + "".join(ID_CHARS[byte % 62] for byte in digest[:14])


class Service:
    """OpenCode 2's service, as its registration names it. The registration is read for each
    request, so a service another terminal's version replaced is found again."""

    def __init__(self, environ=None, opener=None, path=None):
        self.environ = os.environ if environ is None else environ
        self.opener = opener
        self.path = path  # the registration's path, when a terminal record names it

    def registration(self):
        """The registration's `{id, version, url, pid}`, or None when there is none or it can't be read. The
        password is left out: it is read again, inside `request`, for each request."""
        found = self._read()
        if not found:
            return None
        return {key: found.get(key) for key in ("id", "version", "url", "pid")}

    def _read(self):
        try:
            with open(self.path or registration_path(self.environ), encoding="utf-8") as handle:
                found = json.load(handle)
        except (OSError, ValueError):
            return None
        if not isinstance(found, dict) or not isinstance(found.get("url"), str):
            return None
        return found

    def request(self, method, path, body=None, query=None, limit=REQUEST_LIMIT):
        """One request; its JSON answer (None for no content). Raises ServiceError."""
        import base64
        import urllib.error
        import urllib.parse
        import urllib.request
        found = self._read()
        if not found:
            raise ServiceError("connect_failed", "OpenCode 2's service isn't running (no registration).")
        url = found["url"].rstrip("/") + path
        if query:
            url += "?" + urllib.parse.urlencode(query)
        data = None if body is None else json.dumps(body).encode()
        request = urllib.request.Request(url, data=data, method=method)
        password = found.get("password")
        if isinstance(password, str) and password:
            token = base64.b64encode(f"{USERNAME}:{password}".encode()).decode()
            request.add_header("Authorization", "Basic " + token)
        del found, password
        if data is not None:
            request.add_header("Content-Type", "application/json")
        try:
            with (self.opener or urllib.request.urlopen)(request, timeout=limit) as answer:
                text = answer.read()
        except urllib.error.HTTPError as error:
            status = error.code
            detail = _detail(error)
            if status == 409:
                raise ServiceError("conflict", "OpenCode 2's service already has this message.", status) from None
            raise ServiceError("provider_refused", detail or f"OpenCode 2's service answered {status}.", status) from None
        except TimeoutError:
            raise ServiceError("timeout", "OpenCode 2's service didn't answer in time.") from None
        except urllib.error.URLError as error:
            if isinstance(error.reason, TimeoutError):
                raise ServiceError("timeout", "OpenCode 2's service didn't answer in time.") from None
            raise ServiceError("connect_failed", "OpenCode 2's service refused the connection.") from None
        except OSError:
            raise ServiceError("connect_failed", "OpenCode 2's service refused the connection.") from None
        if not text:
            return None
        try:
            return json.loads(text)
        except ValueError:
            raise ServiceError("provider_refused", "OpenCode 2's service answered with something that isn't JSON.") from None

    # ----- the API kaiusap uses ----------------------------

    def info(self, limit=REQUEST_LIMIT):
        return self.request("GET", "/api/info", limit=limit)

    def session(self, session_id, limit=REQUEST_LIMIT):
        return _data(self.request("GET", "/api/session/" + _quote(session_id), limit=limit))

    def sessions(self, roots=True, limit=REQUEST_LIMIT, pages=20):
        """Every session, newest first, following the list's cursor; only roots when `roots`."""
        rows, cursor = [], None
        for _ in range(pages):
            query = {"order": "desc", "limit": "200"}
            if roots:
                query["parentID"] = "null"
            if cursor is not None:
                query["cursor"] = cursor if isinstance(cursor, str) else json.dumps(cursor)
            answer = self.request("GET", "/api/session", query=query, limit=limit) or {}
            data = answer.get("data") if isinstance(answer, dict) else None
            rows.extend(r for r in (data or []) if isinstance(r, dict))
            cursor = (answer.get("cursor") or {}).get("next") if isinstance(answer, dict) else None
            if not cursor or not data:
                break
        return rows

    def set_title(self, session_id, title, limit=REQUEST_LIMIT):
        return self.request("PATCH", "/api/session/" + _quote(session_id), {"title": title}, limit=limit)

    def prompt(self, session_id, msg_id, text, tag, delivery="steer", limit=REQUEST_LIMIT):
        """Admit `text` to the session as a prompt with the given `msg_` id and kaiusap's `metadata` tag."""
        body = {"id": msg_id, "text": text, "metadata": {"kaiusap": tag}, "delivery": delivery}
        return self.request("POST", "/api/session/" + _quote(session_id) + "/prompt", body, limit=limit)

    def context(self, session_id, limit=REQUEST_LIMIT):
        """The session's messages after its last compaction (`GET /api/session/:id/context`)."""
        return _data(self.request("GET", "/api/session/" + _quote(session_id) + "/context", limit=limit)) or []

    def models(self, limit=REQUEST_LIMIT):
        """The service's chat models (`GET /api/model`), each with `limit.context`."""
        answer = _data(self.request("GET", "/api/model", limit=limit))
        return answer if isinstance(answer, list) else []


def _quote(session_id):
    import urllib.parse
    return urllib.parse.quote(str(session_id), safe="")


def _data(answer):
    return answer.get("data") if isinstance(answer, dict) and "data" in answer else answer


def _detail(error):
    """An error answer's message, when it has one; never the request's headers."""
    try:
        body = json.loads(error.read() or b"null")
    except (OSError, ValueError):
        return None
    if isinstance(body, dict):
        for key in ("message", "error", "_tag"):
            value = body.get(key)
            if isinstance(value, str) and value:
                return value[:300]
            if isinstance(value, dict) and isinstance(value.get("message"), str):
                return value["message"][:300]
    return None


# ===== the adapter ====================================================================================

STARTS = "sessions/starts.jsonl"
PROMPTS = "sessions/prompts.jsonl"
TERMINALS = "terminals.jsonl"  # in the adapter's own directory, `sessions/opencode2/`
V1 = "opencode"  # OpenCode 1.x's provider, whose sessions OpenCode 2 copies on its first run
ROOT_SOURCES = {"startup", "resume", "clear", "fork"}
ACTING = {"send", "reply", "iam", "release", "moot", "unpause"}
# kaiusap's MCP tools as OpenCode 2 names them, `<server>_<tool>` (`core/src/tool/runtime.ts:290-293`), and
# `shell`, OpenCode 2's name for `bash`: the calls the plugin hands to the screen (`SCREENED_TOOLS` there).
TOOLS = ("whoami", "who", "iam", "release", "send", "reply", "inbox", "read", "moot", "pause", "unpause", "help")
TOOL_PREFIX = "kaiusap_"
SCREENED_TOOLS = frozenset({"shell", *(TOOL_PREFIX + tool for tool in TOOLS)})
# The hook input the plugin writes, each key with the JSON types it may have.
INPUT_KEYS = {"session_id": (str,), "agent_id": (str,), "cwd": (str,), "title": (str, type(None)),
              "hook_event_name": (str,), "parent_id": (str, type(None)), "fork_of": (str, type(None)),
              "has_history": (bool,), "prompt": (str,), "metadata": (dict, type(None)), "tool": (str,),
              "args": (dict,), "pid": (int,), "tmux_pane": (str, type(None)), "shows": (str, type(None)),
              "quit": (bool,)}
DEFAULT_TITLES = {"New session", "Child session"}  # shown for a session with no stored title

SCREEN_TIMEOUT_TEXT = ("kaiusap couldn't check this call in time, so it is refused. You may retry it: a spawned agent "
                       "may only look (whoami, who, inbox, read, help, pause), and those calls are allowed when they "
                       "get through.")
NO_ID = ("kaiusap can't tell which OpenCode 2 session is calling: the MCP call carried no session id, or kaiusap "
         "couldn't place it under OpenCode 2's service. Check that kaiusap's MCP server is in OpenCode's config: "
         "kaiusap setup." + UNIDENTIFIED_CLOSE)
UNKNOWN = "kaiusap can't tell whether this OpenCode 2 session is a subagent"
UNKNOWN_FIX = "Start OpenCode and try again."
COPY = "This conversation has no handle; if it is a copy of another one, the original keeps its handle."
UPGRADE = ("This is OpenCode 2's copy of the OpenCode session holding `{handle}`. To use that name here, say "
           "'you are {handle}'; that session's earlier messages stay with it.")
REASONS = {
    "not_live": "no running OpenCode 2 terminal shows this session",
    "detached": "the session is in a detached tmux session and wake_detached is off",
    "connect_failed": "OpenCode 2's service refused the connection or wasn't running",
    "provider_refused": "OpenCode 2 refused the prompt",
    "timeout": "a time limit was reached before the notice was sent",
    "no_confirmation": "no confirmation after the notice was sent",
    "store_failed": "a store failure interrupted the send after the attempt line was written and before the notice was sent",
}
FIXES = {
    "not_live": "show it in an OpenCode terminal",
    "detached": "attach it, or set wake_detached true",
    "connect_failed": "start OpenCode",
    "timeout": "try again",
}
DELIVERY_LIMIT = 20  # seconds
LOOKUP_LIMIT = 2  # a lookup outside a delivery: best effort
TERMINAL_WAIT = 1.5  # how long a new session's start waits for its terminal's report
TERMINAL_POLL = 0.05
# `setup`: the three items, at OpenCode 1.x's places; `setup` shows these or OpenCode 1.x's.
SETUP_PLUGIN, SETUP_MCP, SETUP_VERSION = 7, 8, 9
MIN_OPENCODE = (2, 2, 7)
PLUGIN_FILES = ("server.ts", "tui.ts", "hook.ts")
PLUGIN_NOTE = "OpenCode loads plugins when it starts; restart OpenCode, the service included."
MCP_COMMAND = ["kaiusap", "mcp", PROVIDER]
MCP_ENTRY = '"mcp": {"servers": {"kaiusap": {"type": "local", "command": ["kaiusap", "mcp", "opencode2"], "codemode": false}}}'
CODEMODE_NOTE = ("codemode isn't false, so the model sees only the first line of kaiusap's tool descriptions; "
                 'add "codemode": false to {where} in {path}')
ANCESTOR_STEPS = 4  # a shell command's process, its shell, the shell's parent (OpenCode 2 may run it under a pty)


def root_of(session_key):
    return (session_key or "").split(":", 1)[-1].split("/", 1)[0] or None


def no_title(title):
    """A session with no stored title: it counts as no name."""
    return not isinstance(title, str) or not title or title in DEFAULT_TITLES


def iso(value):
    """An OpenCode 2 time (milliseconds since the epoch) as the store's UTC instant, or None."""
    if type(value) not in (int, float) or value <= 0:
        return None
    return datetime.fromtimestamp(value / 1000, timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class OpenCode2Adapter(TerminalProcesses):
    process_label = "OpenCode 2"  # in log lines (adapters/terminal.py)
    provider = PROVIDER
    delivers = True
    harness_maker = "opencode"  # `sessions`
    session_variable = "OPENCODE_SESSION_ID"  # set by OpenCode 2's shell tool itself
    hook_events = frozenset({"tui.route", "session.start", "session.prompt", "tool.execute.before"})
    record_only_events = frozenset()
    # `tui.route` (the terminal part's report) has the start hook's 3 s, for its `ps` and tmux reads; it writes its own record in `events`
    # and maps to nothing, so it gets no session line and nothing is followed.
    start_events = frozenset({"session.start", "tui.route"})
    prompt_events = frozenset({"session.prompt"})
    screen_events = frozenset({"tool.execute.before"})
    screened_tools = SCREENED_TOOLS
    setup_flags = frozenset({"opencode", "opencode_config", "opencode_command"})  # choosing the OpenCode provider

    @classmethod
    def claims(cls, environment):
        """Whether a shell environment is one of this provider's sessions: OpenCode 2's own variable."""
        return bool(environment.get(cls.session_variable))

    # ----- `setup`: read-only --------------------------------

    @classmethod
    def setup_command(cls, setup):
        from .opencode_choice import command
        return command(setup)

    @classmethod
    def setup_items(cls, setup):
        """These items only when `setup` shows this provider."""
        from .opencode_choice import choice, config_dir, suggestion_note
        found = choice(setup)
        if found["shown"] != 2:
            return []
        directory = config_dir(setup)
        plugin = cls.setup_plugin(setup, directory)
        if note := suggestion_note(found):
            plugin["note"] = f"{plugin['note']} {note}" if plugin["note"] else note
        return [(SETUP_PLUGIN, plugin), (SETUP_MCP, cls.setup_mcp(setup, directory)), (SETUP_VERSION, cls.setup_version(setup))]

    @classmethod
    def setup_plugin(cls, setup, directory):
        """Done when `plugins/kaiusap/` holds the installed package's `plugins/opencode2` files (a link or a copy)."""
        from pathlib import Path
        from ..setup import short
        name = "OpenCode 2 plugin"
        target = directory / "plugins" / "kaiusap"
        from .opencode_choice import plugin_command
        command = plugin_command(setup, 2)
        if not (target / "server.ts").is_file():
            return setup.item(name, False, f"no kaiusap plugin directory in {short(directory / 'plugins')}", command, PLUGIN_NOTE)
        source = Path(setup.repo) / "plugins" / "opencode2"
        try:
            wanted = {f: (source / f).read_bytes() for f in PLUGIN_FILES}
        except (OSError, TypeError, ValueError):
            wanted = None  # an install from a URL: the plugin is recognised by its hook command instead
        try:
            found = {f: (target / f).read_bytes() for f in PLUGIN_FILES}
        except OSError:
            found = None
        if found and (found == wanted or (wanted is None and b'"kaiusap", "hook", "opencode2"' in found["hook.ts"])):
            return setup.item(name, True, f"kaiusap is loaded from {short(target)}", note=PLUGIN_NOTE)
        return setup.item(name, False, f"{short(target)} isn't this kaiusap's OpenCode 2 plugin", command, PLUGIN_NOTE)

    @staticmethod
    def setup_mcp(setup, directory):
        """Done when an OpenCode config file has kaiusap's server, local, running `kaiusap mcp opencode2`, not
        disabled: under `mcp.servers` (OpenCode 2's form) or `mcp` (1.x's, which OpenCode 2 also reads)."""
        from ..setup import short
        from .opencode_choice import strip_jsonc
        name = "kaiusap's MCP server in OpenCode's config"
        found = []
        for file in ("opencode.json", "opencode.jsonc"):  # `core/src/config/discovery.ts:12`
            path = directory / file
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
            servers = mcp.get("servers") if isinstance(mcp.get("servers"), dict) else {}
            entry, where = (servers.get("kaiusap"), "mcp.servers.kaiusap") if isinstance(servers.get("kaiusap"), dict) else (mcp.get("kaiusap"), "mcp.kaiusap")
            if not isinstance(entry, dict):
                continue
            from .opencode_choice import runs_kaiusap_mcp  # by name or by full path
            if entry.get("type") != "local" or not runs_kaiusap_mcp(entry.get("command"), PROVIDER):
                return setup.item(name, False, f"{where} in {short(path)} doesn't run kaiusap mcp opencode2", MCP_ENTRY)
            if entry.get("enabled") is False or entry.get("disabled") is True:
                return setup.item(name, False, f"{where} is disabled in {short(path)}", MCP_ENTRY)
            note = None if entry.get("codemode") is False else CODEMODE_NOTE.format(where=where, path=short(path))
            return setup.item(name, True, f"{where} in {short(path)}", note=note)
        where = short(found[0][0]) if found else short(directory / "opencode.json")
        return setup.item(name, False, f"no mcp.servers.kaiusap in {where}; add under \"mcp\": {{\"servers\": ...}}", MCP_ENTRY)

    @staticmethod
    def setup_version(setup):
        """`opencode --version`, from the shared probe: done for 2.2.7 or later within 2.x."""
        from .opencode_choice import choice, probe
        found = probe(setup)
        version = found["version"]
        if version is None:
            return setup.item("OpenCode version", False, found["detail"], found["command"])
        text = ".".join(map(str, version))
        if version < (2, 0, 0):
            if 2 in choice(setup)["installed"]:
                return setup.item("OpenCode version", False,
                                  f"{text}; kaiusap's OpenCode 2 plugin is installed, which doesn't load in OpenCode {text}",
                                  "kaiusap setup --opencode 1")
            return setup.item("OpenCode version", False, f"{text}; kaiusap's OpenCode 2 provider needs OpenCode 2.2.7 or later",
                              "npm install -g @opencode/cli")
        if version < MIN_OPENCODE or version >= (3, 0, 0):
            return setup.item("OpenCode version", False, f"{text}; kaiusap supports OpenCode 2.2.7 up to 3.0", "opencode upgrade")
        return setup.item("OpenCode version", True, text)

    # ----- one call ----------------------------------------------------------

    def __init__(self, store, run=None, environ=None, clock=None, parent_pid=None, service=None):
        self.store = store
        self.run = run or run_command
        self.environ = os.environ if environ is None else environ
        self.clock = clock or time.monotonic
        self.parent_pid = parent_pid  # the caller's parent, for tests; else os.getppid()
        self.service = service or Service(self.environ)  # the caller's own: its process test, and its hooks
        self._services = {}
        self.terminal_wait = TERMINAL_WAIT
        self._deadline = None
        self.reset()

    def begin(self):
        self.reset()

    def reset(self):
        self._processes, self._panes, self._parents, self._terminals = {}, None, None, None
        self._services = {}

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

    def ask(self, call, *args, limit=LOOKUP_LIMIT):
        """A lookup from the service: its answer, or None when it couldn't be had (logged, never the password)."""
        try:
            return call(*args, limit=self.limit(limit))
        except (ServiceError, TimeoutError) as exc:
            self.store.log(f"OpenCode 2 service {getattr(call, '__name__', 'request')}: {getattr(exc, 'code', 'timeout')}: {exc}")
            return None

    # ----- processes and panes: adapters/terminal.py, and these ----------

    def running(self, record):
        started = record.get("process_started_at")
        return bool(started) and self.started_at(record.get("pid")) == started

    def ancestors(self, steps=ANCESTOR_STEPS):
        import subprocess
        result, pid = [], os.getppid() if self.parent_pid is None else self.parent_pid
        for _ in range(steps):
            if not pid or pid in result:
                break
            result.append(pid)
            try:
                code, out = self.run(["ps", "-o", "ppid=", "-p", str(pid)], self.limit(2), None)
                pid = int(out.strip()) if not code else None
            except (OSError, subprocess.TimeoutExpired, ValueError, TimeoutError):
                break
        return result

    # ----- records ------------------------------------------------------------

    def terminals_path(self):
        return f"sessions/{PROVIDER}/{TERMINALS}"

    def terminal_rows(self):
        """Every terminal record, newest first, read once per call."""
        if self._terminals is None:
            self._terminals = list(self.store.rows(self.terminals_path(), reverse=True))
        return self._terminals

    def terminals(self):
        """Each terminal's latest record, newest first: a terminal is its process (pid and start time)."""
        seen, latest = set(), []
        for row in self.terminal_rows():
            terminal = (row.get("pid"), row.get("process_started_at"))
            if terminal in seen:
                continue
            seen.add(terminal)
            latest.append(row)
        return latest

    def showing(self, session_id, exclude=None):
        """The latest records of running terminals that show the session now, newest first."""
        return [row for row in self.terminals() if row.get("shows") == session_id and not row.get("quit")
                and (row.get("pid"), row.get("process_started_at")) != exclude and self.running(row)]

    def service_for(self, session_id):
        """The service a session runs in: the registration path in the newest running
        terminal's record showing it; else in the newest record that ever showed it; else in the newest running
        terminal's; else the caller's own."""
        path = None
        shown = self.showing(session_id) if session_id else []
        for row in shown + [r for r in self.terminal_rows() if r.get("shows") == session_id] + \
                [r for r in self.terminals() if not r.get("quit") and self.running(r)]:
            if isinstance(row.get("service"), str) and row["service"]:
                path = row["service"]
                break
        if not path:
            return self.service
        if path not in self._services:
            self._services[path] = Service(self.environ, self.service.opener, path)
        return self._services[path]

    def records(self, session_id, child=False):
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

    # ----- identity ----------------------------------------------

    def identity(self, environment, store):
        """A shell command's caller: `OPENCODE_SESSION_ID`, which OpenCode 2's shell tool sets, from a process
        under OpenCode 2's service (the process test)."""
        session = environment.get(self.session_variable)
        if not session:
            return Identity()
        cwd = environment.get("PWD") or os.getcwd()
        return self.place(session, cwd)

    def mcp_identity(self, meta, stated=None):
        """An MCP caller: `_meta["ai.opencode/sessionID"]`, on a server the service started."""
        session = meta.get("ai.opencode/sessionID") if isinstance(meta, dict) else None
        if not isinstance(session, str) or not session:
            raise Error(NO_ID)
        found = self.place(session, None)
        if found.classification == "unknown":
            raise Error(NO_ID)
        return found

    def place(self, session, cwd):
        """The session as root or spawned: its start records first, else its `parentID` chain from the service.
        The caller must run under the service the registration names; else, or when nothing places the
        session, it is `unknown`."""
        registration = self.service.registration()
        service_pid = registration.get("pid") if registration else None
        if type(service_pid) is not int or service_pid not in self.ancestors():
            return Identity(PROVIDER, session, None, cwd, "unknown",
                            reason="the caller doesn't run under OpenCode 2's service")
        for row in self.records(session, child=True):
            root = row["session_id"]
            return Identity(PROVIDER, root, f"{PROVIDER}:{root}/{session}", cwd or row.get("cwd"), "spawned",
                            f"{PROVIDER}:{root}", agent_id=session)
        rows = self.records(session)
        if rows:
            return Identity(PROVIDER, session, f"{PROVIDER}:{session}", cwd or rows[0].get("cwd"), "root")
        root, current = None, session
        for _ in range(16):
            info = self.ask(self.service.session, current)
            if not isinstance(info, dict):
                return Identity(PROVIDER, session, None, cwd, "unknown",
                                reason="the OpenCode service didn't answer")
            parent = info.get("parentID")
            if not parent:
                root = current
                break
            current = parent
        if root is None:
            return Identity(PROVIDER, session, None, cwd, "unknown", reason="its parent chain didn't end")
        if root == session:
            return Identity(PROVIDER, session, f"{PROVIDER}:{session}", cwd, "root")
        return Identity(PROVIDER, root, f"{PROVIDER}:{root}/{session}", cwd, "spawned", f"{PROVIDER}:{root}",
                        agent_id=session)

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
        """Whether the screen decides this call: a spawned agent's kaiusap tool call, or its shell command
        mentioning `kaiusap`. Malformed shell input is decided, so it fails closed."""
        if not isinstance(payload, dict) or not payload.get("agent_id"):
            return False
        tool = payload.get("tool")
        if tool == "shell":
            args = payload.get("args")
            command = args.get("command") if isinstance(args, dict) else None
            return not isinstance(command, str) or "kaiusap" in command
        return isinstance(tool, str) and tool.startswith(TOOL_PREFIX)

    @classmethod
    def pre_tool(cls, payload):
        if not cls.screened(payload):
            return None
        tool = payload.get("tool")
        if tool == "shell":
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
        if not isinstance(payload, dict):
            return False
        for key, value in payload.items():
            kinds = INPUT_KEYS.get(key)
            if kinds is None or type(value) not in kinds:
                return False
        return True

    def events(self, payload, deadline=None):
        from ..store import now
        if not self.clean_input(payload):
            return None
        name = payload.get("hook_event_name")
        self._deadline = deadline
        if name == "tui.route":
            self.terminal(payload, now())
            return None
        root, agent = payload.get("session_id"), payload.get("agent_id")
        if not isinstance(root, str) or not root or (agent is not None and not agent):
            return None
        at = now()
        key = f"{PROVIDER}:{root}" + (f"/{agent}" if agent else "")
        identity = Identity(PROVIDER, root, key, payload.get("cwd"), "spawned" if agent else "root",
                            f"{PROVIDER}:{root}" if agent else None, agent_id=agent)
        title = payload.get("title")
        event = {"provider": PROVIDER, "identity": identity, "session_id": root, "session_key": key, "agent_id": agent,
                 "cwd": payload.get("cwd"), "at": at, "title": None if no_title(title) else title, "record": None,
                 "prompt_record": None, "prompt": None, "hook_event_name": name, "tmux_pane": None,
                 "tmux_session": None, "terminal": None, "predecessor": None}
        if name == "session.start":
            parent, fork_of = payload.get("parent_id"), payload.get("fork_of")
            if bool(parent) != bool(agent):
                return None  # a child is named by both, or by neither
            if parent:
                source = "subagent"
            elif fork_of:
                source = "fork"
            elif payload.get("has_history"):
                source = "resume"
            else:
                source = self.new_session(event)
            # The prompt the start comes with: a notice that is the session's first prompt since the plugin loaded
            # is then the mention of its message, and the unread-mail note leaves it out.
            prompt = payload.get("prompt")
            event.update(kind=source, source=source, fork_of=fork_of, prompt=prompt if isinstance(prompt, str) else None)
            record = {"at": at, "provider": PROVIDER, "session_id": root, "session_key": key, "source": source}
            if source == "subagent":
                record["agent_id"] = agent
            record.update(cwd=event["cwd"], fork_of=fork_of)
            event["record"] = record
            return event
        if name == "session.prompt":
            if agent:
                return None  # a child session's prompts write nothing and get no note
            metadata = payload.get("metadata") or {}
            event.update(kind="prompt", prompt=payload.get("prompt"))
            if "kaiusap" not in metadata:  # kaiusap's own notice ends no chain
                event["prompt_record"] = {"at": at, "provider": PROVIDER, "session_id": root, "session_key": key,
                                          "source": "prompt", "cwd": event["cwd"]}
            return event
        return None

    def terminal(self, payload, at):
        """The terminal part's report: one terminal record, and nothing said."""
        pid = payload.get("pid")
        if type(pid) is not int or pid <= 0:
            return
        started = self.started_at(pid)
        if not started:
            self.store.log(f"OpenCode 2 terminal {pid}: no start time; its report can't make a session live")
            return
        record = {"at": at, "provider": PROVIDER, "pid": pid, "process_started_at": started, "tmux_pane": None,
                  "shows": payload.get("shows") if not payload.get("quit") else None, "cwd": payload.get("cwd"),
                  # Where this terminal's service registers: its environment's, which a kaiusap elsewhere can't know
                  # A path, never the password.
                  "service": registration_path(self.environ)}
        if payload.get("quit"):
            record["quit"] = True
        raw_pane = payload.get("tmux_pane")
        if raw_pane and self.environ.get("TMUX"):
            pane = {}
            self.pane_key(pane, raw_pane)
            record["tmux_pane"] = pane.get("tmux_pane")
        self.store.append(self.terminals_path(), record, self._deadline)

    def new_session(self, event):
        """A new session B is `clear` when its terminal showed A, then its home screen, then B, with
        nothing between; else `startup`. B's terminal report can come after its first prompt, so this waits for
        it, within the start hook's time."""
        session = event["session_id"]
        end = self.clock() + self.terminal_wait
        if self._deadline is not None:
            end = min(end, self._deadline - 1)
        while True:
            self._terminals = None
            rows = self.terminal_rows()
            mine = next((r for r in rows if r.get("shows") == session), None)
            if mine is not None or self.clock() >= end:
                break
            time.sleep(TERMINAL_POLL)
        if mine is None:
            return "startup"
        terminal = (mine.get("pid"), mine.get("process_started_at"))
        event["tui.route"] = terminal
        event["tmux_pane"] = mine.get("tmux_pane")
        history = [r for r in rows if (r.get("pid"), r.get("process_started_at")) == terminal]
        shown = []  # what the terminal showed before B, newest first, a repeated report counted once
        for row in history[history.index(mine) + 1:]:
            if row.get("quit"):
                break
            if row.get("shows") != session and (not shown or shown[-1] != row.get("shows")):
                shown.append(row.get("shows"))
            elif row.get("shows") == session and shown:
                break  # B shown earlier, then left: this isn't B's first showing
            if len(shown) == 2:
                break
        before = shown
        if len(before) == 2 and before[0] is None and isinstance(before[1], str) and before[1] != session:
            event["predecessor"] = before[1]
            return "clear"
        return "startup"

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

    def tell(self, text, event="session.prompt"):
        """A text the plugin adds to the turn's model requests in its `context` hook, not stored or shown."""
        return {"hookSpecificOutput": {"tell": text}}

    # ----- naming ---------------------------------------

    def hook_output(self, event, state):
        """The name is set from Python, at once: here only a name a call couldn't set, at the next start or
        prompt, while the service answers."""
        from ..model import held
        if event.get("agent_id") or event.get("kind") not in {"startup", "resume", "clear", "fork", "prompt"}:
            return {}
        ended, bindings = state.data["ended"], state.data["bindings"]
        key = event["session_key"]
        handle = held(bindings, key)
        events = state.data["events"]
        if self.service.registration() is None:
            return {}  # no service to name it in: `mirror` warned when the name was taken
        if handle:
            binding = bindings[handle]
            if not any(e.get("event") == "named" and e.get("binding") == binding["id"] and e.get("kind") == "handle"
                       for e in events):
                self.set_name(binding, handle, "handle", event)
        done = {e.get("binding") for e in events if e.get("event") == "named" and e.get("kind") == "retired"}
        for binding, left_at in ended:
            if binding["session_key"] == key and binding["id"] not in done and not handle:
                self.retire(binding, left_at, cause="hook")
        return {}

    def marker_fields(self, state, event):
        return {}

    def named(self, binding, name, kind, by):
        self.store.event("named", handle=binding["handle"], session_id=binding["session_id"],
                         session_key=binding["session_key"], binding=binding["id"], name=name, kind=kind, retry=False,
                         by_session=by.get("session_id"), by_session_key=by.get("session_key"),
                         deadline=by.get("deadline"))

    def set_name(self, binding, name, kind, by):
        """PATCH the title, then the `named` event; False when the service didn't take it."""
        try:
            self.service_for(binding["session_id"]).set_title(binding["session_id"], name, limit=self.limit(LOOKUP_LIMIT))
        except (ServiceError, TimeoutError) as exc:
            self.store.log(f"OpenCode 2 title {name} for {binding['session_key']} not set: {getattr(exc, 'code', 'timeout')}")
            return False
        try:
            self.named(binding, name, kind, by)
        except OSError as exc:
            self.store.warn(exc)
        return True

    def follow_session_name(self, session_id, name):
        """The tmux session name follows the name applied: the pane of the newest terminal showing the session."""
        from ..model import load_settings
        if load_settings(self.store)["tmux_session_names"] != "follow":
            return []
        shown = self.showing(session_id)
        if not shown or not shown[0].get("tmux_pane"):
            return []
        record = shown[0]
        warning = tmux.rename_session(self.run, self.limit, self.store.log, record["tmux_pane"], name, self.holds(record.get("pid")))
        return [warning] if warning else []

    def tmux_start(self, event):
        """The tmux window name isn't applied; the tmux session name's start rule runs when the name is applied."""
        return None

    def mirror(self, binding):
        """Every new binding is named at once with `PATCH /api/session/<id>`, live or not."""
        if not binding.get("session_key") or "/" in binding["session_key"]:
            return []
        try:
            self._deadline = self._deadline or self.clock() + LOOKUP_LIMIT * 2
            if not self.set_name(binding, binding["handle"], "handle", binding):
                return [f"kaiusap couldn't name the OpenCode 2 session {binding['handle']} now (its service didn't "
                        "answer); it is named at its next prompt."]
            return self.follow_session_name(binding["session_id"], binding["handle"])
        except (OSError, TimeoutError) as exc:
            self.store.log(f"OpenCode 2 rename at {binding.get('reason')} skipped: {type(exc).__name__}: {exc}")
            return []

    def retire(self, binding, left_at, cause=None):
        """Any ended binding, at once: the retirement name, when the session holds no handle now and its title is
        still the handle's."""
        from ..model import handles, held, retirement_name
        try:
            if held(handles(self.store), binding["session_key"]):
                return []
            self._deadline = self._deadline or self.clock() + LOOKUP_LIMIT * 2
            info = self.ask(self.service_for(binding["session_id"]).session, binding["session_id"])
            if not isinstance(info, dict) or info.get("title") != binding["handle"]:
                return []
            name = retirement_name(binding["handle"], left_at)
            if not self.set_name(binding, name, "retired", binding):
                return []
            return self.follow_session_name(binding["session_id"], name)
        except (OSError, ValueError, TimeoutError) as exc:
            self.store.log(f"OpenCode 2 retirement skipped: {type(exc).__name__}: {exc}")
            return []

    # ----- liveness -----------------------------------------------------------

    def status(self, session_key, wake_detached=None):
        from ..model import load_settings
        session = root_of(session_key)
        shown = self.showing(session) if session else []
        result = {"reason_code": None, "record": shown[0] if shown else None, "panes": [], "detached": False}
        if not shown:
            return {**result, "reason_code": "not_live"}
        pane = self.record_pane(shown[0])
        if pane:
            result["panes"] = [pane]
            result["detached"] = not pane["attached"]
        if result["detached"] and not (load_settings(self.store)["wake_detached"] if wake_detached is None else wake_detached):
            result["reason_code"] = "detached"
        return result

    def liveness(self, session_key):
        status = self.status(session_key)
        code = status["reason_code"]
        return {"can_be_woken": code is None, "reason_code": code, "why_not": REASONS.get(code), "fix": FIXES.get(code),
                "detached": status["detached"], "live": code != "not_live"}

    # ----- deliver -----------------------------------------

    def deliver(self, store, message, deadline=None):
        from ..notice import notice
        self._deadline = min(deadline, self.clock() + DELIVERY_LIMIT) if deadline is not None else self.clock() + DELIVERY_LIMIT
        try:
            try:
                status = self.status(message["to"]["session_key"])
            except (OSError, ValueError) as exc:
                store.warn(exc)
                return {**self.outcome("stored", "store_failed"), "store_failed_before_handover": True}
            if status["reason_code"]:
                return self.outcome("stored", status["reason_code"])
            limit = self.limit(DELIVERY_LIMIT)
        except TimeoutError:
            return self.outcome("stored", "timeout")
        try:
            self.service_for(message["to"]["session_id"]).prompt(message["to"]["session_id"], message_id(message),
                                                                 notice(message), message["id"], limit=limit)
        except ServiceError as exc:
            if exc.code == "conflict":
                return self.mentioned(store, message)  # the service admitted this notice before
            if exc.code == "timeout":
                return self.outcome("uncertain", "no_confirmation")
            if exc.code == "provider_refused":
                return {**self.outcome("stored", "provider_refused"), "reason": f"OpenCode 2 refused the prompt: {exc}"}
            return self.outcome("stored", "connect_failed")
        return self.mentioned(store, message)

    def mentioned(self, store, message):
        """The notice the service admitted is the
        message's mention to the session, so the `reminded` event the prompt hook would write is written here, and
        the native part answers the notice's prompt alone. A failed write is a warning; the outcome stands."""
        from ..model import handles
        result = self.outcome("woken")
        to = message["to"]
        binding = handles(store).get(to.get("handle"))
        try:
            store.event("reminded", message_id=message["id"], handle=to.get("handle"), session_id=to["session_id"],
                        session_key=to["session_key"],
                        binding=binding["id"] if binding and binding["session_key"] == to["session_key"] else None)
        except OSError as exc:
            store.warn(exc)
            result["warnings"] = [f"kaiusap couldn't record that {to['session_key']} was told of this message; "
                                  "its next prompt may mention it again."]
        return result

    @staticmethod
    def outcome(outcome, code=None):
        return {"outcome": outcome, "reason_code": code, "reason": REASONS.get(code), "provider_version": None, "warnings": []}

    # ----- list and where ---------------------------------------------------

    def where(self, session_key):
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
        import shlex
        from ..model import load_settings
        command = load_settings(self.store)["opencode2_command"]  # as the user types it, never run
        resume = f"{command} --session {shlex.quote(session)}"
        resume = f"cd {shlex.quote(cwd)} && {resume}" if cwd else resume
        info = self.ask(self.service_for(session).session, session)
        title = info.get("title") if isinstance(info, dict) else None
        row["name"] = None if no_title(title) else title
        if not record:
            return {**row, "where": "not_running", "command": resume}
        if status["panes"]:
            panes = [{k: v for k, v in p.items() if k != "pane_pid"} for p in status["panes"]]
            return {**row, "where": "tmux", "tmux_panes": panes, "command": tmux.attach_command(tmux.attach_pane(panes)["tmux_session"])}
        return {**row, "where": "not_in_tmux", "command": resume}

    def list(self):
        from ..model import handles
        keys = [f"{PROVIDER}:{row['shows']}" for row in self.terminals()
                if isinstance(row.get("shows"), str) and not row.get("quit") and self.running(row)]
        keys.extend(b["session_key"] for b in handles(self.store).values() if b and b["provider"] == PROVIDER)
        return [{"provider": PROVIDER, "session_id": root_of(key), "session_key": key, **self.where(key)}
                for key in dict.fromkeys(keys)]

    # ----- context use ------------------------------------------------------------

    def harness(self, session_key):
        return self.harness_maker

    def activity(self, session_key):
        """None: OpenCode 2's service endpoints are to be checked live first, and not until setting and clearing it on every path is proven."""
        return None

    def harness_version(self, session_key):
        """The service's version (`GET /api/info`), which runs every session, for a session a running terminal
        shows now. None otherwise, or when the service doesn't answer."""
        session = root_of(session_key)
        if not session or not self.showing(session):
            return None
        info = self.ask(self.service_for(session).info)
        version = info.get("version") if isinstance(info, dict) else None
        return version if isinstance(version, str) and version else None

    def context(self, session_key):
        """The newest assistant message with output tokens in the session's active context (since its last
        compaction): input, output, reasoning, cache read and cache write, as OpenCode's own total counts them
        (`schema/src/token-usage.ts`), against the model's context limit from the service's model list."""
        session = root_of(session_key)
        if not session:
            return None
        service = self.service_for(session)
        messages = self.ask(service.context, session)
        if not isinstance(messages, list):
            return None
        for message in reversed(messages):
            if not isinstance(message, dict) or message.get("type") != "assistant":
                continue
            tokens = message.get("tokens")
            if not isinstance(tokens, dict) or not (type(tokens.get("output")) in (int, float) and tokens["output"] > 0):
                continue
            cache = tokens.get("cache") if isinstance(tokens.get("cache"), dict) else {}
            counts = [tokens.get("input", 0), tokens.get("output", 0), tokens.get("reasoning", 0),
                      cache.get("read", 0), cache.get("write", 0)]
            if not all(type(c) in (int, float) and c >= 0 for c in counts):
                continue
            model = message.get("model") if isinstance(message.get("model"), dict) else {}
            model_id = model.get("id") if isinstance(model.get("id"), str) else None
            route = model.get("providerID") if isinstance(model.get("providerID"), str) else None
            at = (message.get("time") or {}).get("completed") or (message.get("time") or {}).get("created")
            return context_use(int(sum(counts)), self.model_limit(model_id, route, service), model_id, iso(at), route)
        return None

    def model_limit(self, model_id, route, service=None):
        if not model_id:
            return None
        models = self.ask((service or self.service).models)
        for model in models if isinstance(models, list) else []:
            if isinstance(model, dict) and model.get("id") == model_id and (route is None or model.get("providerID") == route):
                limit = (model.get("limit") or {}).get("context")
                return limit if type(limit) is int else None
        return None

    # ----- follow a start -----------------------------------------------

    def follow_start(self, event, store):
        self._deadline = event.get("deadline")
        kind = event.get("kind")
        proposal = {"tmux_session": event.get("tmux_session")}
        try:
            if kind == "fork":
                return self.step_fork(event, store)
            if kind == "clear":
                return {**proposal, **(self.step_clear(event, store) or {})}
            if kind == "resume":
                told = self.step_upgrade(event, store)
                return {**proposal, **told} if told else proposal
        except (OSError, ValueError, TimeoutError) as exc:
            store.log(f"start {event['session_key']}: couldn't follow: {type(exc).__name__}: {exc}")
            return {**proposal, "suggest": False}
        return proposal

    def step_fork(self, event, store):
        from ..model import handles, held
        original = event.get("fork_of")
        handle = held(handles(store), f"{PROVIDER}:{original}") if original else None
        if handle:
            return {"text": f"This conversation is a copy of the one holding {handle}, which keeps it; you have no handle.",
                    "suggest": False}
        return {"text": COPY, "suggest": False}

    def step_clear(self, event, store):
        """The move as for OpenCode 1.x: the predecessor A holds a handle and no other running terminal shows
        it; A with no handle continues as a persona; another running terminal showing A: no move."""
        from ..model import handles, held
        predecessor, terminal = event.get("predecessor"), event.get("tui.route")
        if not predecessor or not terminal:
            return None
        self._terminals = None
        if self.showing(predecessor, exclude=terminal):
            return None  # still live in another terminal: its mail goes there
        name = held(handles(store), f"{PROVIDER}:{predecessor}")
        if not name:
            return {"persona": {"continues": f"{PROVIDER}:{predecessor}", "via": "fresh-start"}}
        return {"move": {"handle": name, "from": f"{PROVIDER}:{predecessor}", "to": event["session_key"],
                         "reason": "fresh-start", "via": None},
                "text": f"You are {name}; the handle followed this conversation from session {predecessor}, which it replaced in this terminal.",
                "failed": f"You may be {name}: this conversation replaced its session in this terminal, but kaiusap couldn't follow it in time. Ask the user.",
                "suggest": False}

    def step_upgrade(self, event, store):
        """Upgrading from 1.x (proposed, for the user): a copied session's first start in OpenCode 2 is told
        the handle its OpenCode 1.x original holds. A persona belongs to one provider: no offer is written."""
        from ..model import handles, held
        if len(self.records(event["session_id"])) > 1:
            return None  # not its first start here
        handle = held(handles(store), f"{V1}:{event['session_id']}")
        return {"text": UPGRADE.format(handle=handle)} if handle else None

    def current(self, event, proposal):
        """Currency: the hook's terminal still shows the session, when it has one; else its record is the
        session's newest."""
        terminal = event.get("tui.route")
        if terminal:
            self._terminals = None
            latest = next((r for r in self.terminals() if (r.get("pid"), r.get("process_started_at")) == terminal), None)
            return bool(latest) and latest.get("shows") == event["session_id"]
        records = self.records(event["session_id"])
        return bool(records) and records[0].get("at") == event["at"]
