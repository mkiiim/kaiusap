import json
from dataclasses import replace

import pytest

from conftest import send
from kaiusap.adapters import Identity
from kaiusap.core import Core
from kaiusap.errors import Error
from kaiusap.model import MailView, handles
from kaiusap.store import now


def test_claude_recipient_records_attempt_and_listing_failure(pair, store):
    from kaiusap.adapters import Providers
    from kaiusap.adapters.claude import ClaudeAdapter
    a, _ = pair
    adapter = ClaudeAdapter(store, run=lambda args, limit, env: (1, ''))
    providers = Providers({'codex': a.provider, 'claude': adapter}, store)
    a.provider = providers
    c = Core(store, Identity('claude', 'c', 'claude:c', '/fixture', 'root'), providers)
    c.run('iam', {'handle': 'charlie'})
    result = send(a, to='charlie')
    assert result['outcome'] == 'stored'
    assert result['reason_code'] == 'listing_failed'
    assert result['paste_line'] == 'Run kaiusap read ' + result['message_id']
    assert [e['event'] for e in store.events() if e.get('message_id') == result['message_id']] == ['attempt', 'delivery']
    assert MailView(store).outcome(store.message(result['message_id'])) == ('stored', 'listing_failed')


def test_read_reply_moot_and_handle_move(pair, store):
    a, b = pair
    mid = send(a)['message_id']
    first = b.run('read', {'id': mid[:18]})
    assert first['already_read_at'] is None and first['state'] == 'read'
    assert b.run('read', {'id': mid})['already_read_at']
    reply = b.run('reply', {'id': mid, 'reply_needed': False, 'fields': {'body': 'All good'}})
    message = store.message(reply['message_id'])
    assert message['depth'] == 1 and message['chain_id'] == mid
    assert message['subject'] == 'Re: Review contract'
    assert a.run('inbox', {'sent': True})['messages'] == []
    assert a.run('inbox', {'sent': True, 'all': True})['messages'][0]['state'] == 'answered'
    b.run('moot', {'id': mid, 'reason': 'Cancelled'})
    assert MailView(store).state(store.message(mid)) == 'moot'
    c = Core(store, replace(b.identity, session_id='c', session_key='codex:c'), b.provider)
    result = c.run('iam', {'handle': 'beta'})
    assert result['moved_from']['session_key'] == 'codex:b'
    # c is another persona given the name, so b's earlier mail stays b's.
    taken = c.run('read', {'id': mid})
    assert taken['already_read_at'] is None and taken['mine'] is False
    kept = b.run('read', {'id': mid})
    assert kept['mine'] is True and kept['for_handle'] is None and kept['no_handle'] is True


def test_atomic_move_releases_previous_handle(pair, store):
    a, b = pair
    a.run('iam', {'handle': 'beta'})
    state = handles(store)
    assert state['alpha'] is None
    assert state['beta']['session_key'] == 'codex:a'
    assert len(list(store.rows('handles.jsonl'))) == 3
    a.run('iam', {'handle': 'beta'})
    assert len(list(store.rows('handles.jsonl'))) == 3
    a.run('release', {})
    assert handles(store)['beta'] is None
    with pytest.raises(Error) as exc:
        a.run('release', {})
    assert exc.value.code == 1


def test_process_line_preserves_binding(pair, store):
    a, _ = pair
    c = Core(store, Identity('claude', 'c', 'claude:c', '/fixture', 'root', pid=10, process_started_at=now()), a.provider)
    c.run('iam', {'handle': 'charlie'})
    binding = handles(store)['charlie']['id']
    c.identity = replace(c.identity, pid=11)
    assert c.run('iam', {'handle': 'charlie'})['process_changed']
    assert handles(store)['charlie']['id'] == binding
    c.run('release', {})
    c.run('iam', {'handle': 'charlie'})
    assert handles(store)['charlie']['id'] != binding


def test_audit_read_is_not_receipt(pair, store):
    a, b = pair
    mid = send(a)['message_id']
    shell = Core(store, Identity())
    assert shell.run('read', {'id': mid})['for_handle'] == 'beta'
    assert not list(store.events())[-1]['receipt']
    assert MailView(store).state(store.message(mid)) == 'unread'
    spawned = Core(store, Identity('codex', 'child', 'codex:b/child', '/fixture', 'spawned', 'codex:b'), b.provider)
    assert spawned.run('read', {'id': mid})['for_handle'] is None
    assert list(store.events())[-1]['lineage']['parent_handle'] == 'beta'


