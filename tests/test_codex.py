"""The Codex adapter against a fake daemon, a fake `codex` executable and injected tmux and ps runners."""
from datetime import datetime, timedelta, timezone
import json
import os
import tempfile
import time

import pytest

import re

from conftest import Told, clear_suggestion, tmux_name_suggestion

from fake_codex import FakeDaemon, fake_codex
from fake_tmux import FakeTmux
from kaiusap.adapters import SPAWNED_AGENT_TEXT, Identity, Providers, UnavailableProvider
from kaiusap.adapters.codex import CodexAdapter, classify_command, thread_in_title, thread_in_transcript
from kaiusap.adapters.daemon import run_command
from kaiusap.core import Core
from kaiusap.errors import Error
from kaiusap.following import follow
from kaiusap.model import PERSONAS, MailView, handles, retirement_name
from kaiusap.notice import notice, notice_id, paste_line
from kaiusap.store import now

T1 = "019a1b2c-1111-7000-8000-000000000001"
T2 = "019a1b2c-2222-7000-8000-000000000002"
T3 = "019a1b2c-3333-7000-8000-000000000003"
ROOT = "019a1b2c-0000-7000-8000-000000000000"
SUB = "019a1b2c-5555-7000-8000-000000000005"


def iso(delta=0):
    return (datetime.now(timezone.utc) + timedelta(seconds=delta)).isoformat(timespec="microseconds").replace("+00:00", "Z")


def lstart(delta=0):
    return (datetime.now().astimezone() + timedelta(seconds=delta)).strftime("%a %b %d %H:%M:%S %Y")


def localised(stamp, env):
    """`ps`'s `lstart` as a day-first locale prints it (en_CA: "Thu  5 Nov ..."), unless LC_TIME is C."""
    if env is not None and env.get("LC_TIME") == "C" and "LC_ALL" not in env:
        return stamp
    return datetime.strptime(" ".join(stamp.split()), "%a %b %d %H:%M:%S %Y").strftime("%a %e %b %H:%M:%S %Y")


class Runner:
    """tmux and ps are injected; codex runs as the fake executable on PATH."""

    def __init__(self):
        self.panes, self.ps, self.all, self.calls, self.tmux_fails = [], {}, [], [], False
        self.fake = None  # a FakeTmux takes every tmux call but `list-panes -a` when set

    def pane(self, title, tty="ttys001", attached=1, session="work", pane_pid=100, pane_id="%1", server=("4321", "1793606400")):
        self.panes.append({"id": pane_id, "session": session, "attached": attached, "pane_pid": pane_pid, "tty": "/dev/" + tty,
                           "server_pid": server[0], "server_start": server[1], "title": title})
        return f"{server[0]}@{server[1]}:{pane_id}"

    def process(self, tty="ttys001", pid=200, started=-10, args="codex", ppid=100, stat="S+"):
        self.ps.setdefault(tty, []).append((pid, ppid, pid, pid, stat, lstart(started), args))
        return pid

    def __call__(self, argv, limit, env):
        self.calls.append(argv)
        if argv[0] == "tmux":
            if self.tmux_fails:
                return 1, ""
            if self.fake is not None and argv[3:5] != ["list-panes", "-a"]:
                return self.fake(argv, limit, env)
            keys = ("id", "session", "attached", "pane_pid", "tty", "server_pid", "server_start", "title")
            return 0, "".join("\t".join(str(p[k]) for k in keys) + "\n" for p in self.panes)
        if argv[0] == "ps":
            if argv[1] == "-t":
                return 0, "".join(" ".join(map(str, (*row[:5], localised(row[5], env), row[6]))) + "\n"
                                  for row in self.ps.get(argv[2], []))
            return 0, "\n".join(self.all) + "\n"
        assert argv == ["codex", "app-server", "daemon", "version"], f"unexpected command: {argv}"
        import shutil
        if not shutil.which("codex", path=(env or os.environ).get("PATH", "")):
            return 1, ""
        return run_command(argv, limit, env)


@pytest.fixture
def runner():
    return Runner()


@pytest.fixture
def socket_dir():
    return tempfile.mkdtemp(prefix="kai")


@pytest.fixture
def daemon(socket_dir):
    server = FakeDaemon(os.path.join(socket_dir, "d.sock"), threads={T1: {}, T2: {"cwd": "/elsewhere"}}, loaded=[T1, T2])
    yield server
    server.stop()


@pytest.fixture(autouse=True)
def codex_bin(tmp_path, monkeypatch, socket_dir):
    env_file = tmp_path / "codex-env.txt"
    fake_codex(tmp_path / "bin", os.path.join(socket_dir, "d.sock"), env_file=env_file)
    monkeypatch.setenv("PATH", str(tmp_path / "bin") + os.pathsep + os.environ.get("PATH", ""))
    monkeypatch.setenv("CODEX_HOME", "/should/not/leak")
    return env_file


class Clock:
    """A clock that advances only when the adapter sleeps, so title retries cost no test time."""

    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


@pytest.fixture
def adapter(store, runner, tmp_path):
    clock = Clock()
    adapter = CodexAdapter(store, run=runner, codex_home=tmp_path / "codex-home", clock=clock, sleep=clock.sleep)
    yield adapter
    adapter.reset()


@pytest.fixture
def providers(store, adapter):
    return Providers({"codex": adapter, "claude": UnavailableProvider()}, store)


def root(thread, cwd="/fixture"):
    return Identity("codex", thread, f"codex:{thread}", cwd, "root")


def core(store, providers, thread, cwd="/fixture"):
    return Core(store, root(thread, cwd), providers)


def start(store, thread, source="startup", cwd="/fixture", at=None):
    record = {"at": at or now(), "provider": "codex", "session_id": thread, "session_key": f"codex:{thread}", "source": source, "cwd": cwd}
    store.start(record)
    return record


def prompt(store, thread, cwd="/fixture", at=None):
    store.append("sessions/prompts.jsonl", {"at": at or now(), "provider": "codex", "session_id": thread,
                                            "session_key": f"codex:{thread}", "source": "prompt", "cwd": cwd})


def send(a, to="beta", **overrides):
    args = {"to": to, "subject": "Review contract", "reply_needed": True,
            "fields": {"request": "Review it", "expected_output": "Mismatches"}}
    return a.run("send", args | overrides)


def fresh_text(handle, thread, command, named=True):
    """What a fresh start's new thread is told when the thread left holds `handle`; `named`: with a generated name."""
    head = re.escape(f"This is a new conversation. It is not {handle}: {handle} stays with the conversation left in this "
                     f"terminal, thread {thread}. To go back to it: {command}. This thread has no handle. The user can say "
                     f"'you are {handle}' to carry on as {handle} here, or give it another name")
    return Told(head + (r", for example [a-z]+-[a-z]+-\d{4}\." if named else r"\."))


def fresh_shown(handle, command):
    """The line the user is shown with it."""
    return Told(re.escape(f"kaiusap: new conversation. {handle} stays with the one you left; to go back: {command}. To carry "
                          f"on as {handle} here, say 'you are {handle}'; or name this thread") + r", for example [a-z]+-[a-z]+-\d{4}\.")


def copy_text(handle):
    return (f"This conversation is a copy of {handle}'s. It has no handle. To carry on as {handle} here, the user says "
            f"'you are {handle}'.")


def copy_shown(handle):
    return (f"kaiusap: this conversation is a copy of {handle}'s and holds no handle. To carry on as {handle} here, "
            f"say 'you are {handle}'.")


def offers(store, key):
    return [(row["offered"], row["from"]) for row in store.rows(PERSONAS) if row.get("session_key") == key and row.get("offered")]


def live_pair(store, providers, runner, adapter):
    """alpha on T1 and beta on T2; T2 is shown by a trusted pane."""
    runner.pane(T2[:29] + "... | project")
    runner.process()
    start(store, T2)
    a = core(store, providers, T1)
    b = core(store, providers, T2)
    a.run("iam", {"handle": "alpha"})
    b.run("iam", {"handle": "beta"})
    return a, b


# ----- facts from the source -------------------------------------------------

def test_title_and_transcript_parsing():
    assert thread_in_title(f"Fix tests | project | {T1[:29]}...") == (None, T1[:29])
    assert thread_in_title(f"{T1} | project") == (T1, T1[:29])
    assert thread_in_title("plain title") == (None, None)
    assert thread_in_transcript(f"/x/rollout-2026-11-02T10-00-00-{SUB}.jsonl") == SUB
    assert thread_in_transcript(f"/x/rollout-2026-11-02T10-00-00-{SUB}_7.jsonl") == SUB
    assert thread_in_transcript("/x/other.jsonl") is None


@pytest.mark.parametrize("args,terminal", [
    ("codex", True), ("/opt/homebrew/bin/codex-aarch64-apple-darwin", True), ("codex resume abc", True),
    ("codex fork", True), ("codex agents", True), ("codex -m gpt-5 -C /work 'fix the tests'", True),
    ("codex --no-daemon", True), ("codex -- exec", True), ("codex -c key=value --search", True),
    ("codex exec 'do it'", False), ("codex e 'do it'", False), ("codex app-server --listen unix:///x", False),
    ("codex mcp", False), ("codex a", False), ("codex cloud-tasks", False), ("node /x/codex.js", False),
    ("codex --unknown-flag", False), ("python codex", False), ("codex git-credential get", False),
])
def test_classifier(args, terminal):
    assert classify_command(args) is terminal


def test_resume_command_quotes_the_thread_id():
    row = {}
    CodexAdapter.resume_line(row, "/a dir", "odd id", None)
    assert row["command"] == "cd '/a dir' && codex resume 'odd id'"


# ----- identity --------------------------------------------------------------

def test_hook_identity_rules(adapter):
    base = {"session_id": ROOT, "cwd": "/fixture", "transcript_path": f"/x/rollout-2026-11-02T10-00-00-{ROOT}.jsonl"}
    assert adapter.hook_identity(base).classification == "root"
    assert adapter.hook_identity({**base, "agent_id": SUB}).session_key == f"codex:{ROOT}/{SUB}"
    review = adapter.hook_identity({**base, "transcript_path": f"/x/rollout-2026-11-02T10-00-00-{SUB}.jsonl"})
    assert review.classification == "spawned" and review.agent_id == SUB and review.parent_key == f"codex:{ROOT}"
    assert adapter.hook_identity({**base, "transcript_path": None}) is None
    assert adapter.hook_identity({**base, "transcript_path": "/x/odd-name.jsonl"}).classification == "root"


def test_classify_step0_ids(adapter, store):
    spawned = adapter.identity({"CODEX_THREAD_ID": SUB, "CODEX_SESSION_ID": ROOT, "PWD": "/w"}, store)
    assert spawned.classification == "spawned" and spawned.session_key == f"codex:{ROOT}/{SUB}" and spawned.session_id == ROOT
    assert not any(c[0] == "codex" for c in adapter.run.calls)


def test_classify_step1_records(adapter, store):
    start(store, ROOT)
    store.start({"at": now(), "provider": "codex", "session_id": ROOT, "session_key": f"codex:{ROOT}/{SUB}", "source": "subagent", "agent_id": SUB, "cwd": "/f"})
    assert adapter.identity({"CODEX_THREAD_ID": ROOT, "CODEX_SESSION_ID": ROOT}, store).classification == "root"
    assert adapter.identity({"CODEX_THREAD_ID": SUB}, store).parent_key == f"codex:{ROOT}"


def test_classify_step2_daemon_and_step3_unknown(adapter, store, daemon, codex_bin):
    daemon.threads[SUB] = {"parentThreadId": T1}
    daemon.threads[T1] = {"parentThreadId": ROOT}
    daemon.threads[ROOT] = {}
    identity = adapter.identity({"CODEX_THREAD_ID": SUB}, store)
    assert identity.classification == "spawned" and identity.parent_key == f"codex:{ROOT}"
    adapter.reset()
    assert adapter.identity({"CODEX_THREAD_ID": ROOT}, store).classification == "root"
    adapter.reset()
    unknown = adapter.identity({"CODEX_THREAD_ID": T3}, store)
    assert unknown.classification == "unknown" and unknown.session_key is None and "thread/read failed" in unknown.reason
    with pytest.raises(Error) as exc:
        adapter.refuse_before_acting(unknown)
    assert "can't tell whether this Codex session is a subagent" in str(exc.value)
    assert "CODEX_HOME" not in codex_bin.read_text()


def test_unknown_caller_looks_but_cannot_act(adapter, store, providers, runner):
    unknown = Identity("codex", T3, None, "/f", "unknown", reason="no daemon")
    c = Core(store, unknown, providers)
    before = {p: p.read_bytes() for p in store.root.rglob("*") if p.is_file()}
    for verb, args in [("send", {"to": "x", "subject": "s", "reply_needed": False, "fields": {"context": "c"}}), ("iam", {"handle": "x"}), ("unpause", {})]:
        with pytest.raises(Error) as exc:
            c.run(verb, args)
        assert exc.value.code == 1
    assert {p: p.read_bytes() for p in store.root.rglob("*") if p.is_file()} == before
    who = c.run("whoami", {})
    assert who["classification"] == "unknown" and who["session_key"] is None and not who["can_be_woken"]
    assert c.run("pause", {})["paused"]


def test_both_session_variables_refused(providers, store):
    with pytest.raises(Error):
        providers.identity({"CODEX_THREAD_ID": T1, "CLAUDE_CODE_SESSION_ID": "x"}, store)
    assert providers.identity({}, store).classification == "plain_shell"


def test_spawned_caller_refused(adapter):
    with pytest.raises(Error) as exc:
        adapter.refuse_before_acting(Identity("codex", ROOT, f"codex:{ROOT}/{SUB}", "/f", "spawned", f"codex:{ROOT}"))
    assert "Report to the agent that started you" in str(exc.value)


# ----- events and records ----------------------------------------------------

def hook(name, thread=T1, **extra):
    return {"hook_event_name": name, "session_id": thread, "cwd": "/fixture", "model": "m", "permission_mode": "default",
            "transcript_path": f"/x/rollout-2026-11-02T10-00-00-{thread}.jsonl", **extra}


def test_events_map_to_base_vocabulary_and_records(adapter, store, runner):
    event = adapter.events(hook("SessionStart", source="clear"))
    assert event["kind"] == "clear" and event["file"] == "sessions/starts.jsonl"
    adapter.record(event)
    assert list(store.rows("sessions/starts.jsonl"))[0]["source"] == "clear"
    sub = adapter.events({**hook("SubagentStart", ROOT), "agent_id": SUB, "agent_type": "x", "turn_id": "t"})
    assert sub["kind"] == "subagent" and sub["record"]["agent_id"] == SUB and sub["session_key"] == f"codex:{ROOT}/{SUB}"
    adapter.record(sub)
    assert list(store.rows("sessions/starts.jsonl"))[1]["source"] == "subagent"
    weird = adapter.events(hook("SessionStart", source="weird"))  # identified, so the line; no record
    assert weird["kind"] == "unrecognised" and weird["record"] is None and weird["file"] is None
    assert adapter.events({**hook("SessionStart"), "transcript_path": None}) is None
    assert adapter.events({**hook("UserPromptSubmit", ROOT, prompt="hi", turn_id="t"), "agent_id": SUB}) is None
    assert adapter.events(hook("SessionStart", ROOT, source="startup", transcript_path=f"/x/rollout-2026-11-02T10-00-00-{SUB}.jsonl")) is None


def test_prompt_records_skip_own_notice_but_keep_paste_line(adapter, store, runner):
    key = runner.pane(T1[:29] + "...")
    runner.pane("other pane")
    message = {"id": "20261102T120000Z-abcdef", "in_reply_to": None, "reply_needed": True, "subject": "S", "from": {"handle": "alpha"}}
    own = adapter.events(hook("UserPromptSubmit", prompt=notice(message), turn_id="t"))
    assert own["kind"] == "prompt" and own["notice_id"] == message["id"] and own["record"] is None
    adapter.record(own)
    assert not (store.root / "sessions/prompts.jsonl").exists()
    adapter.reset()
    typed = adapter.events(hook("UserPromptSubmit", prompt=paste_line(message["id"]), turn_id="t"))
    assert typed["record"]["source"] == "prompt" and typed["notice_id"] is None
    adapter.record(typed)
    assert list(store.rows("sessions/prompts.jsonl"))[0]["session_id"] == T1
    lines = list(store.rows("sessions/codex/terminals.jsonl"))
    assert lines == [{**lines[0], "pane": key, "codex_pid": None, "source": "prompt", "thread_id": T1, "thread_prefix": T1[:29]}]
    assert sum(1 for c in runner.calls if c[0] == "tmux") == 1 and not any(c[0] == "ps" for c in runner.calls)
    assert notice_id(notice(message) + "\n") == message["id"] and notice_id("Run kaiusap read " + message["id"]) is None


def test_tell_shape(adapter):
    assert adapter.tell("note", "SessionStart") == {"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": "note"}}


# ----- terminal map, trust and liveness --------------------------------------

LIVE = {"can_be_woken": True, "why_not": None, "reason_code": None, "fix": None, "detached": False, "live": True}


