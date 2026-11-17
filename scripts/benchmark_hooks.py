"""Complete hooks on a synthetic year; all provider executables are fakes.

Exits non-zero when a no-work case (or `bash_unrelated`) has a sample of 50 ms or more, when a new-message
case misses its note (or `bash_spawned_send` its deny) more often than `--max-misses` allows, or when
another check fails (unexpected hook output, a wrong decision, a logged error other than the deadline a
miss logs). A miss is counted and every case is still measured; `--strict` makes the first miss fatal.
"""
import argparse
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import platform
import re
import statistics
import subprocess
import sys
import tempfile
import time

from kaiusap.adapters.codex import CodexAdapter, Pane
from kaiusap.adapters.claude import ClaudeAdapter
from kaiusap.store import now, identifier
import shlex
from kaiusap.store import Store
from kaiusap.hookstate import signature


def fixture(root, at):
    for directory in ('sessions/claude/last', 'sessions/codex', 'events', 'messages'):
        (root / directory).mkdir(parents=True, exist_ok=True)
    counts = dict(starts=0, prompts=0, terminals=0, events=0, messages=0, handles=0)
    streams = {}

    def append(path, row, kind):
        if path not in streams:
            streams[path] = (root / path).open('w')
        streams[path].write(json.dumps(row) + '\n')
        counts[kind] += 1

    try:
        for day in range(365):
            timestamp = at - timedelta(days=364 - day)
            stamp = timestamp.isoformat().replace('+00:00', 'Z')
            prefix = timestamp.strftime('%Y%m%dT%H%M%SZ')
            for actor in range(8):
                provider = 'claude' if actor % 2 else 'codex'
                sid = f'00000000-0000-0000-0000-{actor:012d}'
                key = provider + ':' + sid
                start = dict(at=stamp, provider=provider, session_id=sid, session_key=key,
                             source='resume', cwd='/synthetic', transcript='/synthetic/' + sid)
                if provider == 'claude':
                    start.update(pid=100 + actor, process_started_at=stamp, socket=None, teammate=False, tmux_pane=None)
                    (root / 'sessions/claude/last' / (sid + '.json')).write_text(json.dumps({k: start[k] for k in ('at', 'cwd', 'transcript')}))
                append('sessions/starts.jsonl', start, 'starts')
                binding = dict(at=stamp, id=f'{prefix}-h{actor:05d}', handle=f'actor-{actor}', reason='iam',
                               provider=provider, session_id=sid, session_key=key, by_session=sid, by_session_key=key,
                               pid=start.get('pid'), process_started_at=start.get('process_started_at'),
                               moved_from=None, via=None, also_released=None)
                append('handles.jsonl', binding, 'handles')
                if provider == 'codex':
                    for prompt in range(40):
                        append('sessions/prompts.jsonl', dict(at=stamp, provider=provider, session_id=sid, session_key=key,
                                                            source='prompt', cwd='/synthetic'), 'prompts')
                        append('sessions/codex/terminals.jsonl', dict(at=(timestamp-timedelta(seconds=1)+timedelta(microseconds=actor*40+prompt)).isoformat().replace('+00:00','Z'), pane=f'4321@1793606400:%{actor}', codex_pid=200+actor, codex_started_at=stamp, thread_prefix=sid[:29], thread_id=sid, source='read', session_key=key), 'terminals')
                for number in range(2):
                    mid = f'{prefix}-{actor:03d}{number:03d}'
                    sender = (actor + 1) % 8
                    sender_provider = 'claude' if sender % 2 else 'codex'
                    message = dict(schema=1, id=mid, created=stamp,
                                   **{'from': dict(handle=f'actor-{sender}', provider=sender_provider,
                                                  session_id=f'00000000-0000-0000-0000-{sender:012d}', session_key=f'{sender_provider}:00000000-0000-0000-0000-{sender:012d}')},
                                   to=dict(handle=f'actor-{actor}', session_id=sid, session_key=key),
                                   reply_needed=False, in_reply_to=None, chain_id=mid, depth=0,
                                   subject='Synthetic benchmark message', fields={'context': 'x' * 256})
                    (root / 'messages' / (mid + '.json')).write_text(json.dumps(message))
                    counts['messages'] += 1
                    event_file = 'events/' + timestamp.strftime('%Y-%m') + '.jsonl'
                    append(event_file, dict(at=stamp, event='read', message_id=mid, handle=f'actor-{actor}',
                                           reader_session=sid, reader_session_key=key, receipt=number == 0), 'events')
                    append(event_file, dict(at=stamp, event='reminded', message_id=mid, handle=f'actor-{actor}',
                                           session_id=sid, session_key=key, binding=binding['id']), 'events')
    finally:
        for stream in streams.values():
            stream.close()
    return counts


