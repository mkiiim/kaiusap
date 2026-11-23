"""Through the verbs: a message belongs to the persona that held its recipient handle when it was sent. A persona
here is one session; continuations are covered in test_personas.py."""
from dataclasses import replace

import pytest

from conftest import send
from kaiusap.core import Core
from kaiusap.errors import Error
from kaiusap.model import handles


def test_a_persona_that_takes_another_name_answers_its_earlier_mail_under_it(pair, store):
    a, b = pair
    mid = send(a)['message_id']  # to beta
    b.run('iam', {'handle': 'bravo'})  # beta's persona takes another name; beta is released
    inbox = b.run('inbox', {})['messages']
    assert [m['id'] for m in inbox] == [mid]
    assert b.run('read', {'id': mid})['mine'] is True
    reply = b.run('reply', {'id': mid, 'reply_needed': False, 'fields': {'body': 'Done'}})
    assert store.message(reply['message_id'])['from']['handle'] == 'bravo'
    rows = {r['id']: r for r in b.run('messages', {})['messages']}
    assert rows[mid]['answerable_by'] == 'bravo' and rows[mid]['to'] == 'beta' and rows[mid]['state'] == 'answered'


def test_a_reply_reaches_the_sender_under_its_current_name(pair, store):
    a, b = pair
    mid = send(a)['message_id']  # alpha -> beta
    a.run('iam', {'handle': 'apex'})  # the sender's persona takes another name
    reply = b.run('reply', {'id': mid, 'reply_needed': False, 'fields': {'body': 'Done'}})
    assert store.message(reply['message_id'])['to']['handle'] == 'apex'
    assert [m['id'] for m in a.run('inbox', {'sent': True, 'all': True})['messages']] == [mid]  # --sent follows it


def test_a_send_to_a_released_name_points_to_its_session_without_redirecting(pair, store):
    a, b = pair
    b.run('iam', {'handle': 'bravo'})
    with pytest.raises(Error) as refused:
        send(a, to='beta')
    assert refused.value.code == 3
    assert str(refused.value).startswith('beta is released; the session that held it is now bravo. Unknown or released recipient.')


def test_another_persona_given_the_name_does_not_get_earlier_mail(pair, store):
    a, b = pair
    mid = send(a)['message_id']  # to beta, held by b
    c = Core(store, replace(b.identity, session_id='c', session_key='codex:c'), b.provider)
    c.run('iam', {'handle': 'beta'})  # c takes the name from b
    assert c.run('inbox', {})['messages'] == []
    taken = c.run('read', {'id': mid})
    assert taken['mine'] is False and taken['already_read_at'] is None
    with pytest.raises(Error, match=r"^This message went to another session \(beta at the time\)\. Only that session may reply\.$"):
        c.run('reply', {'id': mid, 'reply_needed': False, 'fields': {'body': 'x'}})
    with pytest.raises(Error, match='Only the recipient may mark'):
        c.run('moot', {'id': mid})
    later = send(a)['message_id']  # sent after the move: c's
    assert [m['id'] for m in c.run('inbox', {})['messages']] == [later]


def test_a_persona_with_no_handle_sees_its_mail_but_cannot_answer(pair, store):
    a, b = pair
    mid = send(a)['message_id']
    b.run('release', {})
    assert [m['id'] for m in b.run('inbox', {})['messages']] == [mid]
    assert b.run('whoami', {})['unread'] == 1
    read = b.run('read', {'id': mid})
    assert read['mine'] is True and read['no_handle'] is True and read['state'] == 'unread'  # no receipt without a handle
    with pytest.raises(Error, match='no handle'):
        b.run('reply', {'id': mid, 'reply_needed': False, 'fields': {'body': 'x'}})
    rows = {r['id']: r for r in b.run('messages', {})['messages']}
    assert rows[mid]['answerable_by'] is None and rows[mid]['owed'] is True


def test_messages_since_reports_a_change_of_owner(pair, store):
    a, b = pair
    mid = send(a)['message_id']
    as_of = b.run('messages', {})['as_of']
    import time
    time.sleep(0.01)
    b.run('iam', {'handle': 'bravo'})
    rows = b.run('messages', {'since': as_of})['messages']
    assert mid in {r['id'] for r in rows}  # within the 60 s overlap anyway; changed_at moved past as_of
    row = next(r for r in rows if r['id'] == mid)
    assert row['changed_at'] > as_of and row['answerable_by'] == 'bravo'


