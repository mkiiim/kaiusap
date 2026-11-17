"""Claude's contract fixture: how to make Claude's inputs, for tests/test_contract.py.

It says nothing about what the adapter should do with them; the contract tests do.
"""
from datetime import datetime
import json
from pathlib import Path
import socket
import tempfile
import threading

from kaiusap.adapters.claude import ClaudeAdapter

SESSION = "12345678-1234-1234-1234-123456789abc"
AGENT = "87654321-4321-4321-4321-cba987654321"
PID = 10
STARTED = datetime(2026, 10, 1).astimezone().strftime("%a %b %d %H:%M:%S %Y")  # `ps` lstart, LC_TIME=C


def hook_input(event, **extra):
    """Claude Code 2.1.318's hook input (the base fields, plus each event's own)."""
    base = dict(session_id=SESSION, transcript_path=f"/fixture/{SESSION}.jsonl", cwd="/fixture",
                permission_mode="default", hook_event_name=event)
    if event == "UserPromptSubmit":
        base |= dict(scratchpad_dir="/private/tmp/x/scratchpad", prompt_id="3f2b1c4d-5e6f-4a7b-8c9d-0e1f2a3b4c5d",
                     effort={"level": "high"}, prompt="Hello", session_title="alpha")
    if event == "PreToolUse":
        base |= dict(tool_name="Bash", tool_input={"command": "ls"})
    return base | extra


# (event, input, the base vocabulary's kind it maps to, or None for an input that is no start or prompt)
INPUTS = [*(("SessionStart", hook_input("SessionStart", source=source), source)
            for source in ("startup", "resume", "clear", "compact", "fork")),
          ("UserPromptSubmit", hook_input("UserPromptSubmit"), "prompt"),
          ("PreToolUse", hook_input("PreToolUse"), None),
          # `activity`'s record-only hooks: the native part writes their records, Python maps them to nothing.
          ("PermissionRequest", hook_input("PermissionRequest", tool_name="Bash", tool_input={"command": "ls"}), None),
          ("Stop", hook_input("Stop", stop_hook_active=False), None),
          ("StopFailure", hook_input("StopFailure", error="api_error"), None),
          ("Elicitation", hook_input("Elicitation", mcp_server_name="fixture"), None)]
SPAWNED = [(event, hook_input(event, agent_id=AGENT, agent_type="general-purpose", **extra))
           for event, extra in (("SessionStart", {"source": "startup"}), ("UserPromptSubmit", {}))]
# A spawned agent's acting calls, which a screen refuses.
SCREENED = [("PreToolUse", hook_input("PreToolUse", agent_id=AGENT, tool_input={"command": "kaiusap send --to beta"})),
            ("PreToolUse", hook_input("PreToolUse", agent_id=AGENT, tool_name="mcp__plugin_kaiusap_kaiusap__reply",
                                      tool_input={"id": "x"}))]

# The fake programs the hook processes find on PATH (the native part and the Python adapter).
PROGRAMS = {"ps": f"printf %s '{STARTED} claude'", "claude": "printf '[]'", "codex": "exit 1", "tmux": "exit 1"}