def test_liveness_order_and_texts(adapter, store, runner, tmp_path, daemon, codex_bin):
    """whoami's Codex tests, amended 2026-11-07: the daemon, then the detached hold; panes and records don't decide."""
    key = f"codex:{T1}"
    # A loaded thread no pane shows, with no record at all, is live (it was no_title_id).
    assert adapter.liveness(key) == LIVE
    pane_key = runner.pane(T1[:29] + "... | project")
    runner.process(started=-10)
    adapter.reset()
    assert adapter.liveness(key) == LIVE  # a pane but no record (it was no_plugin)
    assert adapter.status(T1)["trusted"] == []
    start(store, T1, "compact", at=iso(-30))
    adapter.reset()
    assert adapter.liveness(key) == LIVE  # a pane, no record newer than its process (it was not_live)
    start(store, T1)
    adapter.reset()
    assert adapter.liveness(key) == LIVE and [p.key for p in adapter.status(T1)["trusted"]] == [pane_key]
    reads = [r for r in store.rows("sessions/codex/terminals.jsonl") if r["source"] == "read"]
    assert len(reads) == 1 and reads[0]["pane"] == pane_key and reads[0]["codex_pid"] == 200 and reads[0]["thread_id"] == T1
    adapter.reset()
    live = adapter.liveness(f"codex:{T3}")
    assert (live["reason_code"], live["why_not"], live["fix"]) == (
        "thread_not_loaded", "the thread isn't loaded in the Codex daemon", "resume the thread in a Codex that uses the daemon")
    daemon.loaded.remove(T1)
    adapter.reset()
    assert adapter.liveness(key)["reason_code"] == "thread_not_loaded"
    # The config file is never read for liveness any more (no_title_id is gone).
    assert not (tmp_path / "codex-home").exists()


def test_detached_and_wake_detached(adapter, store, runner, daemon, codex_bin):
    runner.pane(T1, attached=0, session="alpha")
    runner.process()
    start(store, T1)
    assert adapter.liveness(f"codex:{T1}") == {"can_be_woken": True, "why_not": None, "reason_code": None, "fix": None, "detached": True,
                                               "live": True}
    (store.root / "settings.json").write_text(json.dumps({"wake_detached": False}))
    adapter.reset()
    live = adapter.liveness(f"codex:{T1}")
    assert live["reason_code"] == "detached" and live["detached"]


def test_daemon_not_found_paths(adapter, store, runner, tmp_path, monkeypatch, socket_dir):
    runner.pane(T1)
    runner.process()
    start(store, T1)
    monkeypatch.setenv("PATH", str(tmp_path / "nobin"))
    assert adapter.liveness(f"codex:{T1}")["reason_code"] == "daemon_not_found"
    fake_codex(tmp_path / "bin", os.path.join(socket_dir, "missing.sock"))
    monkeypatch.setenv("PATH", str(tmp_path / "bin"))
    adapter.reset()
    assert adapter.liveness(f"codex:{T1}")["reason_code"] == "daemon_not_found"
    fake_codex(tmp_path / "bin", os.path.join(socket_dir, "d.sock"), exit_code=1)
    adapter.reset()
    assert adapter.liveness(f"codex:{T1}")["reason_code"] == "daemon_not_found"


def test_untrusted_process_lookup(adapter, store, runner, daemon, codex_bin):
    """A pane with no Codex terminal process gives no trusted pane, but the loaded thread is still live."""
    runner.pane(T1, tty="ttys007")
    runner.process(tty="ttys007", args="codex exec 'x'")
    runner.process(tty="ttys007", pid=300, args="/bin/zsh", stat="Ss")
    start(store, T1)
    status = adapter.status(T1)
    assert status["reason_code"] is None and len(status["panes"]) == 1 and status["trusted"] == []
    runner.process(tty="ttys007", pid=400, ppid=300, started=-5, args="codex resume " + T1)
    adapter.reset()
    status = adapter.status(T1)
    assert status["reason_code"] is None and len(status["trusted"]) == 1


# ----- deliver ---------------------------------------------------------------

def test_woken_delivery_writes_attempt_then_delivery(store, providers, runner, adapter, daemon, codex_bin):
    a, b = live_pair(store, providers, runner, adapter)
    result = send(a)
    assert result["outcome"] == "woken" and result["paste_line"] is None and result["reason_code"] is None
    events = [e for e in store.events() if e.get("message_id") == result["message_id"]]
    assert [e["event"] for e in events] == ["attempt", "delivery"]
    assert events[0]["binding"] == handles(store)["beta"]["id"] and events[1]["outcome"] == "woken"
    assert events[1]["provider_version"] == "0.166.0" and events[1]["to_session_key"] == f"codex:{T2}"
    message = store.message(result["message_id"])
    assert daemon.turns == [{"threadId": T2, "input": [{"type": "text", "text": notice(message)}]}]
    methods = [r["method"] for r in daemon.requests]
    methods = methods[len(methods) - 1 - methods[::-1].index("initialize"):]
    assert methods[:3] == ["initialize", "initialized", "thread/loaded/list"] and methods[-1] == "turn/start"
    assert "CODEX_HOME" not in codex_bin.read_text() and "PATH=" in codex_bin.read_text()
    assert a.run("inbox", {"sent": True})["messages"][0]["outcome"] == "woken"
    assert "No reply needed." not in notice(message) and notice(message).endswith("kaiusap reply " + message["id"])


def test_stored_when_not_loaded_then_paste_line(store, providers, runner, adapter, daemon, codex_bin):
    a = core(store, providers, T1)
    b = core(store, providers, T2)
    a.run("iam", {"handle": "alpha"})
    b.run("iam", {"handle": "beta"})
    daemon.loaded.remove(T2)
    result = send(a)
    assert result["outcome"] == "stored" and result["reason_code"] == "thread_not_loaded"
    assert result["paste_line"] == paste_line(result["message_id"]) and result["waiting_on"] is None
    events = [e for e in store.events() if e.get("message_id") == result["message_id"]]
    assert [(e["event"], e.get("outcome")) for e in events] == [("attempt", None), ("delivery", "stored")]
    assert MailView(store).outcome(store.message(result["message_id"])) == ("stored", "thread_not_loaded")
    assert daemon.turns == []


@pytest.mark.parametrize("turn,outcome,code", [
    ("refuse", "stored", "codex_refused"), ("not_found", "stored", "thread_not_loaded"),
    ("hang", "uncertain", "no_confirmation"), ("drop", "uncertain", "no_confirmation"), ("malformed", "uncertain", "no_confirmation"),
])
def test_turn_start_outcomes(store, providers, runner, adapter, daemon, codex_bin, turn, outcome, code):
    daemon.turn = turn
    a, b = live_pair(store, providers, runner, adapter)
    message = {"id": "20261102T120000Z-abcdef", "in_reply_to": None, "reply_needed": False, "subject": "S",
               "from": {"handle": "alpha"}, "to": {"handle": "beta", "session_id": T2, "session_key": f"codex:{T2}"}}
    started = time.monotonic()
    result = adapter.deliver(store, message, time.monotonic() + 1.5)
    assert (result["outcome"], result["reason_code"]) == (outcome, code)
    if turn == "refuse":
        assert result["reason"] == "Codex refused the turn: turn refused: workspace switching"
    if turn == "hang":
        assert 1 <= time.monotonic() - started < 3
    daemon.closed.set()


def test_handshake_failures_are_stored(store, providers, runner, adapter, daemon, codex_bin):
    a, b = live_pair(store, providers, runner, adapter)
    before = daemon.connections
    daemon.initialize = "error"
    result = send(a)
    assert (result["outcome"], result["reason_code"]) == ("stored", "protocol_unrecognised")
    daemon.initialize = "ok"
    daemon.loaded.remove(T2)
    result = send(a)
    assert (result["outcome"], result["reason_code"]) == ("stored", "thread_not_loaded")
    assert daemon.turns == []
    assert daemon.connections == before + 2


def test_uncertain_outcome_text_and_sent_projection(store, providers, runner, adapter, daemon, codex_bin):
    daemon.turn = "malformed"
    a, b = live_pair(store, providers, runner, adapter)
    result = send(a)
    assert result["outcome"] == "uncertain" and result["paste_line"] is None
    sent = a.run("inbox", {"sent": True})["messages"][0]
    assert sent["outcome"] == "uncertain" and sent["paste_line"] is None


# ----- amended 2026-11-07: a loaded thread is live, terminal or not -------

def unpaned_pair(store, providers):
    """alpha on T1 and beta on T2, both loaded; no tmux pane shows either."""
    a = core(store, providers, T1)
    b = core(store, providers, T2)
    a.run("iam", {"handle": "alpha"})
    b.run("iam", {"handle": "beta"})
    return a, b


def test_loaded_thread_without_pane_is_woken_and_reachable(store, providers, runner, adapter, daemon, codex_bin):
    from kaiusap.cli import render
    a, b = unpaned_pair(store, providers)
    runner.calls.clear()
    result = send(a)
    assert (result["outcome"], result["reason_code"], result["paste_line"], result["waiting_on"]) == ("woken", None, None, None)
    assert [t["threadId"] for t in daemon.turns] == [T2]
    assert render("send", result) == f"woken: beta accepted it as a turn. [{result['message_id']}]"
    assert not any(c[0] in {"tmux", "ps"} for c in runner.calls), "a send with wake_detached on reads no pane titles"
    who = b.run("whoami", {})
    assert (who["can_be_woken"], who["reason_code"], who["why_not"], who["fix"]) == (True, None, None, None)
    rows = {r["handle"]: r for r in a.run("who", {})["handles"]}
    assert rows["beta"]["reachable"] and rows["beta"]["reason_code"] is None and not rows["beta"]["detached"]


def test_pane_without_trusted_record_no_longer_stores(store, providers, runner, adapter, daemon, codex_bin):
    runner.pane(T2[:29] + "... | project")
    runner.process()  # a Codex terminal shows T2, but there is no start or prompt record for it
    a, _ = unpaned_pair(store, providers)
    for wake_detached in (True, False):
        (store.root / "settings.json").write_text(json.dumps({"wake_detached": wake_detached}))
        result = send(a)
        assert result["outcome"] == "woken", wake_detached
    assert len(daemon.turns) == 2


@pytest.mark.parametrize("wake_detached,panes,outcome", [
    (False, "detached", "stored"),   # the detached hold: trusted panes show it and every one is detached
    (True, "detached", "woken"),     # the default: a detached session is woken
    (False, "none", "woken"),        # a thread no pane shows is never held as detached
    (False, "mixed", "woken"),       # one trusted pane attached
    (False, "untrusted", "woken"),   # a detached pane that isn't trusted doesn't hold it
])
def test_detached_hold(store, providers, runner, adapter, daemon, codex_bin, wake_detached, panes, outcome):
    if panes != "none":
        runner.pane(T2, attached=0, session="beta")
        runner.process()
    if panes == "mixed":
        runner.pane(T2, attached=1, session="other", tty="ttys002", pane_id="%2")
        runner.process(tty="ttys002", pid=201)
    if panes != "untrusted":
        start(store, T2)
    a, _ = unpaned_pair(store, providers)
    (store.root / "settings.json").write_text(json.dumps({"wake_detached": wake_detached}))
    runner.calls.clear()
    result = send(a)
    assert result["outcome"] == outcome
    if outcome == "stored":
        assert result["reason_code"] == "detached" and daemon.turns == []
    assert any(c[0] == "tmux" for c in runner.calls) is (not wake_detached)
    adapter.reset()
    who = {r["handle"]: r for r in a.run("who", {})["handles"]}["beta"]
    assert who["detached"] is (panes == "detached")  # shown whether or not the mail is held


@pytest.mark.parametrize("status,waiting_on,phrase", [
    ({"type": "active", "activeFlags": ["waitingOnApproval"]}, ["approval"], "an approval"),
    ({"type": "active", "activeFlags": ["waitingOnUserInput"]}, ["user_input"], "the user's input"),
    ({"type": "active", "activeFlags": ["waitingOnUserInput", "waitingOnApproval"]}, ["approval", "user_input"],
     "an approval and the user's input"),
    ({"type": "active", "activeFlags": []}, None, None),
    ({"type": "idle"}, None, None),
    ({"type": "systemError"}, None, None),
    (None, None, None),
])
def test_status_read_reports_the_wait(store, providers, runner, adapter, daemon, codex_bin, status, waiting_on, phrase):
    from kaiusap.cli import render
    a, _ = unpaned_pair(store, providers)
    daemon.threads[T2]["status"] = status
    daemon.requests.clear()
    result = send(a)
    assert (result["outcome"], result["waiting_on"]) == ("woken", waiting_on)
    reads = [r for r in daemon.requests if r.get("method") == "thread/read"]
    assert reads[0]["params"] == {"threadId": T2, "includeTurns": False}
    text = render("send", result)
    if phrase:
        assert text == (f"woken: beta accepted it as a turn, but its thread is waiting on {phrase}, so it won't act on this "
                        f"until the user answers there; resume the thread if no terminal shows it. [{result['message_id']}]")
    else:
        assert text == f"woken: beta accepted it as a turn. [{result['message_id']}]"
    events = [e for e in store.events() if e.get("message_id") == result["message_id"]]
    assert events[-1]["outcome"] == "woken"


def test_status_read_error_does_not_stop_delivery(store, providers, runner, adapter, daemon, codex_bin):
    a, _ = unpaned_pair(store, providers)
    daemon.read = "error"
    result = send(a)
    assert (result["outcome"], result["waiting_on"]) == ("woken", None) and len(daemon.turns) == 1


def test_slow_status_read_does_not_stop_delivery(store, providers, runner, adapter, daemon, codex_bin):
    """Step 6's own 2 s: reached, it reports no wait; the late answer is skipped and turn/start still answers."""
    a, _ = unpaned_pair(store, providers)
    daemon.threads[T2]["status"] = {"type": "active", "activeFlags": ["waitingOnApproval"]}
    daemon.delays["thread/read"] = 2.6
    started = time.monotonic()
    result = send(a)
    elapsed = time.monotonic() - started
    assert (result["outcome"], result["reason_code"], result["waiting_on"]) == ("woken", None, None)
    assert 2 <= elapsed < 6 and len(daemon.turns) == 1
    daemon.delays.clear()


def test_status_read_on_a_dropped_connection_is_stored(store, providers, runner, adapter, daemon, codex_bin):
    """A connection that breaks during the status read can't carry turn/start: stored, not uncertain."""
    a, _ = unpaned_pair(store, providers)
    daemon.read = "drop"
    result = send(a)
    assert (result["outcome"], result["reason_code"]) == ("stored", "protocol_unrecognised")
    assert daemon.turns == [] and result["paste_line"]


def test_sessions_lists_a_loaded_thread_without_pane(store, providers, runner, adapter, daemon, codex_bin):
    from kaiusap.cli import render
    a, _ = unpaned_pair(store, providers)
    result = a.run("sessions", {})
    rows = {r["handle"]: r for r in result["sessions"]}
    assert rows["beta"]["where"] == "not_in_tmux" and rows["beta"]["command"].endswith(f"codex resume {rows['beta']['session_id']}")
    assert rows["beta"]["note"].startswith("Still open elsewhere: this joins the same thread as a second terminal, not a copy.")
    lines = render("sessions", result).splitlines()
    beta = [line for line in lines if line.startswith("beta  ")]
    assert beta == [f"beta  Codex  /elsewhere  running, not in tmux  codex:{T2}"]
    at = lines.index(beta[0])
    assert lines[at + 1:at + 3] == [f"cd /elsewhere && codex resume {T2}",
                                    "Still open elsewhere: this joins the same thread as a second terminal, not a copy."]


def rollout(tmp_path, *lines):
    path = tmp_path / f"rollout-2026-11-10T00-00-00-{T1}.jsonl"
    path.write_text("".join(json.dumps(line) + "\n" for line in lines))
    return str(path)


def token_count(at, total, window=258400, info=True):
    usage = {"input_tokens": total - 100, "cached_input_tokens": 0, "output_tokens": 100, "total_tokens": total}
    return {"timestamp": at, "type": "event_msg", "payload": {"type": "token_count", "info": {
        "total_token_usage": {**usage, "total_tokens": total * 10}, "last_token_usage": usage,
        "model_context_window": window} if info else None, "rate_limits": {}}}


@pytest.mark.parametrize("read_model", [True, False])
def test_context_is_the_latest_token_count_with_the_providers_limit(store, providers, adapter, daemon, tmp_path, read_model):
    """The latest `token_count` with usage gives `last_token_usage.total_tokens` and the window; the model
    comes from `thread/read`, else the latest `turn_context`."""
    path = rollout(tmp_path, {"type": "turn_context", "payload": {"model": "gpt-old"}},
                   token_count("2026-11-10T04:00:00Z", 1000),
                   {"type": "turn_context", "payload": {"model": "gpt-6.1-sol"}},
                   token_count("2026-11-10T04:05:00Z", 51725, window=475000),
                   token_count("2026-11-10T04:06:00Z", 0, info=False))
    daemon.threads[T1].update(path=path, **({"model": "gpt-from-read"} if read_model else {}))
    expected = {"used": 51725, "limit": 475000, "limit_source": "provider",
                "model": "gpt-from-read" if read_model else "gpt-6.1-sol", "model_maker": "openai", "at": "2026-11-10T04:05:00Z"}
    assert adapter.context(f"codex:{T1}") == expected
    rows = {r["session_key"]: r for r in core(store, providers, T1).run("sessions", {})["sessions"]}
    assert rows[f"codex:{T1}"]["context"] == expected
    assert rows[f"codex:{T1}"]["harness_maker"] == "openai"


@pytest.mark.parametrize("case", ["no_path", "no_token_count", "unknown_thread", "read_error"])
def test_context_is_none_when_nothing_is_known(store, adapter, daemon, tmp_path, case):
    if case == "no_token_count":
        daemon.threads[T1]["path"] = rollout(tmp_path, {"type": "turn_context", "payload": {"model": "m"}})
    if case == "read_error":
        daemon.read = "error"
    assert adapter.context(f"codex:{T3 if case == 'unknown_thread' else T1}") is None



