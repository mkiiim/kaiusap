from io import StringIO
import json
import os
import subprocess
import sys

import pytest

from conftest import send
from kaiusap.adapters import Identity
from kaiusap.cli import main


def invoke(store, argv, *, identity=None, provider=None, text=''):
    out, err = StringIO(), StringIO()
    code = main(argv, store=store, identity=identity or Identity(), provider=provider,
                stdin=StringIO(text), stdout=out, stderr=err, environ={})
    return code, out.getvalue(), err.getvalue()


@pytest.mark.parametrize('argv,code', [
    (['bad'], 64), (['read'], 1), (['send', '--unknown'], 64),
    (['send', '--to'], 64), (['send', '--reply-needed', 'maybe'], 64),
    (['send', '--body', 'x'], 1), (['reply', 'x', '--subject', 'x'], 1),
    (['read', 'x', '--all'], 1), (['send', '--request', '@-', '--context', '@-'], 64),
    (['send', '--context', '@/no/such/field'], 64), (['send', '--all=yes'], 64),
])
def test_exit_contract(store, argv, code):
    result, out, err = invoke(store, argv)
    assert result == code and out == '' and err.startswith('kaiusap: ')
    result, out, err = invoke(store, argv + ['--json'])
    assert result == code and err == ''
    assert json.loads(out)['code'] == code
    assert json.loads(out)['schema'] == 1


def test_plain_shell_shape(store):
    code, out, err = invoke(store, ['whoami', '--json'])
    r = json.loads(out)
    assert code == 0 and not err
    assert r['plain_shell'] and r['classification'] == 'plain_shell'
    assert all(v is None for k, v in r.items() if k not in {'schema', 'plain_shell', 'classification', 'kaiusap'})
    assert set(r['kaiusap']) == {'version', 'commit', 'commit_date', 'dirty'}  # which kaiusap answered


STOP = ("If you are an assistant, kaiusap can't tell which session you are: stop and tell the user. "
        "Don't read kaiusap's files unless the user asks you to.")


def test_plain_shell_tells_an_assistant_to_stop(store):
    """The plain-shell answer and refusal end with the stop sentences."""
    code, out, err = invoke(store, ['whoami'])
    assert code == 0 and out.strip() == "This is a plain shell, not an assistant session. " + STOP
    code, out, err = invoke(store, ['iam', 'alpha'])
    assert code == 1 and err.strip() == ("kaiusap: This is a plain shell, not an assistant session. "
                                         "Run this verb from an assistant session. " + STOP)


def test_message_prefix_errors(pair, store):
    send(pair[0])
    send(pair[0])
    assert invoke(store, ['read', '2'])[0] == 2
    assert invoke(store, ['read', 'missing'])[0] == 3


def test_cli_send_stored_and_paste_line(store):
    from kaiusap.adapters import Providers, UnavailableProvider
    from kaiusap.adapters.codex import CodexAdapter
    adapter = CodexAdapter(store, run=lambda argv, limit, env: (1, ''))
    providers = Providers({'codex': adapter, 'claude': UnavailableProvider()}, store)
    a = Identity('codex', 'a', 'codex:a', '/fixture', 'root')
    b = Identity('codex', 'b', 'codex:b', '/fixture', 'root')
    assert invoke(store, ['iam', 'alpha'], identity=a, provider=providers)[0] == 0
    assert invoke(store, ['iam', 'beta'], identity=b, provider=providers)[0] == 0
    code, out, err = invoke(store, ['send', '--to', 'beta', '--subject', 'Note', '--reply-needed', 'no', '--context', '@@literal'], identity=a, provider=providers)
    assert code == 0 and not err
    lines = out.splitlines()
    # No daemon: a loaded thread is live with or without a pane, so the daemon is the first test.
    assert lines[0].startswith("stored (the Codex daemon couldn't be found) [")
    assert lines[0].endswith("]. Paste this into beta's session:")
    assert lines[1].startswith('Run kaiusap read ') and len(lines) == 2
    assert list(store.messages())[0]['fields']['context'] == '@literal'
    assert [e['event'] for e in store.events() if e.get('message_id')] == ['attempt', 'delivery']


def test_file_and_stdin_fields(pair, store, tmp_path):
    path = tmp_path / 'field.txt'
    path.write_text('Expected result')
    code, out, err = invoke(store, ['send', '--to', 'beta', '--subject', 'Test', '--reply-needed', 'yes', '--request', '@-', '--expected-output', '@' + str(path), '--json'], identity=pair[0].identity, provider=pair[0].provider, text='Request')
    assert code == 0 and not err
    assert store.message(json.loads(out)['message_id'])['fields']['request'] == 'Request'


def test_store_failure_exit_four(pair, store, monkeypatch):
    def fail(message):
        raise PermissionError('injected')
    monkeypatch.setattr(store, 'save_message', fail)
    code, out, err = invoke(store, ['send', '--to', 'beta', '--subject', 'x', '--reply-needed', 'no', '--context', 'x', '--json'], identity=pair[0].identity, provider=pair[0].provider)
    assert code == 4 and 'sandbox' in out and not err


