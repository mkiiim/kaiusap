"""Codex's contract fixture: how to make Codex's inputs, for tests/test_contract.py.

It says nothing about what the adapter should do with them; the contract tests do.
"""
import json
import os
import tempfile

from fake_codex import FakeDaemon
from kaiusap.adapters.codex import CodexAdapter

SESSION = "12345678-1234-1234-1234-123456789abc"
AGENT = "87654321-4321-4321-4321-cba987654321"
TRANSCRIPT = f"/fixture/rollout-2026-11-04T12-00-00-{SESSION}.jsonl"


def hook_input(event, **extra):
    """Codex 0.166.0's hook input (`codex-rs/hooks/src/schema.rs`)."""
    base = dict(session_id=SESSION, transcript_path=TRANSCRIPT, cwd="/fixture", hook_event_name=event,
                model="gpt-5.5", permission_mode="default")
    if event == "UserPromptSubmit":
        base |= dict(turn_id="019a0000-0000-7000-8000-000000000001", prompt="Hello")
    return base | extra


# (event, input, the base vocabulary's kind it maps to, or None for an input that is no start or prompt)
INPUTS = [*(("SessionStart", hook_input("SessionStart", source=source), source)
            for source in ("startup", "resume", "clear", "compact", "fork")),
          ("UserPromptSubmit", hook_input("UserPromptSubmit"), "prompt"),
          ("SubagentStart", hook_input("SubagentStart", agent_id=AGENT, agent_type="worker"), "subagent")]
SPAWNED = [("UserPromptSubmit", hook_input("UserPromptSubmit", agent_id=AGENT, agent_type="worker")),
           ("SessionStart", hook_input("SessionStart", source="startup", agent_id=AGENT)),
           ("SubagentStart", hook_input("SubagentStart", agent_id=AGENT, agent_type="worker"))]


def tmux(panes, code=0):
    """A fake `tmux` printing `panes` (the list-panes rows), exiting `code`."""
    return b"#!/bin/sh\n/bin/cat <<'EOF'\n" + (panes.encode() if isinstance(panes, str) else panes) + b"EOF\nexit " + str(code).encode() + b"\n"


ONE_PANE = f"%7\talpha\t1\t444\t/dev/fake\t4321\t1793606400\tCodex {SESSION}\n"
OTHER = "aaaaaaaa-4321-4321-4321-cba987654321"
MIXED = (f"%1\ta\t0\t11\t/dev/t1\t4321\t1793606400\tCodex {SESSION}\n"
         f"%2\tb\t1\t12\t/dev/t2\t4321\t1793606400\t{SESSION[:29]}... working\n"
         f"%3\tc\t1\t13\t/dev/t3\t4321\t1793606400\tx{SESSION}\n"
         f"%4\td\t1\tnotdigits\t/dev/t4\t4321\t1793606400\tCodex {SESSION}\n"
         f"%5\te\t1\t15\t/dev/t5\n"
         f"%6\tf\t1\t16\t/dev/t6\t4321\t1793606400\tCodex {OTHER}\n"
         f"%8\tg\t1\t17\t/dev/t8\t4321\t1793606400\ttab\tin title {SESSION}\n")

# The fake programs the hook processes find on PATH (the native part and the Python adapter).
PROGRAMS = {"tmux": tmux(ONE_PANE), "ps": "exit 1", "claude": "printf '[]'", "codex": "exit 1"}