@pytest.mark.parametrize("status,expected", [
    ({"type": "idle"}, ("idle", [])),
    ({"type": "active", "activeFlags": []}, ("working", [])),
    ({"type": "active", "activeFlags": ["waitingOnApproval"]}, ("blocked", ["approval"])),
    ({"type": "active", "activeFlags": ["waitingOnUserInput", "waitingOnApproval"]}, ("blocked", ["approval", "user_input"])),
    ({"type": "active", "activeFlags": ["somethingNew"]}, ("working", [])),
    ({"type": "systemError"}, ("error", [])),
    ({"type": "notLoaded"}, None),
    ({"type": "active"}, None),
    ({"type": "later"}, None),
    ("idle", None),
])
def test_activity_is_the_thread_status(store, providers, adapter, daemon, status, expected):
    """`activity`, from the thread's `status` in thread/read (v2/thread.rs:1721-1737 at 0.168.0)."""
    daemon.threads[T1]["status"] = status
    found = adapter.activity(f"codex:{T1}")
    assert (found and (found["state"], found["waiting_on"])) == (expected or None)
    row = {r["session_key"]: r for r in core(store, providers, T1).run("sessions", {})["sessions"]}[f"codex:{T1}"]
    assert (row["activity"] and (row["activity"]["state"], row["activity"]["waiting_on"])) == (expected or None)


@pytest.mark.parametrize("case", ["unknown_thread", "read_error", "no_key"])
def test_activity_is_none_when_the_status_cant_be_read(store, adapter, daemon, case):
    """A failed read is None, never idle."""
    if case == "read_error":
        daemon.read = "error"
    session = {"unknown_thread": f"codex:{T3}", "read_error": f"codex:{T1}", "no_key": ""}[case]
    assert adapter.activity(session) is None


def test_loaded_thread_is_not_suggested(store, providers, runner, adapter, daemon, codex_bin):
    """Suggestions offer only a handle with no live session; a loaded Codex thread is live."""
    core(store, providers, T1, "/work/one").run("iam", {"handle": "alpha"})
    start(store, T1, cwd="/work/one")
    runner.pane(T3, session="alpha")
    daemon.loaded.append(T3)
    daemon.threads[T3] = {}
    assert follow(store, providers, event("clear", T2, "/work/one"))["texts"] == []
    adapter.reset()
    assert follow(store, providers, event("startup", T3, "/work/two"))["texts"] == []
    daemon.loaded.remove(T1)
    adapter.reset()
    assert follow(store, providers, event("clear", T2, "/work/one"))["texts"] == [
        clear_suggestion("alpha")]
    adapter.reset()
    assert follow(store, providers, event("startup", T3, "/work/two"))["texts"] == [
        tmux_name_suggestion("alpha")]


# ----- the base send path: limits, pause, depth -------------------------------

def test_limits_and_pause_write_delivery_without_attempt(store, providers, runner, adapter, daemon, codex_bin):
    a, b = live_pair(store, providers, runner, adapter)
    (store.root / "settings.json").write_text(json.dumps({"max_per_pair_per_hour": 2, "max_per_hour": 3}))
    assert send(a)["outcome"] == "woken" and send(a)["outcome"] == "woken"
    third = send(a)
    assert (third["outcome"], third["reason_code"]) == ("stored", "limit")
    events = [e for e in store.events() if e.get("message_id") == third["message_id"]]
    assert [e["event"] for e in events] == ["delivery"]
    a.run("pause", {})
    paused = send(a)
    assert (paused["outcome"], paused["reason_code"]) == ("stored", "paused") and paused["paste_line"]
    assert len(daemon.turns) == 2


def test_depth_limit_and_stored_attempts_do_not_count(store, providers, runner, adapter, daemon, codex_bin):
    a, b = live_pair(store, providers, runner, adapter)
    (store.root / "settings.json").write_text(json.dumps({"max_per_hour": 1}))
    daemon.loaded.remove(T2)
    assert send(a)["reason_code"] == "thread_not_loaded"
    daemon.loaded.append(T2)
    assert send(a)["outcome"] == "woken"
    assert send(a)["reason_code"] == "limit"
    mid = send(a, to="beta")["message_id"]
    message = store.message(mid)
    message["depth"] = 7
    store.save_message(message)
    (store.root / "settings.json").write_text("{}")
    deep = store.message(message["id"])
    assert a.blocked(deep, now()) == "limit"


def test_prompt_resets_depth_but_not_hourly_limits(store, providers, runner, adapter, daemon, codex_bin):
    # Amended 2026-11-06: the depth check and the hourly limits are otherwise unchanged.
    a, b = live_pair(store, providers, runner, adapter)
    daemon.loaded.remove(T1)  # replies to alpha are stored, so only alpha's sends count toward the pair's limit
    first = send(a)
    message = store.message(first["message_id"])
    message["depth"] = 6
    store.save_message(message)  # a copy under a fresh id
    deep = b.run("reply", {"id": message["id"], "reply_needed": False, "fields": {"body": "x"}})
    assert store.message(deep["message_id"])["depth"] == 7 and deep["reason_code"] == "limit"
    prompt(store, T2)
    fresh = b.run("reply", {"id": message["id"], "reply_needed": False, "fields": {"body": "x"}})
    assert store.message(fresh["message_id"])["depth"] == 0 and fresh["reason_code"] != "limit"
    (store.root / "settings.json").write_text(json.dumps({"max_per_pair_per_hour": 2}))
    assert send(a)["outcome"] == "woken"
    prompt(store, T1)
    third = send(a)
    assert store.message(third["message_id"])["depth"] == 0 and third["reason_code"] == "limit"


# ----- list, where, sessions -------------------------------------------------

def test_sessions_rows_and_commands(store, providers, runner, adapter, daemon, codex_bin):
    daemon.loaded.append(T3)
    daemon.threads[T3] = {}
    runner.pane(T1, session="alpha-tmux", attached=1)
    runner.process()
    start(store, T1, cwd="/work/one")
    runner.pane(T3[:29] + "...", tty="ttys002", pane_id="%2", session="untrusted")
    runner.process(tty="ttys002", pid=201)
    a = core(store, providers, T1)
    a.run("iam", {"handle": "alpha"})
    c = core(store, providers, T3, "/work/three")
    c.run("iam", {"handle": "gamma"})
    gone = core(store, providers, "019a1b2c-4444-7000-8000-000000000004", "/work/four")
    gone.run("iam", {"handle": "delta"})
    start(store, "019a1b2c-4444-7000-8000-000000000004", cwd="/work/four")
    rows = {r["handle"]: r for r in a.run("sessions", {})["sessions"]}
    assert rows["alpha"]["where"] == "tmux" and rows["alpha"]["command"] == "tmux -L default attach -t '=alpha-tmux'" and rows["alpha"]["trusted"]
    assert rows["alpha"]["cwd"] == "/work/one" and rows["alpha"]["tmux_panes"][0]["attached"]
    assert rows["gamma"]["where"] == "tmux" and rows["gamma"]["trusted"] is False and rows["gamma"]["command"] == "tmux -L default attach -t '=untrusted'"
    # Loaded, no pane shows it: running, not in tmux, with the resume and its warning (2026-11-10 18:09 EST).
    assert rows[None]["session_id"] == T2 and rows[None]["where"] == "not_in_tmux" and rows[None]["cwd"] == "/elsewhere"
    assert rows[None]["command"] == f"cd /elsewhere && codex resume {T2}" and rows[None]["tmux_panes"] == []
    assert rows[None]["note"] == "Still open elsewhere: this joins the same thread as a second terminal, not a copy."
    assert rows["delta"]["where"] == "not_running" and rows["delta"]["command"] == "cd /work/four && codex resume 019a1b2c-4444-7000-8000-000000000004"
    runner.all = ["codex --no-daemon"]
    adapter.reset()
    rows = {r["handle"]: r for r in a.run("sessions", {})["sessions"]}
    assert rows["delta"]["where"] == "unknown" and rows["delta"]["command"] == "cd /work/four && codex resume 019a1b2c-4444-7000-8000-000000000004"
    assert rows["delta"]["note"] == "kaiusap can't tell whether this thread is open somewhere: this could start a second copy."
    runner.all = ["codex exec 'x'", "codex app-server --listen unix:///x"]
    adapter.reset()
    assert {r["handle"]: r for r in a.run("sessions", {})["sessions"]}["delta"]["where"] == "not_running"
    who = {r["handle"]: r for r in a.run("who", {})["handles"]}
    # gamma's pane isn't trusted, but its thread is loaded, so it is reachable (it was no_plugin).
    assert who["alpha"]["reachable"] and who["gamma"]["reachable"] and who["delta"]["reason_code"] == "thread_not_loaded"