@pytest.mark.parametrize('payload', ['{"tool_name":"Bash","tool_input":{"command":"echo hello"}}', '{}', 'null', 'malformed'])
def test_hook_always_zero_and_no_cli_import(tmp_path, payload):
    env = {**os.environ, 'PYTHONPATH': 'src', 'KAIUSAP_HOME': str(tmp_path / 'hook-store')}
    script = 'from kaiusap.hooks import main; import sys; code=main(["claude","PreToolUse"]); assert "kaiusap.cli" not in sys.modules; assert "kaiusap.store" not in sys.modules; sys.exit(code)'
    result = subprocess.run([sys.executable, '-c', script], input=payload, text=True, capture_output=True, env=env)
    assert result.returncode == 0 and result.stdout == '' and result.stderr == ''
    if payload.startswith('{'):
        assert not (tmp_path / 'hook-store').exists()


def test_cli_subprocess_plain_shell(tmp_path):
    env = {k: v for k, v in os.environ.items() if k not in {'CODEX_THREAD_ID', 'CLAUDE_CODE_SESSION_ID'}}
    env.update(PYTHONPATH='src', KAIUSAP_HOME=str(tmp_path / 'store'))
    r = subprocess.run([sys.executable, '-m', 'kaiusap', 'whoami', '--json'], capture_output=True, text=True, env=env)
    assert r.returncode == 0 and json.loads(r.stdout)['plain_shell']


def test_post_decision_flush_failure_never_exit_four(pair, store, monkeypatch):
    monkeypatch.setattr(os, 'fsync', lambda fd: (_ for _ in ()).throw(OSError('injected flush failure')))
    code, out, err = invoke(store, ['send', '--to', 'beta', '--subject', 'x', '--reply-needed', 'no', '--context', 'x', '--json'], identity=pair[0].identity, provider=pair[0].provider)
    assert code == 0 and not err
    result = json.loads(out)
    assert result['message_id'] and result['warnings']
    assert store.message(result['message_id'])


@pytest.mark.parametrize('payload', ['{}', '{"tool_name":"Bash","tool_input":{"command":"echo hello"}}', 'invalid', '[]'])
def test_native_installed_hook(tmp_path, payload):
    executable = os.path.join(os.path.dirname(sys.executable), 'kaiusap')
    if not os.path.exists(executable):
        pytest.skip('install the package to test the native entry point')
    env = dict(os.environ, KAIUSAP_HOME=str(tmp_path / 'native'))
    result = subprocess.run([executable, 'hook', 'claude', 'PreToolUse'], input=payload, text=True, capture_output=True, env=env)
    assert result.returncode == 0 and result.stderr == ''
    if payload.startswith('{'):
        assert result.stdout == ''
        assert not (tmp_path / 'native').exists()
    else:
        # A screen's input that can't be read gets the no-input refusal.
        from kaiusap.adapters.claude import SCREEN_TIMEOUT_TEXT
        assert json.loads(result.stdout)['hookSpecificOutput']['permissionDecisionReason'] == SCREEN_TIMEOUT_TEXT
        assert (tmp_path / 'native/hook-errors.log').exists()


def test_success_text_does_not_reread_published_message(pair, store, monkeypatch):
    monkeypatch.setattr(store, 'message', lambda *args: (_ for _ in ()).throw(OSError('injected later read failure')))
    code, out, err = invoke(store, ['send', '--to', 'beta', '--subject', 'x', '--reply-needed', 'no', '--context', 'x'], identity=pair[0].identity, provider=pair[0].provider)
    assert code == 0 and not err and 'Run kaiusap read ' in out


def test_resume_command_is_alone_on_its_line(store):
    from kaiusap.cli import render
    result = {"sessions": [{"handle": "alpha", "provider": "codex", "cwd": None, "where": "not_running",
                            "session_key": "codex:thread", "command": "codex resume thread",
                            "note": "(run it in the thread's directory)"}]}
    lines = render("sessions", result).splitlines()
    assert "codex resume thread" in lines
    assert "(run it in the thread's directory)" in lines


# A null field never prints as the word None. Column renderers (`who`,
# `sessions`) print `-`; prose (`whoami`) leaves the field out. JSON keeps null.

def test_who_with_null_name_and_directory_prints_dashes_and_json_keeps_null(pair, store):
    from conftest import FakeProvider
    code, out, err = invoke(store, ['who'], provider=FakeProvider())
    assert code == 0 and not err
    assert 'None' not in out
    lines = out.splitlines()
    assert lines[0].startswith("alpha  Codex  -  -  not reachable (the thread isn't loaded in the Codex daemon)")
    code, out, err = invoke(store, ['who', '--json'], provider=FakeProvider())
    rows = json.loads(out)['handles']
    assert code == 0 and all(row['cwd'] is None and row['name'] is None for row in rows)


