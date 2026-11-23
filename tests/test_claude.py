"""Step-3 fixtures: no live provider, process, tmux, transcript or inbox access."""
from datetime import datetime
import json
from pathlib import Path
import re
import socket
import subprocess
import sys
import tempfile
import threading
import time

import pytest

from conftest import clear_suggestion, tmux_name_suggestion

from fake_tmux import FakeTmux
from kaiusap.adapters import Identity, Providers
from kaiusap.adapters.claude import (ClaudeAdapter, COPY, LISTING_ERROR, NO_RECORD,
                                    REASONS, bash_verbs, teammate)
from kaiusap.cli import main
from kaiusap.core import Core
from kaiusap.errors import Error
from kaiusap.following import follow, alive
from kaiusap.model import binding_history, handles, retirement_name
from kaiusap.notice import notice
from kaiusap.store import instant, now

START = '2026-10-01T00:00:00Z'
STAMP = datetime.fromisoformat(START.replace('Z', '+00:00')).astimezone().strftime('%a %b %d %H:%M:%S %Y')


def localised(stamp, env):
    """`ps`'s `lstart` as a day-first locale prints it (en_CA: "Thu  5 Nov ..."), unless LC_TIME is C."""
    if env is not None and env.get("LC_TIME") == "C" and "LC_ALL" not in env:
        return stamp
    return datetime.strptime(" ".join(stamp.split()), "%a %b %d %H:%M:%S %Y").strftime("%a %e %b %H:%M:%S %Y")


class Runner:
    def __init__(self, listing=None):
        self.rows = [] if listing is None else listing
        self.processes = {10: (STAMP, '/fixture/claude'), 20: (STAMP, '/fixture/claude --agent-id=t')}
        self.parents = {100: 10, 10: 1, 1: 0}
        self.commands = {}  # pid -> program path, for `ps -A -o pid=,ppid=,comm=`
        self.panes = ''
        self.display = '7 42 work'
        self.calls = []
        self.error = None
        self.tmux = None  # a FakeTmux takes every tmux call when set

    def __call__(self, args, limit, env):
        assert 0 < limit <= 3
        self.calls.append((args, limit, env))
        if args[0] == 'tmux' and self.tmux is not None:
            return self.tmux(args, limit, env)
        if args[0] == 'claude':
            if self.error:
                raise self.error
            return (0, json.dumps(self.rows))
        if args[:2] == ['ps', '-A'] and 'comm=' in args[3]:  # the host app's process tree, with program paths
            return 0, ''.join(f'{pid} {ppid} {self.commands.get(pid, "/bin/sh")}\n' for pid, ppid in self.parents.items())
        if args[:2] == ['ps', '-A']:  # the pane check's process tree
            return 0, ''.join(f'{pid} {ppid}\n' for pid, ppid in self.parents.items())
        if args[0] == 'ps':
            pid = int(args[-1])
            if args[2] == 'ppid=':
                return 0, str(self.parents.get(pid, 0))
            process = self.processes.get(pid)
            return (0, ' '.join((localised(process[0], env), *process[1:]))) if process else (1, '')
        assert args[:3] == ['tmux', '-L', 'default']
        return 0, self.display if args[3] == 'display-message' else self.panes


class Clock:
    def __init__(self):
        self.value = 0
        self.on_sleep = None

    def __call__(self):
        return self.value

    def sleep(self, seconds):
        self.value += seconds
        if self.on_sleep:
            self.on_sleep()


@pytest.fixture
def rig(store):
    run, clock = Runner(), Clock()
    adapter = ClaudeAdapter(store, run=run, environ={}, clock=clock, sleep=clock.sleep)
    return adapter, run, clock


def start(store, sid='old', pid=10, source='startup', at='2026-11-01T00:00:00Z', **extra):
    row = dict(at=at, provider='claude', session_id=sid, session_key='claude:' + sid, source=source,
               cwd='/fixture', pid=pid, process_started_at=START, socket=None, teammate=False,
               tmux_pane=None, transcript=None)
    row.update(extra)
    ClaudeAdapter(store).record({'record': row})  # the start record, and with it the transcript index
    return row


def bind(store, adapter, sid='old', handle='alpha', pid=10):
    return Core(store, Identity('claude', sid, 'claude:' + sid, '/fixture', 'root', pid=pid,
                               process_started_at=START), adapter).run('iam', {'handle': handle})


def event(store, sid='new', kind='clear', pid=10, **extra):
    row = start(store, sid, pid, kind, at='2026-11-01T00:00:02Z', **extra)
    return {**row, 'kind': kind, 'record': row, 'entered': 0}


def test_registry_and_shell_identity(rig, store):
    adapter, run, _ = rig
    assert isinstance(Providers.default(store).named('claude'), ClaudeAdapter)
    identity = adapter.identity({'CLAUDE_CODE_SESSION_ID': 'current', 'CLAUDE_PID': '10', 'PWD': '/cwd'}, store)
    assert (identity.session_key, identity.pid, identity.process_started_at, identity.cwd) == ('claude:current', 10, START, '/cwd')
    for pid in ('', 'no', '-1', '0'):
        assert adapter.identity({'CLAUDE_CODE_SESSION_ID': 's', 'CLAUDE_PID': pid}, store).classification == 'plain_shell'
    # The session's variable with a process this shell can't see is a sandboxed shell (Linux's sandbox).
    hidden = adapter.identity({'CLAUDE_CODE_SESSION_ID': 's', 'CLAUDE_PID': '99', 'PWD': '/cwd'}, store)
    assert (hidden.classification, hidden.session_key, hidden.pid) == ('sandboxed', 'claude:s', 99)
    assert adapter.identity({'CLAUDE_PID': '10'}, store).classification == 'plain_shell'
    with pytest.raises(Error, match='Both CODEX_THREAD_ID'):
        Providers({'claude': adapter}, store).identity({'CODEX_THREAD_ID': 'x', 'CLAUDE_CODE_SESSION_ID': 'y'}, store)
    assert all(args[0] != 'claude' for args, _, _ in run.calls)


@pytest.mark.parametrize('agent,classification,key', [(None, 'root', 'claude:s'), ('worker', 'spawned', 'claude:s/worker')])
def test_hook_identity(rig, agent, classification, key):
    adapter, _, _ = rig
    identity = adapter.hook_identity({'session_id': 's', 'agent_id': agent, 'agent_type': 'researcher'})
    assert (identity.classification, identity.session_key) == (classification, key)
    assert adapter.hook_identity({}) is None


def test_mcp_ancestor_current_listing_and_teammate(rig, store):
    adapter, run, _ = rig
    start(store)
    run.rows = [{'pid': 10, 'sessionId': 'old'}]
    identity = adapter.mcp_identity({'sessionId': 'stale'}, parent_pid=100)
    assert identity.session_key == 'claude:old' and identity.pid == 10
    # Core's call reset must retain the per-MCP-call listing, then finish discards it.
    adapter.reset()
    adapter.liveness(identity.session_key)
    assert len([c for c in run.calls if c[0][0] == 'claude']) == 1
    adapter.finish()
    start(store, 'new', at='2026-11-01T00:00:02Z')
    run.rows = [{'pid': 10, 'sessionId': 'new'}]
    assert adapter.mcp_identity(parent_pid=100).session_key == 'claude:new'
    run.rows = [{'pid': 10, 'sessionId': 'old'}]
    with pytest.raises(Error, match='no current start record'):
        adapter.mcp_identity(parent_pid=100)
    start(store, 'team', 20, teammate=True)
    run.parents = {100: 20, 20: 1, 1: 0}
    run.rows = []
    assert adapter.mcp_identity(parent_pid=100).session_key == 'claude:team'
    run.rows = [{'pid': 20, 'sessionId': 'different'}]
    with pytest.raises(Error, match='no current start record'):
        adapter.mcp_identity(parent_pid=100)


@pytest.mark.parametrize('kind', ['failed', 'missing', 'background', 'none', 'too_far', 'reused'])
def test_mcp_unidentified_sentences(rig, store, kind):
    adapter, run, _ = rig
    expected = NO_RECORD
    if kind == 'failed':
        run.rows = {}
        expected = LISTING_ERROR
    elif kind in {'missing', 'background'}:
        row = {'pid': 10, 'sessionId': 'abc'}
        if kind == 'background':
            row.update(id='job12345', kind='background')
        run.rows = [row]
        expected = ("kaiusap can't tell which session is calling: this session started before kaiusap's start hook ran; "
                    "quit and run claude --resume abc so it runs (for a background session, claude stop " +
                    ('job12345' if kind == 'background' else '<job>') + " first), and check kaiusap setup."
                    " Tell the user; don't read kaiusap's files unless they ask.")
    elif kind == 'too_far':
        run.parents = {100: 99, 99: 98, 98: 10}
        start(store)
        run.rows = [{'pid': 10, 'sessionId': 'old'}]
    elif kind == 'reused':
        start(store, process_started_at='2026-09-01T00:00:00Z')
    with pytest.raises(Error) as exc:
        adapter.mcp_identity(parent_pid=100)
    assert str(exc.value) == expected


@pytest.mark.parametrize('args,result', [('/x/claude --agent-id=a', True), ('claude --agent-id a', True),
                                       ('claude --agent identity', False), ('claude -- --agent-id x', False),
                                       ('claude --agent-idle', False)])
def test_teammate_mark(args, result):
    assert teammate(args) is result


def test_start_event_and_prompt_index(rig, store):
    adapter, run, _ = rig
    adapter.environ = {'CLAUDE_PID': '20', 'CLAUDE_CODE_MESSAGING_SOCKET': '/fixture/socket',
                       'TMUX': '/fixture/default,7,0', 'TMUX_PANE': '%2'}
    payload = {'hook_event_name': 'SessionStart', 'source': 'startup', 'session_id': 's',
               'cwd': '/fixture', 'transcript_path': '/fixture/one'}
    item = adapter.events(payload)
    assert item['record'] == {k: item[k] for k in item['record']}
    assert (item['teammate'], item['tmux_pane'], item['tmux_session'], item['process_started_at']) == (True, '7@42:%2', 'work', START)
    assert [c[0][0] for c in run.calls] == ['ps', 'tmux']
    adapter.record(item)
    assert ClaudeAdapter(store).index('s')['transcript'] == '/fixture/one'
    prompt = {**payload, 'hook_event_name': 'UserPromptSubmit', 'cwd': '/bash-only'}
    assert adapter.events(prompt)['record'] is None
    prompt.update(transcript_path='/fixture/two', cwd='/moved')
    changed = adapter.events(prompt)
    assert changed['record']['source'] == 'cwd' and 'pid' not in changed['record']
    adapter.record(changed)
    assert ClaudeAdapter(store).index('s')['cwd'] == '/moved'
    prompt['transcript_path'] = ''
    assert adapter.events(prompt)['record']['transcript'] is None
    assert adapter.events({**payload, 'agent_id': 'a'}) is None


def test_start_record_names_where_the_session_was_started(rig, store):
    """The desktop app's environment gives the entrypoint, its own id for the session and its version; a
    terminal gives "cli" and nothing else; empty values are null."""
    adapter, _, _ = rig
    payload = {'hook_event_name': 'SessionStart', 'source': 'startup', 'session_id': 's', 'cwd': '/fixture'}
    adapter.environ = {'CLAUDE_PID': '20', 'CLAUDE_CODE_ENTRYPOINT': 'claude-desktop',
                       'CLAUDE_CODE_HOST_SESSION_ID': 'local_17f664df-5bd6-46fb-be10-14215af55d36',
                       'CLAUDE_CODE_DESKTOP_APP_VERSION': '5.11492.5'}
    record = adapter.events(payload)['record']
    assert (record['entrypoint'], record['host_session_id'], record['host_version']) == (
        'claude-desktop', 'local_17f664df-5bd6-46fb-be10-14215af55d36', '5.11492.5')
    adapter.record(adapter.events(payload))
    written = store.latest('sessions/starts.jsonl', lambda r: r.get('session_id') == 's')
    assert written['host_session_id'] == 'local_17f664df-5bd6-46fb-be10-14215af55d36'
    adapter.environ = {'CLAUDE_PID': '20', 'CLAUDE_CODE_ENTRYPOINT': 'cli', 'CLAUDE_CODE_HOST_SESSION_ID': ''}
    record = adapter.events(payload)['record']
    assert (record['entrypoint'], record['host_session_id'], record['host_version']) == ('cli', None, None)
    adapter.environ = {'CLAUDE_PID': '20'}
    record = adapter.events(payload)['record']
    assert (record['entrypoint'], record['host_session_id'], record['host_version']) == (None, None, None)


@pytest.mark.parametrize('tmux,display', [('socket,8,0', '7 42 work'), ('bad', '7 42 work'), ('socket,7,0', '')])
def test_start_invalid_tmux_logged(rig, tmux, display):
    adapter, run, _ = rig
    adapter.environ = {'CLAUDE_PID': '10', 'TMUX_PANE': '%1', 'TMUX': tmux}
    run.display = display
    item = adapter.events({'session_id': 's', 'source': 'startup', 'hook_event_name': 'SessionStart'})
    assert item['tmux_pane'] is None
    assert (adapter.store.root / 'hook-errors.log').exists()


def test_prompt_missing_index_is_differs_and_rebuild(rig, store):
    adapter, _, _ = rig
    start(store, transcript='/synthetic/old')
    ClaudeAdapter(store).index_path('old').write_text('bad JSON')
    item = adapter.events({'session_id': 'old', 'hook_event_name': 'UserPromptSubmit', 'transcript_path': '/synthetic/old'})
    assert item['record']['source'] == 'cwd'
    assert ClaudeAdapter(store).rebuild_index('old')['transcript'] == '/synthetic/old'


