"""OpenCode 2's contract fixture: how to make OpenCode 2's inputs, for tests/test_contract.py.

It says nothing about what the adapter should do with them; the contract tests do. The hook
inputs are the ones kaiusap's plugin writes (`plugins/opencode2/`): the service part's `session.start`,
`prompt` and `tool.execute.before`, and the terminal part's `terminal`.
"""
from datetime import datetime
import json
import socket
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

from kaiusap.adapters.opencode2 import OpenCode2Adapter

SESSION = "ses_1a2b3c4d5e6fAbCdEfGhIjKlMn"
AGENT = "ses_1a2b3c4d5e70AbCdEfGhIjKlMn"
OTHER = "ses_1a2b3c4d5e00ZyXwVuTsRqPoNm"  # the session a terminal showed before its home screen
TERMINAL_PID, SHELL_PID, SERVICE_PID = 10, 20, 30
STARTED = datetime(2026, 10, 1).astimezone().strftime("%a %b %d %H:%M:%S %Y")  # `ps` lstart, LC_TIME=C
PASSWORD = "fixture-password"


def hook_input(event, **extra):
    """The plugin's hook input for the root session."""
    if event == "tui.route":
        return dict(pid=TERMINAL_PID, tmux_pane=None, shows=SESSION, cwd="/fixture") | extra
    base = dict(session_id=SESSION, cwd="/fixture", title=None)
    if event == "session.start":
        base |= dict(parent_id=None, fork_of=None, has_history=False)
    elif event == "session.prompt":
        base |= dict(prompt="Hello", metadata=None)
    elif event == "tool.execute.before":
        base |= dict(tool="shell", args={"command": "ls"})
    return base | extra


def child(event, **extra):
    """A child session's input: `session_id` the root, `agent_id` its own id."""
    payload = hook_input(event, agent_id=AGENT, **extra)
    if event == "session.start":
        payload |= dict(parent_id=SESSION)
    return payload


# (event, input, the base vocabulary's kind it maps to, or None for an input that is no start or prompt). The order
# matters to the `clear` input: the three terminal reports before it show OTHER, then the home screen, then SESSION.
INPUTS = [("session.start", hook_input("session.start"), "startup"),
          ("session.start", hook_input("session.start", has_history=True), "resume"),
          ("session.start", hook_input("session.start", fork_of=OTHER), "fork"),
          ("tui.route", hook_input("tui.route", shows=OTHER), None),
          ("tui.route", hook_input("tui.route", shows=None), None),
          ("tui.route", hook_input("tui.route"), None),
          ("session.start", hook_input("session.start"), "clear"),
          ("session.prompt", hook_input("session.prompt"), "prompt"),
          ("tool.execute.before", hook_input("tool.execute.before"), None)]
SPAWNED = [("session.start", child("session.start")),
           ("session.prompt", child("session.prompt")),
           ("tool.execute.before", child("tool.execute.before"))]
# A spawned agent's acting calls, which the screen refuses.
SCREENED = [("tool.execute.before", child("tool.execute.before", args={"command": "kaiusap send --to beta"})),
            ("tool.execute.before", child("tool.execute.before", tool="kaiusap_reply", args={"id": "x"}))]

# The fake programs the hook processes find on PATH (the Python adapter's `ps` and tmux reads).
PROGRAMS = {"ps": f"printf %s '{STARTED}'", "tmux": "exit 1", "claude": "printf '[]'", "codex": "exit 1"}


# Inputs the native part and Python must record identically.
_prompt = hook_input("session.prompt")
NATIVE = [
    ("prompt", "session.prompt", _prompt),
    ("prompt-named", "session.prompt", _prompt | {"title": "alpha"}),
    ("prompt-notice", "session.prompt", _prompt | {"metadata": {"kaiusap": "20261112T000000Z-abcdef"}}),
    ("prompt-other-metadata", "session.prompt", _prompt | {"metadata": {"source": "x"}}),
    ("prompt-text-like-a-notice", "session.prompt", _prompt | {"prompt": "kaiusap: 1 unread message"}),
    ("prompt-paste-line", "session.prompt", _prompt | {"prompt": "Run kaiusap read abc"}),
    ("prompt-unicode", "session.prompt", _prompt | {"cwd": "/fixture/café \t\"q\"", "prompt": "café \\ \"q\""}),
    ("prompt-extra-key", "session.prompt", _prompt | {"surprise": 1}),
    ("prompt-child", "session.prompt", child("session.prompt")),
    ("prompt-empty-agent", "session.prompt", _prompt | {"agent_id": ""}),
    ("prompt-no-session", "session.prompt", {k: v for k, v in _prompt.items() if k != "session_id"}),
    ("prompt-no-prompt", "session.prompt", {k: v for k, v in _prompt.items() if k != "prompt"}),
    ("tool-root", "tool.execute.before", hook_input("tool.execute.before", tool="kaiusap_send", args={})),
    ("tool-child-shell", "tool.execute.before", child("tool.execute.before")),
    ("start", "session.start", hook_input("session.start")),
]
NATIVE = [(label, event, payload, PROGRAMS) for label, event, payload in NATIVE]
# Inputs the native part answers alone, with no work, once Python has settled the session.
SETTLED = [("session.prompt", _prompt)]