def test_sessions_without_daemon_or_directory(store, providers, runner, adapter, tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", str(tmp_path / "nobin"))
    gone = core(store, providers, T1, "/w")
    gone.run("iam", {"handle": "delta"})
    rows = gone.run("sessions", {})
    row = rows["sessions"][0]
    assert row["where"] == "not_running" and row["command"] == f"codex resume {T1}" and row["note"] == "(run it in the thread's directory)"
    assert rows["warnings"] == ["Session discovery is not implemented for claude yet."]


# ----- mirror the name ---------------------------------------------

def test_iam_sets_name_and_release_retires(store, providers, runner, adapter, daemon, codex_bin):
    a = core(store, providers, T1)
    result = a.run("iam", {"handle": "alpha"})
    assert result["warnings"] == [] and daemon.names[T1] == "alpha"
    named = [e for e in store.events() if e["event"] == "named"]
    assert named[-1]["kind"] == "handle" and named[-1]["name"] == "alpha" and named[-1]["binding"] == handles(store)["alpha"]["id"]
    b = core(store, providers, T2)
    offer(store, f"codex:{T2}", "alpha", f"codex:{T1}")  # T1 is loaded, so taking its name needs an offer
    b.run("iam", {"handle": "alpha", "confirmed": True})
    # <handle>~<yymmddhhmmss>, the local time, to the second, of the line that moved the handle.
    left = datetime.fromisoformat(handles(store)["alpha"]["at"].replace("Z", "+00:00")).astimezone().strftime("%y%m%d%H%M%S")
    assert daemon.names[T1] == f"alpha~{left}" and daemon.names[T2] == "alpha"
    retired = [e for e in store.events() if e["event"] == "named" and e["kind"] == "retired"]
    assert retired[-1]["session_id"] == T1 and retired[-1]["name"] == f"alpha~{left}"
    daemon.threads[T2]["name"] = "my own name"
    b.run("release", {})
    assert daemon.names[T2] == "alpha" and [e for e in store.events() if e["event"] == "named"][-1]["name"] is None


def test_mirror_after_move_keeps_existing_name(store, providers, runner, adapter, daemon, codex_bin):
    daemon.threads[T2]["name"] = "typed"
    line = {"at": now(), "id": "20261102T120000Z-aaaaaa", "handle": "alpha", "reason": "fresh-start", "session_id": T2,
            "session_key": f"codex:{T2}", "provider": "codex", "moved_from": None, "also_released": None}
    assert adapter.mirror(line) == []
    assert T2 not in daemon.names
    assert [e for e in store.events() if e["event"] == "named"][-1]["name"] == "typed"


def test_pending_mirror_applies_when_daemon_returns(store, providers, runner, adapter, daemon, tmp_path, monkeypatch, socket_dir):
    monkeypatch.setenv("PATH", str(tmp_path / "nobin"))
    a = core(store, providers, T1)
    result = a.run("iam", {"handle": "alpha"})
    assert "wasn't set" in result["warnings"][0] and not [e for e in store.events() if e["event"] == "named"]
    env_file = tmp_path / "env.txt"
    fake_codex(tmp_path / "bin", os.path.join(socket_dir, "d.sock"), env_file=env_file)
    monkeypatch.setenv("PATH", str(tmp_path / "bin") + os.pathsep + os.environ["PATH"])
    b = core(store, providers, T2)
    b.run("iam", {"handle": "beta"})
    assert daemon.names == {T1: "alpha", T2: "beta"}
    assert {e["handle"] for e in store.events() if e["event"] == "named"} == {"alpha", "beta"}


def test_archived_thread_counts_as_done(store, providers, runner, adapter, daemon, codex_bin):
    daemon.threads[T1]["archived"] = True
    a = core(store, providers, T1)
    assert a.run("iam", {"handle": "alpha"})["warnings"] == []
    assert [e for e in store.events() if e["event"] == "named"][-1]["name"] is None
    assert "thread/name/set" in (store.root / "hook-errors.log").read_text()


# ----- follow a start (Codex steps 1–4) ---------------------------------

def event(kind, thread=T1, cwd="/fixture", at=None, **extra):
    return {"provider": "codex", "kind": kind, "session_id": thread, "session_key": f"codex:{thread}", "cwd": cwd, "at": at or now(), **extra}


def test_fork_lineage_moves_or_copies(store, providers, runner, adapter, daemon, codex_bin):
    daemon.threads[T1] = {"cwd": "/work/one"}
    daemon.threads[T3] = {"forkedFromId": T2}
    daemon.threads[T2] = {"forkedFromId": T1, "cwd": "/work/one"}
    core(store, providers, T1, "/work/one").run("iam", {"handle": "alpha"})
    start(store, T1, cwd="/work/one")
    result = follow(store, providers, event("fork", T3, "/work/three"))
    # Another directory moves nothing; the thread is asked, an offer (step 1).
    assert not result["moved"] and handles(store)["alpha"]["session_key"] == f"codex:{T1}"
    assert result["texts"] == [f"You may be alpha: this conversation started from alpha's thread {T1}, in another directory. Ask the user whether this is alpha moving here (/cd, or continuing in a worktree) or a copy (/fork)."]
    adapter.reset()
    # The user: "you are alpha". T1 is loaded (live), so the first call asks for the user's confirmation.
    with pytest.raises(Error) as asked:
        core(store, providers, T3, "/work/three").run("iam", {"handle": "alpha"})
    assert asked.value.reason_code == "confirmation_needed" and asked.value.code == 1
    assert handles(store)["alpha"]["session_key"] == f"codex:{T1}"
    taken = core(store, providers, T3, "/work/three").run("iam", {"handle": "alpha", "confirmed": True})
    assert taken["continued"] and handles(store)["alpha"]["session_key"] == f"codex:{T3}"
    assert daemon.names[T3] == "alpha" and daemon.names[T1].startswith("alpha~")
    adapter.reset()
    daemon.threads[T3]["cwd"] = "/work/three"
    daemon.threads["019a1b2c-6666-7000-8000-000000000006"] = {"forkedFromId": T3, "cwd": "/work/three"}
    copy = follow(store, providers, event("fork", "019a1b2c-6666-7000-8000-000000000006", "/work/three"))
    # No pane shows the copy, so the user wasn't moved into it: no handle, told so, and offered it.
    assert not copy["moved"] and copy["texts"] == [copy_text("alpha")] and copy["shown"] == [copy_shown("alpha")]
    assert offers(store, "codex:019a1b2c-6666-7000-8000-000000000006") == [("alpha", f"codex:{T3}")]
    adapter.reset()
    daemon.threads["019a1b2c-7777-7000-8000-000000000007"] = {"forkedFromId": ROOT}
    daemon.threads[ROOT] = {}
    plain = follow(store, providers, event("fork", "019a1b2c-7777-7000-8000-000000000007"))
    assert plain["texts"] == ["This conversation is a copy of another one, which held no handle; you have no handle."]


def test_fork_with_unreadable_lineage_is_told_to_ask(store, providers, runner, adapter, tmp_path, monkeypatch, daemon, codex_bin):
    daemon.threads[T3] = {"forkedFromId": "019a1b2c-9999-7000-8000-000000000009"}
    result = follow(store, providers, event("fork", T3))
    assert result["texts"] == ["This conversation was copied or moved from another one. Ask the user whether it should hold a handle."]
    monkeypatch.setenv("PATH", str(tmp_path / "nobin"))
    adapter.reset()
    assert follow(store, providers, event("fork", T3))["texts"] == result["texts"]


def test_fresh_start_follows_pane_predecessor(store, providers, runner, adapter, daemon, codex_bin):
    key = runner.pane(T2[:29] + "...")
    runner.process(started=-30)
    core(store, providers, T1).run("iam", {"handle": "alpha"})
    start(store, T1, at=iso(-20))
    store.append("sessions/codex/terminals.jsonl", {"at": iso(-15), "pane": key, "codex_pid": 200, "codex_started_at": adapter.process_of(adapter.panes()[0])[1],
                                              "thread_prefix": T1[:29], "thread_id": T1, "source": "read", "session_key": f"codex:{T1}"})
    adapter.reset()
    start(store, T2, "clear")
    cleared = event("clear", T2)
    result = follow(store, providers, cleared)
    # The handle stays with the thread left; the new thread is told, shown a line, offered alpha and named.
    assert not result["moved"] and result["proposal"]["fresh"] == {"handle": "alpha", "from": f"codex:{T1}"}
    assert result["texts"] == [fresh_text("alpha", T1, f"codex resume {T1}")]
    assert result["shown"] == [fresh_shown("alpha", f"codex resume {T1}")]
    assert handles(store)["alpha"]["session_key"] == f"codex:{T1}"
    assert offers(store, f"codex:{T2}") == [("alpha", f"codex:{T1}")]
    assert daemon.names[T2] == retirement_name("alpha", cleared["at"]) and daemon.names[T1] == "alpha"
    [named] = [e for e in store.events() if e["event"] == "named" and e["kind"] == "fresh"]
    assert (named["session_key"], named["name"], named["handle"]) == (f"codex:{T2}", daemon.names[T2], "alpha")


def test_fresh_start_prompt_line_predecessor_and_walk_with_via(store, providers, runner, adapter, daemon, codex_bin):
    daemon.loaded.append(T3)
    daemon.threads[T3] = {}
    key = runner.pane(T3[:29] + "...")
    runner.process(started=-60)
    core(store, providers, T1).run("iam", {"handle": "alpha"})
    start(store, T1, at=iso(-50))
    for thread, delta in [(T1, -40), (T2, -30)]:
        store.append("sessions/codex/terminals.jsonl", {"at": iso(delta), "pane": key, "codex_pid": None, "codex_started_at": None,
                                                  "thread_prefix": thread[:29], "thread_id": thread, "source": "prompt", "session_key": f"codex:{thread}"})
    start(store, T3, "startup")
    result = follow(store, providers, event("startup", T3))
    # The walk passes T2, which held nothing and was never prompted here, to alpha's T1.
    assert not result["moved"] and result["proposal"]["fresh"] == {"handle": "alpha", "from": f"codex:{T1}"}
    adapter.reset()
    store.append("sessions/codex/terminals.jsonl", {"at": now(), "pane": key, "codex_pid": None, "codex_started_at": None,
                                              "thread_prefix": T2[:29], "thread_id": T2, "source": "prompt", "session_key": f"codex:{T2}"})
    prompt(store, T2, at=now())
    T4 = "019a1b2c-4444-7000-8000-000000000004"
    runner.panes[0]["title"] = T4[:29] + "..."
    daemon.loaded.append(T4)
    daemon.threads[T4] = {}
    daemon.loaded.remove(T3)  # alpha's thread isn't running, so a /clear may suggest it (a loaded one is live)
    start(store, T4, "clear")
    stopped = follow(store, providers, event("clear", T4))
    # T2, prompted here and holding nothing, is the thread left: nothing is said, alpha isn't offered, and no
    # persona line is written for a conversation with no handle.
    assert not stopped["moved"] and stopped["texts"] == [] and "persona" not in stopped["proposal"]
    assert offers(store, f"codex:{T4}") == []
    assert not [row for row in store.rows(PERSONAS) if row.get("continues")]


def test_fresh_start_no_move_cases(store, providers, runner, adapter, daemon, codex_bin):
    key = runner.pane(T2[:29] + "...")
    runner.process(started=-30)
    core(store, providers, T1, "/work/one").run("iam", {"handle": "alpha"})
    start(store, T1, cwd="/work/one", at=iso(-20))
    started = adapter.process_of(adapter.panes()[0])[1]
    store.append("sessions/codex/terminals.jsonl", {"at": iso(-15), "pane": key, "codex_pid": 200, "codex_started_at": started,
                                              "thread_prefix": T1[:29], "thread_id": T1, "source": "read", "session_key": f"codex:{T1}"})
    adapter.reset()
    start(store, T2, "startup", cwd="/work/two")
    # Another directory: the handle stays all the same, and the new thread is told how to go back, with a `cd`.
    other = follow(store, providers, event("startup", T2, "/work/two"))
    assert not other["moved"] and other["texts"] == [fresh_text("alpha", T1, f"cd /work/one && codex resume {T1}")]
    adapter.reset()
    core(store, providers, T2, "/work/one").run("iam", {"handle": "beta"})
    guarded = follow(store, providers, event("clear", T2, "/work/one"))
    # A thread that holds a handle already is told nothing more.
    assert not guarded["moved"] and guarded["texts"] == []
    assert handles(store)["alpha"]["session_key"] == f"codex:{T1}" and handles(store)["beta"]["session_key"] == f"codex:{T2}"
    core(store, providers, T2, "/work/one").run("release", {})
    adapter.reset()
    runner.pane(T2[:29] + "...", tty="ttys002", pane_id="%2")
    runner.process(tty="ttys002", pid=300, started=-30)
    store.append("sessions/codex/terminals.jsonl", {"at": iso(-2), "pane": "4321@1793606400:%2", "codex_pid": 300,
                                              "codex_started_at": adapter.process_of(adapter.panes()[1])[1],
                                              "thread_prefix": T3[:29], "thread_id": T3, "source": "read", "session_key": f"codex:{T3}"})
    core(store, providers, T3, "/work/one").run("iam", {"handle": "gamma"})
    adapter.reset()
    two = follow(store, providers, event("clear", T2, "/work/one"))
    assert not two["moved"] and two["texts"] == ["You may be alpha or gamma: this terminal showed each before this conversation, so kaiusap moved neither. Tell the user."]


def test_fresh_start_without_pane_suggests_idle_and_tmux_named_handles(store, providers, runner, adapter):
    core(store, providers, T1, "/work/one").run("iam", {"handle": "alpha"})
    start(store, T1, cwd="/work/one")
    result = follow(store, providers, event("clear", T2, "/work/one"))
    assert result["texts"] == [clear_suggestion("alpha")]
    assert follow(store, providers, event("startup", T2, "/work/one"))["texts"] == []
    assert follow(store, providers, event("resume", T2, "/work/one"))["texts"] == []
    runner.pane(T2, session="alpha")
    adapter.reset()
    assert follow(store, providers, event("startup", T2, "/work/two"))["texts"] == [tmux_name_suggestion("alpha")]
    adapter.reset()
    assert follow(store, providers, event("fork", T2, "/work/two"))["texts"] == ["This conversation was copied or moved from another one. Ask the user whether it should hold a handle."]


def test_a_fresh_start_without_the_lock_in_time_names_no_generated_name(store, providers, runner, adapter, daemon, codex_bin):
    key = runner.pane(T2[:29] + "...")
    runner.process(started=-30)
    core(store, providers, T1).run("iam", {"handle": "alpha"})
    start(store, T1, at=iso(-20))
    store.append("sessions/codex/terminals.jsonl", {"at": iso(-15), "pane": key, "codex_pid": 200, "codex_started_at": adapter.process_of(adapter.panes()[0])[1],
                                              "thread_prefix": T1[:29], "thread_id": T1, "source": "read", "session_key": f"codex:{T1}"})
    adapter.reset()
    start(store, T2, "clear")
    with store.locked():
        result = follow(store, providers, event("clear", T2), deadline=time.monotonic() + 2)
    # The generated name needs the global lock: without it the text has no example, and none is recorded.
    assert not result["moved"]
    assert result["texts"] == [fresh_text("alpha", T1, f"codex resume {T1}", named=False)]
    assert offers(store, f"codex:{T2}") == [("alpha", f"codex:{T1}")]
    assert not [row for row in store.rows(PERSONAS) if row.get("suggested")]


def test_a_fork_the_pane_switched_to_takes_the_handle(store, providers, runner, adapter, daemon, codex_bin):
    # A /fork in the parent's directory, where the pane that showed the parent in this Codex process now shows
    # the copy: the user was moved into it, so the handle moves, recorded `copy`.
    key = runner.pane(T3[:29] + "...")
    runner.process(started=-30)
    daemon.threads[T3] = {"forkedFromId": T1}
    daemon.loaded.append(T3)
    core(store, providers, T1).run("iam", {"handle": "alpha"})
    start(store, T1, at=iso(-20))
    store.append("sessions/codex/terminals.jsonl", {"at": iso(-15), "pane": key, "codex_pid": 200, "codex_started_at": adapter.process_of(adapter.panes()[0])[1],
                                              "thread_prefix": T1[:29], "thread_id": T1, "source": "read", "session_key": f"codex:{T1}"})
    adapter.reset()
    start(store, T3, "fork")
    result = follow(store, providers, event("fork", T3))
    assert result["moved"] and result["line"]["reason"] == "copy" and result["line"]["moved_from"]["session_id"] == T1
    assert result["texts"] == [f"You are alpha; the handle followed this conversation from thread {T1}, which it replaced in this terminal."]
    assert handles(store)["alpha"]["session_key"] == f"codex:{T3}"
    assert daemon.names[T3] == "alpha" and daemon.names[T1].startswith("alpha~")


def test_a_fresh_thread_name_without_the_daemon_waits_for_the_catch_up(store, providers, runner, adapter, daemon, tmp_path, monkeypatch, socket_dir):
    # The new thread's `~` name can't be set with no daemon at its start: it is recorded as wanted and set by the
    # next kaiusap call that reaches the daemon.
    core(store, providers, T1).run("iam", {"handle": "alpha"})
    path = os.environ["PATH"]
    monkeypatch.setenv("PATH", str(tmp_path / "nobin"))
    adapter.reset()
    cleared = event("clear", T2)
    wanted = retirement_name("alpha", cleared["at"])
    warnings = adapter.name_fresh(cleared, "alpha", wanted)
    assert "wasn't renamed" in warnings[0] and T2 not in daemon.names
    [pending] = [e for e in store.events() if e["event"] == "named" and e["kind"] == "fresh"]
    assert (pending["name"], pending["wanted"]) == (None, wanted)
    monkeypatch.setenv("PATH", path)
    adapter.reset()
    core(store, providers, T1).run("whoami", {})
    assert daemon.names[T2] == wanted
    named = [e for e in store.events() if e["event"] == "named" and e["kind"] == "fresh"]
    assert [(e["name"], e["retry"]) for e in named] == [(None, False), (wanted, True)]


def hanging_ps(adapter, runner, monkeypatch, tty="ttys001"):
    """`ps -t <tty>` answers only when its limit is spent, as a process read that takes the hook's time."""
    def run(argv, limit, env):
        if argv[:3] == ["ps", "-t", tty]:
            time.sleep(limit)
            return 0, ""
        return runner(argv, limit, env)
    monkeypatch.setattr(adapter, "run", run)


@pytest.mark.parametrize("kind", ["clear", "startup", "resume", "compact"])
def test_start_out_of_time_reading_processes_moves_nothing_and_says_nothing(store, providers, runner, adapter, daemon, codex_bin,
                                                                           monkeypatch, kind):
    # Record first: the deadline passes while the panes' processes are read, before any holder is
    # identified: the next read's DaemonError ends the following with nothing moved, a logged line, no text.
    runner.pane(T2[:29] + "...")
    runner.pane(T2[:29] + "...", tty="ttys002", pane_pid=101, pane_id="%2")
    runner.process(tty="ttys002", pid=300, ppid=101, started=-30)
    hanging_ps(adapter, runner, monkeypatch)
    start(store, T2, kind)
    result = follow(store, providers, event(kind, T2), deadline=time.monotonic() + .3)
    assert result["texts"] == [] and not result["moved"]
    log = (store.root / "hook-errors.log").read_text()
    assert f"start codex:{T2}: couldn't follow: DaemonError: no time left to read processes" in log


def test_start_programming_error_while_following_still_surfaces(store, providers, runner, adapter, monkeypatch):
    # Only the daemon's own errors end the following quietly; anything else is a fault and raises.
    runner.pane(T2[:29] + "...")
    def broken(panes, deadline=None):
        raise ValueError("a fault")
    monkeypatch.setattr(adapter, "observe", broken)
    start(store, T2, "clear")
    with pytest.raises(ValueError):
        follow(store, providers, event("clear", T2))


# Regression cases from the independent step-2 review.

def test_review_failed_handle_append_is_not_a_move(store, providers, adapter, monkeypatch):
    core(store, providers, T1).run("iam", {"handle": "alpha"})
    proposal = {"move": {"handle": "alpha", "from": f"codex:{T1}", "to": f"codex:{T2}", "reason": "fresh-start"},
                "text": "moved", "failed": "could not follow", "suggest": False}
    monkeypatch.setattr(adapter, "follow_start", lambda *args: proposal)
    monkeypatch.setattr(adapter, "current", lambda *args: True)
    mirrored = []
    monkeypatch.setattr(providers, "mirror", lambda line: mirrored.append(line) or [])
    append = store.append
    def fail(relative, record, deadline=None):
        if relative == "handles.jsonl":
            raise PermissionError("injected before append")
        return append(relative, record, deadline)
    monkeypatch.setattr(store, "append", fail)
    result = follow(store, providers, event("clear", T2))
    assert not result["moved"] and result["line"] is None
    assert result["texts"] == ["could not follow"] and mirrored == []
    assert handles(store)["alpha"]["session_id"] == T1


@pytest.mark.parametrize("ties", [False, True])
def test_review_currency_uses_timestamp_and_rejects_ties(store, adapter, ties):
    at = "2026-01-01T00:00:05Z"
    start(store, T2, at=at)
    start(store, T2, at=at if ties else "2026-01-01T00:00:06Z")
    start(store, T2, at="2026-01-01T00:00:04Z")
    assert not adapter.current(event("fork", T2, at=at), {})


def test_review_lineage_currency_checks_all_known_panes(store, adapter):
    at = "2026-01-01T00:00:05Z"
    for pane, thread, stamp in [("p1", T2, "04"), ("p1", T3, "06")]:
        store.append("sessions/codex/terminals.jsonl", {"at": f"2026-01-01T00:00:{stamp}Z", "pane": pane, "thread_id": thread})
    assert not adapter.current(event("fork", T2, at=at), {})
    store.append("sessions/codex/terminals.jsonl", {"at": at, "pane": "p2", "thread_id": T2})
    assert adapter.current(event("fork", T2, at=at), {})
    store.append("sessions/codex/terminals.jsonl", {"at": at, "pane": "p2", "thread_id": T3})
    assert not adapter.current(event("fork", T2, at=at), {})


def test_review_predecessor_lines_are_timestamp_ordered(store, adapter, runner):
    runner.pane(T3)
    runner.process()
    pane = adapter.panes()[0]
    process = adapter.process_of(pane)
    for thread, stamp in [(T1, iso(-2)), (T2, iso(-3))]:
        store.append("sessions/codex/terminals.jsonl", {"at": stamp, "pane": pane.key, "thread_id": thread,
                     "source": "read", "codex_pid": process[0], "codex_started_at": process[1]})
    assert [r["thread_id"] for r in adapter.pane_lines(pane, process)] == [T1, T2]


def test_review_terminal_append_honours_deadline(store, adapter, runner):
    from kaiusap.adapters.daemon import Deadline
    runner.pane(T1)
    runner.process()
    import threading
    done = threading.Event()
    def observe():
        try:
            adapter.observe(adapter.panes(), Deadline(.03))
        finally:
            done.set()
    worker = threading.Thread(target=observe)
    with store.locked("sessions/codex/terminals.jsonl"):
        worker.start()
        bounded = done.wait(.3)
    worker.join(timeout=1)
    assert bounded, "terminal-map append ignored its lock deadline"
    assert not list(store.rows("sessions/codex/terminals.jsonl"))


def test_review_handle_append_receives_hook_deadline(store, providers, adapter, monkeypatch):
    core(store, providers, T1).run("iam", {"handle": "alpha"})
    monkeypatch.setattr(adapter, "follow_start", lambda *args: {
        "move": {"handle": "alpha", "from": f"codex:{T1}", "to": f"codex:{T2}", "reason": "fresh-start"}, "text": "moved"})
    monkeypatch.setattr(adapter, "current", lambda *args: True)
    monkeypatch.setattr(providers, "mirror", lambda *args: [])
    append, deadlines = store.append, []
    def capture(relative, row, deadline=None):
        if relative == "handles.jsonl":
            deadlines.append(deadline)
        return append(relative, row, deadline)
    monkeypatch.setattr(store, "append", capture)
    deadline = time.monotonic() + 1
    assert follow(store, providers, event("clear", T2), deadline=deadline)["moved"]
    assert deadlines == [deadline]


def test_review_reset_closes_owned_connection(adapter):
    class Connection:
        closed = False
        def close(self):
            self.closed = True
    connection = Connection()
    adapter._daemon = connection
    adapter.reset()
    assert connection.closed


@pytest.mark.parametrize("command,expected", [
    ("codex help", False), ("codex -- --help", True),
    ("codex --yolo", True), ("codex --not-so-yolo", True),
    ("codex --yolo exec x", False), ("codex --not-so-yolo app-server", False),
    ("codex --image a.png b.png --model x exec task", False),
    ("codex -ia.png b.png --model x resume id", True),
    ("codex --image=a.png b.png --model x exec task", False),
    ("codex --image a.png exec task", True),
    ("codex --image a.png -- exec", True),
    ("codex -mfoo exec task", False), ("codex -C=/work exec task", False),
    ("codex --image", False), ("codex --model", False),
])
def test_review_classifier_grammar(command, expected):
    assert classify_command(command) is expected


@pytest.mark.parametrize("source", ["start", "prompt"])
def test_review_fractional_trust_and_prompt_boundary(store, adapter, runner, source):
    runner.pane(T1)
    pane = adapter.panes()[0]
    adapter._processes[pane.tty] = (200, "2026-01-01T00:00:00Z")
    if source == "start":
        start(store, T1, at="2026-01-01T00:00:01.500000Z")
    else:
        prompt(store, T1, at="2026-01-01T00:00:01.500000Z")
        assert adapter.prompted_in(T1, adapter._processes[pane.tty])
    assert adapter.trusted(pane, T1)


def test_review_ambiguous_loaded_prefix_names_no_thread(store, providers, adapter, runner, daemon, codex_bin):
    """A title prefix two loaded threads share gives no pane; the binding's full id still wakes its thread,
    and the ambiguous pane, though detached, doesn't hold it."""
    collision = T2[:-1] + "9"
    daemon.loaded.append(collision)
    daemon.threads[collision] = {}
    a, _ = live_pair(store, providers, runner, adapter)
    runner.panes[0]["attached"] = 0
    (store.root / "settings.json").write_text('{"wake_detached": false}')
    result = send(a)
    assert result["outcome"] == "woken" and [t["threadId"] for t in daemon.turns] == [T2]
    assert all(row["thread_id"] is None for row in store.rows("sessions/codex/terminals.jsonl"))
    adapter.reset()
    assert adapter.status(T2)["trusted"] == [] and not adapter.status(T2)["detached"]


@pytest.mark.parametrize("verb", ["whoami", "who", "send"])
def test_review_pending_names_catch_up_on_successful_calls(store, providers, adapter, runner, daemon, monkeypatch, verb):
    path = os.environ["PATH"]
    monkeypatch.setenv("PATH", "/nonexistent-test-bin")
    runner.pane(T2)
    runner.process()
    start(store, T2)
    a = core(store, providers, T1)
    b = core(store, providers, T2)
    a.run("iam", {"handle": "alpha"})
    b.run("iam", {"handle": "beta"})
    assert daemon.names == {}
    monkeypatch.setenv("PATH", path)
    if verb == "send":
        assert send(a)["outcome"] == "woken"
    else:
        b.run(verb, {})
    assert daemon.names == {T1: "alpha", T2: "beta"}
    assert adapter._daemon is None


@pytest.mark.parametrize("kind", ["resume", "compact"])
def test_review_tmux_name_suggestion_on_nonfork_starts(store, providers, adapter, runner, kind):
    core(store, providers, T1).run("iam", {"handle": "alpha"})
    runner.pane(T2, session="alpha")
    adapter.reset()
    result = follow(store, providers, event(kind, T2))
    assert result["texts"] == [tmux_name_suggestion("alpha")]


def test_review_tmux_name_suggestion_survives_clear_suppression(store, providers, adapter, monkeypatch):
    core(store, providers, T1).run("iam", {"handle": "alpha"})
    monkeypatch.setattr(adapter, "follow_start", lambda *args: {"suggest": False, "told": True,
                      "text": "already told", "tmux_session": "alpha"})
    result = follow(store, providers, event("clear", T2))
    assert result["texts"] == ["already told", tmux_name_suggestion("alpha")]


@pytest.mark.parametrize("code", ["protocol_unrecognised", "timeout", "sandboxed"])
def test_review_unknown_daemon_state_offers_resume_with_warning(store, providers, adapter, runner, monkeypatch, code):
    from kaiusap.adapters.daemon import DaemonError
    monkeypatch.setattr(adapter, "open", lambda *args: (_ for _ in ()).throw(DaemonError(code)))
    row = adapter.row(T1)
    assert row["where"] == "unknown" and row["command"].endswith(f"codex resume {T1}") and row["note"].startswith("kaiusap can't tell whether this thread is open somewhere: this could start a second copy.")


def test_review_status_observes_other_panes(store, adapter, runner, daemon, codex_bin):
    runner.pane(T1)
    runner.process()
    other = runner.pane(T2, pane_id="%2", tty="ttys002")
    runner.process(tty="ttys002", pid=201)
    start(store, T1)
    adapter.status(T1)
    assert store.latest("sessions/codex/terminals.jsonl", lambda r: r.get("pane") == other)["thread_id"] == T2
    runner.panes[1]["title"] = "shell"
    adapter.reset()
    adapter.status(T1)
    assert store.latest("sessions/codex/terminals.jsonl", lambda r: r.get("pane") == other)["thread_id"] is None


@pytest.mark.parametrize("operation", ["identity", "whoami", "who", "sessions", "iam", "release", "woken", "thread_not_loaded", "detached"])
def test_review_connections_close_on_every_exit(store, providers, adapter, runner, daemon, codex_bin, operation):
    a, b = live_pair(store, providers, runner, adapter)
    if operation == "identity":
        assert adapter.classify(T1).classification == "root"
    elif operation in {"whoami", "who", "sessions", "release"}:
        b.run(operation, {})
    elif operation == "iam":
        b.run("iam", {"handle": "gamma"})
    else:
        if operation == "thread_not_loaded":
            daemon.loaded.remove(T2)
        if operation == "detached":
            runner.panes[0]["attached"] = 0
            (store.root / "settings.json").write_text('{"wake_detached": false}')
        result = send(a)
        assert result["outcome"] == ("woken" if operation == "woken" else "stored")
    assert daemon.wait_idle(), "an initialized WebSocket was left open"
    assert adapter._daemon is None


def test_review_sandboxed_socket_preserves_delivery_advice(store, providers, adapter, runner, monkeypatch):
    from kaiusap.adapters.daemon import DaemonError
    a, _ = live_pair(store, providers, runner, adapter)
    monkeypatch.setattr(adapter, "open", lambda *args: (_ for _ in ()).throw(DaemonError("sandboxed")))
    result = send(a)
    assert result["outcome"] == "stored" and result["reason_code"] == "sandboxed"
    assert result["message_id"] and result["paste_line"]
    assert "use the kaiusap tools instead" in result["reason"]


def test_review_tests_reject_real_commands_and_default_homes(store, tmp_path):
    import subprocess
    adapter = CodexAdapter(store)
    assert adapter.codex_home.is_relative_to(tmp_path)
    assert store.root.is_relative_to(tmp_path)
    for command in (["/test-unapproved-provider/tmux", "list-panes"], ["/test-unapproved-provider/claude", "agents", "--json"], ["/test-unapproved-provider/codex", "app-server", "daemon", "version"]):
        with pytest.raises(AssertionError, match="unexpected external command"):
            subprocess.run(command, capture_output=True)


def test_review_stale_currency_suppresses_all_suggestions(store, providers, adapter, monkeypatch):
    core(store, providers, T1).run("iam", {"handle": "alpha"})
    monkeypatch.setattr(adapter, "follow_start", lambda *args: {
        "move": {"handle": "alpha", "from": f"codex:{T1}", "to": f"codex:{T2}", "reason": "fresh-start"},
        "text": "moved", "suggest": False, "tmux_session": "alpha"})
    monkeypatch.setattr(adapter, "current", lambda *args: False)
    result = follow(store, providers, event("clear", T2))
    assert not result["moved"] and result["texts"] == []


def test_unknown_caller_reason_precedes_terminal_tests(store, providers, adapter, monkeypatch):
    def unexpected(*args, **kwargs):
        pytest.fail("unknown caller must be rejected before terminal tests")
    monkeypatch.setattr(adapter, "status", unexpected)
    result = Core(store, Identity("codex", T1, None, "/fixture", "unknown", reason="no daemon"), providers).run("whoami", {})
    assert result["can_be_woken"] is False
    assert result["reason_code"] == "unknown_caller" and result["session_key"] is None
    assert result["fix"] == "Install the kaiusap plugin, or start the Codex daemon."
    assert adapter.liveness(None)["reason_code"] == "unknown_caller"


@pytest.mark.parametrize("failure", [OSError, PermissionError])
def test_store_read_failure_after_attempt_keeps_id(store, providers, adapter, runner, daemon, codex_bin, monkeypatch, failure):
    from test_cli import invoke
    a, _ = live_pair(store, providers, runner, adapter)
    (store.root / "settings.json").write_text('{"wake_detached": false}')  # so the send reads panes and the terminal map
    def denied(*args, **kwargs):
        raise failure("injected terminal-map read failure")
    monkeypatch.setattr(adapter, "terminal_rows", denied)
    code, output, error = invoke(store, ["send", "--to", "beta", "--subject", "test", "--reply-needed", "no", "--context", "test", "--json"], identity=a.identity, provider=providers)
    assert code == 0 and not error
    result = json.loads(output)
    assert result["outcome"] == "stored" and result["reason_code"] == "store_failed"
    assert result["paste_line"] == paste_line(result["message_id"]) and result["warnings"]
    message = store.message(result["message_id"])
    assert [e["event"] for e in store.events() if e.get("message_id") == message["id"]] == ["attempt"]
    assert MailView(store).outcome(message) == ("uncertain", "no_confirmation")
    assert daemon.turns == [] and daemon.wait_idle()


@pytest.mark.parametrize("verb", ["sessions", "messages", "setup"])
def test_read_only_calls_defer_pending_names(store, providers, adapter, runner, daemon, codex_bin, monkeypatch, verb):
    core(store, providers, T1).run("iam", {"handle": "alpha"})
    path = os.environ["PATH"]
    monkeypatch.setenv("PATH", "/nonexistent-test-bin")
    b = core(store, providers, T2)
    b.run("iam", {"handle": "alpha"})
    before = dict(daemon.names)
    named = [e for e in store.events() if e["event"] == "named"]
    monkeypatch.setenv("PATH", path)
    requests = len(daemon.requests)
    b.run(verb, {})
    if verb == "messages":
        assert daemon.requests[requests:] == []  # it reads only the store
    assert daemon.names == before
    assert [e for e in store.events() if e["event"] == "named"] == named
    assert daemon.wait_idle()
    b.run("who", {})
    assert daemon.names[T2] == "alpha" and daemon.names[T1].startswith("alpha~")


@pytest.mark.parametrize("turn,outcome", [("ok", "woken"), ("refuse", "stored")])
def test_delivery_catches_up_names_after_turn_answer(store, providers, adapter, runner, daemon, monkeypatch, turn, outcome):
    path = os.environ["PATH"]
    monkeypatch.setenv("PATH", "/nonexistent-test-bin")
    a, _ = live_pair(store, providers, runner, adapter)
    assert daemon.names == {}
    monkeypatch.setenv("PATH", path)
    daemon.turn = turn
    daemon.requests.clear()
    result = send(a)
    assert result["outcome"] == outcome
    methods = [request["method"] for request in daemon.requests]
    turn = methods.index("turn/start")
    assert methods[turn - 1] == "thread/read"  # the status read
    after = methods[turn + 1:]
    assert "thread/read" in after and after.index("thread/read") < after.index("thread/name/set")
    assert daemon.names == {T1: "alpha", T2: "beta"}
    events = list(store.events())
    delivery_index = next(i for i, e in enumerate(events) if e.get("message_id") == result["message_id"] and e["event"] == "delivery")
    assert all(i > delivery_index for i, e in enumerate(events) if e["event"] == "named")
    assert daemon.wait_idle() and adapter._daemon is None


def test_delivery_catch_up_error_is_only_a_warning(store, providers, adapter, runner, daemon, monkeypatch):
    a, _ = live_pair(store, providers, runner, adapter)
    def failed():
        assert daemon.turns, "catch-up ran before the delivery"
        raise OSError("injected catch-up failure")
    monkeypatch.setattr(adapter, "catch_up", failed)
    result = send(a)
    assert result["outcome"] == "woken"
    assert any("injected catch-up failure" in warning for warning in result["warnings"])
    assert daemon.wait_idle() and adapter._daemon is None


def test_delivery_record_survives_catch_up_exhausting_deadline(store, providers, adapter, runner, daemon, monkeypatch):
    a, _ = live_pair(store, providers, runner, adapter)
    event_lock = None
    real_clock = time.monotonic
    observed_before_catch_up = []
    def exhaust():
        nonlocal event_lock
        observed_before_catch_up.extend(e for e in store.events() if e["event"] == "delivery")
        month = now()[:7]
        event_lock = store.locked(f"events/{month}.jsonl")
        event_lock.__enter__()
        monkeypatch.setattr(time, "monotonic", lambda: real_clock() + 30)
    monkeypatch.setattr(adapter, "catch_up", exhaust)
    try:
        result = send(a)
    finally:
        if event_lock is not None:
            event_lock.__exit__(None, None, None)
    assert observed_before_catch_up, "catch-up began before the delivery was recorded"
    assert result["outcome"] == "woken"
    assert MailView(store).outcome(store.message(result["message_id"])) == ("woken", None)
    assert daemon.wait_idle() and adapter._daemon is None


# ----- review fixes (step 5): the lookup writes nothing; call time limits ----------------------

def cli(store, providers, argv, environ, **extra):
    """A CLI call whose identity comes from `environ`, as `kaiusap` on a session's shell would resolve it."""
    from io import StringIO
    from kaiusap.cli import main
    out, err = StringIO(), StringIO()
    code = main([*argv, "--json"], store=store, provider=providers, stdin=StringIO(""), stdout=out, stderr=err, environ=environ, **extra)
    return code, json.loads(out.getvalue())


def snapshot(store):
    return {p: p.read_bytes() for p in store.root.rglob("*") if p.is_file()}


def pending_mirror(store, providers, monkeypatch):
    """alpha on T1, taken while no daemon could be found: its name mirror is pending."""
    path = os.environ["PATH"]
    monkeypatch.setenv("PATH", "/nonexistent-test-bin")
    result = core(store, providers, T1).run("iam", {"handle": "alpha"})
    assert "wasn't set" in result["warnings"][0] and not [e for e in store.events() if e["event"] == "named"]
    monkeypatch.setenv("PATH", path)


ACTING_ARGV = {"send": ["send", "--to", "alpha", "--subject", "s", "--reply-needed", "no", "--context", "c"],
               "reply": ["reply", "x", "--body", "b", "--reply-needed", "no"], "iam": ["iam", "x"],
               "release": ["release"], "moot": ["moot", "x"], "unpause": ["unpause"]}


@pytest.mark.parametrize("caller", ["spawned", "unknown"])
def test_cli_lookup_by_the_daemon_writes_nothing_before_a_refusal(store, providers, adapter, daemon, monkeypatch, caller):
    pending_mirror(store, providers, monkeypatch)
    daemon.threads[ROOT], daemon.threads[SUB] = {}, {"parentThreadId": ROOT}  # the daemon places SUB under ROOT; T3 it doesn't know
    thread = SUB if caller == "spawned" else T3
    environ = {"CODEX_THREAD_ID": thread, "CODEX_SESSION_ID": thread}
    before = snapshot(store)
    for verb, argv in ACTING_ARGV.items():
        code, result = cli(store, providers, argv, environ)
        assert code == 1 and result["code"] == 1, verb
        if caller == "spawned":
            assert result["error"] == "kaiusap: " + SPAWNED_AGENT_TEXT
        else:
            assert result["error"].startswith("kaiusap: kaiusap can't tell whether this Codex session is a subagent (no start record for thread " + T3)
            assert "thread/read failed" in result["error"]
        assert adapter._daemon is None
    assert snapshot(store) == before, "a refused call wrote to the store"
    assert daemon.names == {} and "thread/name/set" not in [q["method"] for q in daemon.requests]
    # A looking verb the caller may use completes, and the lookup's daemon reach lets the catch-up run after it.
    code, result = cli(store, providers, ["whoami"], environ)
    assert code == 0 and result["classification"] == caller
    assert daemon.wait_idle() and daemon.names == {T1: "alpha"} and adapter._daemon is None
    assert [e["handle"] for e in store.events() if e["event"] == "named"] == ["alpha"]


@pytest.mark.parametrize("verb", ["whoami", "pause"])
def test_cli_lookup_by_the_daemon_lets_the_catch_up_run_after_the_verb(store, providers, adapter, daemon, monkeypatch, verb):
    pending_mirror(store, providers, monkeypatch)
    # T2 has no start record: only the daemon says it is top-level. `pause` never reaches the
    # daemon itself, so the catch-up runs on the lookup's account; `whoami` now reaches it too.
    code, result = cli(store, providers, [verb], {"CODEX_THREAD_ID": T2, "CODEX_SESSION_ID": T2})
    assert code == 0 and adapter._daemon is None
    if verb == "whoami":
        assert (result["classification"], result["session_key"], result["can_be_woken"], result["reason_code"]) == ("root", f"codex:{T2}", True, None)
    else:
        assert result["paused"] and store.paused
    assert daemon.wait_idle() and daemon.names == {T1: "alpha"}
    events = list(store.events())
    assert [e["handle"] for e in events if e["event"] == "named"] == ["alpha"]
    if verb == "pause":
        kinds = [e["event"] for e in events]
        assert kinds.index("pause") < kinds.index("named"), "the catch-up ran before the verb"


def test_cli_plain_shell_still_unpauses(store, providers, adapter):
    core(store, providers, T1).run("pause", {})
    code, result = cli(store, providers, ["unpause"], {})
    assert code == 0 and result["paused"] is False and not store.paused
    assert [e for e in store.events() if e["event"] == "unpause"][-1]["by_session"] is None


def mcp_face(store, tmp_path, monkeypatch):
    """The MCP server's per-call function, in process, against this test's store and fake executables."""
    import stat
    from kaiusap import mcp
    tmux = tmp_path / "bin" / "tmux"
    tmux.write_text("#!/bin/sh\nexit 1\n")
    tmux.chmod(tmux.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("KAIUSAP_HOME", str(store.root))

    def call(tool, args, thread):
        text, structured, failed = mcp.call("codex", tool, args, {"threadId": thread, "sessionId": thread} if thread else None)
        return (1 if failed else 0), structured
    return call


@pytest.mark.parametrize("face", ["cli", "mcp"])
def test_call_limit_caps_the_lookup_and_skips_the_catch_up(store, providers, adapter, daemon, monkeypatch, tmp_path, face):
    """A slow daemon: the lookup is capped by the call's 20 s (here 1 s), the catch-up starts nothing
    once no time is left, and the next call that reaches the daemon runs what was skipped."""
    import kaiusap.adapters.codex as codex
    pending_mirror(store, providers, monkeypatch)
    monkeypatch.setattr(codex, "CALL_LIMIT", 1.0)
    daemon.delays["thread/read"] = 3.0
    if face == "cli":
        call = lambda tool, args, thread: cli(store, providers, [tool], {"CODEX_THREAD_ID": thread, "CODEX_SESSION_ID": thread})
    else:
        call = mcp_face(store, tmp_path, monkeypatch)
    started = time.monotonic()
    code, result = call("whoami", {}, T2)
    elapsed = time.monotonic() - started
    assert code == 0 and result["classification"] == "unknown" and result["reason_code"] == "unknown_caller"
    assert "no answer to thread/read in time" in result["why_not"]
    assert elapsed < 2, f"the lookup ran {elapsed:.2f} s: the call limit didn't cap it"
    assert daemon.names == {} and not [e for e in store.events() if e["event"] == "named"]
    assert "thread/name/set" not in [q["method"] for q in daemon.requests] and adapter._daemon is None
    # The daemon answers promptly again: a plain-shell `who` reaches it and applies the name.
    daemon.delays.clear()
    time.sleep(0.1)
    code, result = call("who", {}, None)
    assert code == 0 and daemon.names == {T1: "alpha"}
    assert [e["handle"] for e in store.events() if e["event"] == "named"] == ["alpha"]


@pytest.mark.parametrize("face", ["cli", "mcp"])
def test_call_limit_reaches_the_catch_up(store, providers, adapter, daemon, monkeypatch, tmp_path, face):
    """The catch-up's own requests are capped by the time the call has left, not by their 5 s limit."""
    import kaiusap.adapters.codex as codex
    pending_mirror(store, providers, monkeypatch)
    monkeypatch.setattr(codex, "CALL_LIMIT", 1.5)
    daemon.delays["thread/name/set"] = 3.0
    if face == "cli":
        call = lambda tool, args, thread: cli(store, providers, [tool], {})
    else:
        call = mcp_face(store, tmp_path, monkeypatch)
    started = time.monotonic()
    code, result = call("who", {}, None)  # reaches the daemon for alpha's name; the catch-up then tries the pending mirror
    elapsed = time.monotonic() - started
    assert code == 0 and elapsed < 2.5, f"the catch-up ran {elapsed:.2f} s: the call limit didn't reach it"
    # The fake applied the name on receipt, as the daemon would; kaiusap, without an answer in
    # time, records nothing and says the name is still pending.
    assert "thread/name/set" in [q["method"] for q in daemon.requests]
    assert not [e for e in store.events() if e["event"] == "named"] and adapter._daemon is None
    assert any("wasn't set" in w and "time limit" in w for w in result["warnings"])
    daemon.delays.clear()
    time.sleep(0.1)
    code, result = call("who", {}, None)
    assert code == 0 and daemon.names == {T1: "alpha"} and result.get("warnings", []) == []
    assert [e["handle"] for e in store.events() if e["event"] == "named"] == ["alpha"]


# ----- tmux names for a trusted pane -------------------------

WINDOW_FORMAT = "#{s/ [|] .*//:#{s/^[^ -~]+ //:pane_title}}"  # the window rule, literally


def window_write(pane):
    return ["set-option", "-w", "-t", pane, "automatic-rename-format", WINDOW_FORMAT]


@pytest.fixture
def fake_tmux(runner):
    runner.fake = FakeTmux(4321, 1793606400)  # the server Runner.pane() reports
    return runner.fake


def shown(store, runner, fake, thread, session="0", pane_id="%1", tty="ttys001", pid=200, trusted=True):
    """A pane in its own tmux session whose title shows the thread; trusted by a start record."""
    fake.add(session, ids=[pane_id])
    runner.pane(f"{thread} | project", tty=tty, session=session, pane_id=pane_id)
    runner.process(tty=tty, pid=pid)
    if trusted:
        start(store, thread)
    return pane_id


def follow_setting(store, value="follow"):
    store.root.mkdir(parents=True, exist_ok=True)
    (store.root / "settings.json").write_text(json.dumps({"tmux_session_names": value}))


@pytest.mark.parametrize("source", ["startup", "resume", "clear", "compact", "fork"])
def test_codex_start_hook_sets_the_window_rule_for_a_trusted_pane(store, runner, adapter, fake_tmux, source):
    pane = shown(store, runner, fake_tmux, T1)
    adapter.tmux_start({"kind": source, "session_id": T1, "session_key": f"codex:{T1}"})
    assert fake_tmux.writes() == [window_write(pane)]
    assert fake_tmux.window_options[fake_tmux.window(pane)] == {"automatic-rename-format": WINDOW_FORMAT}


def test_codex_untrusted_pane_waits(store, runner, adapter, fake_tmux):
    shown(store, runner, fake_tmux, T1, trusted=False)
    adapter.tmux_start({"kind": "startup", "session_id": T1, "session_key": f"codex:{T1}"})
    assert fake_tmux.calls == []


def test_codex_iam_sets_the_window_rule_and_a_failure_is_harmless(store, providers, runner, adapter, daemon, fake_tmux):
    pane = shown(store, runner, fake_tmux, T1)
    fake_tmux.fail = {"set-option"}
    assert core(store, providers, T1).run("iam", {"handle": "alpha"})["warnings"] == []
    assert "tmux window name" in (store.root / "hook-errors.log").read_text()
    fake_tmux.fail = set()
    core(store, providers, T1).run("iam", {"handle": "beta"})
    assert fake_tmux.window_options[fake_tmux.window(pane)] == {"automatic-rename-format": WINDOW_FORMAT}
    assert daemon.names[T1] == "beta"


def test_codex_tmux_session_follows_iam_unless_turned_off(store, providers, runner, adapter, daemon, fake_tmux):
    pane = shown(store, runner, fake_tmux, T1)
    follow_setting(store, "off")
    core(store, providers, T1).run("iam", {"handle": "alpha"})
    assert fake_tmux.name(pane) == "0" and fake_tmux.titles(pane) == {}
    (store.root / "settings.json").unlink()  # no settings: the default is follow
    core(store, providers, T1).run("iam", {"handle": "beta"})
    assert fake_tmux.titles(pane) == {"set-titles": "on", "set-titles-string": "#S"}
    assert fake_tmux.name(pane) == "beta" and daemon.names[T1] == "beta"


def test_codex_retirement_renames_before_the_mirror(store, providers, runner, adapter, daemon, fake_tmux):
    follow_setting(store)
    first = shown(store, runner, fake_tmux, T1)
    second = shown(store, runner, fake_tmux, T2, session="1", pane_id="%2", tty="ttys002", pid=201)
    core(store, providers, T1).run("iam", {"handle": "alpha"})
    assert fake_tmux.name(first) == "alpha"
    offer(store, f"codex:{T2}", "alpha", f"codex:{T1}")  # T1 is loaded, so taking its name needs an offer
    result = core(store, providers, T2).run("iam", {"handle": "alpha", "confirmed": True})
    retired = retirement_name("alpha", handles(store)["alpha"]["at"])
    assert result["warnings"] == [] and daemon.names == {T1: retired, T2: "alpha"}
    assert (fake_tmux.name(first), fake_tmux.name(second)) == (retired, "alpha")


def test_codex_tmux_collision_is_skipped_with_iams_warning(store, providers, runner, adapter, daemon, fake_tmux):
    follow_setting(store)
    fake_tmux.add("alpha", ids=["%9"])
    pane = shown(store, runner, fake_tmux, T1)
    result = core(store, providers, T1).run("iam", {"handle": "alpha"})
    assert result["warnings"] == ["tmux session `alpha` already exists, so this tmux session keeps its name."]
    assert fake_tmux.name(pane) == "0" and daemon.names[T1] == "alpha"


@pytest.mark.parametrize("name,renamed", [("alpha", True), ("Fix the tests", False)])
def test_codex_start_renames_when_the_thread_is_named_the_handle(store, providers, runner, adapter, daemon, fake_tmux, name, renamed):
    """At start: Codex's current name from thread/read; a generated or typed name isn't followed."""
    follow_setting(store)
    pane = shown(store, runner, fake_tmux, T1)
    core(store, providers, T1).run("iam", {"handle": "alpha"})
    fake_tmux.sessions["$0"] = "0"  # after a reboot
    daemon.threads[T1]["name"] = name
    adapter.reset()
    adapter.tmux_start({"kind": "resume", "session_id": T1, "session_key": f"codex:{T1}"})
    assert fake_tmux.name(pane) == ("alpha" if renamed else "0")
    fake_tmux.sessions["$0"] = "0"
    adapter.reset()
    adapter.tmux_start({"kind": "clear", "session_id": T1, "session_key": f"codex:{T1}"})
    assert fake_tmux.name(pane) == "0"


def test_codex_window_rule_only_in_the_start_hook(store, runner, fake_tmux, monkeypatch, daemon):
    """Through the hook entry: the start hook writes the rule; the constant hooks make no window-rule call."""
    from kaiusap.hooks import dispatch
    monkeypatch.setenv("KAIUSAP_HOME", str(store.root))
    monkeypatch.setattr("kaiusap.adapters.codex.run_command", runner)
    pane = shown(store, runner, fake_tmux, T1)
    data = {"session_id": T1, "cwd": "/fixture", "transcript_path": f"/x/rollout-2026-11-02T10-00-00-{T1}.jsonl",
            "source": "resume", "prompt": "Hello"}
    dispatch("codex", "SessionStart", data, time.monotonic() + 3)
    assert fake_tmux.writes() == [window_write(pane)]
    fake_tmux.calls.clear()
    dispatch("codex", "UserPromptSubmit", data, time.monotonic() + 3)
    dispatch("codex", "SubagentStart", {**data, "agent_id": SUB}, time.monotonic() + 3)
    assert fake_tmux.calls == []


# ----- the terminal map's one-time move to sessions/codex/ -----------------

OLD_MAP, NEW_MAP = "sessions/terminals.jsonl", "sessions/codex/terminals.jsonl"


def map_line(at, pane="4321@1793606400:%1", thread=T1, source="read"):
    return json.dumps({"at": at, "pane": pane, "codex_pid": 200, "codex_started_at": "2026-01-01T00:00:00Z",
                       "thread_prefix": thread[:29], "thread_id": thread, "source": source, "session_key": f"codex:{thread}"},
                      ensure_ascii=False).encode()


def write_lines(store, relative, lines):
    path = store.root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"".join(line + b"\n" for line in lines))


def test_the_adapter_owns_the_terminal_map_and_writers_use_only_the_new_path(store):
    adapter = CodexAdapter(store, run=lambda *a: (1, ""))
    assert adapter.terminal_map == NEW_MAP and store.adapter_dir("codex") == store.root / "sessions/codex"
    runner = Runner()
    runner.pane(f"Codex {T1}")
    adapter = CodexAdapter(store, run=runner)
    adapter.record({"record": {"at": now(), "provider": "codex", "session_id": T1, "session_key": f"codex:{T1}",
                               "source": "prompt", "cwd": "/w"}, "file": "sessions/prompts.jsonl", "kind": "prompt",
                    "session_id": T1, "session_key": f"codex:{T1}", "at": now()})
    adapter.observe(adapter.panes())
    assert [r["source"] for r in store.rows(NEW_MAP)] == ["prompt", "read"]
    assert not (store.root / OLD_MAP).exists()


def test_readers_merge_the_old_and_new_maps_by_time_until_the_move(store):
    old = [map_line(f"2026-01-01T00:00:0{n}Z") for n in (1, 3, 5)]
    new = [map_line("2026-01-01T00:00:04Z", thread=T2), map_line("2026-01-01T00:00:06Z", thread=T2)]
    write_lines(store, OLD_MAP, old)
    write_lines(store, NEW_MAP, new)
    adapter = CodexAdapter(store, run=lambda *a: (1, ""))
    assert [r["at"][-3:-1] for r in adapter.terminal_rows()] == ["01", "03", "04", "05", "06"]
    assert [r["at"][-3:-1] for r in adapter.terminal_rows(reverse=True)] == ["06", "05", "04", "03", "01"]
    # A line read from both files (a move interrupted after its rename, or one read while it ran) counts once.
    write_lines(store, NEW_MAP, [*old, *new])
    assert len(list(adapter.terminal_rows())) == 5
    assert store.latest_of(adapter.terminal_rows(reverse=True), lambda r: r["thread_id"] == T1)["at"] == "2026-01-01T00:00:05Z"


def test_the_move_keeps_every_line_once_and_empties_the_old_file(store):
    old = [map_line(f"2026-01-01T00:00:{n:02d}Z", pane=f"4321@1793606400:%{n % 3}") for n in range(40)] + [b"not json"]
    new = [map_line(now(), thread=T2, source="prompt")]  # a native append made before the move
    write_lines(store, OLD_MAP, old)
    write_lines(store, NEW_MAP, new)
    adapter = CodexAdapter(store, run=lambda *a: (1, ""))
    before = [r for r in adapter.terminal_rows()]
    adapter.move_terminal_map()
    assert (store.root / OLD_MAP).read_bytes() == b""
    assert (store.root / NEW_MAP).read_bytes().splitlines() == [*old, *new]  # byte for byte, old history first
    assert list(adapter.terminal_rows()) == before
    adapter.move_terminal_map()  # nothing left to move
    assert (store.root / NEW_MAP).read_bytes().splitlines() == [*old, *new]
    # Rerun after a crash between the rename and the emptying: the old lines are already there.
    write_lines(store, OLD_MAP, old)
    adapter.move_terminal_map()
    assert (store.root / NEW_MAP).read_bytes().splitlines() == [*old, *new]
    assert not [p for p in (store.root / "sessions/codex").iterdir() if p.name.startswith(".tmp-")]


def test_the_move_runs_at_a_calls_begin_and_at_a_codex_start_and_never_under_the_global_lock(store, monkeypatch):
    write_lines(store, OLD_MAP, [map_line("2026-01-01T00:00:01Z")])
    adapter = CodexAdapter(store, run=lambda *a: (1, ""))
    with store.locked():
        adapter.move_terminal_map()  # it would wait for itself: the next call moves it
        assert (store.root / OLD_MAP).stat().st_size and not (store.root / NEW_MAP).exists()
    adapter.begin()
    adapter.finish(catch_up=False)
    assert not (store.root / OLD_MAP).stat().st_size and len(list(store.rows(NEW_MAP))) == 1
    write_lines(store, OLD_MAP, [map_line("2026-01-01T00:00:02Z")])
    calls = []
    monkeypatch.setattr(adapter, "step_fork", lambda *a: calls.append("fork") or None)
    adapter.follow_start({"kind": "fork", "session_id": T1, "session_key": f"codex:{T1}", "deadline": time.monotonic() + 3}, store)
    assert calls == ["fork"] and len(list(store.rows(NEW_MAP))) == 2


def test_the_move_waits_for_the_locks_and_otherwise_leaves_both_files(store):
    import fcntl
    write_lines(store, OLD_MAP, [map_line("2026-01-01T00:00:01Z")])
    adapter = CodexAdapter(store, run=lambda *a: (1, ""))
    with open(store.root / OLD_MAP, "a") as held:
        fcntl.flock(held, fcntl.LOCK_EX)  # an older build appending
        started = time.monotonic()
        adapter.move_terminal_map(wait=.05)
        assert time.monotonic() - started < 1
    assert (store.root / OLD_MAP).stat().st_size and len(list(adapter.terminal_rows())) == 1
    adapter.move_terminal_map()
    assert len(list(store.rows(NEW_MAP))) == 1


def test_a_writer_waiting_for_the_new_maps_lock_appends_to_the_moved_file(store, monkeypatch):
    # Every append locks the file at the path. One that waited while the move renamed a file over it
    # finds the file replaced and appends to the new one (Store.locked; native/hook.rs's append likewise).
    import threading
    write_lines(store, OLD_MAP, [map_line("2026-01-01T00:00:01Z")])
    adapter = CodexAdapter(store, run=lambda *a: (1, ""))
    original, writer = store.locked, []

    from contextlib import contextmanager

    @contextmanager
    def locked(relative="lock", deadline=None):
        with original(relative, deadline) as fd:
            if relative == NEW_MAP and not writer:
                other = __import__("kaiusap.store", fromlist=["Store"]).Store(store.root)
                writer.append(threading.Thread(target=other.append, args=(NEW_MAP, {"at": now(), "pane": "late"})))
                writer[0].start()
                time.sleep(.05)
            yield fd

    monkeypatch.setattr(store, "locked", locked)
    adapter.move_terminal_map()
    writer[0].join(5)
    rows = list(store.rows(NEW_MAP))
    assert [r["pane"] for r in rows] == ["4321@1793606400:%1", "late"]


# =====================================================================================================
# Codex's behaviour checklist.
#
# Scenarios for following a Codex start (and Codex's identity, liveness, suggestions and naming), each
# checked through the public surface only: `kaiusap hook codex <event>` with fake inputs, the verbs (CLI),
# the store's records and the hook's output, with the fake daemon, tmux and `ps` as programs on PATH. Nothing
# below names anything internal to the Codex adapter. Everything above this line is the author's own tests.
# =====================================================================================================

import subprocess as _subprocess
import sys as _sys
from pathlib import Path as _Path

from kaiusap.notice import SESSION_LINE

T4 = "019a1b2c-4444-7000-8000-000000000004"
T5 = "019a1b2c-6666-7000-8000-000000000006"
PANE = "4321@1793606400:%1"
PANE2 = "4321@1793606400:%2"


class Terminal:
    """A machine as Codex's hooks and verbs see it: tmux panes, the processes on their ttys and the daemon,
    all fakes on PATH, and the store under KAIUSAP_HOME."""

    def __init__(self, store, tmp_path, daemon, monkeypatch):
        self.store, self.daemon = store, daemon
        self.dir = tmp_path / "checklist"
        self.dir.mkdir()
        self.panes, self.calls = self.dir / "panes", self.dir / "tmux-calls"
        self.panes.write_text("")
        bin_dir = tmp_path / "bin"  # the fake codex's directory, already on PATH
        for name, body in {
            "tmux": f'printf "%s\\n" "$*" >> "{self.calls}"\n'
                    f'if [ "$3" = list-panes ] && [ "$4" = -a ]; then\n'
                    f'  [ -f "{self.dir}/tmux-fails" ] && exit 1\n'
                    f'  [ -f "{self.dir}/tmux-slow" ] && /bin/sleep "$(/bin/cat "{self.dir}/tmux-slow")"\n'
                    f'  /bin/cat "{self.panes}"\nfi\nexit 0',
            "ps": f'if [ "$1" = -t ] && [ -f "{self.dir}/ps-$2.hangs" ]; then exec /bin/sleep 10; fi\n'
                  f'if [ "$1" = -t ] && [ -f "{self.dir}/ps-$2" ]; then /bin/cat "{self.dir}/ps-$2"; fi\nexit 0',
            "claude": "printf '[]'",
        }.items():
            (bin_dir / name).write_text("#!/bin/sh\n" + body + "\n")
            (bin_dir / name).chmod(0o700)
            # A program's first run after it is written is slow on macOS (it is checked), which alone would
            # spend a constant hook's 50 ms.
            _subprocess.run([str(bin_dir / name), "-V"], capture_output=True)
        self.calls.unlink(missing_ok=True)
        monkeypatch.setenv("KAIUSAP_HOME", str(store.root))
        # A store that already exists, as on any machine past its first hook: creating it takes most of a
        # constant hook's 50 ms.
        (store.root / "sessions" / "codex").mkdir(parents=True, exist_ok=True)
        self.native = _Path(_sys.executable).parent / "kaiusap"
        self.rows = {}

    # ----- the fakes ---------------------------------------------------------------------------------

    def pane(self, pane_id, title, tty="ttys001", session="work", attached=1, pane_pid=100):
        """A tmux pane (its eight fields), replacing one with the same id."""
        self.rows[pane_id] = (pane_id, session, str(attached), str(pane_pid), "/dev/" + tty, "4321", "1793606400", title)
        self.panes.write_text("".join("\t".join(row) + "\n" for row in self.rows.values()))
        return f"4321@1793606400:{pane_id}"

    def title(self, pane_id, title):
        row = self.rows[pane_id]
        return self.pane(pane_id, title, tty=row[4][5:], session=row[1], attached=int(row[2]), pane_pid=int(row[3]))

    def codex(self, tty="ttys001", pid=200, ppid=100, started=-60, args="codex"):
        """The pane's Codex terminal process, started `started` seconds from now (`ps -t <tty>`)."""
        (self.dir / f"ps-{tty}").write_text(f"{pid} {ppid} {pid} {pid} S+ {lstart(started)} {args}\n")

    # ----- the public surface ------------------------------------------------------------------------

    def hook(self, event, thread, cwd="/fixture", transcript=None, **extra):
        """`kaiusap hook codex <event>` with Codex's hook input (codex-rs/hooks/src/schema.rs); its output."""
        data = {"session_id": thread, "transcript_path": transcript or f"/x/rollout-2026-11-02T10-00-00-{thread}.jsonl",
                "cwd": cwd, "hook_event_name": event, "model": "gpt-5.5", "permission_mode": "default"}
        if event == "UserPromptSubmit":
            data |= {"turn_id": "019a0000-0000-7000-8000-000000000001", "prompt": "Hello"}
        data |= extra
        r = _subprocess.run([str(self.native), "hook", "codex", event], input=json.dumps(data), text=True,
                            capture_output=True, timeout=6)
        assert r.returncode == 0 and not r.stderr, r.stderr
        return json.loads(r.stdout) if r.stdout else {}

    def start(self, thread, source, cwd="/fixture", **extra):
        """The start hook's told text, less the session line, or None when it said nothing at all."""
        out = self.hook("SessionStart", thread, cwd, source=source, **extra)
        if not out:
            return None
        text = out["hookSpecificOutput"]["additionalContext"].split("\n")
        assert text[0] == SESSION_LINE
        return "\n".join(text[1:])

    def prompt(self, thread, cwd="/fixture", **extra):
        return self.hook("UserPromptSubmit", thread, cwd, **extra)

    def verb(self, thread, *argv, session=None, cwd="/fixture"):
        """A verb from the thread's own shell (CODEX_THREAD_ID and CODEX_SESSION_ID): exit code and JSON."""
        env = {**os.environ, "CODEX_THREAD_ID": thread, "CODEX_SESSION_ID": session or thread, "PWD": cwd}
        r = _subprocess.run([str(self.native), *argv, "--json"], text=True, capture_output=True, timeout=25, env=env)
        return r.returncode, json.loads(r.stdout) if r.stdout.strip() else None

    def iam(self, thread, handle, cwd="/fixture", *extra):
        code, result = self.verb(thread, "iam", handle, *extra, cwd=cwd)
        assert code == 0, result

    # ----- the store ---------------------------------------------------------------------------------

    def handles(self):
        bound = {}
        for line in self.store.rows("handles.jsonl"):
            bound[line["handle"]] = line if line.get("session_key") else None
        return bound

    def holder(self, handle):
        line = self.handles().get(handle)
        return line and line["session_key"]

    def lines(self, name):
        path = self.store.root / name
        return list(self.store.rows(name)) if path.exists() else []

    def events(self, kind):
        return [e for e in self.store.events() if e.get("event") == kind]

    def log(self):
        path = self.store.root / "hook-errors.log"
        return path.read_text() if path.exists() else ""


@pytest.fixture
def world(store, tmp_path, daemon, monkeypatch):
    """Five threads, every one loaded in the daemon (live) until a scenario unloads it."""
    daemon.threads.update({thread: {} for thread in (T1, T2, T3, T4, T5)})
    daemon.loaded[:] = [T1, T2, T3, T4, T5]
    return Terminal(store, tmp_path, daemon, monkeypatch)


# ----- the prompt hook --------------------------------------------------

def test_checklist_prompt_records_and_one_terminal_line_per_pane_showing_t(world):
    # The prompt record and one `prompt` line per pane whose
    # title shows T (in full or as a 29-character prefix), none for a pane showing another thread.
    world.pane("%1", f"Codex | {T1}")
    world.pane("%2", f"{T1[:29]}... working", tty="ttys002")
    world.pane("%3", f"Codex | {T3}", tty="ttys003")
    assert world.prompt(T1) == {}
    [record] = world.lines("sessions/prompts.jsonl")
    assert (record["session_key"], record["source"], record["cwd"]) == (f"codex:{T1}", "prompt", "/fixture")
    lines = world.lines("sessions/codex/terminals.jsonl")
    assert sorted(l["pane"] for l in lines) == [PANE, PANE2]
    assert all(l["source"] == "prompt" and l["thread_id"] == T1 and l["codex_pid"] is None for l in lines)


def test_checklist_prompt_without_tmux_still_records(world):
    # A `list-panes` call that fails gives no `prompt` lines; the prompt record is still written.
    world.pane("%1", f"Codex | {T1}")
    (world.dir / "tmux-fails").write_text("")
    world.prompt(T1)
    assert len(world.lines("sessions/prompts.jsonl")) == 1 and world.lines("sessions/codex/terminals.jsonl") == []


@pytest.mark.parametrize("caller", ["agent_id", "review", "null-transcript"])
def test_checklist_spawned_or_unidentified_prompt_does_nothing(world, caller):
    # A subagent (`agent_id`), a /review or guardian thread (the transcript names
    # another thread) or an unidentified input (null transcript) gets no note and writes nothing.
    world.pane("%1", f"Codex | {T1}")
    world.iam(T1, "alpha")
    world.verb(T3, "send", "--to", "alpha", "--subject", "s", "--reply-needed", "no", "--context", "c")
    extra = {"agent_id": [{"agent_id": T4, "agent_type": "worker"}], "review": [{"transcript": f"/x/rollout-2026-11-02T10-00-00-{T4}.jsonl"}],
             "null-transcript": [{"transcript_path": None}]}[caller][0]
    before = world.lines("sessions/prompts.jsonl")
    assert world.prompt(T1, **extra) == {}
    assert world.lines("sessions/prompts.jsonl") == before and world.lines("sessions/codex/terminals.jsonl") == []
    assert world.events("reminded") == []


def test_checklist_a_notice_as_generated_is_not_recorded_and_a_typed_paste_line_is(world):
    # A prompt that is a kaiusap notice exactly as generated writes neither line; a
    # paste line the user typed is a real prompt and is recorded.
    world.pane("%1", f"Codex | {T1}")
    world.iam(T1, "alpha")
    world.iam(T3, "beta")
    code, sent = world.verb(T3, "send", "--to", "alpha", "--subject", "Review", "--reply-needed", "no", "--context", "c")
    assert code == 0 and sent["outcome"] == "woken"
    [turn] = world.daemon.turns
    notice_text = turn["input"][0]["text"]
    world.prompt(T1, prompt=notice_text + "\n")
    assert world.lines("sessions/prompts.jsonl") == [] and world.lines("sessions/codex/terminals.jsonl") == []
    world.prompt(T1, prompt=f"Run kaiusap read {sent['message_id']}")
    assert len(world.lines("sessions/prompts.jsonl")) == 1 and len(world.lines("sessions/codex/terminals.jsonl")) == 1


def test_checklist_prompt_adds_the_unread_mail_note(world):
    # Through Codex's prompt hook: mail for the session's handle is told at its next prompt, once.
    world.iam(T1, "alpha")
    world.iam(T3, "beta")
    world.daemon.loaded.remove(T1)
    code, sent = world.verb(T3, "send", "--to", "alpha", "--subject", "s", "--reply-needed", "no", "--context", "c")
    assert sent["outcome"] == "stored" and sent["reason_code"] == "thread_not_loaded"
    note = world.prompt(T1)["hookSpecificOutput"]["additionalContext"]
    assert note.startswith("kaiusap: 1 unread message, from beta. Tell the user.")
    assert world.prompt(T1) == {}


# ----- start hooks and identity ---------------------------------------------------------

@pytest.mark.parametrize("caller", ["agent_id", "review", "null-transcript"])
def test_checklist_spawned_or_unidentified_start_says_nothing_and_writes_nothing(world, caller):
    # A spawned or unidentified start hook writes no record and says
    # nothing, not even the session line.
    extra = {"agent_id": {"agent_id": T4}, "review": {"transcript": f"/x/rollout-2026-11-02T10-00-00-{T4}.jsonl"},
             "null-transcript": {"transcript_path": None}}[caller]
    assert world.start(T1, "startup", **extra) is None
    assert world.lines("sessions/starts.jsonl") == []


def test_checklist_subagent_start_is_record_only(world):
    # `SubagentStart` writes the subagent's start record and nothing else.
    out = world.hook("SubagentStart", T1, turn_id="t", agent_id=T4, agent_type="worker")
    assert out == {}
    [record] = world.lines("sessions/starts.jsonl")
    assert (record["source"], record["agent_id"], record["session_key"]) == ("subagent", T4, f"codex:{T1}/{T4}")


def test_checklist_every_identified_start_gives_the_session_line(world):
    # Every identified start, on every source, gives the session line first; an unrecognised source the
    # line alone and no record.
    for source in ("startup", "resume", "compact"):
        assert world.start(T1, source) == ""
    assert world.start(T1, "weird") == ""
    assert [r["source"] for r in world.lines("sessions/starts.jsonl")] == ["startup", "resume", "compact"]


def test_checklist_a_spawned_shell_may_look_but_not_act(world):
    # CODEX_SESSION_ID differing from the thread is a spawned agent under that top-level
    # thread, refused for acting verbs (exit 1, before anything is written) and allowed to look.
    world.iam(T1, "alpha")
    before = world.lines("handles.jsonl")
    code, result = world.verb(T4, "iam", "beta", session=T1)
    assert code == 1 and "Report to the agent that started you" in result["error"]
    assert world.lines("handles.jsonl") == before
    code, me = world.verb(T4, "whoami", session=T1)
    assert code == 0 and me["spawned"] and me["parent_handle"] == "alpha"


# ----- liveness and trust ------------------------------------------------

def test_checklist_a_loaded_thread_is_live_and_an_unloaded_one_is_stored(world):
    # Live when the daemon has it loaded, whether or not a pane shows it; otherwise
    # mail is `stored`, `thread_not_loaded`.
    world.iam(T1, "alpha")
    world.iam(T3, "beta")
    code, me = world.verb(T1, "whoami")
    assert me["can_be_woken"] and me["reason_code"] is None
    assert world.verb(T3, "send", "--to", "alpha", "--subject", "s", "--reply-needed", "no", "--context", "c")[1]["outcome"] == "woken"
    world.daemon.loaded.remove(T1)
    code, me = world.verb(T1, "whoami")
    assert not me["can_be_woken"] and me["reason_code"] == "thread_not_loaded"
    sent = world.verb(T3, "send", "--to", "alpha", "--subject", "s", "--reply-needed", "no", "--context", "c")[1]
    assert sent["outcome"] == "stored" and sent["reason_code"] == "thread_not_loaded"


def test_checklist_detached_only_when_trusted_panes_show_it(world):
    # A thread is held as detached only when trusted panes show it and every one
    # is detached; a pane is trusted only by a non-compact start or a prompt record at least 1 s after its
    # Codex process started.
    world.pane("%1", f"Codex | {T1}", session="alpha", attached=0)
    world.codex()
    world.iam(T1, "alpha")
    world.hook("SessionStart", T1, source="compact")
    code, me = world.verb(T1, "whoami")
    assert me["can_be_woken"]
    rows = world.verb(T1, "who")[1]["handles"]
    assert not rows[0]["detached"]  # not yet trusted: a compact start doesn't count
    world.prompt(T1)
    rows = world.verb(T1, "who")[1]["handles"]
    assert rows[0]["detached"]
    (world.store.root / "settings.json").write_text(json.dumps({"wake_detached": False}))
    code, me = world.verb(T1, "whoami")
    assert not me["can_be_woken"] and me["reason_code"] == "detached"


# ----- step 1, `fork` -----------------------------------------------------------------

def held_by_t1(world, cwd="/work/one"):
    """alpha on T1, whose last recorded directory is `cwd` (its prompt record)."""
    world.prompt(T1, cwd)
    world.iam(T1, "alpha", cwd)


def titled(world, *threads):
    """A pane whose title shows each of `threads`, with no Codex process: a start of one of them finds its
    pane at once (no title retry) and no predecessor in it."""
    world.pane("%9", "Codex | " + " ".join(threads), tty="ttys009")


def test_checklist_fork_to_another_directory_asks_and_a_confirmation_continues(world):
    # Step 1: a holder found in another directory is a move, `moved-directory`, told
    # "You are <handle>; ...". The new thread is named (it has no name) and the one left is retired.
    held_by_t1(world)
    world.daemon.threads[T3] = {"forkedFromId": T1}
    text = world.start(T3, "fork", "/work/two")
    assert text == f"You may be alpha: this conversation started from alpha's thread {T1}, in another directory. Ask the user whether this is alpha moving here (/cd, or continuing in a worktree) or a copy (/fork)."  # asked, nothing moved
    assert world.holder("alpha") == f"codex:{T1}"
    # The user says "you are alpha"; T1 is loaded (live), so kaiusap asks the user to confirm first.
    code, asked = world.verb(T3, "iam", "alpha")
    assert code == 1 and asked["reason_code"] == "confirmation_needed" and world.holder("alpha") == f"codex:{T1}"
    world.iam(T3, "alpha", "/fixture", "--confirmed")  # the user confirms: the persona continues in T3, with its mail
    line = world.handles()["alpha"]
    assert (line["session_key"], line["reason"], line["moved_from"]["session_key"]) == (f"codex:{T3}", "iam", f"codex:{T1}")
    assert world.daemon.names[T3] == "alpha" and world.daemon.names[T1].startswith("alpha~")


def test_checklist_fork_in_the_same_directory_is_a_copy(world):
    # Step 1: the same directory, with no pane that switched from the parent to the copy: a copy, no handle,
    # told so, shown the copy line, and offered the handle.
    held_by_t1(world)
    world.daemon.threads[T3] = {"forkedFromId": T1}
    out = world.hook("SessionStart", T3, "/work/one", source="fork")
    assert out["hookSpecificOutput"]["additionalContext"] == SESSION_LINE + "\n" + copy_text("alpha")
    assert out["systemMessage"] == copy_shown("alpha")
    assert world.holder("alpha") == f"codex:{T1}"
    assert offers(world.store, f"codex:{T3}") == [("alpha", f"codex:{T1}")]


def test_checklist_fork_walks_past_threads_without_a_start_record(world):
    # Step 1: the walk goes up `forkedFromId` through threads with no start record (opened and left
    # without a prompt) to the thread that holds a handle.
    held_by_t1(world)
    world.daemon.threads[T4] = {"forkedFromId": T1}
    world.daemon.threads[T3] = {"forkedFromId": T4}
    assert world.start(T3, "fork", "/work/two").startswith("You may be alpha: this conversation started from alpha's thread " + T1)
    assert world.holder("alpha") == f"codex:{T1}"  # asked, nothing moved


@pytest.mark.parametrize("parent", ["recorded", "root", "none"])
def test_checklist_fork_of_a_thread_holding_nothing_is_a_copy(world, parent):
    # Step 1: the walk stops at a thread holding no handle (one with a start record, or
    # the root): a copy, no handle. A `thread/read` of T with no `forkedFromId` ends the walk
    # at once, with the copy text too.
    held_by_t1(world)
    if parent == "recorded":
        titled(world, T4)
        world.start(T4, "startup", "/work/two")
        world.daemon.threads[T4] = {"forkedFromId": T1}
    if parent != "none":
        world.daemon.threads[T3] = {"forkedFromId": T4}
    assert world.start(T3, "fork", "/work/two") == "This conversation is a copy of another one, which held no handle; you have no handle."
    assert world.holder("alpha") == f"codex:{T1}"


ASK = "This conversation was copied or moved from another one. Ask the user whether it should hold a handle."


@pytest.mark.parametrize("lookup", ["read-error", "loop", "not-a-string", "no-directory"])
def test_checklist_fork_with_an_incomplete_lookup_is_told_to_ask(world, lookup):
    # Step 1: a `thread/read` error, a `forkedFromId` that isn't a string
    # or names a thread the walk already passed, and a holder whose directory can't be found:
    # no start or prompt record, no `cwd` from `thread/read`): no handle, told to ask.
    if lookup == "no-directory":
        world.iam(T1, "alpha")
        world.daemon.threads[T1] = {"cwd": None, "name": "alpha"}
        world.daemon.threads[T3] = {"forkedFromId": T1}
    else:
        held_by_t1(world)
        world.daemon.threads[T3] = {"forkedFromId": {"read-error": T1, "loop": T4, "not-a-string": 7}[lookup]}
        world.daemon.threads[T4] = {"forkedFromId": T5}  # T4 and T5, opened and left without a prompt, name
        world.daemon.threads[T5] = {"forkedFromId": T4}  # each other
        if lookup == "read-error":
            world.daemon.read = "error"
    assert world.start(T3, "fork", "/work/two") == ASK
    assert world.holder("alpha") == f"codex:{T1}"


def test_checklist_fork_without_the_daemon_is_told_to_ask(world, tmp_path, socket_dir):
    # Step 1: no daemon reachable.
    held_by_t1(world)
    fake_codex(tmp_path / "bin", os.path.join(socket_dir, "missing.sock"))
    assert world.start(T3, "fork", "/work/two") == ASK


def test_checklist_fork_skips_step_4(world):
    # Step 1: a `fork` start gets no tmux-name suggestion, even in a tmux session named
    # after a handle with no live session.
    world.iam(T5, "beta")
    world.daemon.loaded.remove(T5)
    world.pane("%1", f"Codex | {T3}", session="beta")
    world.codex()
    assert world.start(T3, "fork", "/work/two") == "This conversation is a copy of another one, which held no handle; you have no handle."


# ----- step 2, `startup` and `clear` --------------------------------------------------

def replaced_in(world, holder, cwd="/fixture", prompted=True):
    """Pane %1's Codex showed `holder`, prompted there in `cwd` when `prompted` (otherwise only looked at
    by a verb, which writes a `read` line naming it); its title now shows T2."""
    world.pane("%1", f"Codex | {holder}")
    world.codex()
    if prompted:
        world.prompt(holder, cwd)
    else:
        world.verb(holder, "whoami", cwd=cwd)
    world.title("%1", f"Codex | {T2}")


@pytest.mark.parametrize("source", ["clear", "startup"])
def test_checklist_fresh_start_leaves_the_handle_and_names_the_new_thread(world, source):
    # Step 2: the handle stays with the thread left. The new thread is told the fresh-session text, the user is
    # shown its line, and the thread is named `<handle>~<yymmddhhmmss>`; the thread left keeps its name.
    world.iam(T1, "alpha")
    replaced_in(world, T1)
    out = world.hook("SessionStart", T2, source=source)
    told = out["hookSpecificOutput"]["additionalContext"].split("\n")
    assert told[0] == SESSION_LINE and "\n".join(told[1:]) == fresh_text("alpha", T1, f"codex resume {T1}")
    assert out["systemMessage"] == fresh_shown("alpha", f"codex resume {T1}")
    assert world.holder("alpha") == f"codex:{T1}"
    assert re.fullmatch(r"alpha~\d{12}", world.daemon.names[T2]) and world.daemon.names[T1] == "alpha"
    assert offers(world.store, f"codex:{T2}") == [("alpha", f"codex:{T1}")]


def test_checklist_a_fresh_thread_keeps_its_own_name(world):
    # A fresh thread is named only when `thread/read` shows it has no name, or the handle's; a name typed
    # with /new is kept.
    world.iam(T1, "alpha")
    world.daemon.threads[T2] = {"name": "typed with /new"}
    replaced_in(world, T1)
    assert world.start(T2, "clear").startswith("This is a new conversation. It is not alpha:")
    assert T2 not in world.daemon.names
    assert [e for e in world.events("named") if e.get("session_key") == f"codex:{T2}"] == []


def test_checklist_a_relaunch_moves_nothing(world):
    # Step 2: a pane with no predecessor in its current Codex process (a relaunch):
    # no move; a `startup` (`/new`) then gets no /clear suggestion (step 4).
    world.iam(T1, "alpha")
    world.daemon.loaded.remove(T1)
    world.pane("%1", f"Codex | {T1}")
    world.codex(started=-120)
    world.prompt(T1)
    world.codex(started=5)  # a new Codex process in the pane, started after T1's line: that line doesn't count
    world.title("%1", f"Codex | {T2}")
    assert world.start(T2, "startup") == ""
    assert world.holder("alpha") == f"codex:{T1}"


def test_checklist_no_pane_moves_nothing_and_clear_suggests(world):
    # Step 2 (no pane showing T, so no tmux) and step 4: a `clear` without tmux still
    # suggests the /clear candidates; a `startup` says nothing.
    world.prompt(T1)
    world.iam(T1, "alpha")
    world.daemon.loaded.remove(T1)
    assert world.start(T2, "startup") == ""
    assert world.start(T3, "clear") == clear_suggestion("alpha")
    assert world.holder("alpha") == f"codex:{T1}"


def test_checklist_a_holder_in_another_directory_stays_and_is_told(world):
    # Step 2: a different directory is told the same fresh-session text, whose way back starts with a `cd`.
    # Step 4: no /clear suggestion though a candidate exists (beta, not live, last in T's directory).
    world.iam(T1, "alpha", "/work/one")
    world.prompt(T5, "/work/two")
    world.iam(T5, "beta", "/work/two")
    world.daemon.loaded.remove(T5)
    replaced_in(world, T1, "/work/one")
    assert world.start(T2, "clear", "/work/two") == fresh_text("alpha", T1, f"cd /work/one && codex resume {T1}")
    assert world.holder("alpha") == f"codex:{T1}" and world.holder("beta") == f"codex:{T5}"


def test_checklist_a_holder_in_another_directory_still_gets_the_tmux_name_suggestion(world):
    # Step 4: the tmux-name suggestion still applies to a fresh thread told that the handle stayed.
    world.iam(T1, "alpha", "/work/one")
    world.iam(T5, "beta")
    world.daemon.loaded.remove(T5)
    world.pane("%1", f"Codex | {T1}", session="beta")
    world.codex()
    world.prompt(T1, "/work/one")
    world.title("%1", f"Codex | {T2}")
    told = world.start(T2, "clear", "/work/two")
    assert told.startswith("This is a new conversation. It is not alpha:")
    assert told.split("\n", 1)[1] == tmux_name_suggestion("beta")


def test_checklist_a_holder_whose_directory_cant_be_found(world):
    # Step 2: the directory of the thread left doesn't matter: with none known, the way back has no `cd`.
    world.iam(T1, "alpha")
    world.daemon.threads[T1] = {"cwd": None, "name": "alpha"}
    replaced_in(world, T1, prompted=False)
    assert world.start(T2, "clear") == fresh_text("alpha", T1, f"codex resume {T1}")
    assert world.holder("alpha") == f"codex:{T1}"


def test_checklist_the_walk_back_passes_threads_holding_nothing(world):
    # Step 2: a predecessor holding nothing and never prompted in this process is walked
    # past, to the first holder, which is the thread left.
    world.iam(T1, "alpha")
    replaced_in(world, T1)
    world.title("%1", f"Codex | {T4}")
    world.verb(T4, "whoami")  # T4 shown, looked at, never prompted
    world.title("%1", f"Codex | {T2}")
    assert world.start(T2, "clear").startswith(f"This is a new conversation. It is not alpha: alpha stays with the "
                                               f"conversation left in this terminal, thread {T1}.")
    assert world.holder("alpha") == f"codex:{T1}"


def test_checklist_a_prompted_thread_holding_nothing_stops_the_walk(world):
    # Step 2: a thread the user prompted in this process that holds nothing stops the
    # walk with no move, and nothing is told.
    world.iam(T1, "alpha")
    replaced_in(world, T1)
    world.title("%1", f"Codex | {T4}")
    world.prompt(T4)
    world.title("%1", f"Codex | {T2}")
    assert world.start(T2, "startup") == ""
    assert world.holder("alpha") == f"codex:{T1}"


def two_panes(world, first, second, prompted=True):
    """Panes %1 and %2, each with its own Codex, showed `first` and `second`; both now show T2."""
    world.pane("%1", f"Codex | {first}")
    world.pane("%2", f"Codex | {second}", tty="ttys002", pane_pid=101)
    world.codex()
    world.codex(tty="ttys002", pid=300, ppid=101)
    for thread in dict.fromkeys((first, second)):
        world.prompt(thread) if prompted else world.verb(thread, "whoami")
    world.title("%1", f"Codex | {T2}")
    world.title("%2", f"Codex | {T2}")


def test_checklist_panes_with_different_holders_move_neither(world):
    # Step 2: the panes resolve to different holders: no move, told the holders found,
    # joined by " or ".
    world.iam(T1, "alpha")
    world.iam(T3, "beta")
    two_panes(world, T1, T3)
    assert world.start(T2, "clear") == ("You may be alpha or beta: this terminal showed each before this conversation, so kaiusap "
                                        "moved neither. Tell the user.")
    assert world.holder("alpha") == f"codex:{T1}" and world.holder("beta") == f"codex:{T3}"


def test_checklist_one_pane_with_a_holder_and_one_without_is_told_the_one(world):
    # Step 2: some panes resolve to a holder and some to none: no move,
    # told the one-name text. Step 4: no /clear suggestion.
    world.iam(T1, "alpha")
    two_panes(world, T1, T4)
    assert world.start(T2, "clear") == ("You may be alpha: this terminal showed it before this conversation, so kaiusap didn't "
                                        "move it. Tell the user.")
    assert world.holder("alpha") == f"codex:{T1}"


def test_checklist_panes_with_no_holder_tell_nothing(world):
    # Step 2: no pane resolves to a holder: no move, nothing told.
    two_panes(world, T3, T4)
    assert world.start(T2, "startup") == ""


def test_checklist_panes_that_walk_back_to_one_holder_move_it(world):
    # Step 2: each pane's predecessor is first resolved by the walk-back,
    # then the holders compared; when every pane resolves to the same holder, it is the thread left, as for one.
    # Pane %2 showed T4 (holding nothing, never prompted) after alpha's T1.
    world.iam(T1, "alpha")
    two_panes(world, T1, T1)
    world.title("%2", f"Codex | {T4}")
    world.verb(T4, "whoami")
    world.title("%2", f"Codex | {T2}")
    assert world.start(T2, "clear") == fresh_text("alpha", T1, f"codex resume {T1}")
    assert world.holder("alpha") == f"codex:{T1}"


def test_checklist_a_destination_holding_a_handle_is_never_displaced(world):
    # Through step 2: T already holds a handle: nothing moves and nothing more is told.
    world.iam(T1, "alpha")
    world.iam(T2, "beta")
    replaced_in(world, T1)
    assert world.start(T2, "clear") == ""
    assert world.holder("alpha") == f"codex:{T1}"


def test_checklist_a_fresh_start_without_the_lock_names_no_generated_name(world):
    # Codex step 2: the holder identified, the global lock not had in time: the fresh-session text without a
    # generated name; the start record is still written.
    world.iam(T1, "alpha")
    replaced_in(world, T1)
    with world.store.locked():
        text = world.start(T2, "clear")
    assert text == fresh_text("alpha", T1, f"codex resume {T1}", named=False)
    assert world.holder("alpha") == f"codex:{T1}"
    assert [r["source"] for r in world.lines("sessions/starts.jsonl") if r["session_id"] == T2] == ["clear"]


def test_checklist_a_start_out_of_time_while_reading_panes_still_gives_the_session_line(world):
    # Codex step 2: the hook runs out of time while it reads the processes of
    # the panes showing T, before any holder is identified: nothing moves, a logged line, and the session
    # line is still given, with nothing else ("Couldn't follow": with no holder identified, nothing is said).
    # The pane listing is slow, pane %1's `ps` never answers, so the deadline passes before pane %2's is read.
    world.pane("%1", f"Codex | {T2}")
    world.pane("%2", f"Codex | {T2}", tty="ttys002", pane_pid=101)
    world.daemon.loaded.remove(T2)
    world.verb(T1, "whoami")  # pane %1's latest line, as the late hook will see it: no process, no loaded thread
    world.daemon.loaded.append(T2)
    world.codex(tty="ttys002", pid=300, ppid=101)
    (world.dir / "ps-ttys001.hangs").write_text("")
    (world.dir / "tmux-slow").write_text("1.5")
    assert world.start(T2, "clear") == ""
    assert [r["source"] for r in world.lines("sessions/starts.jsonl") if r["session_id"] == T2] == ["clear"]
    assert f"start codex:{T2}: couldn't follow" in world.log()
    assert "hook failed" not in world.log()


# ----- step 3, `resume` and `compact` -------------------------------------------------

@pytest.mark.parametrize("source", ["resume", "compact"])
def test_checklist_resume_and_compact_move_nothing(world, source):
    # Step 3: no move; a resumed thread keeps its own handle, and the one left keeps its own.
    world.iam(T1, "alpha")
    replaced_in(world, T1)
    assert world.start(T2, source) == ""
    assert world.holder("alpha") == f"codex:{T1}"


# ----- step 4, suggestions ------------------------------------------------------

def test_checklist_clear_suggests_only_a_handle_with_no_live_session(world):
    # First trigger: on `clear`, active handles whose session isn't live and was last in the same
    # directory; a loaded thread is live, so it isn't suggested.
    world.prompt(T1)
    world.iam(T1, "alpha")
    titled(world, T3, T4, T5)
    assert world.start(T3, "clear") == ""
    world.daemon.loaded.remove(T1)
    assert "This conversation began with a /clear. alpha's session was in this directory" in world.start(T4, "clear")
    assert world.start(T5, "clear", "/elsewhere") == ""


def test_checklist_suggests_the_handle_the_tmux_session_is_named_after(world):
    # Second trigger: on any source but `fork`, the active handle whose name equals the tmux session's,
    # if it has no live session; Codex reads the tmux session from the pane whose title shows T.
    world.iam(T1, "alpha")
    world.pane("%1", f"Codex | {T2}", session="alpha")
    world.codex()
    assert world.start(T2, "startup", "/elsewhere") == ""  # alpha's thread is loaded: live
    world.daemon.loaded.remove(T1)
    for source in ("startup", "resume"):
        assert world.start(T2, source, "/elsewhere") == tmux_name_suggestion("alpha")


@pytest.mark.parametrize("answer,suggested", [("daemon_not_found", True), ("protocol_unrecognised", False),
                                              ("timeout", False), ("sandboxed", False)])
def test_checklist_a_codex_candidate_is_live_unless_known_not_to_be(world, tmp_path, socket_dir, answer, suggested):
    # Live, for a Codex candidate: not live only for `daemon_not_found`
    # and `thread_not_loaded`; a protocol not recognised, a timeout, a sandbox or any other failure counts
    # as live, so nothing is suggested. Both triggers: on `clear`, and in a tmux session named alpha.
    world.prompt(T1)
    world.iam(T1, "alpha")
    world.pane("%1", f"Codex | {T3}", session="alpha")
    world.codex()
    if answer != "timeout":
        titled(world, T4)
    if answer == "daemon_not_found":
        fake_codex(tmp_path / "bin", os.path.join(socket_dir, "d.sock"), exit_code=1)
    elif answer == "protocol_unrecognised":
        world.daemon.initialize = "error"
    elif answer == "timeout":
        world.daemon.delays["thread/loaded/list"] = 4
    else:
        os.chmod(os.path.join(socket_dir, "d.sock"), 0)
    told = world.start(T3, "resume", "/elsewhere")
    assert told == (tmux_name_suggestion("alpha") if suggested else "")
    if answer != "timeout":  # a timeout spends the start hook's 3 s: the tmux-name case alone
        cleared = world.start(T4, "clear")
        assert ("This conversation began with a /clear. alpha's session was in this directory" in cleared) is suggested


# ----- naming --------------------------------------------------------------------------------

def test_checklist_iam_names_the_thread(world):
    # `iam` sets the thread's name with `thread/name/set`, always, and writes the `named` event.
    world.daemon.threads[T1] = {"name": "something else"}
    world.iam(T1, "alpha")
    assert world.daemon.names[T1] == "alpha"
    assert [(e["kind"], e["name"]) for e in world.events("named")] == [("handle", "alpha")]


def test_checklist_a_start_hook_never_runs_the_pending_name_catch_up(world, tmp_path, socket_dir):
    # The catch-up never runs from a hook, the start hook included,
    # even one that reached the daemon; it runs at the next call that does.
    titled(world, T1)
    world.start(T1, "startup")  # so `iam` can place T1 without the daemon, from its start record
    fake_codex(tmp_path / "bin", os.path.join(socket_dir, "d.sock"), exit_code=1)
    code, result = world.verb(T1, "iam", "alpha")
    assert code == 0 and any("The Codex thread name wasn't set" in w for w in result["warnings"])
    assert world.events("named") == []
    fake_codex(tmp_path / "bin", os.path.join(socket_dir, "d.sock"))
    world.pane("%1", f"Codex | {T3}", session="alpha")
    world.codex()
    assert world.start(T3, "resume") == ""  # the tmux-name check asked the daemon whether alpha's thread is live: it is
    assert any(r.get("method") == "thread/loaded/list" for r in world.daemon.requests)
    assert T1 not in world.daemon.names and world.events("named") == []
    world.verb(T3, "whoami")
    assert world.daemon.names[T1] == "alpha"


def offer(store, key, handle, holder):
    """The start hook's offer of `handle` to session `key`, so `iam` may take it from a live holder."""
    from kaiusap.model import PERSONAS
    from kaiusap.store import now
    store.append(PERSONAS, {"at": now(), "session_key": key, "offered": handle, "from": holder})


def test_checklist_a_clear_after_a_thread_with_no_handle_says_nothing_and_continues_nothing(world):
    # A conversation that holds no handle is never followed: a /clear after it says nothing and writes no
    # persona line.
    replaced_in(world, T1)  # T1 held no handle
    assert world.start(T2, "clear") == ""
    assert [row for row in world.store.rows(PERSONAS) if row.get("continues")] == []