def test_an_index_at_the_old_path_is_not_read_and_the_prompt_rebuilds_it(rig, store):
    # Batch F (2026-11-09): the index moved to sessions/claude/last/ with no migration, since it is derived:
    # the old file is ignored, the next prompt finds no index, writes its cwd line and with it the new index.
    adapter, _, _ = rig
    start(store, transcript='/synthetic/old')
    old = store.root / 'sessions/last/claude/old.json'
    old.parent.mkdir(parents=True, exist_ok=True)
    old.write_text(json.dumps({'at': '2026-01-01T00:00:00Z', 'cwd': '/synthetic', 'transcript': '/synthetic/old'}))
    ClaudeAdapter(store).index_path('old').unlink()
    item = adapter.events({'session_id': 'old', 'hook_event_name': 'UserPromptSubmit', 'transcript_path': '/synthetic/old',
                           'cwd': '/synthetic', 'prompt': 'hi'})
    assert item['record']['source'] == 'cwd'
    adapter.record(item)
    assert ClaudeAdapter(store).index('old')['transcript'] == '/synthetic/old'
    assert ClaudeAdapter(store).index_path('old') == store.root / 'sessions/claude/last/old.json'
    assert not (store.root / 'hook-errors.log').exists()


@pytest.mark.parametrize('case,code', [('failed', 'listing_failed'), ('none', 'not_live'), ('two', 'two_processes'),
                                      ('no_record', 'no_socket'), ('reused', 'no_socket'), ('no_path', 'no_socket'),
                                      ('valid', None), ('selected', None), ('teammate', None),
                                      ('team_moved', 'not_live'), ('listed_moved', 'not_live'), ('detached', 'detached'),
                                      ('background', None)])
def test_liveness_order(rig, store, tmp_path, case, code):
    adapter, run, _ = rig
    path = tmp_path / 'inbox'
    path.touch()
    record = start(store, socket=str(path), tmux_pane='7@42:%1')
    run.rows = [{'pid': 10, 'sessionId': 'old'}]
    if case == 'failed':
        run.rows = {}
    if case == 'none':
        run.rows = []
    if case in {'two', 'selected'}:
        run.rows.append({'pid': 20, 'sessionId': 'old'})
    if case == 'selected':
        bind(store, adapter)
    if case == 'no_record':
        run.rows[0]['sessionId'] = 'other'
    if case == 'reused':
        run.processes[10] = ('Fri Oct  2 00:00:00 2026', 'claude')
    if case == 'no_path':
        path.unlink()
    if case in {'teammate', 'team_moved', 'listed_moved'}:
        run.rows = []
        start(store, 'old', 20, teammate=True, socket=str(path), at='2026-11-01T00:00:01Z')
        if case == 'team_moved':
            start(store, 'else', 20, teammate=True, at='2026-11-01T00:00:02Z')
        if case == 'listed_moved':
            run.rows = [{'pid': 20, 'sessionId': 'else'}]
    if case in {'detached', 'background'}:
        run.panes = '%1\twork\t0\t7\t42'
        (store.root / 'settings.json').write_text('{"wake_detached": false}')
        if case == 'background':
            run.rows[0]['kind'] = 'background'
    if case == 'no_record':
        key = 'claude:other'
    else:
        key = 'claude:old'
    live = adapter.liveness(key)
    assert live['reason_code'] == code
    assert live['can_be_woken'] is (code is None)
    if code:
        assert live['fix']
    if case == 'two':
        assert not any(args[0] == 'ps' for args, _, _ in run.calls)
    if case == 'background':
        assert not live['detached']


def test_binding_process_selection_does_not_use_old_binding(rig, store, tmp_path):
    adapter, run, _ = rig
    path = tmp_path / 'inbox'
    path.touch()
    start(store, socket=str(path))
    start(store, 'old', 20, socket=str(path))
    bind(store, adapter, pid=10)
    bind(store, adapter, pid=20)
    run.rows = [{'pid': 10, 'sessionId': 'old'}, {'pid': 20, 'sessionId': 'old'}]
    assert adapter.status('claude:old')['entry']['pid'] == 20
    Core(store, Identity('claude', 'old', 'claude:old', '/fixture', 'root'), adapter).run('release', {})
    bind(store, adapter, pid=99)
    assert adapter.status('claude:old')['reason_code'] == 'two_processes'


def test_listing_failure_never_uses_teammate(rig, store):
    adapter, run, _ = rig
    start(store, teammate=True)
    run.error = subprocess.TimeoutExpired(['claude'], 3)
    assert adapter.status('claude:old')['reason_code'] == 'listing_failed'
    assert [c[0][0] for c in run.calls] == ['claude']


@pytest.mark.parametrize('mode', ['ok', 'fail', 'bad', 'timeout'])
def test_real_runner_with_fake_claude(tmp_path, monkeypatch, store, mode):
    executable = tmp_path / 'claude'
    body = "import json, sys, time\nassert sys.argv[1:] == ['agents', '--json']\n"
    body += {'ok': "print('[{\"pid\":10,\"sessionId\":\"s\"}]')", 'fail': 'sys.exit(2)',
             'bad': "print('{}')", 'timeout': 'time.sleep(2)'}[mode]
    executable.write_text('#!' + sys.executable + '\n' + body + '\n')
    executable.chmod(0o755)
    monkeypatch.setenv('PATH', str(tmp_path))
    adapter = ClaudeAdapter(store)
    if mode == 'timeout':
        adapter._deadline = time.monotonic() + .05
    listing = adapter.listing()
    assert (listing is False) is (mode != 'ok')


class Inbox:
    """A fake inbox socket taking `connections` connections, one line each, in order."""
    def __init__(self, connections=1):
        self.directory = tempfile.TemporaryDirectory(prefix='kaiusap-')
        self.path = str(Path(self.directory.name) / 'inbox.sock')
        self.server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.server.bind(self.path)
        self.server.listen()
        self.server.settimeout(3)
        self.connections = connections
        self.data = b''
        self.received = []  # each connection's bytes
        self.errors = []
        self.thread = threading.Thread(target=self.receive)
        self.thread.start()

    def receive(self):
        try:
            for _ in range(self.connections):
                connection, _ = self.server.accept()
                data = b''
                with connection:
                    while chunk := connection.recv(8192):
                        data += chunk
                self.received.append(data)
                self.data += data
        except Exception as exc:
            self.errors.append(exc)

    def close(self):
        self.thread.join(4)
        self.server.close()
        self.directory.cleanup()
        assert not self.thread.is_alive() and not self.errors


def test_core_delivers_claude_recipient(rig, store):
    adapter, run, _ = rig
    inbox = Inbox(connections=2)  # alpha's iam renames it first, then the notice
    try:
        start(store, socket=inbox.path)
        run.rows = [{'pid': 10, 'sessionId': 'old'}]
        providers = Providers({'claude': adapter}, store)
        bind(store, providers, sid='sender', handle='sender')
        bind(store, providers)
        sender = Core(store, Identity('claude', 'sender', 'claude:sender', '/fixture', 'root'), providers)
        result = sender.run('send', {'to': 'alpha', 'subject': 'Review', 'reply_needed': False, 'fields': {'context': 'Check it'}})
        assert result['outcome'] == 'delivered' and result['paste_line'] is None
        message = store.message(result['message_id'])
        events = [e for e in store.events() if e.get('message_id') == message['id']]
        assert [e['event'] for e in events] == ['attempt', 'delivery']
        assert events[0]['binding'] == handles(store)['alpha']['id']
    finally:
        inbox.close()
    rename, post = inbox.received
    assert rename == b'{"type":"control","action":"rename","name":"alpha","session_id":"old"}\n'
    assert post.endswith(b'\n') and post.count(b'\n') == 1
    assert json.loads(post) == {'type': 'user', 'message': {'role': 'user', 'content': notice(message)}, 'session_id': 'old'}


class FaultSocket:
    def __init__(self, mode):
        self.mode, self.count, self.closes = mode, 0, 0
        self.timeouts = []

    def settimeout(self, value):
        self.timeouts.append(value)

    def connect(self, path):
        if self.mode == 'connect':
            raise ConnectionRefusedError()
        if self.mode == 'permission':
            raise PermissionError()
        if self.mode == 'connect_timeout':
            raise TimeoutError()

    def send(self, data):
        self.count += 1
        if self.mode in {'early', 'write_timeout'}:
            raise TimeoutError() if self.mode == 'write_timeout' else BrokenPipeError()
        if self.mode in {'partial', 'partial_timeout'}:
            if self.count == 1:
                return 2
            raise TimeoutError() if self.mode == 'partial_timeout' else BrokenPipeError()
        return len(data)

    def close(self):
        self.closes += 1
        if self.mode == 'close':
            raise OSError()


@pytest.mark.parametrize('mode,outcome,code', [('connect', 'stored', 'connect_failed'), ('permission', 'stored', 'sandboxed'),
    ('connect_timeout', 'stored', 'timeout'), ('early', 'stored', 'connect_failed'), ('write_timeout', 'stored', 'timeout'),
    ('partial', 'uncertain', 'no_confirmation'), ('partial_timeout', 'uncertain', 'no_confirmation'),
    ('close', 'uncertain', 'no_confirmation'), ('ok', 'delivered', None)])
def test_socket_outcomes_and_limits(rig, store, tmp_path, mode, outcome, code):
    adapter, run, _ = rig
    path = tmp_path / 'inbox'
    path.touch()
    start(store, socket=str(path))
    run.rows = [{'pid': 10, 'sessionId': 'old'}]
    sock = FaultSocket(mode)
    adapter.socket_factory = lambda *args: sock
    message = {'id': 'm', 'from': {'handle': 'sender'}, 'to': {'session_id': 'old', 'session_key': 'claude:old'},
               'subject': 'Hi', 'in_reply_to': None, 'reply_needed': False}
    result = adapter.deliver(store, message)
    assert (result['outcome'], result['reason_code']) == (outcome, code)
    assert sock.closes >= 1
    assert sock.timeouts[0] <= 2 and all(t <= 5 for t in sock.timeouts[1:])


DESKTOP = dict(entrypoint='claude-desktop', host_session_id='local_17f664df-5bd6-46fb-be10-14215af55d36',
               host_version='5.11492.5')


def test_a_desktop_app_session_is_hosted_with_the_app_found_above_its_helper(rig, store, monkeypatch):
    """The session's own binary sits in a bundle of its own; the app is found above it, through its helper."""
    import kaiusap.adapters.claude as claude
    adapter, run, _ = rig
    monkeypatch.setattr(claude, 'bundle_id', lambda path: 'com.anthropic.claudefordesktop' if path == '/Applications/Claude.app' else None)
    run.rows = [{'pid': 10, 'sessionId': 'old', 'cwd': '/live', 'name': 'Decision app'}]
    run.parents = {10: 30, 30: 40, 40: 1, 1: 0}
    run.commands = {10: '/Users/u/Library/Application Support/Claude/claude-code/2.1.325/x/claude.app/Contents/MacOS/claude',
                    30: '/Applications/Claude.app/Contents/Helpers/disclaimer',
                    40: '/Applications/Claude.app/Contents/MacOS/Claude'}
    start(store, **DESKTOP)
    row = adapter.where('claude:old')
    assert (row['where'], row['command'], row['note']) == ('hosted', None, 'Open in the Claude desktop app')
    assert row['hosts'] == [{
        'kind': 'app', 'app': 'claude-desktop', 'name': 'Claude', 'version': '5.11492.5',
        'app_id': 'com.anthropic.claudefordesktop', 'path': '/Applications/Claude.app', 'pid': 40,
        'session_id': 'local_17f664df-5bd6-46fb-be10-14215af55d36',
        'link': 'claude://code/continue?session=local_17f664df-5bd6-46fb-be10-14215af55d36&source=desktop_action',
        'link_opens': 'session', 'window': None, 'archived': None}]


@pytest.mark.parametrize('platform,app_id', [('darwin', 'com.anthropic.claudefordesktop'), ('linux', None)])
def test_an_ended_desktop_session_keeps_its_host_and_has_no_terminal_command(rig, store, monkeypatch, platform, app_id):
    """With the process gone the app's id is the known one on macOS and the link needs it; elsewhere there's none."""
    import sys
    adapter, run, _ = rig
    monkeypatch.setattr(sys, 'platform', platform)
    start(store, **DESKTOP)
    row = adapter.where('claude:old')
    host = row['hosts'][0]
    assert (row['where'], row['command'], host['pid'], host['path'], host['app_id']) == ('not_running', None, None, None, app_id)
    assert (host['link'] is None) == (app_id is None) and host['link_opens'] == ('session' if app_id else None)


def test_a_desktop_session_resumed_in_a_terminal_has_no_host(rig, store):
    """The newest start record decides: a later `claude --resume` in a terminal says cli."""
    adapter, run, _ = rig
    start(store, **DESKTOP)
    start(store, source='resume', at='2026-11-01T00:00:05Z', entrypoint='cli')
    row = adapter.where('claude:old')
    assert row['hosts'] == [] and row['where'] == 'not_running' and row['command'] == 'cd /fixture && claude --resume old'


def test_a_host_id_of_another_form_gives_no_link(rig, store, monkeypatch):
    import sys
    adapter, _, _ = rig
    monkeypatch.setattr(sys, 'platform', 'darwin')
    start(store, **{**DESKTOP, 'host_session_id': 'local_x&source=other'})
    host = adapter.where('claude:old')['hosts'][0]
    assert host['link'] is None and host['link_opens'] is None and host['session_id'] == 'local_x&source=other'


