"""OpenCode's contract fixture: how to make OpenCode's inputs, for tests/test_contract.py.

It says nothing about what the adapter should do with them; the contract tests do. The hook
inputs are the ones kaiusap's plugin writes (`plugins/opencode/kaiusap.ts`).
"""
from datetime import datetime
import json
import os
from pathlib import Path
import socket
import tempfile
import threading

from kaiusap.adapters.opencode import OpenCodeAdapter

SESSION = "ses_e39ba0254ffe3e3h6ZR3gtwKN2"
AGENT = "ses_e39b9f1a2ffeAbCdEf0123456789"
PID = 10
STARTED = datetime(2026, 10, 1).astimezone().strftime("%a %b %d %H:%M:%S %Y")  # `ps` lstart, LC_TIME=C
# The plugin's socket for the session's process: a short path, as the plugin's own is.
SOCKET_DIR = tempfile.mkdtemp(prefix="kai-oc-")
SOCKET = os.path.join(SOCKET_DIR, f"{PID}-0a1b2c3d.sock")
TITLE = "New session - 2026-11-10T16:23:58.123Z"


def hook_input(event, **extra):
    """The plugin's hook input: the shared keys, then each event's own."""
    base = dict(session_id=SESSION, cwd="/fixture", pid=PID, socket=SOCKET, tmux_pane=None, title=TITLE)
    if event == "session.start":
        base |= dict(parent_id=None, created_here=True, roots_started_here=0, prompted_roots=[])
    elif event == "chat.message":
        base |= dict(prompt="Hello")
    elif event == "tool.execute.before":
        base |= dict(tool="bash", args={"command": "ls"})
    return base | extra


def child(event, **extra):
    """A child session's input: `session_id` the root, `agent_id` its own id."""
    payload = hook_input(event, agent_id=AGENT, title="Child session - 2026-11-10T16:25:00.000Z", **extra)
    if event == "session.start":
        payload |= dict(parent_id=SESSION)
    return payload


# (event, input, the base vocabulary's kind it maps to, or None for an input that is no start or prompt)
INPUTS = [("session.start", hook_input("session.start"), "startup"),
          ("session.start", hook_input("session.start", created_here=False), "resume"),
          ("session.start", hook_input("session.start", roots_started_here=1,
                                       prompted_roots=[{"session_id": "ses_other", "at": "2026-11-10T16:20:00.000Z"}]), "clear"),
          ("session.start", hook_input("session.start", title="alpha (fork #1)"), "fork"),
          ("chat.message", hook_input("chat.message"), "prompt"),
          ("tool.execute.before", hook_input("tool.execute.before"), None)]
SPAWNED = [("session.start", child("session.start")),
           ("chat.message", child("chat.message")),
           ("tool.execute.before", child("tool.execute.before"))]
# A spawned agent's acting calls, which the screen refuses.
SCREENED = [("tool.execute.before", child("tool.execute.before", args={"command": "kaiusap send --to beta"})),
            ("tool.execute.before", child("tool.execute.before", tool="kaiusap_reply", args={"id": "x"}))]

# The fake programs the hook processes find on PATH (the Python adapter's `ps` and tmux reads).
PROGRAMS = {"ps": f"printf %s '{STARTED}'", "tmux": "exit 1", "claude": "printf '[]'", "codex": "exit 1"}

