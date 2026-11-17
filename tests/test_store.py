from datetime import datetime, timedelta, timezone
import json
import multiprocessing
import os
import time

import pytest

from kaiusap.adapters.claude import ClaudeAdapter
from kaiusap.store import Store, identifier, now


def append_worker(root, worker):
    store = Store(root)
    for i in range(20):
        store.append('events/2026-01.jsonl', {'worker': worker, 'i': i, 'at': now()})


def index_worker(root, at):
    # Claude's transcript index is the adapter's, in its own store directory, under the starts lock.
    ClaudeAdapter(Store(root)).record({'record': {'at': at, 'provider': 'claude', 'session_id': 's', 'session_key': 'claude:s',
                                                  'source': 'cwd', 'cwd': at, 'transcript': at}})


def test_concurrent_append(store):
    context = multiprocessing.get_context('spawn')
    workers = [context.Process(target=append_worker, args=(store.root, i)) for i in range(4)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(10)
        assert worker.exitcode == 0
    rows = list(store.rows('events/2026-01.jsonl'))
    assert len(rows) == len({(r['worker'], r['i']) for r in rows}) == 80


def test_truncated_line_and_short_write(store, monkeypatch):
    store.root.mkdir()
    (store.root / 'log.jsonl').write_bytes(b'{"incomplete":')
    write = os.write
    monkeypatch.setattr(os, 'write', lambda fd, data: write(fd, data[:3]))
    store.append('log.jsonl', {'valid': True})
    assert list(store.rows('log.jsonl')) == [{'valid': True}]
    assert list(store.rows('log.jsonl', reverse=True)) == [{'valid': True}]


def test_multiblock_reverse_scan(store):
    for i in range(5):
        store.append('log.jsonl', {'i': i, 'body': 'é' * 40000})
    assert [r['i'] for r in store.rows('log.jsonl', reverse=True)] == [4, 3, 2, 1, 0]


def test_latest_at_order_and_bounded_scan(store):
    base = datetime.now(timezone.utc)
    for seconds, session in [(-100, 'old'), (0, 'match'), (-2, 'match')]:
        store.append('sessions/starts.jsonl', {'at': (base + timedelta(seconds=seconds)).isoformat(), 'session': session})
    result = store.latest('sessions/starts.jsonl', lambda r: r['session'] == 'match')
    assert result['at'] == base.isoformat()
    assert store.latest('sessions/starts.jsonl', lambda r: r['session'] == 'missing') is None


def test_index_concurrent_writers_keep_newest(store):
    context = multiprocessing.get_context('spawn')
    dates = ['2026-01-01T00:00:01Z', '2026-01-01T00:00:03Z', '2026-01-01T00:00:02Z']
    workers = [context.Process(target=index_worker, args=(store.root, at)) for at in dates]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(10)
        assert worker.exitcode == 0
    assert ClaudeAdapter(store).index('s')['at'] == max(dates)
    path = store.root / 'sessions/claude/last/s.json'
    path.write_text('broken')
    assert ClaudeAdapter(store).rebuild_index('s')['at'] == max(dates)
    assert len(list(store.rows('sessions/starts.jsonl'))) == 3


def test_index_rebuild_cannot_overwrite_newer_concurrent_value(store, monkeypatch):
    index_worker(store.root, '2026-01-01T00:00:01Z')
    snapshot = list(store.rows('sessions/starts.jsonl'))[0]
    index_worker(store.root, '2026-01-01T00:00:03Z')
    monkeypatch.setattr(store, 'latest', lambda *args, **kwargs: snapshot)
    assert ClaudeAdapter(store).rebuild_index('s')['at'] == '2026-01-01T00:00:03Z'


def test_index_tie_keeps_existing(store):
    index_worker(store.root, '2026-01-01T00:00:01Z')
    with store.locked('sessions/starts.jsonl'):
        ClaudeAdapter(store).update_index({'session_id': 's', 'at': '2026-01-01T00:00:01Z', 'cwd': 'different', 'transcript': None})
    assert ClaudeAdapter(store).index('s')['transcript'] == '2026-01-01T00:00:01Z'


def test_lock_deadline(store):
    with store.locked():
        start = time.monotonic()
        with pytest.raises(TimeoutError):
            with Store(store.root).locked(deadline=start + .01):
                pass
        assert time.monotonic() - start < .1


def test_complete_write_flush_failure_is_warning(store, monkeypatch):
    def fail(fd):
        raise OSError('fsync injected')
    monkeypatch.setattr(os, 'fsync', fail)
    store.append('handles.jsonl', {'visible': True})
    assert list(store.rows('handles.jsonl')) == [{'visible': True}]
    assert store.warnings


def test_message_link_collision_and_immutability(store, monkeypatch):
    at = now()
    mid = identifier(at)
    message = {'id': mid, 'created': at, 'chain_id': mid, 'body': 'first'}
    store.save_message(message)
    old = (store.root / 'messages' / (mid + '.json')).read_bytes()
    second = dict(message, body='second')
    store.save_message(second)
    assert second['id'] != mid and second['chain_id'] == second['id']
    assert (store.root / 'messages' / (mid + '.json')).read_bytes() == old
    assert not list((store.root / 'messages').glob('.tmp-*'))


def test_failed_link_never_publishes(store, monkeypatch):
    monkeypatch.setattr(os, 'link', lambda *args: (_ for _ in ()).throw(PermissionError('injected')))
    mid = identifier()
    with pytest.raises(PermissionError):
        store.save_message({'id': mid, 'created': now(), 'chain_id': mid})
    assert list(store.messages()) == []


def test_month_selection_uses_utc(store):
    store.event('read', at='2026-03-01T00:15:00+01:00')
    assert (store.root / 'events/2026-02.jsonl').exists()
    assert len(list(store.events('2026-03-01T00:00:00+01:00'))) == 1


def handle_worker(root, handle):
    from kaiusap.adapters import Identity
    from kaiusap.core import Core
    core = Core(Store(root), Identity('codex', 'same', 'codex:same', '/fixture', 'root'))
    for _ in range(10):
        core.run('iam', {'handle': handle})


def test_concurrent_handle_changes_keep_one_binding(store):
    from kaiusap.model import handles
    context = multiprocessing.get_context('spawn')
    workers = [context.Process(target=handle_worker, args=(store.root, handle)) for handle in ['first', 'second', 'third']]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(10)
        assert worker.exitcode == 0
    assert len([binding for binding in handles(store).values() if binding]) == 1


def test_partial_deciding_write_fails_and_next_append_recovers(store, monkeypatch):
    write = os.write
    calls = 0
    def fail_partway(fd, data):
        nonlocal calls
        calls += 1
        if calls == 1:
            return write(fd, data[:2])
        raise OSError('injected partial write')
    monkeypatch.setattr(os, 'write', fail_partway)
    with pytest.raises(OSError):
        store.append('handles.jsonl', {'handle': 'broken'})
    assert not list(store.rows('handles.jsonl'))
    monkeypatch.setattr(os, 'write', write)
    store.append('handles.jsonl', {'handle': 'complete'})
    assert list(store.rows('handles.jsonl')) == [{'handle': 'complete'}]


def test_identifier_shape_and_alphabet():
    import re
    from collections import Counter
    ids = [identifier('2026-10-29T20:15:00Z') for _ in range(2000)]
    assert all(re.fullmatch(r'20261029T201500Z-[a-z0-9]{6}', i) for i in ids)
    counts = Counter(c for i in ids for c in i[-6:])
    assert set(counts) == set('abcdefghijklmnopqrstuvwxyz0123456789')


def test_hook_path_does_not_import_secrets():
    # `secrets` cost ~4 ms of the 50 ms hook budget on every hook write.
    import subprocess, sys
    code = ('import sys, kaiusap.hooks, kaiusap.store, kaiusap.hookstate, kaiusap.adapters.codex, kaiusap.adapters.claude;'
            'kaiusap.store.identifier(); print("secrets" in sys.modules)')
    assert subprocess.run([sys.executable, '-c', code], capture_output=True, text=True, check=True).stdout.strip() == 'False'


def test_retirement_name_is_local_time_to_the_second(monkeypatch):
    """`<handle>~<yymmddhhmmss>`, the local date and time of the handle line's `at`,
    e.g. `sband-cld~261108173612`; both adapters build it here."""
    from kaiusap.model import retirement_name
    monkeypatch.setenv("TZ", "America/Toronto")
    time.tzset()
    try:
        assert retirement_name("sband-cld", "2026-11-08T22:36:12Z") == "sband-cld~261108173612"
        assert retirement_name("sband-cld", "2026-11-08T22:36:13.900000Z") == "sband-cld~261108173613"
    finally:
        monkeypatch.undo()
        time.tzset()