# ----- a handle is a persona's name ------------------------------------------------------


class LiveProvider:
    """The pair's FakeProvider, with every session live."""
    def __init__(self, base):
        self.base = base

    def __getattr__(self, name):
        return getattr(self.base, name)

    def liveness(self, session_key):
        return {"can_be_woken": True, "why_not": None, "reason_code": None, "fix": None, "live": True}


def other(store, provider, sid="c"):
    from kaiusap.adapters import Identity
    return Core(store, Identity("codex", sid, f"codex:{sid}", "/fixture", "root"), provider)


def offer(store, key, handle, holder):
    from kaiusap.model import PERSONAS
    from kaiusap.store import now
    store.append(PERSONAS, {"at": now(), "session_key": key, "offered": handle, "from": holder})


def test_a_name_a_live_persona_holds_is_refused_with_nothing_written(pair, store):
    a, b = pair
    lines = len(list(store.rows("handles.jsonl")))
    c = other(store, LiveProvider(b.provider))
    with pytest.raises(Error) as refused:
        c.run('iam', {'handle': 'beta'})
    assert str(refused.value) == ("beta is in use by a running Codex session in an unknown directory. "
                                  "Tell that session to take another name (its messages go with it), or choose another name.")
    assert len(list(store.rows("handles.jsonl"))) == lines


def test_a_name_taken_from_a_stopped_persona_says_its_mail_stays(pair, store):
    a, b = pair
    send(a)
    result = other(store, b.provider).run('iam', {'handle': 'beta'})  # FakeProvider: b isn't live
    assert result['notice'] == ("That session isn't running; its earlier messages stay with it. To carry on as that "
                                "assistant instead, resume it from its provider's own list.")
    assert result['continued'] is False


def test_an_offer_lets_you_are_continue_the_persona_with_its_mail(pair, store):
    a, b = pair
    mid = send(a)['message_id']
    offer(store, 'codex:c', 'beta', 'codex:b')
    c = other(store, LiveProvider(b.provider))
    result = c.run('iam', {'handle': 'beta', 'confirmed': True})  # b is live, c was offered beta, the user confirmed
    assert result['continued'] is True and result['notice'] is None
    assert [m['id'] for m in c.run('inbox', {})['messages']] == [mid]  # the mail came with it


def test_an_offer_from_a_live_session_asks_the_user_to_confirm_first(pair, store):
    from kaiusap.model import PERSONAS
    a, b = pair
    offer(store, 'codex:c', 'beta', 'codex:b')
    lines, personas = len(list(store.rows("handles.jsonl"))), len(list(store.rows(PERSONAS)))
    c = other(store, LiveProvider(b.provider))
    with pytest.raises(Error) as asked:
        c.run('iam', {'handle': 'beta'})
    assert (asked.value.code, asked.value.reason_code) == (1, "confirmation_needed")
    assert str(asked.value) == ("beta is held by a running Codex session in an unknown directory. Ask the user to "
                                "confirm that beta should move here, with its mail and its earlier handles; if they "
                                "do, run iam again with --confirmed.")
    # Nothing is written between the two calls.
    assert len(list(store.rows("handles.jsonl"))) == lines
    assert len([r for r in store.rows(PERSONAS) if not r.get("suggested")]) == personas
    assert handles(store)['beta']['session_key'] == 'codex:b'


def test_confirmed_without_an_offer_is_still_refused(pair, store):
    a, b = pair
    with pytest.raises(Error, match='is in use by a running'):
        other(store, LiveProvider(b.provider)).run('iam', {'handle': 'beta', 'confirmed': True})


def test_an_offer_from_a_stopped_session_moves_at_once(pair, store):
    a, b = pair
    offer(store, 'codex:c', 'beta', 'codex:b')
    result = other(store, b.provider).run('iam', {'handle': 'beta'})  # FakeProvider: b isn't live
    assert result['continued'] is True