def test_a_terminal_session_has_no_hosts(rig, store):
    adapter, run, _ = rig
    run.rows = [{'pid': 10, 'sessionId': 'old', 'cwd': '/live'}]
    start(store, entrypoint='cli')
    row = adapter.where('claude:old')
    assert row['hosts'] == [] and row['where'] == 'not_in_tmux'


def test_listing_and_who_with_teammates_and_failure(rig, store):
    adapter, run, _ = rig
    run.rows = [{'pid': 10, 'sessionId': 'old', 'cwd': '/live', 'name': 'Provider title'}]
    start(store)
    start(store, 'team', 20, teammate=True)
    bind(store, adapter)
    rows = adapter.list()
    assert {r['session_key'] for r in rows} == {'claude:old', 'claude:team'}
    assert rows[0]['cwd'] == '/live' and rows[0]['name'] == 'Provider title'
    assert rows[0]['where'] == 'not_in_tmux'  # no socket still running
    assert rows[0]['command'] == 'cd /live && claude --resume old'
    assert rows[0]['note'] == 'Still open in another terminal: this starts a second copy of the session.'
    providers = Providers({'claude': adapter}, store)
    who = Core(store, Identity(), providers).run('who', {})['handles'][0]
    assert who['name'] == 'Provider title' and who['reason_code'] == 'no_socket'
    run.rows = {}
    result = Core(store, Identity(), providers).run('sessions', {})
    assert result['sessions'][0]['where'] == 'unknown' and result['sessions'][0]['command'] is None
    assert len(result['warnings']) == 1 and 'listing_failed' in result['warnings'][0]


@pytest.mark.parametrize('command,verbs', [
    ('kaiusap send --to beta', ['send']), ('A=one B="two words" /x/kaiusap iam alpha', ['iam']),
    ('echo ok && kaiusap reply x || /x/kaiusap moot x; kaiusap unpause\nkaiusap release | cat', ['reply', 'moot', 'unpause', 'release']),
    ('echo "$(kaiusap send --to x)"', ['send']), ('echo $(echo $(kaiusap reply x))', ['reply']),
    ("echo '$(kaiusap send)'", []), ('echo "x; kaiusap send"', []), ('python script.py kaiusap send', []),
    ('kaiusap --json send', ['send']), ('kaiusap whoami', ['whoami']), ('echo unrelated', [])])
def test_bash_recognizer(command, verbs):
    assert bash_verbs(command) == verbs


@pytest.mark.parametrize('verb', ['whoami', 'who', 'inbox', 'read', 'help', 'pause', 'send', 'reply', 'iam', 'release', 'moot', 'unpause'])
def test_pretool_agent_refusal(rig, verb):
    adapter, _, _ = rig
    for name, tool_input in [('Bash', {'command': 'kaiusap ' + verb}),
                              ('mcp__plugin_kaiusap_kaiusap__' + verb, {})]:
        payload = {'agent_id': 'a', 'tool_name': name, 'tool_input': tool_input}
        decision = adapter.pre_tool_use(payload)
        assert bool(decision) is (verb in {'send', 'reply', 'iam', 'release', 'moot', 'unpause'})
        assert adapter.pre_tool_use({**payload, 'agent_id': None}) == {}
    assert adapter.pre_tool_use({'agent_id': 'a', 'tool_name': 'Bash', 'tool_input': {'command': 'echo hi'}}) == {}


def test_pretool_screen_at_the_hook_entry_needs_no_store(tmp_path):
    # The hook entry asks the class for its spawned-agent screen before any store work; the deny included.
    from kaiusap.adapters import SPAWNED_AGENT_TEXT
    from kaiusap.adapters.claude import ClaudeAdapter
    assert ClaudeAdapter.screen_events == {'PreToolUse'}
    assert ClaudeAdapter.pre_tool({'tool_name': 'Bash', 'tool_input': {'command': 'kaiusap send'}}) is None
    assert ClaudeAdapter.pre_tool({'agent_id': 'a', 'tool_name': 'Bash', 'tool_input': {'command': 'echo hi'}}) is None
    assert ClaudeAdapter.pre_tool({'agent_id': 'a', 'tool_name': 'Bash', 'tool_input': {'command': 'kaiusap whoami'}}) == {}
    payload = {'agent_id': 'a', 'tool_name': 'mcp__plugin_kaiusap_kaiusap__send', 'tool_input': {}}
    script = ('import json, sys; from kaiusap.hooks import dispatch; '
              'print(json.dumps(dispatch("claude", "PreToolUse", json.loads(sys.stdin.read()), 0))); '
              'assert "kaiusap.store" not in sys.modules and "kaiusap.model" not in sys.modules')
    result = subprocess.run([sys.executable, '-c', script], input=json.dumps(payload), text=True, capture_output=True,
                            env={'KAIUSAP_HOME': str(tmp_path / 'store'), 'PATH': '/usr/bin:/bin'})
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == ClaudeAdapter(None).pre_tool_use(payload) == {'hookSpecificOutput': {
        'hookEventName': 'PreToolUse', 'permissionDecision': 'deny', 'permissionDecisionReason': SPAWNED_AGENT_TEXT}}
    assert not (tmp_path / 'store').exists()


def test_tell(rig):
    adapter, _, _ = rig
    assert adapter.tell('hello', 'SessionStart') == {'hookSpecificOutput': {'hookEventName': 'SessionStart', 'additionalContext': 'hello'}}


@pytest.mark.parametrize('kind', ['startup', 'compact', 'resume'])
def test_follow_no_move(rig, store, kind):
    adapter, _, _ = rig
    start(store)
    bind(store, adapter)
    proposal = adapter.follow_start(event(store, kind=kind), store)
    assert not proposal.get('move')


def test_clear_leaves_the_handle_and_places_the_new_session(rig, store):
    adapter, run, _ = rig
    start(store)
    bind(store, adapter)
    inbox = Inbox()
    item = event(store, socket=inbox.path)
    run.rows = [{'pid': 10, 'sessionId': 'new'}]
    result = follow(store, Providers({'claude': adapter}, store), item, deadline=time.monotonic() + 3)
    inbox.close()
    assert not result['moved'] and handles(store)['alpha']['session_key'] == 'claude:old'
    suggested = next(r['suggested'] for r in store.rows('sessions/personas.jsonl') if r.get('suggested'))
    assert result['texts'] == [
        "This is a new conversation. It is not alpha: alpha stays with the conversation left in this terminal, session "
        "old. To go back to it: claude --resume old. This session has no handle. The user can say 'you are alpha' to "
        f"carry on as alpha here, or give it another name, for example {suggested}."]
    assert result['shown'] == [
        "kaiusap: new conversation. alpha stays with the one you left; to go back: claude --resume old. To carry on as "
        f"alpha here, say 'you are alpha'; or name this session, for example {suggested}."]
    offers = [r for r in store.rows('sessions/personas.jsonl') if r.get('offered')]
    assert offers == [{**offers[0], 'session_key': 'claude:new', 'offered': 'alpha', 'from': 'claude:old'}]
    name = retirement_name('alpha', item['at'])
    assert json.loads(inbox.data) == {'type': 'control', 'action': 'rename', 'name': name, 'session_id': 'new'}
    named = [e for e in store.events() if e['event'] == 'named' and e['kind'] == 'fresh']
    assert named[-1]['name'] == name and named[-1]['session_key'] == 'claude:new'


def test_clear_without_a_socket_names_the_session_at_its_next_prompt(rig, store):
    adapter, run, _ = rig
    start(store)
    bind(store, adapter)
    item = event(store)
    run.rows = [{'pid': 10, 'sessionId': 'new'}]
    follow(store, Providers({'claude': adapter}, store), item, deadline=time.monotonic() + 3)
    name = retirement_name('alpha', item['at'])
    pending = [e for e in store.events() if e['event'] == 'named' and e['kind'] == 'fresh']
    assert pending[-1]['name'] is None and pending[-1]['wanted'] == name
    assert adapter.mirror_output(title_event(sid='new', title='alpha')) == {
        'hookSpecificOutput': {'hookEventName': 'UserPromptSubmit', 'sessionTitle': name}}
    assert adapter.mirror_output(title_event(sid='new')) == {}  # tried once more, then left


@pytest.mark.parametrize('source,at', [('clear', '2026-11-01T00:00:01Z'), ('resume', '2026-11-01T00:00:01Z')])
def test_clear_finds_only_the_session_just_left(rig, store, source, at):
    # A quick second /clear finds the session the first one made, which holds no handle: nothing is said.
    adapter, _, _ = rig
    start(store, at='2026-10-31T23:59:50Z')
    bind(store, adapter)
    start(store, 'middle', source=source, at=at)
    proposal = adapter.follow_start(event(store), store)
    assert not proposal.get('move') and not proposal.get('fresh') and proposal['suggest'] is False


def test_clear_places_the_new_session_even_while_another_process_holds_the_old_one(rig, store):
    adapter, run, _ = rig
    start(store)
    start(store, 'old', 20, teammate=True)
    bind(store, adapter, pid=20)
    run.rows = [{'pid': 20, 'sessionId': 'old'}]
    proposal = adapter.follow_start(event(store), store)
    assert not proposal.get('move') and proposal['fresh'] == {'handle': 'alpha', 'from': 'claude:old'}


def test_rewind_history_and_holder_predicates(rig, store):
    # A binding a /clear made before handles stayed with the session left still moves back on a rewind.
    adapter, run, _ = rig
    start(store)
    bind(store, adapter)
    start(store, 'new', at='2026-11-01T00:00:02Z', source='clear')
    old = handles(store)['alpha']
    at = '2026-11-01T00:00:03Z'
    store.append('handles.jsonl', {'at': at, 'id': '20261101T000003Z-aaaaaa', 'handle': 'alpha', 'reason': 'fresh-start',
                                   'session_id': 'new', 'session_key': 'claude:new', 'provider': 'claude',
                                   'by_session': 'new', 'by_session_key': 'claude:new', 'pid': 10,
                                   'process_started_at': START,
                                   'moved_from': {'session_id': 'old', 'provider': 'claude', 'session_key': old['session_key']},
                                   'via': None, 'also_released': None})
    run.rows = [{'pid': 10, 'sessionId': 'new'}]
    resume = {**event(store, 'old', kind='resume'), 'at': now()}
    proposal = adapter.follow_start(resume, store)
    assert proposal['move']['reason'] == 'rewind'
    assert proposal['move']['from'] == 'claude:new'
    run.rows = []  # the name is taken by another session only once 'new' isn't running
    bind(store, adapter, sid='else')
    assert not adapter.follow_start(resume, store).get('move')


LATE = ("You may be alpha: this conversation started as alpha's session ended. Ask the user whether this is alpha "
        "continuing in the background.")


def test_late_background_offer_skips_a_holder_still_running(rig, store, tmp_path):
    adapter, run, _ = rig
    transcript = tmp_path / 'synthetic.jsonl'
    transcript.write_text('bad JSON\n')
    start(store, transcript=str(transcript))
    bind(store, adapter)
    run.rows = [{'pid': 10, 'sessionId': 'old'}]  # alpha's session is still running: a copy, not an offer
    assert adapter.follow_start(event(store, kind='fork', pid=20), store)['text'] == COPY


@pytest.mark.parametrize('kind', ['found', 'late', 'branch', 'respawn', 'none', 'outside_tail', 'missing', 'cwd_index'])
def test_background_fork(rig, store, tmp_path, kind):
    adapter, _, clock = rig
    transcript = tmp_path / 'synthetic.jsonl'
    match = json.dumps({'type': 'continued-in', 'continuedInSessionId': 'new'}) + '\n'
    transcript.write_text(match if kind == 'found' else 'bad JSON\n')
    if kind == 'outside_tail':
        transcript.write_text(match + 'x' * 65536 + '\n')
    start(store, transcript=str(transcript))
    bind(store, adapter)
    item = event(store, kind='fork', pid=20)
    if kind == 'late':
        clock.on_sleep = lambda: transcript.write_text(match) if clock.value >= .1 else None
    if kind == 'branch':
        start(store, 'before-branch', 20, at='2026-11-01T00:00:01Z')
    if kind == 'respawn':
        bind(store, adapter, 'new')
    if kind == 'missing':
        transcript.unlink()
    if kind == 'cwd_index':
        moved = tmp_path / 'relocated.jsonl'
        moved.write_text(match)
        ClaudeAdapter(store).record({'record': {'at': '2026-11-01T00:00:01Z', 'provider': 'claude', 'session_id': 'old',
                                                'session_key': 'claude:old', 'source': 'cwd', 'cwd': '/moved', 'transcript': str(moved)}})
        ClaudeAdapter(store).index_path('old').write_text('bad')
    proposal = adapter.follow_start(item, store)
    if kind in {'found', 'late', 'cwd_index'}:
        assert proposal['move']['reason'] == 'moved-background'
        assert proposal['text'] == 'You are alpha; the handle followed this conversation from session old.'
    elif kind == 'respawn':
        assert not proposal.get('move') and not proposal.get('text')
    elif kind in {'none', 'outside_tail'}:
        # No record in time, but alpha's session ended in this directory with its transcript just
        # written: offered, not taken for a copy.
        assert proposal['text'] == LATE and proposal['offers'] == [{'handle': 'alpha', 'from': 'claude:old'}]
    else:
        assert proposal['text'] == COPY
    if kind in {'branch', 'respawn', 'found', 'cwd_index'}:
        assert clock.value == 0
    elif kind == 'late':
        assert .1 <= clock.value < 1.5
    else:
        assert clock.value == pytest.approx(1.5)