@pytest.mark.parametrize('verb,args', [('send', {}), ('reply', {}), ('iam', {'handle': 'child'}), ('release', {}), ('moot', {}), ('unpause', {})])
def test_spawned_refused_before_writes(pair, store, verb, args):
    a, _ = pair
    a.identity = replace(a.identity, classification='spawned', parent_key='codex:b')
    before = {p: p.read_bytes() for p in store.root.rglob('*') if p.is_file()}
    with pytest.raises(Error):
        a.run(verb, args)
    assert {p: p.read_bytes() for p in store.root.rglob('*') if p.is_file()} == before


@pytest.mark.parametrize('overrides,code', [
    ({'to': 'unknown'}, 3), ({'to': '../bad'}, 3), ({'to': 'alpha'}, 1),
    ({'reply_needed': None}, 1), ({'fields': {}}, 1),
    ({'subject': 'a\nb'}, 1), ({'subject': 'x' * 81}, 1),
    ({'fields': {'request': ' ', 'expected_output': 'x'}}, 1),
    ({'fields': {'request': 'é' * 32769, 'expected_output': 'x'}}, 1),
    ({'fields': {'body': 'x'}}, 1),
])
def test_validation_no_message(pair, store, overrides, code):
    with pytest.raises(Error) as exc:
        send(pair[0], **overrides)
    assert exc.value.code == code
    assert list(store.messages()) == []


@pytest.mark.parametrize('secret', ['AKIA' + 'A' * 16, 'ghp_' + 'a' * 36, 'sk-proj-' + 'a' * 40,
                                   'sk-ant-' + 'a' * 40, 'xoxb-' + 'a' * 30,
                                   '-----BEGIN RSA PRIVATE KEY-----', 'eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.abcdef'])
def test_credentials_never_echoed_or_stored(pair, store, secret):
    with pytest.raises(Error) as exc:
        send(pair[0], subject=secret)
    assert secret not in str(exc.value)
    assert not list(store.messages())


def test_released_original_sender_refuses_reply(pair, store):
    a, b = pair
    mid = send(a)['message_id']
    a.run('release', {})
    with pytest.raises(Error) as exc:
        b.run('reply', {'id': mid, 'reply_needed': False, 'fields': {'body': 'Answer'}})
    assert exc.value.code == 3
    assert len(list(store.messages())) == 1


def test_chain_joins_inflight_but_not_stored(pair, store):
    a, b = pair
    mid = send(a)['message_id']
    store.event('attempt', message_id=mid, to_session='b', to_session_key='codex:b', binding=handles(store)['beta']['id'])
    sent = send(b, to='alpha')
    assert store.message(sent['message_id'])['depth'] == 1
    store.event('delivery', message_id=mid, outcome='stored', reason_code='not_live')
    assert store.message(send(b, to='alpha')['message_id'])['depth'] == 0


def test_pause_history_failure_is_warning(pair, store, monkeypatch):
    def fail(*args, **kwargs):
        raise PermissionError('denied')
    monkeypatch.setattr(store, 'event', fail)
    assert pair[0].run('pause', {})['warnings']
    assert store.paused
    assert pair[0].run('unpause', {})['warnings']
    assert not store.paused


def test_malformed_settings_fall_back(pair, store):
    (store.root / 'settings.json').write_text(json.dumps({'max_depth': True}))
    assert pair[0].settings()['max_depth'] == 6
    assert store.warnings


def test_loop_limit_defaults(pair):
    settings = pair[0].settings()
    assert (settings['max_depth'], settings['max_per_pair_per_hour'], settings['max_per_hour']) == (6, 240, 600)


@pytest.mark.parametrize('value', [' ', 7])
def test_a_malformed_provider_setting_means_the_defaults(pair, store, value):
    # A provider's own setting is checked like the base's.
    (store.root / 'settings.json').write_text(json.dumps({'opencode2_command': value, 'max_depth': 3}))
    settings = pair[0].settings()
    assert settings['opencode2_command'] == 'opencode' and settings['max_depth'] == 6
    assert store.warnings