def test_who_renderer_with_every_nullable_field_null():
    from kaiusap.cli import render
    row = {"handle": "bravo", "provider": "claude", "cwd": None, "name": None, "reachable": False,
           "detached": False, "why_not": None, "unread": 0, "session_key": "claude:s"}
    assert render("who", {"handles": [row]}) == "bravo  Claude  -  -  not reachable  unread: 0  claude:s"
    reachable = row | {"cwd": "/w", "name": "bravo", "reachable": True}
    assert render("who", {"handles": [reachable]}) == "bravo  Claude  /w  bravo  reachable  unread: 0  claude:s"


def test_sessions_renderer_names_the_app_hosting_a_session():
    from kaiusap.cli import render
    host = {"kind": "app", "name": "Claude"}
    row = {"handle": "decider", "provider": "claude", "cwd": None, "where": "hosted", "session_key": "claude:s",
           "command": None, "note": "Open in the Claude desktop app", "tmux_panes": [], "hosts": [host], "trusted": None}
    assert render("sessions", {"sessions": [row]}).splitlines() == [
        "decider  Claude  -  in the Claude desktop app  claude:s", "Open in the Claude desktop app"]
    row["hosts"] = [{"kind": "editor", "name": "Example Editor"}]
    assert "  in Example Editor  " in render("sessions", {"sessions": [row]})


def test_sessions_renderer_with_null_directory_prints_dash():
    from kaiusap.cli import render
    row = {"handle": None, "provider": "codex", "cwd": None, "where": "unknown", "session_key": "codex:t",
           "command": None, "note": None, "tmux_panes": [], "trusted": None}
    assert render("sessions", {"sessions": [row]}) == "(no handle)  Codex  -  unknown  codex:t"


def whoami_result(**fields):
    base = {"handle": "alpha", "provider": "codex", "cwd": None, "can_be_woken": True, "why_not": None,
            "fix": None, "unread": 0, "paused": False, "spawned": False, "parent_handle": None, "plain_shell": False}
    return base | fields


def test_whoami_renderer_leaves_out_null_fields():
    from kaiusap.cli import render
    assert render("whoami", whoami_result()) == "You are alpha (Codex). Can be woken: yes. Unread: 0."
    assert render("whoami", whoami_result(handle=None, unread=None)) == "You have no handle (Codex). Can be woken once named: yes."
    assert render("whoami", whoami_result(can_be_woken=False)) == "You are alpha (Codex). Can be woken: no. Unread: 0."
    assert render("whoami", whoami_result(can_be_woken=False, why_not="spawned agent")) == \
        "You are alpha (Codex). Can be woken: no. spawned agent. Unread: 0."
    assert render("whoami", whoami_result(can_be_woken=False, why_not="not live", fix="resume it", cwd="/w")) == \
        "You are alpha (Codex, /w). Can be woken: no. not live; resume it. Unread: 0."
    spawned = render("whoami", whoami_result(handle=None, spawned=True, parent_handle=None))
    assert spawned.startswith("You are a spawned agent (Codex). You may look")
    spawned = render("whoami", whoami_result(handle=None, spawned=True, parent_handle="alpha", cwd="/w"))
    assert spawned.startswith("You are a spawned agent of alpha (Codex, /w). You may look")


def test_delivered_line_names_no_provider():
    # The send result carries no provider, so the `delivered` line says only what kaiusap did.
    from kaiusap.cli import render
    result = {"outcome": "delivered", "message_id": "20261109T000000Z-abcdef", "recipient": "beta"}
    expected = "delivered: posted to beta's session, which decides when to read it. [20261109T000000Z-abcdef]"
    assert render("send", result) == render("reply", result) == expected


def test_no_text_renderer_prints_none_for_a_null_field():
    from kaiusap.cli import render
    results = {
        "iam": {"handle": "alpha", "already_held": False, "session_key": "codex:a", "previous_provider": None,
                "released": None, "process_changed": False, "unread": 0, "unread_from": [],
                "moved_from": {"provider": "claude", "cwd": None, "name": None}},
        "inbox": {"messages": [{"id": "20261029T180200Z-k3f9qa", "state": "unread", "at": "2026-10-29T18:02:00Z",
                                "from": "beta", "subject": "x", "in_reply_to": None, "reply_needed": False}]},
        "who": {"handles": [{"handle": "bravo", "provider": "claude", "cwd": None, "name": None, "reachable": False,
                             "detached": False, "why_not": None, "unread": 0, "session_key": "claude:s"}]},
        "whoami": whoami_result(can_be_woken=False, spawned=True, handle=None),
        "sessions": {"sessions": [{"handle": None, "provider": "codex", "cwd": None, "where": "not_running",
                                   "session_key": "codex:t", "command": None, "note": None}]},
    }
    for verb, result in results.items():
        assert "None" not in render(verb, result), verb
