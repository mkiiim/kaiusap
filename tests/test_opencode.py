"""OpenCode's behaviour checklist: scenarios from the OpenCode adapter's rules,
each through the public surface only: `kaiusap hook opencode <event>` with the plugin's inputs, the verbs
(CLI and MCP), the store's records and the hook's output."""
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading

import pytest

from kaiusap.adapters import SPAWNED_AGENT_TEXT
from kaiusap.mcp import call
from kaiusap.notice import SESSION_LINE, notice
from kaiusap.store import Store

ROOT = "ses_aaaaaaaaaaaaaaaaaaaaaaaaaaaa"
NEXT = "ses_bbbbbbbbbbbbbbbbbbbbbbbbbbbb"
CHILD = "ses_cccccccccccccccccccccccccccc"
OTHER_PID = 4343
DEFAULT_TITLE = "New session - 2026-11-10T16:23:58.123Z"


def lstart(delta):
    return (datetime.now().astimezone() + timedelta(seconds=delta)).strftime("%a %b %d %H:%M:%S %Y")


class Plugin:
    """A fake of the plugin's socket: one request line per connection, one answer line. `touched` are
    the sessions this process has seen; `refuse` makes a notice answer `refused`."""

    def __init__(self, path, touched, title=None):
        self.path, self.touched, self.title, self.refuse = path, set(touched), title, None
        self.requests = []
        Path(path).unlink(missing_ok=True)
        self.server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.server.bind(path)
        self.server.listen()
        threading.Thread(target=self.serve, daemon=True).start()

    def answer(self, request):
        if request.get("session_id") not in self.touched:
            return {"ok": False, "code": "not_live"}
        if request["type"] == "notice" and self.refuse:
            return {"ok": False, "code": "refused", "message": self.refuse}
        if request["type"] == "rename":
            self.title = request["name"]
        if request["type"] == "session":
            return {"ok": True, "root": ROOT if request["session_id"] == CHILD else request["session_id"],
                    "parent_id": ROOT if request["session_id"] == CHILD else None, "title": self.title}
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
                self.requests.append(request)
                connection.sendall((json.dumps(self.answer(request)) + "\n").encode())

    def close(self):
        self.server.close()
        Path(self.path).unlink(missing_ok=True)


class Machine:
    """One `opencode` process (pid `pid`, started 60 s ago) with its plugin's socket, and a fake `ps` on PATH.
    The process is this test's parent, so an MCP call made here has it as its nearest ancestor."""

    def __init__(self, tmp_path):
        self.home = Path(os.environ["KAIUSAP_HOME"])
        self.bin = Path(os.environ["PATH"].split(os.pathsep)[0])
        self.store = Store(self.home)
        self.pid = os.getppid()
        self.started = lstart(-60)
        self.dir = tempfile.mkdtemp(prefix="kai-oc-")
        self.socket = os.path.join(self.dir, f"{self.pid}-0a1b2c3d.sock")
        self.plugin = None
        self.alive = {self.pid: self.started, OTHER_PID: lstart(-30)}
        self.write_ps()

    def write_ps(self):
        cases = "".join(f'  {pid}) echo "{started}" ;;\n' for pid, started in self.alive.items())
        script = f'''#!/bin/sh
last=""; for a in "$@"; do last="$a"; done
case "$*" in
  "-A -o pid=,ppid=") echo "{self.pid} 1"; echo "1 0"; exit 0 ;;
  *ppid=*) echo 1; exit 0 ;;
esac
case "$last" in
{cases}  *) exit 1 ;;
esac
'''
        path = self.bin / "ps"
        path.write_text(script)
        path.chmod(0o755)

    def serve(self, touched, title=None):
        self.plugin = Plugin(self.socket, touched, title)
        return self.plugin

    def hook(self, event, session=ROOT, agent=None, **extra):
        payload = dict(session_id=session, cwd="/work", pid=self.pid, socket=self.socket, tmux_pane=None, title=DEFAULT_TITLE)
        if agent:
            payload["agent_id"] = agent
        if event == "session.start":
            payload |= dict(parent_id=session if agent else None, created_here=True, roots_started_here=0, prompted_roots=[])
        if event == "chat.message":
            payload |= dict(prompt="Hello")
        payload |= extra
        result = subprocess.run([sys.executable, "-m", "kaiusap", "hook", "opencode", event], input=json.dumps(payload),
                                text=True, capture_output=True, timeout=10, env=os.environ)
        assert result.returncode == 0 and result.stderr == "", result.stderr
        return json.loads(result.stdout) if result.stdout.strip() else None

    def cli(self, *argv, session=ROOT, pid=None):
        env = {**os.environ, "KAIUSAP_OPENCODE_SESSION": session, "OPENCODE_PID": str(pid or self.pid), "PWD": "/work"}
        result = subprocess.run([sys.executable, "-m", "kaiusap", *argv, "--json"], text=True, capture_output=True,
                                timeout=30, env=env)
        return result.returncode, json.loads(result.stdout or result.stderr or "null")

    def starts(self):
        return [r for r in self.store.rows("sessions/starts.jsonl")]

    def prompts(self):
        return [r for r in self.store.rows("sessions/prompts.jsonl")]

    def close(self):
        if self.plugin:
            self.plugin.close()