def test_reading_settings_imports_no_adapter(tmp_path):
    import subprocess, sys
    code = ("import sys; from kaiusap.store import Store; from kaiusap.model import load_settings; "
            f"load_settings(Store({str(tmp_path)!r})); "
            "print(sorted(m for m in sys.modules if m.startswith('kaiusap.adapters.')))")
    assert subprocess.check_output([sys.executable, '-c', code], text=True).strip() == '[]'


def test_whoami_store_probe_warns_without_failing(pair, store, monkeypatch):
    import os
    original = os.open
    def deny_probe(path, *args, **kwargs):
        if str(path) == str(store.root / 'lock'):
            raise PermissionError('injected')
        return original(path, *args, **kwargs)
    monkeypatch.setattr(os, 'open', deny_probe)
    result = pair[0].run('whoami', {})
    assert result['handle'] == 'alpha'
    assert 'sandbox' in result['warnings'][0]
    # The sandbox hides tmux and sockets too, so liveness is unknown, not "no".
    assert result['can_be_woken'] is None and result['reason_code'] == 'sandboxed'
    from kaiusap.cli import render
    assert 'Can be woken: unknown.' in render('whoami', dict(result))


# ----- a prompt in the sending session ends the chain ------

def prompt_line(store, key, at=None):
    provider, session = key.split(':', 1)
    store.append('sessions/prompts.jsonl', {'at': at or now(), 'provider': provider, 'session_id': session,
                                            'session_key': key, 'source': 'prompt'})


def delivered(store, mid, key):
    store.event('attempt', message_id=mid, to_session=key.split(':', 1)[1], to_session_key=key,
                binding=handles(store)['beta']['id'])


@pytest.mark.parametrize('prompted', [False, True])
def test_reply_after_a_prompt_starts_a_new_chain(pair, store, prompted):
    a, b = pair
    mid = send(a)['message_id']
    if prompted:
        prompt_line(store, 'codex:b')
    reply = store.message(b.run('reply', {'id': mid, 'reply_needed': False, 'fields': {'body': 'Done'}})['message_id'])
    assert reply['in_reply_to'] == mid and reply['subject'] == 'Re: Review contract'
    if prompted:
        assert (reply['chain_id'], reply['depth']) == (reply['id'], 0)
    else:
        assert (reply['chain_id'], reply['depth']) == (mid, 1)


@pytest.mark.parametrize('prompted', [False, True])
def test_joining_send_after_a_prompt_starts_a_new_chain(pair, store, prompted):
    a, b = pair
    mid = send(a)['message_id']
    delivered(store, mid, 'codex:b')
    if prompted:
        prompt_line(store, 'codex:b')
    sent = store.message(send(b, to='alpha')['message_id'])
    assert sent['in_reply_to'] is None
    assert (sent['chain_id'], sent['depth']) == ((sent['id'], 0) if prompted else (mid, 1))


def test_prompt_before_the_ancestor_or_in_another_session_keeps_the_chain(pair, store):
    from datetime import timedelta
    from kaiusap.store import instant
    a, b = pair
    old = (instant(now()) - timedelta(minutes=5)).isoformat().replace('+00:00', 'Z')
    prompt_line(store, 'codex:b', at=old)  # older than the ancestor's created
    prompt_line(store, 'codex:b')  # within the 3 s read window, but before created
    mid = send(a)['message_id']
    prompt_line(store, 'codex:a')  # the recipient's own prompt, not the sender's
    prompt_line(store, 'claude:b')  # another provider's session with the same id
    delivered(store, mid, 'codex:b')
    assert store.message(send(b, to='alpha')['message_id'])['depth'] == 1
    reply = store.message(b.run('reply', {'id': mid, 'reply_needed': False, 'fields': {'body': 'Done'}})['message_id'])
    assert (reply['chain_id'], reply['depth']) == (mid, 1)