HARNESS_VERSION = "2.2.7"  # the fake service's `/api/info`


class Service:
    """A fake of OpenCode 2's service, on a port registered from the start and served only
    once the session is reachable, so a call before that finds the connection refused."""

    def __init__(self, state_home):
        probe = socket.socket()
        probe.bind(("127.0.0.1", 0))
        self.port = probe.getsockname()[1]
        probe.close()
        self.title = None
        self.server = None
        path = state_home / "opencode" / "service.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"id": "svc", "version": "2.2.7", "url": f"http://127.0.0.1:{self.port}",
                                    "pid": SERVICE_PID, "password": PASSWORD}))
        path.chmod(0o600)

    def serve(self, title):
        self.title = title
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def answer(self, status, body=None):
                data = b"" if body is None else json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def route(self, method):
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length)) if length else None
                path = self.path.split("?", 1)[0]
                parts = path.split("/")
                if path == "/api/info":
                    return self.answer(200, {"version": "2.2.7", "pid": SERVICE_PID})
                if path == "/api/model":
                    return self.answer(200, {"data": [{"id": "big-pickle", "providerID": "opencode",
                                                       "limit": {"context": 200000, "output": 32000}}]})
                if len(parts) < 4 or parts[2] != "session" or parts[3] not in {SESSION, AGENT}:
                    return self.answer(404, {"message": "Session not found"})
                if len(parts) == 4 and method == "GET":
                    info = {"id": parts[3], "title": fake.title or "New session", "location": {"directory": "/fixture"}}
                    if parts[3] == AGENT:
                        info["parentID"] = SESSION
                    return self.answer(200, {"data": info})
                if len(parts) == 4 and method == "PATCH":
                    fake.title = body.get("title", fake.title)
                    return self.answer(204)
                if len(parts) == 5 and parts[4] == "prompt" and method == "POST":
                    return self.answer(200, {"data": {"id": body["id"]}})
                if len(parts) == 5 and parts[4] == "context":
                    return self.answer(200, {"data": [
                        {"type": "user", "id": "msg_1"},
                        {"type": "assistant", "id": "msg_2", "model": {"id": "big-pickle", "providerID": "opencode"},
                         "tokens": {"input": 1200, "output": 30, "reasoning": 0, "cache": {"read": 4000, "write": 0}},
                         "time": {"created": 1794328800000, "completed": 1794328801000}}]})
                return self.answer(404, {"message": "no route"})

            def do_GET(self):
                self.route("GET")

            def do_POST(self):
                self.route("POST")

            def do_PATCH(self):
                self.route("PATCH")

        self.server = HTTPServer(("127.0.0.1", self.port), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        if self.server:
            self.server.shutdown()
            self.server.server_close()


class World:
    """OpenCode 2's fakes for one test: `ps` for the terminal, the shell and the service, and the service."""
    session, agent = SESSION, AGENT

    def __init__(self, store, tmp_path):
        state = tmp_path / "opencode2-state"
        self.service = Service(state)
        self.adapter = OpenCode2Adapter(store, run=self.run, environ={"XDG_STATE_HOME": str(state)}, parent_pid=SHELL_PID)
        self.adapter.terminal_wait = 0.05

    def run(self, args, limit, env):
        if args[:2] == ["ps", "-A"]:
            return 0, f"{TERMINAL_PID} 1\n{SHELL_PID} {SERVICE_PID}\n{SERVICE_PID} 1\n1 0\n"
        if args[0] == "ps" and "ppid=" in args:
            return 0, {SHELL_PID: str(SERVICE_PID), SERVICE_PID: "1"}.get(int(args[-1]), "0")
        if args[0] == "ps":
            return (0, STARTED) if int(args[-1]) == TERMINAL_PID else (1, "")
        return 1, ""  # no tmux server

    def environment(self):
        """A command the session's shell tool runs: OpenCode 2 sets these itself."""
        return {"OPENCODE_SESSION_ID": SESSION, "OPENCODE": "1", "PWD": "/fixture"}

    def reachable(self, name=None):
        """The service runs, and a terminal shows the session, titled `name`."""
        self.service.serve(name)
        self.adapter.events({**hook_input("tui.route"), "hook_event_name": "tui.route"})
        self.adapter.reset()

    def close(self):
        self.service.close()