@pytest.fixture
def machine(tmp_path):
    made = Machine(tmp_path)
    yield made
    made.close()


def told(output):
    return ((output or {}).get("hookSpecificOutput") or {}).get("tell")


# ----- the plugin's hooks ------------------------------------------------------------------------


def test_a_first_prompt_records_the_start_and_gives_the_session_line(machine):
    output = machine.hook("session.start")
    assert told(output).startswith(SESSION_LINE)
    [record] = machine.starts()
    assert list(record) == ["at", "provider", "session_id", "session_key", "source", "cwd", "pid", "process_started_at",
                            "socket", "tmux_pane"]
    assert (record["provider"], record["session_key"], record["source"], record["pid"], record["socket"]) == (
        "opencode", f"opencode:{ROOT}", "startup", machine.pid, machine.socket)
    assert record["process_started_at"].endswith("Z")


@pytest.mark.parametrize("facts,source", [
    ({"created_here": False}, "resume"),
    ({"roots_started_here": 2}, "clear"),
    ({"title": "alpha (fork #3)"}, "fork"),
    ({}, "startup"),
])
def test_the_adapter_derives_the_source_from_the_plugins_facts(machine, facts, source):
    machine.hook("session.start", **facts)
    assert [r["source"] for r in machine.starts()] == [source]


def test_a_prompt_writes_a_prompt_record_with_the_pid_and_a_notice_writes_none(machine):
    machine.hook("session.start")
    machine.hook("chat.message")
    [record] = machine.prompts()
    assert list(record) == ["at", "provider", "session_id", "session_key", "source", "cwd", "pid"]
    assert (record["source"], record["pid"], record["cwd"]) == ("prompt", machine.pid, "/work")
    message = {"id": "20261110T120000Z-abcdef", "in_reply_to": None, "reply_needed": False, "subject": "S",
               "from": {"handle": "beta", "provider": "claude"}}
    machine.hook("chat.message", prompt=notice(message))
    assert len(machine.prompts()) == 1


def test_a_child_session_records_a_subagent_start_says_nothing_and_writes_no_prompt(machine):
    assert machine.hook("session.start", agent=CHILD) is None
    [record] = machine.starts()
    assert list(record) == ["at", "provider", "session_id", "session_key", "source", "agent_id", "cwd", "pid",
                            "process_started_at", "socket", "tmux_pane"]
    assert (record["source"], record["session_id"], record["agent_id"], record["session_key"]) == (
        "subagent", ROOT, CHILD, f"opencode:{ROOT}/{CHILD}")
    assert machine.hook("chat.message", agent=CHILD) is None
    assert machine.prompts() == []


@pytest.mark.parametrize("tool,args", [("kaiusap_send", {"to": "beta"}), ("kaiusap_iam", {"handle": "x"}),
                                       ("bash", {"command": "kaiusap reply abc --body hi"})])