def test_prompted_since_stops_three_seconds_before_created(store):
    from datetime import timedelta
    from kaiusap.model import prompted_since
    from kaiusap.store import instant
    created = instant(now())
    stamp = lambda seconds: (created + timedelta(seconds=seconds)).isoformat().replace('+00:00', 'Z')
    # Out of `at` order by more than one hook's budget: the reader has stopped before it.
    prompt_line(store, 'codex:b', at=stamp(1))
    prompt_line(store, 'codex:x', at=stamp(-4))
    store.append('sessions/prompts.jsonl', {'malformed': True})
    assert not prompted_since(store, 'codex:b', stamp(0))
    prompt_line(store, 'codex:x', at=stamp(-2))
    prompt_line(store, 'codex:b', at=stamp(0))  # equal to created is not newer
    assert not prompted_since(store, 'codex:b', stamp(0))
    prompt_line(store, 'codex:b', at=stamp(.5))
    assert prompted_since(store, 'codex:b', stamp(0))


def test_claude_prompt_lines_leave_existing_readers_alone(store):
    from kaiusap.model import last_directory
    key = 'claude:s'
    store.start({'at': now(), 'provider': 'claude', 'session_id': 's', 'session_key': key, 'source': 'startup',
                 'cwd': '/start', 'pid': None, 'process_started_at': None, 'socket': None, 'teammate': False,
                 'tmux_pane': None, 'transcript': None})
    prompt_line(store, key)  # no cwd: never "the session's last directory"
    assert last_directory(store, key) == '/start'


def test_tail_records_reads_back_in_steps_up_to_the_limit(tmp_path):
    """The reader: newest first, lines across step boundaries joined, a line cut by the limit and lines that
    aren't JSON objects skipped, nothing for a missing file."""
    import json
    from kaiusap.adapters import tail_records
    path = tmp_path / "file.jsonl"
    lines = [{"n": n, "pad": "x" * (n * 7)} for n in range(20)]
    path.write_text("".join(json.dumps(line) + "\n" for line in lines) + "[1, 2]\nnot json\n")
    assert [r["n"] for r in tail_records(path, limit=10**6, step=13)] == list(range(19, -1, -1))
    size = path.stat().st_size
    kept = [r["n"] for r in tail_records(path, limit=size // 2, step=17)]
    assert kept == list(range(19, 19 - len(kept), -1)) and 0 < len(kept) < 20
    assert list(tail_records(tmp_path / "missing.jsonl")) == []
    assert list(tail_records(None)) == []


@pytest.mark.parametrize("verb, args", [
    ("send", {"to": "alpha", "subject": "x", "reply_needed": False, "fields": {"context": "x"}}),
    ("iam", {"handle": "gamma"}), ("inbox", {}), ("release", {}),
])
def test_sandboxed_shell_is_refused_before_anything_is_written(pair, store, verb, args):
    """A session's shell that can't see its own process is told to use the tools, exit 4."""
    sandboxed = Core(store, Identity("codex", "b", "codex:b", "/fixture", "sandboxed"), pair[1].provider)
    before = sorted(p.name for p in store.root.rglob("*") if p.is_file())
    with pytest.raises(Error) as caught:
        sandboxed.run(verb, args)
    assert caught.value.code == 4 and "use the kaiusap tools" in str(caught.value)
    assert sorted(p.name for p in store.root.rglob("*") if p.is_file()) == before


def test_sandboxed_shell_whoami_says_unknown(pair, store):
    sandboxed = Core(store, Identity("codex", "b", "codex:b", "/fixture", "sandboxed"), pair[1].provider)
    result = sandboxed.run("whoami", {})
    assert result["handle"] == "beta" and not result["plain_shell"]
    assert result["can_be_woken"] is None and result["reason_code"] == "sandboxed"
    from kaiusap.cli import render
    assert "Can be woken: unknown." in render("whoami", dict(result))


def test_read_only_store_counts_as_the_sandbox(pair, store, monkeypatch):
    """A read-only file system (EROFS) is handled as a permission error."""
    import errno
    import os
    original = os.open
    def read_only(path, *args, **kwargs):
        if str(path) == str(store.root / "lock"):
            raise OSError(errno.EROFS, "Read-only file system")
        return original(path, *args, **kwargs)
    monkeypatch.setattr(os, "open", read_only)
    result = pair[0].run("whoami", {})
    assert result["reason_code"] == "sandboxed" and "sandbox" in result["warnings"][0]
    from kaiusap.errors import store_error
    assert "sandbox" in str(store_error(OSError(errno.EROFS, "Read-only file system")))
    assert "sandbox" not in str(store_error(OSError(errno.ENOSPC, "No space left")))