# Inputs the native part and Python must record identically, each with
# the programs it changes.
_prompt = hook_input("UserPromptSubmit")
_review = f"/fixture/rollout-2026-11-04T12-00-00-{OTHER}.jsonl"
NATIVE = [
    ("prompt", "UserPromptSubmit", _prompt, {}),
    ("prompt-claude-keys", "UserPromptSubmit", _prompt | {"prompt_id": "p", "effort": {"level": "x"}}, {}),
    ("prompt-mixed-panes", "UserPromptSubmit", _prompt, {"tmux": tmux(MIXED)}),
    ("prompt-no-panes", "UserPromptSubmit", _prompt, {"tmux": tmux("")}),
    ("prompt-tmux-fails", "UserPromptSubmit", _prompt, {"tmux": tmux(ONE_PANE, 1)}),
    ("prompt-tmux-not-utf8", "UserPromptSubmit", _prompt, {"tmux": tmux(b"%7\ta\t1\t1\t/d\t1\t2\t\xff\n")}),
    ("prompt-notice", "UserPromptSubmit", _prompt | {"prompt": "kaiusap: 1 unread message"}, {}),
    ("prompt-mentions-kaiusap", "UserPromptSubmit", _prompt | {"prompt": "Explain kaiusap: ordinary"}, {}),
    ("prompt-no-transcript", "UserPromptSubmit", {k: v for k, v in _prompt.items() if k != "transcript_path"}, {}),
    ("prompt-null-transcript", "UserPromptSubmit", _prompt | {"transcript_path": None}, {}),
    ("prompt-review", "UserPromptSubmit", _prompt | {"transcript_path": _review}, {}),
    ("prompt-spawned", "UserPromptSubmit", _prompt | {"agent_id": AGENT}, {}),
    ("prompt-non-uuid-session", "UserPromptSubmit", _prompt | {"session_id": "abc"}, {}),
    ("prompt-unicode-cwd", "UserPromptSubmit", _prompt | {"cwd": "/fixture/café \t\"q\""}, {}),
    ("prompt-unicode-prompt", "UserPromptSubmit", _prompt | {"prompt": "café \\ \"q\""}, {}),
    ("subagent", "SubagentStart", hook_input("SubagentStart", agent_id=AGENT, agent_type="worker"), {}),
    ("subagent-review", "SubagentStart", hook_input("SubagentStart", transcript_path=_review), {}),
    ("subagent-no-agent", "SubagentStart", hook_input("SubagentStart"), {}),
    ("subagent-null-transcript", "SubagentStart", hook_input("SubagentStart", agent_id=AGENT, transcript_path=None), {}),
    ("start", "SessionStart", hook_input("SessionStart", source="startup"), {}),
]
NATIVE = [(label, event, payload, PROGRAMS | programs) for label, event, payload, programs in NATIVE]
# Inputs the native part answers alone, with no work, once Python has settled the session.
SETTLED = [("UserPromptSubmit", _prompt)]


HARNESS_VERSION = "0.166.0"  # the faked `daemon version`'s appServerVersion
ACTIVITY = "idle"  # the fake daemon's thread/read `status`, for `activity`


class World:
    """Codex's fakes for one test: the daemon (fake_codex.FakeDaemon), `codex app-server daemon version`,
    tmux and ps, all in-process."""
    session, agent = SESSION, AGENT

    def __init__(self, store, tmp_path):
        self.directory = tempfile.TemporaryDirectory(prefix="kai-")  # a short path for the daemon's Unix socket
        self.daemon = None
        self.adapter = CodexAdapter(store, run=self.run, codex_home=tmp_path / "codex-home")

    def run(self, args, limit, env):
        if args[0] == "codex":
            if not self.daemon:
                return 1, ""
            return 0, json.dumps({"status": "running", "socketPath": self.daemon.path, "appServerVersion": "0.166.0"})
        return 1, ""  # no tmux server; no ps

    def environment(self):
        """The session's own shell."""
        return {"CODEX_THREAD_ID": SESSION, "PWD": "/fixture"}

    def reachable(self, name=None):
        """The thread is loaded in the default daemon, named `name`."""
        self.daemon = FakeDaemon(os.path.join(self.directory.name, "d.sock"), threads={SESSION: {"name": name}}, loaded=[SESSION])

    def close(self):
        if self.daemon:
            self.daemon.stop()
        self.directory.cleanup()