def test_an_offer_closes_when_the_handle_moves_or_the_session_starts_again(pair, store):
    a, b = pair
    offer(store, 'codex:c', 'beta', 'codex:b')
    store.append("sessions/starts.jsonl", {"at": __import__('kaiusap.store', fromlist=['now']).now(), "provider": "codex",
                                           "session_id": "c", "session_key": "codex:c", "source": "resume", "cwd": "/w"})
    with pytest.raises(Error, match='is in use by a running'):
        other(store, LiveProvider(b.provider)).run('iam', {'handle': 'beta'})


def test_a_persona_taking_another_name_is_told_its_mail_comes_along(pair, store):
    a, b = pair
    send(a)
    assert b.run('iam', {'handle': 'bravo'})['notice'] == "Its earlier messages to beta are now answered as bravo."


def test_taking_a_released_name_says_where_its_last_persona_s_mail_stays(pair, store):
    a, b = pair
    send(a)
    b.run('release', {})
    result = other(store, b.provider).run('iam', {'handle': 'beta'})
    assert result['notice'].startswith("That session isn't running; its earlier messages stay with it.")


# ----- offers written by the base, and mail held while one is open --------------------------


def test_mail_to_a_handle_with_an_open_offer_is_held_not_woken(pair, store):
    a, b = pair
    offer(store, 'codex:c', 'beta', 'codex:b')  # beta's conversation may have moved to c (the Codex fork question)

    class Delivering(LiveProvider):
        delivers = True

    result = send(Core(store, a.identity, Delivering(a.provider)))
    assert (result['outcome'], result['reason_code']) == ('stored', 'offer_pending') and result['paste_line']


def test_the_clear_suggestion_is_recorded_as_an_offer(pair, store):
    from kaiusap.adapters import Providers
    from kaiusap.following import suggest
    from kaiusap.model import PERSONAS
    from kaiusap.store import now
    a, b = pair
    store.append("sessions/starts.jsonl", {"at": now(), "provider": "codex", "session_id": "b", "session_key": "codex:b",
                                           "source": "startup", "cwd": "/w"})
    providers = Providers({'codex': b.provider}, store)
    event = {"provider": "codex", "kind": "clear", "session_id": "c", "session_key": "codex:c", "cwd": "/w"}
    text = suggest(store, providers, event, {})
    assert text == ("This conversation began with a /clear. beta's session was in this directory; if that is the "
                    "conversation left, beta stays with it: resume it from its provider's session list. The user can "
                    "say 'you are beta' to carry on as beta here.")
    assert [(r["session_key"], r["offered"], r["from"]) for r in store.rows(PERSONAS)] == [("codex:c", "beta", "codex:b")]


def test_a_clear_whose_session_left_was_found_gets_no_suggestion(pair, store):
    # The adapter found the session left (it held no handle), so nothing is said and nothing is offered.
    from kaiusap.adapters import Providers
    from kaiusap.following import suggest
    from kaiusap.model import PERSONAS
    from kaiusap.store import now
    a, b = pair
    store.append("sessions/starts.jsonl", {"at": now(), "provider": "codex", "session_id": "b", "session_key": "codex:b",
                                           "source": "startup", "cwd": "/w"})
    event = {"provider": "codex", "kind": "clear", "session_id": "c", "session_key": "codex:c", "cwd": "/w"}
    assert suggest(store, Providers({'codex': b.provider}, store), event, {"suggest": False}) is None
    assert not [r for r in store.rows(PERSONAS) if r.get("offered")]


# ----- sessions' formerly, stopped personas with open mail, and the user's moot ----------------


def rows(core):
    return {r['session_key']: r for r in core.run('sessions', {})['sessions']}


def test_formerly_lists_how_each_earlier_handle_ended(pair, store):
    a, b = pair
    b.run('iam', {'handle': 'bravo'})  # changed
    b.run('release', {})               # released
    b.run('iam', {'handle': 'beta'})
    other(store, b.provider).run('iam', {'handle': 'beta'})  # taken (b isn't live under FakeProvider)
    b.run('iam', {'handle': 'bravo'})  # a handle again, so b is listed
    formerly = [(f['handle'], f['how']) for f in rows(a)['codex:b']['formerly']]
    assert formerly == [('beta', 'taken'), ('bravo', 'released'), ('beta', 'changed')]
    assert rows(a)['codex:a']['formerly'] == []