@pytest.mark.parametrize('case,current', [('valid', True), ('absent', True), ('tie', False), ('newer', False),
                                         ('lag', True), ('stuck', False), ('failed', False)])
def test_currency(rig, store, case, current):
    adapter, run, clock = rig
    item = event(store)
    run.rows = [{'pid': 10, 'sessionId': 'new'}]
    if case == 'absent':
        run.rows = []
    if case == 'tie':
        start(store, 'tie', at=item['at'])
    if case == 'newer':
        start(store, 'later', at='2026-11-01T00:00:03Z')
    if case in {'lag', 'stuck'}:
        run.rows = [{'pid': 10, 'sessionId': 'old'}]
        if case == 'lag':
            clock.on_sleep = lambda: setattr(run, 'rows', [{'pid': 10, 'sessionId': 'new'}])
    if case == 'failed':
        run.rows = {}
    item['deadline'] = 1
    assert adapter.current(item, {}) is current


def test_suggestions_do_not_treat_missing_socket_as_dead(rig, store):
    adapter, run, _ = rig
    start(store)
    bind(store, adapter)
    providers = Providers({'claude': adapter}, store)
    run.rows = [{'pid': 10, 'sessionId': 'old'}]
    assert alive(providers, handles(store)['alpha'])
    run.rows = {}
    adapter.reset()
    assert alive(providers, handles(store)['alpha'])
    run.rows = []
    adapter.reset()
    item = event(store, pid=20)
    result = follow(store, providers, item, deadline=time.monotonic() + 3)
    assert not result['moved'] and result['texts'] == [
        "This conversation began with a /clear. alpha's session was in this directory; if that is the conversation "
        "left, alpha stays with it: claude --resume old. The user can say 'you are alpha' to carry on as alpha here."]


def test_tmux_suggestion_on_startup(rig, store):
    adapter, _, _ = rig
    bind(store, adapter)
    item = event(store, kind='startup', tmux_session='alpha')
    result = follow(store, Providers({'claude': adapter}, store), item, deadline=time.monotonic() + 3)
    assert result['texts'] == [tmux_name_suggestion("alpha")]


def title_event(kind='prompt', sid='old', title=None):
    return {'kind': kind, 'session_id': sid, 'session_key': 'claude:' + sid, 'session_title': title}


@pytest.mark.parametrize('kind', ['startup', 'resume', 'fork', 'prompt'])
def test_mirror_binding_once_then_one_prompt_retry(rig, store, kind):
    adapter, _, _ = rig
    bind(store, adapter)
    item = title_event(kind, title='existing')
    result = adapter.mirror_output(item)
    assert result['hookSpecificOutput']['sessionTitle'] == 'alpha'
    assert adapter.mirror_output(title_event(title='alpha')) == {}
    assert adapter.mirror_output(title_event())['hookSpecificOutput']['sessionTitle'] == 'alpha'
    assert adapter.mirror_output(title_event()) == {}
    records = [e for e in store.events() if e['event'] == 'named']
    assert len(records) == 2 and [e['retry'] for e in records] == [False, True]
    assert all(e['binding'] == handles(store)['alpha']['id'] for e in records)


@pytest.mark.parametrize('kind', ['clear', 'compact'])
def test_start_mirror_deferred_until_prompt(rig, store, kind):
    adapter, _, _ = rig
    bind(store, adapter)
    assert adapter.mirror_output(title_event(kind)) == {}
    assert not list(store.events())
    assert adapter.mirror_output(title_event())['hookSpecificOutput']['sessionTitle'] == 'alpha'


def test_mirror_retirement_and_current_binding_wins(rig, store):
    adapter, _, _ = rig
    bind(store, adapter)
    bind(store, adapter, 'new')
    ended = [b for b, _ in binding_history(store)[0]]
    left_at = binding_history(store)[0][0][1]
    result = adapter.mirror_output(title_event(title='alpha'))
    retired = result['hookSpecificOutput']['sessionTitle']
    # <handle>~<yymmddhhmmss>, the local time of the line that ended the binding, to the second.
    assert re.fullmatch(r'alpha~\d{12}', retired)
    assert retired == 'alpha~' + instant(left_at).astimezone().strftime('%y%m%d%H%M%S')
    assert adapter.mirror_output(title_event(title=retired)) == {}
    assert [e for e in store.events() if e['kind'] == 'retired'][0]['binding'] == ended[0]['id']
    bind(store, adapter)
    bind(store, adapter, 'else', handle='beta')
    bind(store, adapter, 'old', handle='beta')
    # old owes a retirement of alpha and a mirror of beta: mirror wins.
    assert adapter.mirror_output(title_event(title='alpha'))['hookSpecificOutput']['sessionTitle'] == 'beta'
    retired = [e for e in store.events() if e['kind'] == 'retired']
    assert retired[-1]['name'] is None


def test_retirement_preserves_user_title(rig, store):
    adapter, _, _ = rig
    bind(store, adapter)
    bind(store, adapter, 'new')
    assert adapter.mirror_output(title_event(title='user name')) == {}
    assert [e for e in store.events() if e['kind'] == 'retired'][0]['name'] is None


def test_store_failure_before_inbox_leaves_attempt_without_delivery(rig, store, monkeypatch):
    adapter, run, _ = rig
    bind(store, adapter, 'sender', 'sender')
    bind(store, adapter)
    run.rows = [{'pid': 10, 'sessionId': 'old'}]
    original = store.rows
    def broken(relative, **kwargs):
        if relative == 'sessions/starts.jsonl':
            raise OSError('injected store failure')
        return original(relative, **kwargs)
    monkeypatch.setattr(store, 'rows', broken)
    sender = Core(store, Identity('claude', 'sender', 'claude:sender', '/fixture', 'root'), adapter)
    result = sender.run('send', {'to': 'alpha', 'subject': 'Review', 'reply_needed': False, 'fields': {'context': 'Check it'}})
    assert result['outcome'] == 'stored' and result['reason_code'] == 'store_failed'
    assert result['warnings'] and result['paste_line']
    events = [e for e in store.events() if e.get('message_id') == result['message_id']]
    assert [e['event'] for e in events] == ['attempt']


def test_currency_rechecks_records_while_listing_catches_up(rig, store):
    adapter, run, clock = rig
    item = event(store)
    item['deadline'] = 1
    run.rows = [{'pid': 10, 'sessionId': 'old'}]
    def overtake():
        start(store, 'later', at='2026-11-01T00:00:03Z')
        run.rows = [{'pid': 10, 'sessionId': 'new'}]
    clock.on_sleep = overtake
    assert not adapter.current(item, {})


@pytest.mark.parametrize('mode', ['paused', 'limit'])
def test_claude_delivery_reservations_skip_provider_when_blocked(rig, store, mode):
    adapter, run, _ = rig
    bind(store, adapter, 'sender', 'sender')
    bind(store, adapter)
    if mode == 'paused':
        store.set_paused(True)
    else:
        (store.root / 'settings.json').write_text('{"max_per_hour": 0}')
    sender = Core(store, Identity('claude', 'sender', 'claude:sender', '/fixture', 'root'), adapter)
    result = sender.run('send', {'to': 'alpha', 'subject': 'Review', 'reply_needed': False, 'fields': {'context': 'Check it'}})
    assert result['reason_code'] == mode
    assert not run.calls
    assert [e['event'] for e in store.events() if e.get('message_id') == result['message_id']] == ['delivery']


def test_user_rename_then_clear_spends_retry_once_per_binding(rig, store):
    adapter, _, _ = rig
    bind(store, adapter)
    binding = handles(store)['alpha']
    assert adapter.mirror_output(title_event())['hookSpecificOutput']['sessionTitle'] == 'alpha'
    assert adapter.mirror_output(title_event(title='user name')) == {}
    records = [e for e in store.events() if e.get('binding') == binding['id'] and e.get('kind') == 'handle']
    assert [(e['name'], e['retry']) for e in records] == [('alpha', False), (None, True)]
    # Repeated prompts with the user title cannot append another end marker.
    assert adapter.mirror_output(title_event(title='user name')) == {}
    assert adapter.mirror_output(title_event()) == {}
    assert adapter.mirror_output(title_event('resume')) == {}
    assert len([e for e in store.events() if e.get('binding') == binding['id'] and e.get('kind') == 'handle']) == 2
    # Spending a retry belongs to a binding, not the session or handle forever.
    Core(store, Identity('claude', 'old', 'claude:old', '/fixture', 'root'), adapter).run('release', {})
    bind(store, adapter)
    assert handles(store)['alpha']['id'] != binding['id']
    assert adapter.mirror_output(title_event())['hookSpecificOutput']['sessionTitle'] == 'alpha'


def test_fresh_start_keeps_user_title_and_never_retries_a_skipped_mirror(rig, store):
    adapter, _, _ = rig
    bind(store, adapter)
    line = {**handles(store)['alpha'], 'id': 'synthetic-new-binding', 'reason': 'fresh-start'}
    store.append('handles.jsonl', line)
    assert adapter.mirror_output(title_event(title='user name')) == {}
    assert adapter.mirror_output(title_event()) == {}
    records = [e for e in store.events() if e.get('binding') == line['id']]
    assert len(records) == 1 and records[0]['name'] is None


@pytest.mark.parametrize('command,verbs', [
    ('echo ok # comment\nkaiusap send', ['send']),
    ('# ignored ; kaiusap send\nkaiusap who', ['who']),
    ('# $(kaiusap send)\necho ok', []),
    ('echo "# literal"; kaiusap reply x', ['reply']),
    ('A=foo#bar kaiusap iam alpha', ['iam']),
])
def test_bash_comments_preserve_command_boundaries(command, verbs):
    assert bash_verbs(command) == verbs


def test_process_test_listing_failure_tells_known_holder(rig, store):
    # A /branch whose source's binding records another running process, with the listing failing.
    adapter, run, _ = rig
    start(store)
    bind(store, adapter, pid=20)
    run.rows = {}
    result = adapter.follow_start(event(store, kind='fork'), store)
    assert not result.get('move')
    assert result['text'] == "You may be alpha: this conversation replaced its session in this terminal, but kaiusap couldn't follow it in time. Ask the user."
    assert result['offers'] == [{'handle': 'alpha', 'from': 'claude:old'}]  # so "you are alpha" continues the persona


def test_branch_moves_the_handle_to_the_copy(rig, store):
    adapter, run, _ = rig
    start(store)
    bind(store, adapter)
    run.rows = [{'pid': 10, 'sessionId': 'new'}]
    item = event(store, kind='fork')
    result = follow(store, Providers({'claude': adapter}, store), item, deadline=time.monotonic() + 3)
    assert result['moved'] and result['line']['reason'] == 'copy'
    assert result['line']['moved_from']['session_key'] == 'claude:old'
    assert result['texts'] == ['You are alpha; the handle followed this conversation from session old, which it replaced in this terminal.']
    assert result['shown'] == []


def test_branch_moves_the_handle_while_the_listing_still_names_the_original(rig, store):
    # At a /branch, Claude's listing names the original session until after the start hook.
    adapter, run, _ = rig
    start(store)
    bind(store, adapter)
    run.rows = [{'pid': 10, 'sessionId': 'old'}]
    began = time.monotonic()
    result = follow(store, Providers({'claude': adapter}, store), event(store, kind='fork'), deadline=time.monotonic() + 3)
    assert result['moved'] and result['line']['reason'] == 'copy'
    assert time.monotonic() - began < 1


def test_branch_renames_the_copy_at_once_over_the_inbox_socket(rig, store):
    # The start hook's sessionTitle is ignored at a /branch and Claude titles the copy itself, so the copy is
    # renamed with the inbox `rename` line, as at `iam`.
    adapter, run, _ = rig
    start(store)
    bind(store, adapter)
    run.rows = [{'pid': 10, 'sessionId': 'new'}]
    inbox = Inbox()
    result = follow(store, Providers({'claude': adapter}, store), event(store, kind='fork', socket=inbox.path),
                    deadline=time.monotonic() + 3)
    inbox.close()
    assert result['moved'] and json.loads(inbox.data) == {'type': 'control', 'action': 'rename', 'name': 'alpha',
                                                          'session_id': 'new'}
    named = [e for e in store.events() if e['event'] == 'named' and e['kind'] == 'handle']
    assert named[-1]['name'] == 'alpha' and named[-1]['session_key'] == 'claude:new'


@pytest.mark.parametrize('title,applied', [('you are alpha (Branch 4)', True), ('my own name', False)])
def test_the_first_prompt_after_a_branch_names_the_copy_over_claudes_branch_title(rig, store, title, applied):
    # Claude titles the copy after the start hook, over the inbox rename; that title isn't the user's rename.
    adapter, run, _ = rig
    start(store)
    bind(store, adapter)
    run.rows = [{'pid': 10, 'sessionId': 'new'}]
    inbox = Inbox()
    follow(store, Providers({'claude': adapter}, store), event(store, kind='fork', socket=inbox.path),
           deadline=time.monotonic() + 3)
    inbox.close()
    prompt = {'provider': 'claude', 'kind': 'prompt', 'session_id': 'new', 'session_key': 'claude:new',
              'session_title': title, 'hook_event_name': 'UserPromptSubmit'}
    output = adapter.mirror_output(prompt)
    assert output.get('hookSpecificOutput', {}).get('sessionTitle') == ('alpha' if applied else None)