# Inputs the native part and Python must record identically.
_prompt = hook_input("UserPromptSubmit")
NATIVE = [
    ("prompt", "UserPromptSubmit", _prompt),
    ("prompt-lean", "UserPromptSubmit", {k: _prompt[k] for k in ("session_id", "transcript_path", "cwd", "prompt", "session_title")}),
    ("prompt-effort-text", "UserPromptSubmit", _prompt | {"effort": "high"}),
    ("prompt-turn-id", "UserPromptSubmit", _prompt | {"turn_id": "t1"}),
    ("prompt-other-title", "UserPromptSubmit", _prompt | {"session_title": "beta"}),
    ("prompt-moved-transcript", "UserPromptSubmit", _prompt | {"transcript_path": "/moved/t.jsonl", "cwd": "/moved"}),
    ("prompt-no-transcript", "UserPromptSubmit", {k: v for k, v in _prompt.items() if k != "transcript_path"}),
    ("prompt-null-transcript", "UserPromptSubmit", _prompt | {"transcript_path": None}),
    ("prompt-extra-key", "UserPromptSubmit", _prompt | {"surprise": 1}),
    ("prompt-notice", "UserPromptSubmit", _prompt | {"prompt": "kaiusap: 1 unread message"}),
    ("prompt-mentions-kaiusap", "UserPromptSubmit", _prompt | {"prompt": "Explain kaiusap: ordinary"}),
    ("prompt-paste-line", "UserPromptSubmit", _prompt | {"prompt": "Run kaiusap read abc"}),
    ("prompt-unicode", "UserPromptSubmit", _prompt | {"cwd": "/fixture/café \t\"q\"", "prompt": "café \\ \"q\""}),
    ("prompt-spawned", "UserPromptSubmit", _prompt | {"agent_id": AGENT}),
    ("prompt-empty-agent", "UserPromptSubmit", _prompt | {"agent_id": ""}),
    ("prompt-no-session", "UserPromptSubmit", {k: v for k, v in _prompt.items() if k != "session_id"}),
    ("prompt-no-prompt", "UserPromptSubmit", {k: v for k, v in _prompt.items() if k != "prompt"}),
    ("pretool-no-agent", "PreToolUse", hook_input("PreToolUse", tool_input={"command": "kaiusap send"})),
    ("pretool-plain-bash", "PreToolUse", hook_input("PreToolUse", agent_id=AGENT)),
    ("start", "SessionStart", hook_input("SessionStart", source="startup")),
]
NATIVE = [(label, event, payload, PROGRAMS) for label, event, payload in NATIVE]
# Inputs the native part answers alone, with no work, once Python has settled the session.
SETTLED = [("UserPromptSubmit", _prompt),
           ("UserPromptSubmit", {k: _prompt[k] for k in ("session_id", "transcript_path", "cwd", "prompt", "session_title")})]


class Inbox:
    """A fake inbox socket: any number of connections, one line each."""

    def __init__(self):
        self.directory = tempfile.TemporaryDirectory(prefix="kai-")
        self.path = str(Path(self.directory.name) / "inbox.sock")
        self.server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.server.bind(self.path)
        self.server.listen()
        self.lines = []
        self.thread = threading.Thread(target=self.receive, daemon=True)
        self.thread.start()

    def receive(self):
        while True:
            try:
                connection, _ = self.server.accept()
            except OSError:
                return
            with connection:
                data = b""
                while chunk := connection.recv(8192):
                    data += chunk
            self.lines.append(data)

    def close(self):
        self.server.close()
        self.thread.join(2)
        self.directory.cleanup()


class World:
    """Claude's fakes for one test: `claude agents --json`, `ps`, tmux and the inbox socket, all in-process."""
    session, agent = SESSION, AGENT

    def __init__(self, store, tmp_path):
        self.rows = []  # the listing
        self.inbox = None
        self.environ = {}
        self.adapter = ClaudeAdapter(store, run=self.run, environ=self.environ)

    def run(self, args, limit, env):
        if args[0] == "claude":
            return 0, json.dumps(self.rows)
        if args[:2] == ["ps", "-A"]:
            return 0, f"{PID} 1\n1 0\n"
        if args[0] == "ps" and args[2] == "ppid=":
            return 0, "1" if int(args[-1]) == PID else "0"
        if args[0] == "ps":
            return (0, f"{STARTED} /fixture/claude") if int(args[-1]) == PID else (1, "")
        return 1, ""  # no tmux server

    def environment(self):
        """The session's own shell."""
        return {"CLAUDE_CODE_SESSION_ID": SESSION, "CLAUDE_PID": str(PID), "PWD": "/fixture"}

    def reachable(self, name=None):
        """The session runs in process PID with an inbox socket, the listing names it `name`."""
        self.inbox = Inbox()
        self.environ.update(CLAUDE_PID=str(PID), CLAUDE_CODE_MESSAGING_SOCKET=self.inbox.path)
        self.rows[:] = [{"pid": PID, "sessionId": SESSION, "cwd": "/fixture", "kind": "interactive", "status": "idle",
                         **({"name": name} if name else {})}]

    def close(self):
        if self.inbox:
            self.inbox.close()