def summary(samples):
    ordered = sorted(samples)
    return {'median_ms': round(statistics.median(samples), 3),
            'p95_ms': round(ordered[max(0, int(len(samples) * .95 + .999) - 1)], 3),
            'max_ms': round(max(samples), 3)}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--samples', type=int, default=100)
    parser.add_argument('--max-misses', type=float, default=10,
                        help="misses allowed per case (a missed note, or the spawned send's missed deny), per 100 samples "
                             "(default 10), scaled to --samples")
    parser.add_argument('--strict', action='store_true', help='the first miss is fatal')
    args = parser.parse_args()
    allowed_misses = args.max_misses * args.samples / 100
    shell = os.environ.get('SHELL', '/bin/sh')
    executable = Path(sys.executable).parent / 'kaiusap'
    if not executable.exists():
        parser.error('build the native entry in this Python environment first')
    at = datetime.now(timezone.utc)
    with tempfile.TemporaryDirectory(prefix='kaiusap-benchmark-') as temporary:
        root = Path(temporary) / 'store'
        counts = fixture(root, at)
        binary = Path(temporary) / 'bin'
        binary.mkdir()
        sid = lambda n: f'00000000-0000-0000-0000-{n:012d}'
        pane = f'%0\tactor-0\t1\t200\t/dev/fake\t4321\t1793606400\t{sid(0)}\n'
        fake_ps = (at - timedelta(days=1)).astimezone().strftime('%a %b %d %H:%M:%S %Y') + ' claude\n'
        for name, output in {'tmux': pane, 'ps': fake_ps, 'claude': '[]\n', 'codex': ''}.items():
            path = binary / name
            path.write_text("#!/bin/sh\nprintf '%s' " + shlex.quote(output)
                            + ('\nexit 1\n' if name == 'codex' else '\n'))
            path.chmod(0o700)
        env = {k: v for k, v in os.environ.items() if not k.startswith(('CLAUDE', 'CODEX', 'TMUX', 'KAIUSAP_'))}
        env.update(KAIUSAP_HOME=str(root), HOME=str(Path(temporary) / 'home'),
                   PATH=str(binary) + os.pathsep + str(executable.parent), CLAUDE_PID='101')
        store = Store(root)
        def payload(provider, event='UserPromptSubmit', **extra):
            number = 1 if provider == 'claude' else 0
            if provider == 'codex':
                # Codex's hook inputs, key for key (codex-rs/hooks/src/schema.rs, 0.166.0 to 0.167.0): every
                # UserPromptSubmit carries turn_id, and neither event a session_title.
                data = dict(session_id=sid(number), transcript_path=f'/synthetic/{sid(number)}', cwd='/synthetic',
                            hook_event_name=event, model='gpt-5.5', permission_mode='default')
                data.update(source='resume') if event == 'SessionStart' else data.update(
                    turn_id='019a0000-0000-7000-8000-000000000001', prompt='Hello')
                return data | extra
            if event == 'UserPromptSubmit':
                # Claude Code 2.1.319's UserPromptSubmit input, with only the keys that are always set: each
                # native part accepts only its own provider's keys, and Claude sends no `source` here.
                return dict(session_id=sid(number), transcript_path=f'/synthetic/{sid(number)}', cwd='/synthetic',
                            permission_mode='default', hook_event_name=event, prompt='Hello',
                            session_title=f'actor-{number}', **extra)
            return dict(session_id=sid(number), cwd='/synthetic', prompt='Hello', source='resume',
                        session_title=f'actor-{number}', transcript_path=f'/synthetic/{sid(number)}', **extra)
        cases = {}
        for provider in ('claude', 'codex'):
            for kind in ('no_work', 'new_message', 'start'):
                name = provider + '_' + kind
                event = 'SessionStart' if kind == 'start' else 'UserPromptSubmit'
                cases[name] = (provider, [shell, '-c', 'kaiusap hook ' + provider + ' ' + event], payload(provider, event))
        # Claude Code 2.1.319's UserPromptSubmit input: its hook input base adds prompt_id
        # (normally set), scratchpad_dir and effort (when set) to the fields above.
        cases['claude_real_input_no_work'] = ('claude', [shell, '-c', 'kaiusap hook claude UserPromptSubmit'], dict(
            session_id=sid(1), transcript_path=f'/synthetic/{sid(1)}', cwd='/synthetic',
            scratchpad_dir=f'/private/tmp/claude-501/-synthetic/{sid(1)}/scratchpad',
            prompt_id='3f2b1c4d-5e6f-4a7b-8c9d-0e1f2a3b4c5d', permission_mode='default', effort={'level': 'high'},
            hook_event_name='UserPromptSubmit', prompt='Hello', session_title='actor-1'))
        for name, command in [('bash_unrelated', 'echo hello'), ('bash_spawned_send', 'kaiusap send --to beta')]:
            cases[name] = ('claude', [shell, '-c', 'kaiusap hook claude PreToolUse'],
                           dict(agent_id='spawned', tool_name='Bash', tool_input=dict(command=command)))
        # Bootstrap through complete start hooks, then allow both providers to
        # consume the named/reminded events written by those starts.
        for _ in range(5):
            for provider in ('claude', 'codex'):
                subprocess.run([shell, '-c', 'kaiusap hook ' + provider + ' SessionStart'], input=json.dumps(payload(provider, 'SessionStart')),
                               text=True, capture_output=True, env=env, check=True)
                subprocess.run([shell, '-c', 'kaiusap hook ' + provider + ' UserPromptSubmit'], input=json.dumps(payload(provider)),
                               text=True, capture_output=True, env=env, check=True)
        samples, outputs, errors, warmup_deferrals, misses, error_lines = {}, {}, {}, {}, {}, {}
        def case_order(item):
            name = item[0]
            return (0 if name.endswith('no_work') or name == 'bash_unrelated' else
                    1 if name.endswith('new_message') else 2 if name.endswith('start') else 3, name)
        # Keep no-work cases before cases that deliberately invalidate markers.
        # A dirty marker is work even if its message belongs to another handle.
        for name, (provider, command, data) in sorted(cases.items(), key=case_order):
            if name.endswith('no_work'):
                marker = json.loads((root / 'markers' / provider / (data['session_id'] + '.json')).read_text())
                assert marker['signature'] == signature(root) and not marker['mail_pending'], 'no-work case has a dirty marker'
            if name.endswith('new_message'):
                # Other cases add mail for other handles. Synchronize that
                # unrelated history outside this one-new-message measurement.
                for _ in range(2):
                    prepared = subprocess.run([shell, '-c', 'kaiusap hook ' + provider + ' SessionStart'],
                                              input=json.dumps(payload(provider, 'SessionStart')), text=True, capture_output=True, env=env, timeout=4)
                    if prepared.returncode or prepared.stderr:
                        raise RuntimeError(f'{name}: setup failed: {prepared}')
            samples[name], outputs[name], warmup_deferrals[name] = [], 0, []
            if name.endswith('new_message') or name == 'bash_spawned_send':
                misses[name] = 0
            before_errors = (root / 'hook-errors.log').read_text() if (root / 'hook-errors.log').exists() else ''
            for i in range(args.samples + 5):
                if i == 5:
                    before_errors = (root / 'hook-errors.log').read_text() if (root / 'hook-errors.log').exists() else ''
                if name.endswith('new_message'):
                    stamp = now()
                    mid = identifier(stamp)
                    number = 1 if provider == 'claude' else 0
                    store.save_message(dict(schema=1, id=mid, created=stamp,
                        **{'from': dict(handle='sender', session_key='codex:sender', session_id='sender', provider='codex')},
                        to=dict(handle=f'actor-{number}', session_id=sid(number), session_key=provider+':'+sid(number)),
                        subject='New message', fields=dict(context='Benchmark'), reply_needed=False,
                        in_reply_to=None, chain_id=mid, depth=0))
                start = time.perf_counter()
                result = subprocess.run(command, input=json.dumps(data), text=True, capture_output=True, env=env, timeout=5)
                elapsed = (time.perf_counter() - start) * 1000
                if result.returncode or result.stderr:
                    raise RuntimeError(f'{name}: {result}')
                if name.endswith('new_message') and i < 5 and not result.stdout:
                    warmup_deferrals[name].append(i)
                if name.endswith('new_message') and i >= 5:
                    if not result.stdout or '1 unread message' not in json.loads(result.stdout).get('hookSpecificOutput', {}).get('additionalContext', ''):
                        # A miss is counted and the sample still measured; --strict makes it fatal.
                        misses[name] += 1
                        if args.strict:
                            raise RuntimeError(f'{name} sample {i-5} ({elapsed:.3f} ms): the new message did not return its note: {result.stdout!r}; reminded='
                                               + str(any(e.get('event') == 'reminded' and e.get('message_id') == mid for e in store.events()))
                                               + '; hook log tail=' + repr((root / 'hook-errors.log').read_text().splitlines()[-4:] if (root / 'hook-errors.log').exists() else []))
                if name == 'bash_spawned_send' and i >= 5 and not result.stdout:
                    # A deny dropped at the hook's deadline (logged) is a miss, as a missed note is.
                    misses[name] += 1
                    if args.strict:
                        raise RuntimeError(f'{name} sample {i-5} ({elapsed:.3f} ms): the spawned send returned no deny')
                if result.stdout:
                    out = json.loads(result.stdout)
                    if name.endswith('no_work') or name == 'bash_unrelated':
                        raise RuntimeError(f'{name}: unexpected hook work: {out}')
                    if name == 'bash_spawned_send' and out.get('hookSpecificOutput', {}).get('permissionDecision') != 'deny':
                        raise RuntimeError('spawned send did not return its deny')
                if i >= 5:
                    samples[name].append(elapsed)
                    outputs[name] += bool(result.stdout)
            after_errors = (root / 'hook-errors.log').read_text() if (root / 'hook-errors.log').exists() else ''
            error_lines[name] = after_errors[len(before_errors):].splitlines()
            errors[name] = len(error_lines[name])
        # Bounded paths, before and after, on the same year-long terminal map.
        workloads = {'current_full_map': [], 'current_bounded': [], 'pane_lines_bounded': [], 'claude_all_event_months': [], 'claude_naming': []}
        adapter = CodexAdapter(store, run=lambda *a: (1, ''))
        adapter._deadline = None
        event = dict(session_id=sid(0), at=now())
        store.append('sessions/codex/terminals.jsonl', dict(at=now(),pane='4321@1793606400:%0',codex_pid=None,codex_started_at=None,thread_prefix=sid(0)[:29],thread_id=sid(0),source='prompt',session_key='codex:'+sid(0)))
        pane_obj = Pane(['%0','actor-0','1','200','/dev/fake','4321','1793606400',sid(0)])
        for _ in range(args.samples):
            start = time.perf_counter(); list(store.rows('sessions/codex/terminals.jsonl'))
            workloads['current_full_map'].append((time.perf_counter()-start)*1000)
            start = time.perf_counter(); adapter.current(event, {})
            workloads['current_bounded'].append((time.perf_counter()-start)*1000)
            start = time.perf_counter(); adapter.pane_lines(pane_obj, (200, (at-timedelta(days=1)).isoformat()))
            workloads['pane_lines_bounded'].append((time.perf_counter()-start)*1000)
            start = time.perf_counter(); list(store.events())
            workloads['claude_all_event_months'].append((time.perf_counter()-start)*1000)
            start = time.perf_counter(); ClaudeAdapter(store).mirror_output(dict(kind='prompt', session_id=sid(1), session_key='claude:'+sid(1), session_title='actor-1'))
            workloads['claude_naming'].append((time.perf_counter()-start)*1000)
        no_work_all_under_50 = all(max(v)<50 for k,v in samples.items() if k.endswith('no_work') or k=='bash_unrelated')
        failures = [] if no_work_all_under_50 else ['a no-work case had a sample of 50 ms or more']
        failures += [f'{k}: {n} misses, more than the {allowed_misses:g} allowed' for k, n in misses.items() if n > allowed_misses]
        # A hook out of time logs a TimeoutError and returns, leaving the rest for the next call; that is
        # how a miss happens, so a case with work may log one. Any other logged error fails the run.
        deadline = re.compile(r'^\S+ (TimeoutError: |hook admission deadline reached; returning committed output$)')
        for k, lines in error_lines.items():
            unexpected = [line for line in lines if not (k in misses and deadline.match(line))]
            if unexpected:
                failures.append(f'{k}: {len(unexpected)} logged errors: {unexpected[:3]}')
        print(json.dumps(dict(measured_at=at.isoformat(), python=platform.python_version(), platform=platform.platform(),
            shell=shell, samples=args.samples, warmups=5, counts=counts,
            store_bytes=sum(p.stat().st_size for p in root.rglob('*') if p.is_file()),
            timings={k: summary(v) for k,v in samples.items()}, nonempty_outputs=outputs, logged_errors=errors, warmup_note_deferrals=warmup_deferrals,
            no_work_all_under_50=no_work_all_under_50,
            note_misses=misses, allowed_misses=allowed_misses, strict=args.strict, failures=failures,
            store_workloads={k: summary(v) for k,v in workloads.items()}), indent=2))
        return 1 if failures else 0


if __name__ == '__main__':
    sys.exit(main())