def test_branch_while_another_process_holds_the_source_is_offered(rig, store):
    adapter, run, _ = rig
    start(store)
    bind(store, adapter, pid=20)
    run.rows = [{'pid': 20, 'sessionId': 'old'}]
    result = follow(store, Providers({'claude': adapter}, store), event(store, kind='fork'), deadline=time.monotonic() + 3)
    assert not result['moved'] and handles(store)['alpha']['session_key'] == 'claude:old'
    assert result['texts'] == ["This conversation is a copy of alpha's. It has no handle. To carry on as alpha here, the "
                               "user says 'you are alpha'."]
    assert result['shown'] == ["kaiusap: this conversation is a copy of alpha's and holds no handle. To carry on as "
                               "alpha here, say 'you are alpha'."]
    assert [r['offered'] for r in store.rows('sessions/personas.jsonl') if r.get('offered')] == ['alpha']


def test_an_unplaced_copy_is_shown_a_line(rig, store):
    adapter, _, _ = rig
    result = follow(store, Providers({'claude': adapter}, store), event(store, kind='fork', pid=20),
                    deadline=time.monotonic() + 3)
    assert result['texts'] == [COPY]
    assert result['shown'] == ['kaiusap: this conversation holds no handle. If it is a copy, the original keeps its handle.']
    assert adapter.show('x') == {'systemMessage': 'x'}


def test_a_clone_is_told_where_the_handle_went_and_offered_it(rig, store):
    adapter, run, _ = rig
    start(store)
    bind(store, adapter)
    run.rows = [{'pid': 10, 'sessionId': 'new'}]
    providers = Providers({'claude': adapter}, store)
    follow(store, providers, event(store, kind='fork'), deadline=time.monotonic() + 3)
    resumed = {**event(store, 'old', kind='resume', pid=20), 'at': now()}
    result = follow(store, providers, resumed, deadline=time.monotonic() + 3)
    assert result['texts'] == [
        "This conversation is not alpha's current one. alpha went on in session new; what was said there isn't here. "
        "To carry on as alpha from there: claude --resume new. To continue from here instead, the user says 'you are alpha'."]
    assert result['shown'][0].startswith("kaiusap: this conversation is not alpha's current one.")
    assert [(r['session_key'], r['offered'], r['from']) for r in store.rows('sessions/personas.jsonl') if r.get('offered')] == [
        ('claude:old', 'alpha', 'claude:new')]


def test_sessions_shows_both_terminals_and_puts_running_first(rig, store):
    adapter, run, _ = rig
    bind(store, adapter, 'inactive', 'inactive')
    start(store, tmux_pane='7@42:%1')
    start(store, pid=20, tmux_pane='7@42:%2')
    run.rows = [{'pid': 10, 'sessionId': 'old'}, {'pid': 20, 'sessionId': 'old'}]
    run.panes = '%1\tfirst pane\t1\t7\t42\n%2\tsecond pane\t0\t7\t42'
    providers = Providers({'claude': adapter}, store)
    rows = Core(store, Identity(), providers).run('sessions', {})['sessions']
    assert [r['session_key'] for r in rows] == ['claude:old', 'claude:inactive']
    assert rows[0]['where'] == 'tmux'
    assert [p['tmux_session'] for p in rows[0]['tmux_panes']] == ['first pane', 'second pane']
    assert rows[0]['command'] == "tmux -L default attach -t '=first pane'"
    assert rows[1]['command'] == 'claude --resume inactive'


def test_sessions_attach_line_prefers_an_attached_pane(rig, store):
    adapter, run, _ = rig
    start(store, tmux_pane='7@42:%1')
    start(store, pid=20, tmux_pane='7@42:%2')
    run.rows = [{'pid': 10, 'sessionId': 'old'}, {'pid': 20, 'sessionId': 'old'}]
    run.panes = '%1\tfirst pane\t0\t7\t42\n%2\tsecond pane\t1\t7\t42'
    row = Core(store, Identity(), Providers({'claude': adapter}, store)).run('sessions', {})['sessions'][0]
    assert row['command'] == "tmux -L default attach -t '=second pane'"


def test_sessions_resume_line_changes_to_the_directory(rig, store):
    """A Claude session not running: `cd <dir> && claude --resume <id>` when the directory is known."""
    adapter, run, _ = rig
    bind(store, adapter, 'gone', 'gone')
    start(store, 'gone', cwd='/work/my dir')
    run.rows = []
    row = Core(store, Identity(), Providers({'claude': adapter}, store)).run('sessions', {})['sessions'][0]
    assert row['where'] == 'not_running'
    assert row['command'] == "cd '/work/my dir' && claude --resume gone"


@pytest.mark.parametrize('job', ['job12345', None])
def test_sessions_gives_background_attach_command(rig, store, job):
    """A running background session's way back is `claude attach <job>`, never `claude --resume`."""
    adapter, run, _ = rig
    start(store)
    run.rows = [{'pid': 10, 'sessionId': 'old', 'kind': 'background', **({'id': job} if job else {})}]
    providers = Providers({'claude': adapter}, store)
    row = Core(store, Identity(), providers).run('sessions', {})['sessions'][0]
    assert row['where'] == 'background'
    assert row['command'] == ('claude attach job12345' if job else None)


# ----- context use, from the transcript's end ----------------------------------------------------

def reply(at, model='claude-opus-5-5', sidechain=False, **usage):
    usage = {'input_tokens': 2, 'cache_creation_input_tokens': 600, 'cache_read_input_tokens': 228175,
             'output_tokens': 114, **usage}
    return {'type': 'assistant', 'timestamp': at, 'isSidechain': sidechain,
            'message': {'model': model, 'role': 'assistant', 'usage': usage}}


def transcript(tmp_path, *lines, raw=b''):
    path = tmp_path / 'transcript.jsonl'
    path.write_bytes(b''.join(json.dumps(line).encode() + b'\n' for line in lines) + raw)
    return str(path)


def test_context_is_the_latest_replys_input_with_no_limit(rig, store, tmp_path):
    """The sum of the latest main-thread reply's input fields; sidechain and synthetic replies, user
    lines and a cut last line are skipped; the limit is null, never inferred from the model."""
    adapter, run, _ = rig
    path = transcript(tmp_path, reply('2026-11-10T04:00:00Z', cache_read_input_tokens=1000),
                      reply('2026-11-10T04:01:00Z'),
                      reply('2026-11-10T04:02:00Z', sidechain=True, cache_read_input_tokens=5),
                      {'type': 'user', 'timestamp': '2026-11-10T04:03:00Z', 'message': {'role': 'user'}},
                      reply('2026-11-10T04:04:00Z', model='<synthetic>', input_tokens=0, cache_creation_input_tokens=0,
                            cache_read_input_tokens=0),
                      raw=b'{"type": "assistant", "timest')
    start(store, transcript=path)
    expected = {'used': 228777, 'limit': None, 'limit_source': None, 'model': 'claude-opus-5-5',
                'model_maker': 'anthropic', 'at': '2026-11-10T04:01:00Z'}
    assert adapter.context('claude:old') == expected
    run.rows = [{'pid': 10, 'sessionId': 'old'}]
    rows = Core(store, Identity(), Providers({'claude': adapter}, store)).run('sessions', {})['sessions']
    assert rows[0]['context'] == expected


@pytest.mark.parametrize('case', ['no_record', 'no_transcript', 'missing_file', 'no_reply', 'bad_usage'])
def test_context_is_none_when_nothing_is_known(rig, store, tmp_path, case):
    adapter, _, _ = rig
    if case == 'no_transcript':
        start(store)
    elif case == 'missing_file':
        start(store, transcript=str(tmp_path / 'gone.jsonl'))
    elif case == 'no_reply':
        start(store, transcript=transcript(tmp_path, {'type': 'user', 'message': {}}, raw=b'not json\n'))
    elif case == 'bad_usage':
        start(store, transcript=transcript(tmp_path, reply('2026-11-10T04:00:00Z', input_tokens='2')))
    assert adapter.context('claude:old') is None


@pytest.mark.parametrize('case,expected', [('failed', 'listing_failed'), ('none', 'not_live'),
    ('two', 'two_processes'), ('socket', 'no_socket'), ('detached', 'detached')])
def test_claude_whoami_json_through_cli_in_test_order(rig, store, tmp_path, case, expected):
    from io import StringIO
    adapter, run, _ = rig
    socket_path = tmp_path / 'inbox'
    socket_path.touch()
    start(store, socket=str(socket_path), tmux_pane='7@42:%1')
    run.rows = [{'pid': 10, 'sessionId': 'old', 'kind': 'background', 'id': 'job12345'}]
    if case == 'failed':
        run.rows = {}
    elif case == 'none':
        run.rows = []
    elif case == 'two':
        run.rows.append({'pid': 20, 'sessionId': 'old'})
        socket_path.unlink()  # two_processes wins over no_socket
    elif case == 'socket':
        socket_path.unlink()
    elif case == 'detached':
        run.rows[0]['kind'] = 'interactive'
        run.panes = '%1\twork\t0\t7\t42'
        (store.root / 'settings.json').write_text('{"wake_detached": false}')
    providers = Providers({'claude': adapter}, store)
    stdout, stderr = StringIO(), StringIO()
    code = main(['whoami', '--json'], store=store, provider=providers,
                environ={'CLAUDE_CODE_SESSION_ID': 'old', 'CLAUDE_PID': '10', 'PWD': '/fixture'},
                stdout=stdout, stderr=stderr)
    result = json.loads(stdout.getvalue())
    assert code == 0 and not stderr.getvalue()
    assert result['reason_code'] == expected and result['can_be_woken'] is False
    if case == 'socket':
        assert 'claude --resume old' in result['fix'] and 'claude stop job12345' in result['fix']


@pytest.mark.parametrize('phase,outcome', [('connect', 'stored'), ('write', 'uncertain'), ('close', 'uncertain')])
def test_delivery_deadlines_cover_write_and_close_together(rig, store, tmp_path, phase, outcome):
    adapter, run, clock = rig
    path = tmp_path / 'inbox'
    path.touch()
    start(store, socket=str(path))
    run.rows = [{'pid': 10, 'sessionId': 'old'}]
    class DelayedSocket(FaultSocket):
        def connect(self, path):
            if phase == 'connect':
                clock.value += 21
        def send(self, data):
            if phase == 'write':
                clock.value += 6
            return len(data)
        def close(self):
            self.closes += 1
            if phase == 'close':
                clock.value += 6
    sock = DelayedSocket('ok')
    adapter.socket_factory = lambda *args: sock
    message = {'id': 'm', 'from': {'handle': 'sender'}, 'to': {'session_id': 'old', 'session_key': 'claude:old'},
               'subject': 'Hi', 'in_reply_to': None, 'reply_needed': False}
    result = adapter.deliver(store, message)
    assert result['outcome'] == outcome
    assert result['reason_code'] == ('timeout' if outcome == 'stored' else 'no_confirmation')
    assert sock.closes >= 1


def test_multiple_process_selection_rejects_reused_binding_pid(rig, store, tmp_path):
    adapter, run, _ = rig
    path = tmp_path / 'inbox'
    path.touch()
    start(store, socket=str(path))
    start(store, pid=20, socket=str(path))
    bind(store, adapter, pid=10)
    reused_start = '2026-10-02T00:00:00Z'
    reused_stamp = datetime.fromisoformat(reused_start.replace('Z', '+00:00')).astimezone().strftime('%a %b %d %H:%M:%S %Y')
    run.processes[10] = (reused_stamp, '/fixture/claude')
    start(store, socket=str(path), process_started_at=reused_start, at='2026-11-01T00:00:01Z')
    run.rows = [{'pid': 10, 'sessionId': 'old'}, {'pid': 20, 'sessionId': 'old'}]
    # Both processes have current records and a socket. The old binding names
    # the previous lifetime of pid 10 and cannot select its replacement.
    assert adapter.status('claude:old')['reason_code'] == 'two_processes'
    caller = Identity('claude', 'old', 'claude:old', '/fixture', 'root', pid=10,
                      process_started_at=reused_start)
    assert Core(store, caller, adapter).run('iam', {'handle': 'alpha'})['process_changed']
    status = adapter.status('claude:old')
    assert status['reason_code'] is None and status['entry']['pid'] == 10
    assert status['record']['process_started_at'] == reused_start


def test_both_adapters_and_claude_pretool_share_the_spawned_agent_wording(rig, store):
    from kaiusap.adapters import SPAWNED_AGENT_TEXT
    from kaiusap.adapters.codex import CodexAdapter
    claude, _, _ = rig
    expected = ('You are a spawned agent and may only look (whoami, who, inbox, read, help, pause). '
                'Report to the agent that started you, which sends.')
    assert SPAWNED_AGENT_TEXT == expected
    for adapter in (claude, CodexAdapter(store)):
        identity = Identity(adapter.provider, 'root', adapter.provider + ':root/child',
                            '/fixture', 'spawned', adapter.provider + ':root', agent_id='child')
        with pytest.raises(Error) as exc:
            adapter.refuse_before_acting(identity)
        assert str(exc.value) == expected
    for name, tool_input in [('Bash', {'command': 'kaiusap send'}),
                              ('mcp__plugin_kaiusap_kaiusap__send', {})]:
        decision = claude.pre_tool_use({'agent_id': 'child', 'tool_name': name, 'tool_input': tool_input})
        assert decision['hookSpecificOutput']['permissionDecision'] == 'deny'
        assert decision['hookSpecificOutput']['permissionDecisionReason'] == expected


# ----- the name at iam, over the inbox (added 2026-11-08) --------------------------

def rename_line(name, sid='old'):
    return json.dumps({'type': 'control', 'action': 'rename', 'name': name, 'session_id': sid},
                      separators=(',', ':')).encode() + b'\n'