def test_sessions_lists_a_stopped_persona_with_no_handle_and_open_mail(pair, store):
    a, b = pair
    send(a)
    other(store, b.provider).run('iam', {'handle': 'beta'})  # b's persona keeps the mail, with no handle
    row = rows(a)['codex:b']
    assert row['handle'] is None and row['formerly'][0]['how'] == 'taken'


def test_the_user_may_moot_mail_no_session_can_answer(pair, store):
    from kaiusap.adapters import Identity
    a, b = pair
    mid = send(a)['message_id']
    shell = Core(store, Identity(), b.provider)
    with pytest.raises(Error, match='From a plain shell you may only moot'):
        shell.run('moot', {'id': mid})  # beta still holds it
    b.run('release', {})
    shell.run('moot', {'id': mid, 'reason': 'its session is gone'})
    assert rows(a).get('codex:b') is None  # nothing open now, so it isn't listed


# ----- 2026-11-13: mail joined to personas (`messages`), and a waiting sender listed (`sessions`) -----------


def test_messages_join_mail_to_personas_not_names(pair, store):
    # A later persona given the sender's name by `iam` isn't the sender; the row's persona keys say who is.
    a, b = pair
    mid = send(a)['message_id']  # alpha (codex:a) -> beta (codex:b), a request
    row = {r['id']: r for r in a.run('messages', {})['messages']}[mid]
    assert (row['from_persona_session_key'], row['to_persona_session_key'], row['reply_to']) == ('codex:a', 'codex:b', 'alpha')
    c = other(store, a.provider)
    c.run('iam', {'handle': 'alpha'})  # FakeProvider: a isn't live, so c takes the name
    row = {r['id']: r for r in c.run('messages', {})['messages']}[mid]
    assert row['from'] == 'alpha' and row['from_session_key'] == 'codex:a'
    assert row['from_persona_session_key'] == 'codex:a' and row['reply_to'] is None  # not c, the name's new holder
    assert row['to_persona_session_key'] == 'codex:b' and row['answerable_by'] == 'beta'


def test_a_persona_that_takes_another_name_keeps_its_persona_key(pair, store):
    a, b = pair
    mid = send(a)['message_id']
    a.run('iam', {'handle': 'apex'})
    row = {r['id']: r for r in a.run('messages', {})['messages']}[mid]
    assert (row['from'], row['from_persona_session_key'], row['reply_to']) == ('alpha', 'codex:a', 'apex')


def test_messages_since_reports_a_change_on_the_sending_side(pair, store):
    import time
    from kaiusap.store import now
    a, b = pair
    mid = send(a)['message_id']
    time.sleep(0.01)
    mark = now()
    other(store, a.provider).run('iam', {'handle': 'alpha'})  # changes reply_to
    row = {r['id']: r for r in a.run('messages', {})['messages']}[mid]
    assert row['changed_at'] > mark


def test_sessions_lists_a_stopped_sender_still_owed_a_reply(pair, store):
    # Both ends of an owed request are listed, the sender too once another persona takes its name.
    a, b = pair
    mid = send(a)['message_id']
    other(store, a.provider).run('iam', {'handle': 'alpha'})
    row = rows(b)['codex:a']
    assert row['handle'] is None and row['formerly'][0] == {**row['formerly'][0], 'handle': 'alpha', 'how': 'taken'}
    with pytest.raises(Error, match='holds no handle now'):
        b.run('reply', {'id': mid, 'reply_needed': False, 'fields': {'body': 'Done'}})  # nowhere for a reply to go
    b.run('moot', {'id': mid})
    assert rows(b).get('codex:a') is None  # moot: nothing waits, so it drops off


def test_a_stopped_sender_of_mail_that_needs_no_reply_is_not_listed(pair, store):
    a, b = pair
    send(a, reply_needed=False, fields={'context': 'FYI'})
    other(store, a.provider).run('iam', {'handle': 'alpha'})
    assert rows(b).get('codex:a') is None