def test_the_screen_refuses_a_childs_acting_call_with_the_spawned_agent_text(machine, tool, args):
    assert machine.hook("tool.execute.before", agent=CHILD, tool=tool, args=args) == {"hookSpecificOutput": {"deny": SPAWNED_AGENT_TEXT}}


@pytest.mark.parametrize("agent,tool,args", [(CHILD, "kaiusap_whoami", {}), (CHILD, "bash", {"command": "ls"}),
                                             (None, "kaiusap_send", {"to": "beta"}),
                                             (CHILD, "bash", {"command": "kaiusap inbox"})])
def test_the_screen_lets_looking_calls_and_root_callers_through(machine, agent, tool, args):
    output = machine.hook("tool.execute.before", agent=agent, tool=tool, args=args)
    assert not (output or {}).get("hookSpecificOutput", {}).get("deny")


def test_unknown_input_keys_are_not_the_plugins(machine):
    assert machine.hook("session.start", surprise=1) is None
    assert machine.starts() == []


# ----- identity: the shell and the identity argument ----------------------------------------------


def test_the_shell_is_placed_by_a_start_record_of_its_process(machine):
    machine.hook("session.start")
    code, result = machine.cli("whoami")
    assert code == 0 and result["session_key"] == f"opencode:{ROOT}" and result["classification"] == "root"


def test_a_shell_whose_process_recorded_nothing_cannot_act(machine):
    machine.hook("session.start")
    code, result = machine.cli("iam", "alpha", pid=OTHER_PID)
    assert code == 1 and "can't tell whether this OpenCode session is a subagent" in result["error"]


def test_a_child_shell_is_spawned_and_refused_for_acting_verbs(machine):
    machine.hook("session.start")
    machine.hook("session.start", agent=CHILD)
    code, result = machine.cli("whoami", session=CHILD)
    assert code == 0 and result["spawned"] is True and result["session_key"] == f"opencode:{ROOT}/{CHILD}"
    code, result = machine.cli("iam", "alpha", session=CHILD)
    assert code == 1 and SPAWNED_AGENT_TEXT in result["error"]


def test_an_mcp_call_is_identified_by_the_stamp_the_plugin_wrote(machine):
    machine.hook("session.start")
    _, result, failed = call("opencode", "whoami", {"_kaiusap_session": ROOT}, {})
    assert not failed and result["session_key"] == f"opencode:{ROOT}"


@pytest.mark.parametrize("arguments", [{}, {"_kaiusap_session": ""}, {"_kaiusap_session": 5}, {"_kaiusap_session": NEXT}])
def test_an_mcp_call_without_a_stamp_this_process_recorded_is_unidentified(machine, arguments):
    machine.hook("session.start")
    text, result, failed = call("opencode", "whoami", arguments, {})
    assert failed and "carried no _kaiusap_session kaiusap could match to this OpenCode process" in text
    assert text.endswith("Tell the user; don't read kaiusap's files unless they ask.")


def test_a_root_the_plugin_knows_but_no_start_record_names_is_not_identified(machine):
    # The socket alone never places a root (a start hook that timed out writes no record).
    machine.hook("session.start", session=NEXT)
    machine.serve({ROOT, NEXT})
    text, _, failed = call("opencode", "send", {"_kaiusap_session": ROOT, "to": "beta", "subject": "s",
                                                 "reply_needed": False, "context": "c"}, {})
    assert failed and "carried no _kaiusap_session kaiusap could match" in text


def test_a_child_without_its_own_record_is_placed_through_the_socket_as_spawned(machine):
    machine.hook("session.start")
    machine.serve({ROOT, CHILD})
    _, result, failed = call("opencode", "whoami", {"_kaiusap_session": CHILD}, {})
    assert not failed and result["spawned"] is True and result["session_key"] == f"opencode:{ROOT}/{CHILD}"


def test_a_start_whose_child_facts_disagree_writes_nothing(machine):
    assert machine.hook("session.start", agent=CHILD, parent_id=None) is None
    assert machine.hook("session.start", parent_id=ROOT) is None
    assert machine.starts() == []