def claude_core(store, provider, sid='old', pid=10):
    return Core(store, Identity('claude', sid, 'claude:' + sid, '/fixture', 'root', pid=pid, process_started_at=START), provider)


def test_iam_renames_the_session_at_once_through_its_inbox(rig, store):
    adapter, run, _ = rig
    inbox = Inbox()
    try:
        start(store, socket=inbox.path)
        # Another process's record for the session isn't the binding's; its socket isn't used.
        start(store, pid=20, at='2026-11-01T00:00:05Z', socket='/fixture/other.sock')
        store.set_paused(True)  # the rename isn't a delivery
        result = bind(store, adapter)
    finally:
        inbox.close()
    assert result['warnings'] == [] and handles(store)['alpha']['session_key'] == 'claude:old'
    assert inbox.received == [b'{"type":"control","action":"rename","name":"alpha","session_id":"old"}\n']
    binding = handles(store)['alpha']
    named = [e for e in store.events() if e['event'] == 'named']
    assert [(e['binding'], e['kind'], e['name'], e['retry'], e['session_key']) for e in named] == [
        (binding['id'], 'handle', 'alpha', False, 'claude:old')]
    # Applied: the prompt hook doesn't name it again while Claude shows the handle...
    assert adapter.mirror_output(title_event(title='alpha')) == {}
    # ...and its one retry covers a rename Claude dropped (nothing confirms the line).
    assert adapter.mirror_output(title_event())['hookSpecificOutput']['sessionTitle'] == 'alpha'
    assert adapter.mirror_output(title_event()) == {}


@pytest.mark.parametrize('mode', ['no_socket', 'connect', 'connect_timeout', 'permission', 'partial', 'close'])
def test_iam_rename_not_written_in_full_leaves_the_name_to_the_hook(rig, store, tmp_path, mode):
    adapter, run, _ = rig
    path = tmp_path / 'inbox'
    path.touch()
    start(store, socket=None if mode == 'no_socket' else str(path))
    sock, made = FaultSocket(mode), []
    adapter.socket_factory = lambda *args: made.append(args) or sock
    result = bind(store, adapter)
    assert result['handle'] == 'alpha' and result['warnings'] == []  # iam still succeeds
    assert bool(made) == (mode != 'no_socket')
    assert not [e for e in store.events() if e['event'] == 'named']
    if made:
        assert 'Claude rename to alpha not sent' in (store.root / 'hook-errors.log').read_text()
    # The hook sets the name as before, whatever the title, after iam.
    assert adapter.mirror_output(title_event(title='something'))['hookSpecificOutput']['sessionTitle'] == 'alpha'


def test_iam_already_held_sends_nothing(rig, store, tmp_path):
    adapter, run, _ = rig
    inbox = Inbox()
    try:
        start(store, socket=inbox.path)
        bind(store, adapter)
    finally:
        inbox.close()
    made = []
    adapter.socket_factory = lambda *args: made.append(args) or FaultSocket('ok')
    assert bind(store, adapter)['already_held'] and not made


def test_iam_retires_a_running_session_named_the_handle_at_once(rig, store):
    adapter, run, _ = rig
    providers = Providers({'claude': adapter}, store)
    inbox = Inbox(connections=2)
    try:
        start(store, socket=inbox.path)
        bind(store, providers)
        old = handles(store)['alpha']
        start(store, sid='new', pid=20, at='2026-11-01T00:00:05Z')
        run.rows = [{'pid': 10, 'sessionId': 'old', 'name': 'alpha', 'kind': 'interactive', 'cwd': '/fixture'}]
        offer(store, 'claude:new', 'alpha', 'claude:old')  # a live holder's name needs an offer
        result = confirmed_iam(claude_core(store, providers, 'new', 20), 'alpha')
    finally:
        inbox.close()
    retired = retirement_name('alpha', handles(store)['alpha']['at'])
    assert result['warnings'] == [] and result['moved_from']['session_key'] == 'claude:old'
    assert inbox.received == [rename_line('alpha'), rename_line(retired)]
    events = [e for e in store.events() if e['event'] == 'named' and e['kind'] == 'retired']
    assert [(e['binding'], e['name'], e['session_key']) for e in events] == [(old['id'], retired, 'claude:old')]
    # Done at once: the old session's next hook neither renames it again nor writes another event.
    assert adapter.mirror_output(title_event(title=retired)) == {}
    assert adapter.mirror_output(title_event(title='alpha')) == {}
    assert len([e for e in store.events() if e['event'] == 'named' and e['kind'] == 'retired']) == 1


@pytest.mark.parametrize('case', ['no_name', 'other_name', 'listing_failed', 'not_listed', 'holds_another'])
def test_retirement_waits_for_the_hook_unless_the_listing_shows_the_handles_name(rig, store, case):
    adapter, run, _ = rig
    providers = Providers({'claude': adapter}, store)
    inbox = Inbox(connections=2 if case == 'holds_another' else 1)
    try:
        start(store, socket=inbox.path)
        bind(store, providers)
        start(store, sid='new', pid=20, at='2026-11-01T00:00:05Z')
        entry = {'pid': 10, 'sessionId': 'old', 'kind': 'interactive', 'name': 'alpha'}
        if case == 'no_name':
            entry.pop('name')
        if case == 'other_name':
            entry['name'] = 'my notes'
        run.rows = [] if case == 'not_listed' else [entry]
        if case == 'listing_failed':
            run.error = OSError('injected')
        if case == 'holds_another':
            # old gives alpha up by taking beta: the mirror wins, at its next hook.
            claude_core(store, providers).run('iam', {'handle': 'beta'})
        else:
            offer(store, 'claude:new', 'alpha', 'claude:old')  # a live holder's name needs an offer
            # A holder no listing shows isn't live: the offered iam moves at once.
            core = claude_core(store, providers, 'new', 20)
            core.run('iam', {'handle': 'alpha'}) if case == 'not_listed' else confirmed_iam(core, 'alpha')
    finally:
        inbox.close()
    assert not [e for e in store.events() if e['event'] == 'named' and e['kind'] == 'retired']
    if case == 'holds_another':
        # beta's own rename is sent; alpha's retirement is the hook's, which records it done.
        assert inbox.received == [rename_line('alpha'), rename_line('beta')]
        assert adapter.mirror_output(title_event(title='beta')) == {}
        assert [e['name'] for e in store.events() if e['event'] == 'named' and e['kind'] == 'retired'] == [None]
        return
    assert inbox.received == [rename_line('alpha')]
    left_at = list(store.rows('handles.jsonl'))[-1]['at']
    title = adapter.mirror_output(title_event(title='alpha'))['hookSpecificOutput']['sessionTitle']
    assert title == retirement_name('alpha', left_at)


def test_release_retires_at_once(rig, store):
    adapter, run, _ = rig
    providers = Providers({'claude': adapter}, store)
    inbox = Inbox(connections=2)
    try:
        start(store, socket=inbox.path)
        bind(store, providers)
        run.rows = [{'pid': 10, 'sessionId': 'old', 'name': 'alpha', 'kind': 'interactive'}]
        result = claude_core(store, providers).run('release', {})
    finally:
        inbox.close()
    release = list(store.rows('handles.jsonl'))[-1]
    assert result['handle'] == 'alpha' and result['warnings'] == [] and release['reason'] == 'release'
    assert inbox.received == [rename_line('alpha'), rename_line(retirement_name('alpha', release['at']))]
    assert [e['name'] for e in store.events() if e['event'] == 'named' and e['kind'] == 'retired'] == [
        retirement_name('alpha', release['at'])]


@pytest.mark.parametrize('reason', ['fresh-start', 'rewind', 'moved-background'])
def test_hook_moves_send_no_rename_line(rig, store, reason):
    """Hook moves are named by the hooks' sessionTitle, as before; nothing is sent on a socket."""
    adapter, run, _ = rig
    made = []
    adapter.socket_factory = lambda *args: made.append(args) or FaultSocket('ok')
    start(store, socket='/fixture/old.sock')
    start(store, sid='new', at='2026-11-01T00:00:05Z', socket='/fixture/new.sock')
    run.rows = [{'pid': 10, 'sessionId': 'new', 'name': 'alpha'}, {'pid': 10, 'sessionId': 'old', 'name': 'alpha'}]
    old = {'at': '2026-11-01T00:00:01Z', 'id': 'binding-old', 'handle': 'alpha', 'reason': 'iam', 'session_id': 'old',
           'session_key': 'claude:old', 'provider': 'claude', 'by_session': 'old', 'by_session_key': 'claude:old',
           'pid': 10, 'process_started_at': START, 'moved_from': None, 'via': None, 'also_released': None}
    store.append('handles.jsonl', old)
    line = {**old, 'at': '2026-11-01T00:00:06Z', 'id': 'binding-new', 'reason': reason, 'session_id': 'new',
            'session_key': 'claude:new', 'by_session': 'new', 'by_session_key': 'claude:new',
            'moved_from': {'session_id': 'old', 'provider': 'claude', 'session_key': 'claude:old'}}
    store.append('handles.jsonl', line)
    assert Providers({'claude': adapter}, store).mirror(line) == []
    assert not made and not [c for c in run.calls if c[0][0] in {'claude', 'tmux'}]
    assert not [e for e in store.events() if e['event'] == 'named']


# ----- the window name rule ---------------------------------------------------------

WINDOW_FORMAT = '#{s/ [|] .*//:#{s/^[^ -~]+ //:pane_title}}'
DEFAULT_FORMAT = '#{?pane_in_mode,[tmux],#{pane_current_command}}#{?pane_dead,[dead],}'


def window_read(pane):
    return ['display-message', '-p', '-t', pane, '#{pid} #{start_time} #{pane_pid}', ';',
            'show-options', '-wvA', '-t', pane, 'automatic-rename', ';',
            'show-options', '-wv', '-t', pane, 'automatic-rename-format', ';',
            'show-options', '-gwv', 'automatic-rename-format']


def window_write(pane):
    return ['set-option', '-w', '-t', pane, 'automatic-rename-format', WINDOW_FORMAT]


@pytest.fixture
def tmux(rig):
    _, run, _ = rig
    run.tmux = FakeTmux()
    return run.tmux


def start_event(adapter, pane, source='startup', sid='s'):
    adapter.environ = {'CLAUDE_PID': '10', 'TMUX': '/fixture/default,7,0', 'TMUX_PANE': pane}
    return adapter.events({'hook_event_name': 'SessionStart', 'source': source, 'session_id': sid,
                           'cwd': '/fixture', 'transcript_path': '/fixture/t'})


@pytest.mark.parametrize('source', ['startup', 'resume', 'clear', 'compact'])
def test_start_hook_sets_the_window_name_rule_after_one_read(rig, store, tmux, source):
    adapter, _, _ = rig
    pane, = tmux.add('work')
    item = start_event(adapter, pane, source)
    assert item['tmux_pane'] == tmux.key(pane)
    tmux.calls.clear()
    adapter.tmux_start(item)
    assert tmux.calls == [window_read(pane), window_write(pane)]
    assert tmux.window_options[tmux.window(pane)] == {'automatic-rename-format': WINDOW_FORMAT}
    # The window has a format of its own now: setting it again changes nothing.
    adapter.tmux_start(item)
    assert tmux.calls[2:] == [window_read(pane)]
    assert not (store.root / 'hook-errors.log').exists()


@pytest.mark.parametrize('case', ['renamed', 'global_off', 'own_format', 'global_format'])
def test_window_name_or_format_the_user_set_is_kept(rig, tmux, case):
    adapter, _, _ = rig
    pane, = tmux.add('work')
    if case == 'renamed':  # rename-window turns the window's automatic-rename off
        tmux.window_options[tmux.window(pane)]['automatic-rename'] = 'off'
    if case == 'global_off':
        tmux.globals['automatic-rename'] = 'off'
    if case == 'own_format':
        tmux.window_options[tmux.window(pane)]['automatic-rename-format'] = '#{pane_current_path}'
    if case == 'global_format':
        tmux.globals['automatic-rename-format'] = '#{pane_title}'
    adapter.tmux_start({'kind': 'startup', 'pid': 10, 'tmux_pane': tmux.key(pane)})
    assert tmux.calls == [window_read(pane)] and not tmux.writes()


def test_no_window_rule_on_fork_or_outside_tmux(rig, store, tmux):
    adapter, run, _ = rig
    pane, = tmux.add('work')
    adapter.tmux_start({**start_event(adapter, pane, 'fork'), 'kind': 'fork'})
    adapter.tmux_start({'kind': 'startup', 'tmux_pane': None})
    adapter.environ = {'CLAUDE_PID': '10'}  # not in tmux
    item = adapter.events({'hook_event_name': 'SessionStart', 'source': 'startup', 'session_id': 's', 'cwd': '/fixture'})
    adapter.tmux_start(item)
    assert not [c for c in tmux.calls if c[0] != 'display-message']


@pytest.mark.parametrize('fault', ['read', 'write', 'timeout', 'stale'])
def test_window_rule_failure_is_logged_and_harmless(rig, store, tmux, fault):
    adapter, _, _ = rig
    pane, = tmux.add('work')
    key = tmux.key(pane)
    if fault == 'read':
        tmux.fail = {'show-options'}
    if fault == 'write':
        tmux.fail = {'set-option'}
    if fault == 'timeout':
        tmux.raises = subprocess.TimeoutExpired('tmux', 2)
    if fault == 'stale':
        key = f'8@42:{pane}'  # a pane key from an earlier server names another pane now
    adapter.tmux_start({'kind': 'startup', 'pid': 10, 'tmux_pane': key})
    assert 'tmux window name' in (store.root / 'hook-errors.log').read_text()
    assert tmux.window_options[tmux.window(pane)] == {}


