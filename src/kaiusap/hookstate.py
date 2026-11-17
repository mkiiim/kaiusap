"""Disposable hook projections. Source fingerprints fence concurrent writes."""
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import time
import signal

from .model import PERSONAS, Personas, apply_line, ended_by_state, held, load_settings
from .store import decoded, instant, identifier


def signature(root):
    paths = ['handles.jsonl', PERSONAS, 'messages', 'settings.json', 'paused', 'markers/changed']
    paths += sorted(str(p.relative_to(root)) for p in (root / 'events').glob('????-??.jsonl'))
    parts = []
    for relative in paths:
        try:
            st = (root / relative).stat()
            parts.append(f'{relative}:{st.st_ino}:{st.st_size}:{st.st_mtime_ns}')
        except FileNotFoundError:
            parts.append(relative + ':-')
    return '\n'.join(parts)


def atomic(path, value):
    blocked = signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGALRM})
    try:
        _atomic(path, value)
    finally:
        signal.pthread_sigmask(signal.SIG_SETMASK, blocked)


def _atomic(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.parent / ('.tmp-' + identifier())
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, 'w') as stream:
            stream.write(json.dumps(value, ensure_ascii=False))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def cache_path(store, provider, sid, suffix):
    if not sid or any(c not in 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_' for c in sid):
        raise ValueError('unsafe hook session id')
    return store.root / 'markers' / provider / (sid + suffix)


class HookState:
    def __init__(self, store, provider, sid):
        self.store, self.key = store, provider + ':' + sid
        self.path = cache_path(store, provider, sid, '.state')
        self.marker = cache_path(store, provider, sid, '.json')
        try:
            if not isinstance(json.loads((store.root / 'markers/changed').read_text()), str):
                raise ValueError('unrecognised change marker')
        except (OSError, ValueError):
            store.changed()
        self.fence = signature(store.root)
        try:
            self.data = json.loads(self.path.read_text())
            if self.data['version'] != 1 or self.data['key'] != self.key:
                raise ValueError('unknown hook state')
            for field, kind in (('offsets', dict), ('bindings', dict), ('ended', list),
                                ('events', list), ('messages', list), ('seen', list)):
                if not isinstance(self.data[field], kind):
                    raise ValueError('unrecognised hook projection')
            if not all(isinstance(row, dict) for field in ('events', 'messages') for row in self.data[field]):
                raise ValueError('unrecognised hook rows')
            if not all(isinstance(name, str) for name in self.data['seen']):
                raise ValueError('unrecognised message cursor')
            if not all(value is None or isinstance(value, dict) for value in self.data['bindings'].values()):
                raise ValueError('unrecognised binding projection')
            if not all(isinstance(row, list) and len(row) == 2 and isinstance(row[0], dict)
                       for row in self.data['ended']):
                raise ValueError('unrecognised binding history')
            for relative, offset in self.data['offsets'].items():
                if type(offset) is not int or offset < 0 or not (
                        relative == 'handles.jsonl' or (len(Path(relative).parts) == 2
                        and Path(relative).parts[0] == 'events' and len(Path(relative).name) == 13
                        and Path(relative).suffix == '.jsonl')):
                    raise ValueError('unrecognised hook cursor')
                if (store.root / relative).stat().st_size < offset:
                    raise ValueError('hook source shrank')
        except (OSError, ValueError, KeyError, TypeError):
            self.data = dict(version=1, key=self.key, offsets={}, bindings={}, ended=[], events=[], messages=[], seen=[])
        self.refresh()

    def progress(self):
        if self.store.deadline is not None and time.monotonic() >= self.store.deadline:
            self.store.writable()
            atomic(self.path, self.data)
            raise TimeoutError("hook projection checkpointed; continue next call")

    def tail(self, relative):
        path = self.store.root / relative
        try:
            with path.open('rb') as stream:
                offset = self.data['offsets'].get(relative, 0)
                if offset > path.stat().st_size:
                    raise ValueError('hook source shrank')
                stream.seek(offset)
                while line := stream.readline():
                    self.progress()
                    if not line.endswith(b'\n'):
                        break  # A concurrent append is retried, including its partial line.
                    row = decoded(line)
                    if row is not None:
                        yield row
                    self.data['offsets'][relative] = stream.tell()
        except FileNotFoundError:
            return

    def refresh(self):
        d = self.data
        previous = d.get('scope')
        for row in self.tail('handles.jsonl'):
            try:
                d['ended'].extend([b, row['at']] for b in ended_by_state(d['bindings'], row) if b['session_key'] == self.key)
                apply_line(d['bindings'], row)
            except (KeyError, TypeError):
                continue
        handle = held(d['bindings'], self.key)
        # The mail is the persona's, so the scope is the sessions that can lead here, and the handle.
        personas = Personas(self.store)
        sessions = sorted(personas.sessions_of(self.key))
        scope = [handle, sessions]
        if previous != scope:
            d['events'], d['messages'], d['seen'] = [], [], []
            d.pop('message_directory', None)
            d['offsets'] = {k: v for k, v in d['offsets'].items() if not k.startswith('events/')}
        d['scope'] = scope
        self.handle = handle
        self.binding = d['bindings'].get(handle)
        days = load_settings(self.store)['unread_note_days']
        self.since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        month = instant(self.since).strftime('%Y-%m')
        # Naming reads begin at the oldest binding this session could still owe.
        naming = [b['at'][:7] for b, _ in d['ended']]
        if self.binding:
            naming.append(self.binding['at'][:7])
        oldest = min([month, *naming])
        for path in sorted((self.store.root / 'events').glob('????-??.jsonl')):
            if path.stem >= oldest:
                for row in self.tail(str(path.relative_to(self.store.root))):
                    if (row.get('event') == 'named' and row.get('session_key') == self.key) or (
                            row.get('event') == 'read' and row.get('receipt')) or (
                            row.get('event') == 'reminded' and row.get('session_key') == self.key):
                        d['events'].append(row)
        cutoff = instant(self.since).strftime('%Y%m%dT%H%M%SZ')
        seen = set(d['seen'])
        try:
            st = (self.store.root / 'messages').stat()
            message_directory = [st.st_ino, st.st_mtime_ns]
        except FileNotFoundError:
            message_directory = None
        if ('message_directory' not in d or d['message_directory'] != message_directory
                       or cutoff < d.get('message_window', cutoff)):
            try:
                entries = os.scandir(self.store.root / 'messages')
            except FileNotFoundError:
                entries = []
            for count, entry in enumerate(entries):
                if count % 128 == 0:
                    self.progress()
                name = entry.name
                if name.endswith('.json') and name >= cutoff and name not in seen and not name.startswith('.'):
                    self.progress()
                    message = json.loads(Path(entry.path).read_text())
                    if message['to'].get('session_key') in sessions:
                        d['messages'].append(message)
                    seen.add(name)
                    d['seen'].append(name)
            d['message_directory'] = message_directory
            d['message_window'] = cutoff
        d['seen'] = sorted(name for name in seen if name >= cutoff)
        # Each cached message is checked against its time: a session the persona left keeps what came after.
        d['messages'] = [m for m in d['messages'] if m['id'] >= cutoff
                         and personas.current(m['to']['session_key'], m['created']) == self.key]
        d['message_window'] = cutoff

    def save(self, event, fields=None):
        """Write the projection, then the acknowledgement (hook markers). The adapter's own
        fields (`index_signature`, `title_mode`, `title`) come from its `marker_fields`; without
        them the marker says no index was verified and any title will do."""
        # Never acknowledge beyond the fingerprint taken BEFORE the source reads.
        # Our own named/reminded writes deliberately cause one further replay.
        d = self.data
        fields = fields or {}
        self.store.writable()  # a newer store format refuses these too
        atomic(self.path, d)
        receipts = {e.get('message_id') for e in d['events'] if e.get('event') == 'read' and e.get('receipt')}
        reminded = {e.get('message_id') for e in d['events'] if e.get('event') == 'reminded'
                    and e.get('session_key') == self.key}  # once per session of the persona
        mail_pending = bool(not self.store.paused and any(
            m['id'] not in receipts | reminded for m in d['messages']))
        st = self.path.stat()
        state_signature = f'{st.st_ino}:{st.st_size}:{st.st_mtime_ns}'
        atomic(self.marker, dict(version=1, signature=self.fence, state_signature=state_signature,
                                 index_signature=fields.get('index_signature'), mail_pending=mail_pending,
                                 transcript=event.get('transcript'), title_mode=fields.get('title_mode', 'any'),
                                 title=fields.get('title')))