def test_the_identity_argument_is_removed_for_every_provider_before_the_schema_check():
    # No schema lists it, and an adapter that doesn't use it ignores it.
    text, _, failed = call("codex", "help", {"_kaiusap_session": "x"}, {})
    assert not failed and "Arguments not allowed" not in text


# ----- delivery, names -----------------------------------------------------------------------


def take(machine, handle="alpha", session=ROOT, pid=None):
    code, result = machine.cli("iam", handle, session=session, pid=pid)
    assert code == 0, result
    return result


def send(machine, to="alpha"):
    machine.hook("session.start", session=NEXT, roots_started_here=1)
    take(machine, "beta", NEXT)
    code, result = machine.cli("send", "--to", to, "--subject", "hello", "--reply-needed", "no", "--context", "body", session=NEXT)
    assert code == 0, result
    return result


def test_iam_renames_the_session_at_once_over_the_socket(machine):
    plugin = machine.serve({ROOT, NEXT})
    machine.hook("session.start")
    take(machine)
    assert {"type": "rename", "session_id": ROOT, "name": "alpha"} in plugin.requests
    named = [e for e in machine.store.events() if e["event"] == "named"]
    assert [(e["kind"], e["name"]) for e in named] == [("handle", "alpha")]


def test_a_handle_taken_with_the_socket_down_is_named_by_the_next_prompt(machine):
    machine.hook("session.start", socket=None)
    take(machine)
    output = machine.hook("chat.message")
    assert output["hookSpecificOutput"]["title"] == "alpha"


def test_a_send_wakes_a_live_session_through_its_plugin(machine):
    plugin = machine.serve({ROOT, NEXT})
    machine.hook("session.start")
    take(machine)
    result = send(machine)
    assert result["outcome"] == "woken", result
    [line] = [r for r in plugin.requests if r["type"] == "notice"]
    assert line["session_id"] == ROOT and line["text"].startswith("kaiusap: message from")


def test_a_plugin_that_never_touched_the_session_stores_the_message(machine):
    machine.serve({NEXT})  # the plugin answers not_live for ROOT
    machine.hook("session.start")
    take(machine)
    result = send(machine)
    assert (result["outcome"], result["reason_code"]) == ("stored", "not_live") and result["paste_line"]


def test_a_refused_prompt_is_stored_with_opencodes_message(machine):
    plugin = machine.serve({ROOT, NEXT})
    plugin.refuse = "session is busy"
    machine.hook("session.start")
    take(machine)
    result = send(machine)
    assert (result["outcome"], result["reason_code"]) == ("stored", "provider_refused")
    assert result["reason"] == "OpenCode refused the prompt: session is busy"


def test_a_session_whose_process_exited_is_not_live(machine):
    machine.hook("session.start", pid=OTHER_PID)
    machine.alive.pop(OTHER_PID)
    machine.write_ps()
    code, result = machine.cli("who")
    assert code == 0
    code, result = machine.cli("sessions")
    assert code == 0


# ----- /new follows the session last typed in -------------------------------------------


def test_new_moves_the_handle_from_the_session_last_prompted_in_this_process(machine):
    machine.serve({ROOT, NEXT})
    machine.hook("session.start")
    take(machine)
    machine.hook("chat.message")
    output = machine.hook("session.start", session=NEXT, roots_started_here=1,
                          prompted_roots=[{"session_id": ROOT, "at": "2026-11-10T00:00:00.000Z"}])
    assert f"You are alpha; the handle followed this conversation from session {ROOT}, which it replaced in this terminal." in told(output)
    line = list(machine.store.rows("handles.jsonl"))[-1]
    assert (line["handle"], line["reason"], line["session_key"]) == ("alpha", "fresh-start", f"opencode:{NEXT}")


def test_new_without_a_prompt_of_this_process_moves_nothing(machine):
    machine.hook("session.start")
    take(machine)
    output = machine.hook("session.start", session=NEXT, roots_started_here=1,
                          prompted_roots=[{"session_id": ROOT, "at": "2026-11-10T00:00:00.000Z"}])
    assert "You are alpha" not in (told(output) or "")
    assert list(machine.store.rows("handles.jsonl"))[-1]["session_key"] == f"opencode:{ROOT}"