def test_iam_sets_the_window_rule_for_the_bindings_pane(rig, store, tmux):
    adapter, _, _ = rig
    pane, = tmux.add('work')
    start(store, tmux_pane=tmux.key(pane))  # no socket: the rename waits for the hook; the window rule doesn't
    assert bind(store, adapter)['warnings'] == []
    assert tmux.calls == [window_read(pane), window_write(pane)]


def test_window_rule_only_in_the_start_hook(store, monkeypatch):
    """Through the hook entry: the start hook writes the rule; the constant hooks make no window name call."""
    from kaiusap.hooks import dispatch
    monkeypatch.setenv('KAIUSAP_HOME', str(store.root))
    tmux = FakeTmux()
    pane, = tmux.add('work')
    run = Runner()
    run.tmux = tmux
    monkeypatch.setattr('kaiusap.adapters.claude.run_command', run)
    for name, value in {'CLAUDE_PID': '10', 'TMUX': '/fixture/default,7,0', 'TMUX_PANE': pane}.items():
        monkeypatch.setenv(name, value)
    data = {'session_id': 's', 'cwd': '/fixture', 'source': 'startup', 'transcript_path': '/fixture/t', 'prompt': 'Hello'}
    dispatch('claude', 'SessionStart', data, time.monotonic() + 3)
    assert window_write(pane) in tmux.calls
    tmux.calls.clear()
    for event in ('UserPromptSubmit', 'PreToolUse'):
        dispatch('claude', event, {**data, 'tool_name': 'Bash', 'tool_input': {'command': 'ls'}}, time.monotonic() + 3)
    assert tmux.calls == []


# ----- the tmux session name, on unless turned off -----------------------------------

def follow_setting(store, value='follow'):
    store.root.mkdir(parents=True, exist_ok=True)
    (store.root / 'settings.json').write_text(json.dumps({'tmux_session_names': value}))


def session_calls(tmux):
    return [c for c in tmux.calls if c[0] in {'list-panes', 'rename-session'}]


def test_tmux_session_names_follows_by_default(rig, store, tmux):
    adapter, _, _ = rig
    pane, = tmux.add('0')
    inbox = Inbox()
    try:
        start(store, socket=inbox.path, tmux_pane=tmux.key(pane))
        assert bind(store, adapter)['warnings'] == []
    finally:
        inbox.close()
    assert not (store.root / 'settings.json').exists()
    assert tmux.name(pane) == 'alpha'


def test_tmux_session_names_off_does_nothing(rig, store, tmux):
    adapter, _, _ = rig
    follow_setting(store, 'off')
    pane, = tmux.add('0')
    inbox = Inbox()
    try:
        start(store, socket=inbox.path, tmux_pane=tmux.key(pane))
        bind(store, adapter)
    finally:
        inbox.close()
    adapter.mirror_output(title_event())
    adapter.mirror_output({**title_event('startup', title='alpha'), 'tmux_pane': tmux.key(pane)})
    assert session_calls(tmux) == [] and tmux.name(pane) == '0' and tmux.titles(pane) == {}


def test_iam_renames_a_single_pane_tmux_session_once_the_name_is_applied(rig, store, tmux):
    adapter, _, _ = rig
    follow_setting(store)
    pane, = tmux.add('0')
    inbox = Inbox()
    try:
        start(store, socket=inbox.path, tmux_pane=tmux.key(pane))
        assert bind(store, adapter)['warnings'] == []
    finally:
        inbox.close()
    assert tmux.name(pane) == 'alpha'
    assert session_calls(tmux) == [
        ['list-panes', '-s', '-t', pane, '-F', '#{pid}\t#{start_time}\t#{pane_id}\t#{pane_pid}\t#{session_id}\t#{session_name}\t#{S:#{session_name}:}'],
        ['rename-session', '-t', '$0', 'alpha']]
    assert tmux.titles(pane) == {'set-titles': 'on', 'set-titles-string': '#S'}
    assert ['set-option', '-t', '$0', 'set-titles', 'on', ';', 'set-option', '-t', '$0', 'set-titles-string', '#S'] in tmux.calls


@pytest.mark.parametrize('case', ['already_named', 'collision'])
def test_session_titles_are_set_when_no_rename_is_needed_or_possible(rig, store, tmux, case):
    """The title options follow the rule, not the rename: a tmux session already named, or one whose
    rename is skipped for a collision, still gets them."""
    adapter, _, _ = rig
    pane, = tmux.add('alpha' if case == 'already_named' else '0')
    if case == 'collision':
        tmux.add('alpha')
    start(store, tmux_pane=tmux.key(pane))
    bind(store, adapter)
    adapter.mirror_output(title_event())
    assert tmux.name(pane) == ('alpha' if case == 'already_named' else '0')
    assert tmux.titles(pane) == {'set-titles': 'on', 'set-titles-string': '#S'}
    assert not [c for c in tmux.calls if c[0] == 'rename-session']


def test_session_titles_failure_is_logged_and_the_rename_stands(rig, store, tmux):
    adapter, _, _ = rig
    pane, = tmux.add('0')
    tmux.fail = {'set-option'}
    start(store, tmux_pane=tmux.key(pane))
    bind(store, adapter)
    adapter.mirror_output(title_event())
    assert tmux.name(pane) == 'alpha' and tmux.titles(pane) == {}
    assert 'tmux session titles' in (store.root / 'hook-errors.log').read_text()


def test_rename_line_not_written_leaves_the_tmux_session_to_the_hook(rig, store, tmux):
    adapter, _, _ = rig
    follow_setting(store)
    pane, = tmux.add('0')
    start(store, tmux_pane=tmux.key(pane))  # no socket
    bind(store, adapter)
    assert tmux.name(pane) == '0'  # the tmux session never shows a name the session doesn't have yet
    assert adapter.mirror_output(title_event())['hookSpecificOutput']['sessionTitle'] == 'alpha'
    assert tmux.name(pane) == 'alpha'


def test_hook_retirement_renames_the_tmux_session(rig, store, tmux):
    adapter, _, _ = rig
    follow_setting(store)
    pane, = tmux.add('alpha')
    start(store, tmux_pane=tmux.key(pane))
    bind(store, adapter)
    bind(store, adapter, 'new')
    left_at = binding_history(store)[0][0][1]
    assert adapter.mirror_output(title_event(title='alpha'))['hookSpecificOutput']['sessionTitle'] == retirement_name('alpha', left_at)
    assert tmux.name(pane) == retirement_name('alpha', left_at)


@pytest.mark.parametrize('panes,windows', [(2, 1), (2, 2)])
def test_tmux_session_with_more_than_one_pane_is_never_renamed(rig, store, tmux, panes, windows):
    adapter, _, _ = rig
    follow_setting(store)
    pane = tmux.add('0', panes=panes, windows=windows)[0]
    start(store, tmux_pane=tmux.key(pane))
    bind(store, adapter)
    adapter.mirror_output(title_event())
    assert tmux.name(pane) == '0' and not [c for c in tmux.calls if c[0] == 'rename-session']
    assert tmux.titles(pane) == {}


def test_retirement_renames_before_the_mirror_in_one_call(rig, store, tmux):
    """A handle moving between two tmux sessions: the retirement frees the name first."""
    adapter, run, _ = rig
    follow_setting(store)
    providers = Providers({'claude': adapter}, store)
    old_pane, = tmux.add('0')
    new_pane, = tmux.add('1')
    old_inbox, new_inbox = Inbox(connections=2), Inbox()
    try:
        start(store, socket=old_inbox.path, tmux_pane=tmux.key(old_pane))
        bind(store, providers)
        assert tmux.name(old_pane) == 'alpha'
        tmux.pane_pids[new_pane] = 20  # the new session's process runs directly in its pane
        start(store, sid='new', pid=20, at='2026-11-01T00:00:05Z', socket=new_inbox.path, tmux_pane=tmux.key(new_pane))
        run.rows = [{'pid': 10, 'sessionId': 'old', 'name': 'alpha', 'kind': 'interactive'}]
        offer(store, 'claude:new', 'alpha', 'claude:old')  # a live holder's name needs an offer
        result = confirmed_iam(claude_core(store, providers, 'new', 20), 'alpha')
    finally:
        old_inbox.close()
        new_inbox.close()
    retired = retirement_name('alpha', handles(store)['alpha']['at'])
    assert result['warnings'] == []
    assert (tmux.name(old_pane), tmux.name(new_pane)) == (retired, 'alpha')
    renames = [c[3] for c in tmux.calls if c[0] == 'rename-session']
    assert renames == ['alpha', retired, 'alpha']


def test_tmux_name_collision_is_skipped_with_iams_warning(rig, store, tmux):
    adapter, _, _ = rig
    follow_setting(store)
    tmux.add('alpha')  # the user's own tmux session already has the name
    pane, = tmux.add('0')
    inbox = Inbox()
    try:
        start(store, socket=inbox.path, tmux_pane=tmux.key(pane))
        result = bind(store, adapter)
    finally:
        inbox.close()
    warning = 'tmux session `alpha` already exists, so this tmux session keeps its name.'
    assert result['warnings'] == [warning] and tmux.name(pane) == '0'
    assert not [c for c in tmux.calls if c[0] == 'rename-session']
    assert warning in (store.root / 'hook-errors.log').read_text()


@pytest.mark.parametrize('source', ['startup', 'resume'])
def test_start_renames_a_tmux_session_with_another_name(rig, store, tmux, source):
    """At start: a session holding a handle, named the handle, in a tmux session named `0` after a reboot."""
    adapter, _, _ = rig
    follow_setting(store)
    pane, = tmux.add('0')
    inbox = Inbox()
    try:
        start(store, socket=inbox.path)
        bind(store, adapter)
    finally:
        inbox.close()
    item = {**title_event(source, title='alpha'), 'pid': 10, 'tmux_pane': tmux.key(pane)}
    assert adapter.mirror_output(item) == {}  # already named: nothing to apply...
    assert tmux.name(pane) == 'alpha'  # ...but the tmux session follows it
    for kind in ('clear', 'compact', 'fork'):
        tmux.sessions['$0'] = '0'
        adapter.mirror_output({**title_event(kind, title='alpha'), 'pid': 10, 'tmux_pane': tmux.key(pane)})
        assert tmux.name(pane) == '0', kind


def test_prompt_hook_renames_only_within_its_budget(rig, store, tmux):
    """The prompt hook's rename is new work after the name is committed: past 50 ms it waits for the next start."""
    from kaiusap.hooks import HookBudget
    adapter, _, _ = rig
    follow_setting(store)
    pane, = tmux.add('0')
    start(store, tmux_pane=tmux.key(pane))
    bind(store, adapter)

    class Spent(HookBudget):
        def check(self):
            if self.committed:  # the name's event is written; time ran out meanwhile
                raise TimeoutError('hook admission deadline reached')

    store.hook_budget = Spent(time.monotonic() + 1, time.monotonic() + 2)
    out = adapter.mirror_output(title_event())
    assert out['hookSpecificOutput']['sessionTitle'] == 'alpha'  # the committed name is still given
    assert tmux.name(pane) == '0' and not session_calls(tmux)
    assert 'tmux session name for claude:old skipped' in (store.root / 'hook-errors.log').read_text()
    store.hook_budget = None
    adapter.mirror_output({**title_event('startup', title='alpha'), 'pid': 10, 'tmux_pane': tmux.key(pane)})
    assert tmux.name(pane) == 'alpha'


def test_user_rename_is_not_followed(rig, store, tmux):
    adapter, _, _ = rig
    follow_setting(store)
    pane, = tmux.add('0')
    start(store, tmux_pane=tmux.key(pane))
    bind(store, adapter)
    adapter.mirror_output(title_event())
    assert tmux.name(pane) == 'alpha'
    tmux.calls.clear()
    adapter.mirror_output(title_event(title='my notes'))  # the user's /rename ends the attempt
    adapter.mirror_output(title_event(title='my notes'))
    adapter.mirror_output({**title_event('startup', title='my notes'), 'tmux_pane': tmux.key(pane)})
    assert tmux.name(pane) == 'alpha' and not session_calls(tmux)


@pytest.mark.parametrize('value', ['always', True, None, 1])
def test_malformed_tmux_session_names_means_follow(rig, store, tmux, value):
    from kaiusap.model import load_settings
    adapter, _, _ = rig
    follow_setting(store, value)
    assert load_settings(store)['tmux_session_names'] == 'follow'
    assert 'Malformed settings; using defaults.' in store.warnings
    pane, = tmux.add('0')
    start(store, tmux_pane=tmux.key(pane))
    bind(store, adapter)
    adapter.mirror_output(title_event())
    assert tmux.name(pane) == 'alpha'
    follow_setting(store, 'follow')
    assert load_settings(store)['tmux_session_names'] == 'follow'


# ----- a Claude pane is used only when it holds the session -----------------

@pytest.mark.parametrize('holder', [10, 1])  # the Claude process itself, or an ancestor (the pane's shell)
def test_pane_holding_the_session_is_used(rig, store, tmux, holder):
    adapter, run, _ = rig
    follow_setting(store)
    pane, = tmux.add('0')
    tmux.pane_pids[pane] = holder
    adapter.tmux_start({'kind': 'startup', 'pid': 10, 'tmux_pane': tmux.key(pane)})
    assert tmux.window_options[tmux.window(pane)] == {'automatic-rename-format': WINDOW_FORMAT}
    assert adapter.follow_session_name(tmux.key(pane), 'alpha', 10) == [] and tmux.name(pane) == 'alpha'
    assert ['ps', '-A', '-o', 'pid=,ppid='] in [c[0] for c in run.calls]