# Inputs the native part and Python must record identically.
_prompt = hook_input("chat.message")
NATIVE = [
    ("prompt", "chat.message", _prompt),
    ("prompt-named", "chat.message", _prompt | {"title": "alpha"}),
    ("prompt-no-socket", "chat.message", _prompt | {"socket": None, "title": None}),
    ("prompt-in-tmux", "chat.message", _prompt | {"tmux_pane": "%3"}),
    ("prompt-extra-key", "chat.message", _prompt | {"surprise": 1}),
    ("prompt-float-pid", "chat.message", _prompt | {"pid": 10.5}),
    ("prompt-notice", "chat.message", _prompt | {"prompt": "kaiusap: 1 unread message"}),
    ("prompt-mentions-kaiusap", "chat.message", _prompt | {"prompt": "Explain kaiusap: ordinary"}),
    ("prompt-paste-line", "chat.message", _prompt | {"prompt": "Run kaiusap read abc"}),
    ("prompt-unicode", "chat.message", _prompt | {"cwd": "/fixture/café \t\"q\"", "prompt": "café \\ \"q\""}),
    ("prompt-child", "chat.message", child("chat.message")),
    ("prompt-empty-agent", "chat.message", _prompt | {"agent_id": ""}),
    ("prompt-negative-zero-pid", "chat.message", _prompt | {"pid": -0.0}),
    ("prompt-no-session", "chat.message", {k: v for k, v in _prompt.items() if k != "session_id"}),
    ("prompt-no-prompt", "chat.message", {k: v for k, v in _prompt.items() if k != "prompt"}),
    ("tool-root", "tool.execute.before", hook_input("tool.execute.before", tool="kaiusap_send", args={})),
    ("tool-child-bash", "tool.execute.before", child("tool.execute.before")),
    ("start", "session.start", hook_input("session.start")),
]
NATIVE = [(label, event, payload, PROGRAMS) for label, event, payload in NATIVE]
# Inputs the native part answers alone, with no work, once Python has settled the session.
SETTLED = [("chat.message", _prompt)]


HARNESS_VERSION = "1.19.5"  # the plugin's `context` answer below


class Plugin:
    """A fake of the plugin's socket: one request line per connection, one answer line."""

    def __init__(self, title=None):
        Path(SOCKET).unlink(missing_ok=True)
        self.title = title
        self.lines = []
        self.server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.server.bind(SOCKET)
        self.server.listen()
        self.thread = threading.Thread(target=self.serve, daemon=True)
        self.thread.start()

    def answer(self, request):
        if request.get("session_id") not in {SESSION, AGENT}:
            return {"ok": False, "code": "not_live"}
        kind = request.get("type")
        if kind == "rename":
            self.title = request.get("name")
            return {"ok": True}
        if kind == "session":
            return {"ok": True, "root": SESSION, "parent_id": None if request["session_id"] == SESSION else SESSION,
                    "title": self.title}
        if kind == "context":
            return {"ok": True, "limits": {"opencode/big-pickle": 200000}, "version": "1.19.5",
                    "messages": [{"tokens": {"input": 1200, "output": 30, "reasoning": 0, "cache": {"read": 4000, "write": 0}},
                                  "model": "big-pickle", "provider": "opencode", "at": 1794328800000}]}
        return {"ok": True}

    def serve(self):
        while True:
            try:
                connection, _ = self.server.accept()
            except OSError:
                return
            with connection:
                data = b""
                while b"\n" not in data and (chunk := connection.recv(8192)):
                    data += chunk
                request = json.loads(data.split(b"\n", 1)[0])
                self.lines.append(request)
                connection.sendall((json.dumps(self.answer(request)) + "\n").encode())

    def close(self):
        self.server.close()
        self.thread.join(2)
        Path(SOCKET).unlink(missing_ok=True)


class World:
    """OpenCode's fakes for one test: `ps` for the opencode process and its plugin's socket, all in-process."""
    session, agent = SESSION, AGENT

    def __init__(self, store, tmp_path):
        self.plugin = None
        self.adapter = OpenCodeAdapter(store, run=self.run, environ={}, parent_pid=PID)

    def run(self, args, limit, env):
        if args[:2] == ["ps", "-A"]:
            return 0, f"{PID} 1\n1 0\n"
        if args[0] == "ps" and "ppid=" in args:
            return 0, "1" if int(args[-1]) == PID else "0"
        if args[0] == "ps":
            return (0, STARTED) if int(args[-1]) == PID else (1, "")
        return 1, ""  # no tmux server

    def environment(self):
        """The session's own shell: the plugin's variable and OpenCode's pid."""
        return {"KAIUSAP_OPENCODE_SESSION": SESSION, "OPENCODE_PID": str(PID), "PWD": "/fixture"}

    def reachable(self, name=None):
        """The process PID runs with the plugin serving its socket; the session is titled `name`."""
        self.plugin = Plugin(name)

    def close(self):
        if self.plugin:
            self.plugin.close()