def test_new_leaves_a_handle_whose_session_another_running_process_opened_later(machine):
    machine.hook("session.start")
    take(machine)
    machine.hook("chat.message")
    machine.hook("session.start", created_here=False, pid=OTHER_PID)  # resumed in another running process
    output = machine.hook("session.start", session=NEXT, roots_started_here=1,
                          prompted_roots=[{"session_id": ROOT, "at": "2026-11-10T00:00:00.000Z"}])
    assert "You are alpha" not in (told(output) or "")
    assert list(machine.store.rows("handles.jsonl"))[-1]["session_key"] == f"opencode:{ROOT}"
    assert told(output).startswith(SESSION_LINE)  # no move, then step 4: nothing else to say here


# ----- a fork ------------------------------------------------------------------------------


def test_a_fork_of_a_session_holding_a_handle_is_told_it_is_a_copy(machine):
    machine.hook("session.start")
    take(machine)
    output = machine.hook("session.start", session=NEXT, title="alpha (fork #1)", roots_started_here=1)
    assert "This conversation is a copy of the one holding alpha, which keeps it; you have no handle." in told(output)


def test_a_fork_of_anything_else_is_told_it_has_no_handle(machine):
    output = machine.hook("session.start", title="some work (fork #2)")
    assert "This conversation has no handle; if it is a copy of another one, the original keeps its handle." in told(output)


# ----- sessions -----------------------------------------------------------------------------------


def test_sessions_gives_a_resume_command_for_a_handle_not_running(machine):
    machine.hook("session.start", pid=OTHER_PID)
    take(machine, pid=OTHER_PID)
    machine.alive.pop(OTHER_PID)
    machine.write_ps()
    code, result = machine.cli("sessions")
    [row] = [r for r in result["sessions"] if r["session_key"] == f"opencode:{ROOT}"]
    assert (row["where"], row["command"]) == ("not_running", f"cd /work && opencode -s {ROOT}")


def test_sessions_shows_a_live_session_outside_tmux_with_the_copy_warning(machine):
    machine.serve({ROOT}, title="alpha")
    machine.hook("session.start")
    code, result = machine.cli("sessions")
    [row] = [r for r in result["sessions"] if r["session_key"] == f"opencode:{ROOT}"]
    assert (row["where"], row["name"], row["command"]) == ("not_in_tmux", "alpha", f"cd /work && opencode -s {ROOT}")
    assert row["note"] == "Still open in another OpenCode process: this opens it in a second one."


def test_sessions_says_unknown_when_the_process_runs_without_its_socket(machine):
    machine.hook("session.start")
    take(machine)
    code, result = machine.cli("sessions")
    [row] = [r for r in result["sessions"] if r["session_key"] == f"opencode:{ROOT}"]
    assert (row["where"], row["command"]) == ("unknown", None)


# ----- sessions: the makers ------------------------------------------------------------------------


def test_sessions_rows_name_the_harness_maker(machine):
    machine.hook("session.start", pid=OTHER_PID)
    take(machine, pid=OTHER_PID)
    code, result = machine.cli("sessions")
    [row] = [r for r in result["sessions"] if r["session_key"] == f"opencode:{ROOT}"]
    assert row["harness_maker"] == "opencode" and row["context"] is None


def test_new_with_no_handle_still_continues_the_persona(machine):
    # The predecessor holds no handle, but /new still continues its persona.
    machine.hook("session.start")
    machine.hook("chat.message")
    machine.hook("session.start", session=NEXT, roots_started_here=1,
                 prompted_roots=[{"session_id": ROOT, "at": "2026-11-10T00:00:00.000Z"}])
    from kaiusap.model import PERSONAS
    lines = list(machine.store.rows(PERSONAS))
    assert [(l["session_key"], l["continues"], l["via"]) for l in lines] == [
        (f"opencode:{NEXT}", f"opencode:{ROOT}", "fresh-start")]