@pytest.mark.parametrize('pid', [10, None])
def test_inherited_pane_is_left_alone(rig, store, tmux, pid):
    """A background worker's TMUX_PANE names the dispatching shell's pane: no window name, no tmux session rename, no log."""
    adapter, _, _ = rig
    follow_setting(store)
    pane, = tmux.add('shell')
    tmux.pane_pids[pane] = 555  # the dispatching shell, not an ancestor of process 10
    adapter.tmux_start({'kind': 'startup', 'pid': pid, 'tmux_pane': tmux.key(pane)})
    assert adapter.follow_session_name(tmux.key(pane), 'alpha', pid) == []
    assert not tmux.writes() and tmux.name(pane) == 'shell'
    assert tmux.window_options[tmux.window(pane)] == {}
    assert not (store.root / 'hook-errors.log').exists()


def test_pane_check_failure_is_logged_and_harmless(rig, store, tmux):
    adapter, run, _ = rig
    follow_setting(store)
    pane, = tmux.add('0')
    real = run.__call__

    class FailingPs:
        def __call__(self, args, limit, env):
            return (1, '') if args[:2] == ['ps', '-A'] else real(args, limit, env)
    adapter.run = FailingPs()
    adapter.tmux_start({'kind': 'startup', 'pid': 10, 'tmux_pane': tmux.key(pane)})
    assert adapter.follow_session_name(tmux.key(pane), 'alpha', 10) == []
    assert not tmux.writes()
    log = (store.root / 'hook-errors.log').read_text()
    assert 'tmux window name' in log and 'tmux session name' in log


@pytest.mark.parametrize("name", ["builder", "builder~261108233000", "first pane", "it's", "=odd"])
def test_attach_line_names_the_tmux_session_exactly(name):
    # `-t '=<session>'`. The quotes are always written: zsh expands an unquoted word that starts
    # with `=`, and shlex.quote leaves `=name` bare.
    import shlex
    from kaiusap.adapters.tmux import attach_command
    line = attach_command(name)
    prefix = "tmux -L default attach -t "
    assert line.startswith(prefix + "'=")
    assert shlex.split(line) == ["tmux", "-L", "default", "attach", "-t", "=" + name]


def confirmed_iam(core, handle):
    """An offered `iam` while the holder's session runs: refused once for the user's confirmation, then taken."""
    with pytest.raises(Error) as refused:
        core.run('iam', {'handle': handle})
    assert refused.value.reason_code == 'confirmation_needed' and refused.value.code == 1
    return core.run('iam', {'handle': handle, 'confirmed': True})


def offer(store, key, handle, holder):
    """The start hook's offer of `handle` to session `key`, so `iam` may take it from a live holder."""
    from kaiusap.model import PERSONAS
    from kaiusap.store import now
    store.append(PERSONAS, {"at": now(), "session_key": key, "offered": handle, "from": holder})


def test_a_clear_in_a_session_with_no_handle_says_nothing_and_writes_no_persona_line(rig, store):
    adapter, run, _ = rig
    start(store)
    run.rows = [{'pid': 10, 'sessionId': 'new'}]
    result = follow(store, Providers({'claude': adapter}, store), event(store), deadline=time.monotonic() + 3)
    assert result['texts'] == [] and result['shown'] == [] and not result['moved']
    assert not [r for r in store.rows('sessions/personas.jsonl') if r.get('continues')]


def test_harness_version_is_the_running_version_from_the_newest_transcript_line(store, tmp_path):
    # The version on the transcript's newest line that has one, which the running process writes; null when
    # the session isn't live, never the version of an older line.
    import importlib.util
    import json as _json
    from pathlib import Path
    from kaiusap.store import now
    path = Path(__file__).parent / "contract" / "claude.py"
    spec = importlib.util.spec_from_file_location("contract_fixture_claude_version", path)
    fixture = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fixture)
    world = fixture.World(store, tmp_path)
    try:
        transcript = tmp_path / "t.jsonl"
        transcript.write_text("\n".join(_json.dumps(line) for line in [
            {"type": "user", "version": "2.1.300"}, {"type": "assistant", "version": "2.1.319"},
            {"type": "cost-state"}]) + "\n")
        key = f"claude:{fixture.SESSION}"
        store.start({"at": now(), "provider": "claude", "session_id": fixture.SESSION, "session_key": key,
                     "source": "startup", "cwd": "/fixture", "transcript": str(transcript), "pid": fixture.PID})
        assert world.adapter.harness_version(key) is None  # no Claude process holds it
        world.reachable()
        world.adapter.reset()
        assert world.adapter.harness_version(key) == "2.1.319"
    finally:
        world.close()


# ----- activity, from the activity hooks' records and the transcripts -----------------------------------


def activity_world(store, tmp_path):
    import importlib.util
    path = Path(__file__).parent / "contract" / "claude.py"
    spec = importlib.util.spec_from_file_location("contract_fixture_claude_activity", path)
    fixture = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fixture)
    return fixture, fixture.World(store, tmp_path)


def at(seconds):
    """A time on 2026-11-13, after the fixture's process started (2026-10-01)."""
    return f"2026-11-13T12:00:{seconds:06.3f}Z"


def user(seconds, text="a prompt", **extra):
    return {"type": "user", "timestamp": at(seconds), "message": {"role": "user", "content": text}, **extra}


def assistant(seconds):
    return {"type": "assistant", "timestamp": at(seconds), "message": {"role": "assistant", "content": []}}


def ask(seconds, tool="Bash", agent=None, kind="PermissionRequest"):
    return {"at": at(seconds), "kind": kind, "agent_id": agent, "tool": tool}


def stop(seconds, kind="Stop"):
    return {"at": at(seconds), "kind": kind, "agent_id": None, "tool": None}


INTERRUPT_LINE = [{"type": "text", "text": "[Request interrupted by user for tool use]"}]
SUBAGENT = "a6cb7233048c11b8d"


@pytest.mark.parametrize("records, lines, subagent, expected", [
    ([], [user(1)], None, None),  # no record since the process started: its hooks aren't loaded, or no turn ended
    ([stop(2)], [user(1), assistant(2)], None, ("idle", [])),
    ([stop(2)], [user(1), assistant(2), user(3)], None, ("working", [])),  # a new prompt after the turn ended
    ([stop(2)], [user(1), assistant(2), user(3, INTERRUPT_LINE)], None, ("idle", [])),  # an interrupt
    ([stop(1), ask(3)], [user(2), assistant(2.5)], None, ("blocked", ["approval"])),  # the dialog is open
    ([stop(1), ask(3, tool="AskUserQuestion")], [user(2), assistant(2.5)], None, ("blocked", ["user_input"])),
    ([stop(1), ask(3, tool=None, kind="Elicitation")], [user(2)], None, ("blocked", ["user_input"])),
    ([stop(1), ask(3)], [user(2), assistant(2.5), user(4, [{"type": "tool_result", "content": "ok"}])], None,
     ("working", [])),  # approved and done: the tool result is newer than the ask
    ([stop(1), ask(3)], [user(2), user(4, "The user doesn't want to proceed"), user(4.001, INTERRUPT_LINE)], None,
     ("idle", [])),  # rejected: no hook, only the transcript's interrupt
    ([stop(1), ask(3, agent=SUBAGENT)], [user(2), assistant(5)], [assistant(2.5)], ("blocked", ["approval"])),
    ([stop(1), ask(3, agent=SUBAGENT)], [user(2)], [assistant(2.5), user(4, "denied")], ("working", [])),
    ([stop(1), ask(3, agent=SUBAGENT), ask(4)], [user(2)], [assistant(2.5)], ("blocked", ["approval"])),
    ([stop(1), ask(3, agent=SUBAGENT, tool="AskUserQuestion"), ask(4)], [user(2)], [assistant(2.5)],
     ("blocked", ["approval", "user_input"])),  # two askers wait
    ([stop(1), ask(3, agent=SUBAGENT)], [user(2)], None, None),  # the subagent's transcript can't be read
    ([stop(1), stop(3, "StopFailure")], [user(2)], None, ("error", [])),
    ([stop(1), stop(3, "StopFailure")], [user(2), user(4)], None, ("working", [])),  # the user went on
    ([stop(2)], [], None, None),  # an empty transcript
    ([stop(2)], [user(1), assistant(2), user(3, "summary", isCompactSummary=True, isVisibleInTranscriptOnly=True)],
     None, ("idle", [])),  # compacted while idle: the summary isn't a prompt
    ([stop(2)], [user(1), assistant(2), user(3, "summary", isCompactSummary=True), user(4)], None,
     ("working", [])),  # a prompt after the compaction
    ([stop(1)], [user(2), user(3, "summary", isCompactSummary=True), assistant(4)], None,
     ("working", [])),  # compacted mid-turn: the turn's prompt still counts until its Stop
])
def test_activity_from_the_records_and_the_transcripts(store, tmp_path, records, lines, subagent, expected):
    from kaiusap.adapters.claude import ACTIVITY_FILE
    from kaiusap.store import now
    fixture, world = activity_world(store, tmp_path)
    try:
        transcript = tmp_path / f"{fixture.SESSION}.jsonl"
        if lines:
            transcript.write_text("".join(json.dumps(line) + "\n" for line in lines) + '{"type": "last-prompt"}\n')
        if subagent:
            folder = tmp_path / fixture.SESSION / "subagents"
            folder.mkdir(parents=True)
            (folder / f"agent-{SUBAGENT}.jsonl").write_text("".join(json.dumps(line) + "\n" for line in subagent))
        key = f"claude:{fixture.SESSION}"
        store.start({"at": now(), "provider": "claude", "session_id": fixture.SESSION, "session_key": key,
                     "source": "startup", "cwd": "/fixture", "transcript": str(transcript), "pid": fixture.PID})
        store.append(ACTIVITY_FILE, {**stop(0), "session_id": "someone-else"})
        for record in records:
            store.append(ACTIVITY_FILE, {**record, "session_id": fixture.SESSION})
        assert world.adapter.activity(key) is None  # no Claude process holds it
        world.reachable()
        world.adapter.reset()
        found = world.adapter.activity(key)
        assert (found and (found["state"], found["waiting_on"])) == expected
        assert world.adapter.activity(f"{key}/{fixture.AGENT}") is None  # a spawned agent's key
    finally:
        world.close()


def test_activity_ignores_records_from_before_the_process_started(store, tmp_path):
    from kaiusap.adapters.claude import ACTIVITY_FILE
    from kaiusap.store import now
    fixture, world = activity_world(store, tmp_path)
    try:
        transcript = tmp_path / "t.jsonl"
        transcript.write_text(json.dumps(user(1)) + "\n")
        key = f"claude:{fixture.SESSION}"
        store.start({"at": now(), "provider": "claude", "session_id": fixture.SESSION, "session_key": key,
                     "source": "startup", "cwd": "/fixture", "transcript": str(transcript), "pid": fixture.PID})
        # Before the fixture's process started (2026-10-01): a dialog of an earlier run of the session.
        store.append(ACTIVITY_FILE, {"at": "2026-09-30T12:00:00.000000Z", "kind": "PermissionRequest",
                                     "session_id": fixture.SESSION, "agent_id": None, "tool": "Bash"})
        world.reachable()
        world.adapter.reset()
        assert world.adapter.activity(key) is None
    finally:
        world.close()


@pytest.mark.parametrize("event, extra, expected", [
    ("PermissionRequest", {"tool_name": "Bash", "tool_input": {"command": "ls"}},
     {"kind": "PermissionRequest", "agent_id": None, "tool": "Bash"}),
    ("PermissionRequest", {"tool_name": "AskUserQuestion", "agent_id": SUBAGENT},
     {"kind": "PermissionRequest", "agent_id": SUBAGENT, "tool": "AskUserQuestion"}),
    ("Stop", {"stop_hook_active": False, "agent_id": ""}, {"kind": "Stop", "agent_id": None, "tool": None}),
    ("StopFailure", {"error": "api_error"}, {"kind": "StopFailure", "agent_id": None, "tool": None}),
    ("Elicitation", {"mcp_server_name": "x", "tool_name": "ignored"}, {"kind": "Elicitation", "agent_id": None, "tool": None}),
    ("Stop", {"session_id": "../x"}, None),  # an unsafe session id owes no record
    ("Stop", {"agent_id": 3}, None),
])
def test_the_native_part_writes_the_activity_records_and_prints_nothing(store, tmp_path, event, extra, expected):
    # `activity`: record-only constant hooks, written by the native part alone, with no output, so Stop
    # never keeps Claude from stopping and PermissionRequest never decides.
    binary = Path(sys.executable).parent / "kaiusap"
    payload = {"session_id": "s-1", "transcript_path": "/t.jsonl", "cwd": "/w", "permission_mode": "default",
               "hook_event_name": event, **extra}
    result = subprocess.run([str(binary), "hook", "claude", event], input=json.dumps(payload), text=True,
                            capture_output=True, timeout=10, env={"KAIUSAP_HOME": str(store.root), "PATH": "/usr/bin:/bin"})
    assert (result.returncode, result.stdout, result.stderr) == (0, "", "")
    path = store.root / "sessions/claude/activity.jsonl"
    rows = [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []
    if expected is None:
        assert rows == []
        return
    assert len(rows) == 1 and {k: v for k, v in rows[0].items() if k != "at"} == {"session_id": "s-1", **expected}
    assert list(rows[0]) == ["at", "kind", "session_id", "agent_id", "tool"]
    assert not (store.root / "hook-errors.log").exists()
