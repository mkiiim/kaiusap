import os
from pathlib import Path
import shutil
import subprocess
import sys

import re

import pytest

from kaiusap.adapters import Identity, UnavailableProvider
from kaiusap.core import Core
from kaiusap.store import Store


@pytest.fixture(autouse=True)
def isolated_provider_environment(tmp_path, monkeypatch):
    """No test may discover a real provider or read the user's default homes."""
    home = tmp_path / "home"
    home.mkdir()
    binary_dir = tmp_path / "isolated-bin"
    binary_dir.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("PATH", str(binary_dir))
    monkeypatch.setenv("KAIUSAP_HOME", str(home / ".kaiusap"))
    monkeypatch.setenv("CODEX_HOME", str(home / ".codex"))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(home / ".claude"))
    # tmux finds its default server's socket under TMUX_TMPDIR: never the user's real server.
    monkeypatch.setenv("TMUX_TMPDIR", str(tmp_path / "tmux-tmpdir"))
    for name in ("CODEX_THREAD_ID", "CODEX_SESSION_ID", "CLAUDE_CODE_SESSION_ID", "CLAUDE_PID", "TMUX", "TMUX_PANE"):
        monkeypatch.delenv(name, raising=False)
    popen = subprocess.Popen
    repo = Path(__file__).resolve().parent.parent
    # The plugins' launchers are this repo's reviewed files; tests run them against fakes.
    allowed = {Path(sys.executable).absolute(), Path(sys.executable).parent / "kaiusap",
               repo / "plugins/claude/bin/kaiusap", repo / "plugins/codex/bin/kaiusap"}

    def guarded_popen(args, *positional, **kwargs):
        assert not kwargs.get("shell"), "tests must not launch an unreviewed shell"
        command = os.fspath(args[0])
        path = Path(command) if os.path.isabs(command) else Path(shutil.which(command, path=(kwargs.get("env") or os.environ).get("PATH", "")) or binary_dir / command)
        fake = path.name in {"codex", "claude", "opencode", "opencode2", "tmux", "ps"} and path.is_relative_to(tmp_path) and path.is_file()
        assert path in allowed or fake, f"unexpected external command: {command}"
        return popen(args, *positional, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", guarded_popen)


class FakeProvider(UnavailableProvider):
    def identity(self, environment, store):
        return Identity("codex", "a", "codex:a", "/fixture", "root")

    def liveness(self, session_key):
        return {"can_be_woken": False, "why_not": "the thread isn't loaded in the Codex daemon", "reason_code": "thread_not_loaded",
                "fix": "resume the thread in a Codex that uses the daemon"}

    def mirror(self, binding):
        return []


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / "store")


@pytest.fixture
def pair(store):
    provider = FakeProvider()
    a = Core(store, Identity("codex", "a", "codex:a", "/fixture", "root"), provider)
    b = Core(store, Identity("codex", "b", "codex:b", "/fixture", "root"), provider)
    a.run("iam", {"handle": "alpha"})
    b.run("iam", {"handle": "beta"})
    return a, b


def send(core, **overrides):
    args = {"to": "beta", "subject": "Review contract", "reply_needed": True,
            "fields": {"request": "Review it", "expected_output": "Mismatches"}}
    return core.run("send", args | overrides)


class Told(str):
    """A told text matched by pattern: its resume command differs per test."""
    def __new__(cls, pattern):
        obj = super().__new__(cls, pattern)
        obj.pattern = re.compile(pattern, re.S)
        return obj

    def __eq__(self, other):
        return isinstance(other, str) and bool(self.pattern.fullmatch(other))

    __hash__ = str.__hash__


def clear_suggestion(name):
    """The suggestion on a /clear whose session left kaiusap couldn't find, made as an offer; its resume command
    differs per test."""
    return Told(re.escape(f"This conversation began with a /clear. {name}'s session was in this directory; if that is "
                          f"the conversation left, {name} stays with it: ") + ".*" +
                re.escape(f". The user can say 'you are {name}' to carry on as {name} here."))


def tmux_name_suggestion(name, before=""):
    """The tmux-name suggestion with its two choices; `before` is any told text ahead of it."""
    return Told(re.escape(before) + re.escape(f"You may be `{name}` (this tmux session is named {name}), whose session isn't "
                "running. Ask the user: to carry on as that same assistant, with its memory and its messages, quit and "
                "resume it (") + ".*" + re.escape(f"); to take only the name here, say 'you are {name}': its earlier "
                "messages stay with the old session."))
