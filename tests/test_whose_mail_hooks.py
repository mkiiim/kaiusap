"""Whose mail, on the hook side: the unread-mail note and the hook's projection follow the persona."""
from kaiusap.hooks import unread_note
from kaiusap.hookstate import HookState
from kaiusap.model import PERSONAS
from kaiusap.store import now


def bind(store, handle, key, reason="iam", moved_from=None, also_released=None):
    at = now()
    store.append("handles.jsonl", {"at": at, "id": f"id-{at}", "handle": handle, "reason": reason,
                                   "session_id": key.split(":")[1], "session_key": key, "provider": "codex",
                                   "moved_from": {"session_key": moved_from} if moved_from else None,
                                   "also_released": also_released})


def mail(store, to, to_key, mid_suffix):
    at = now()
    message = {"schema": 1, "id": at[:4] + at[5:7] + at[8:10] + "T" + at[11:13] + at[14:16] + at[17:19] + "Z-" + mid_suffix,
               "created": at, "from": {"handle": "zed", "session_id": "z", "provider": "codex", "session_key": "codex:z"},
               "to": {"handle": to, "session_id": to_key.split(":")[1], "session_key": to_key}, "reply_needed": False,
               "in_reply_to": None, "chain_id": "c", "depth": 0, "subject": "S", "fields": {"context": "c"}}
    store.save_message(message)
    return message


def note(store, key):
    state = HookState(store, "codex", key.split(":")[1])
    return unread_note(store, state, {"session_id": key.split(":")[1], "session_key": key, "prompt": "hi"}, None)


def test_a_persona_that_took_another_name_is_told_of_its_earlier_mail(store):
    bind(store, "alpha", "codex:s1")
    mail(store, "alpha", "codex:s1", "aaaaaa")
    bind(store, "bravo", "codex:s1", also_released="alpha")
    text = note(store, "codex:s1")
    assert text.startswith("kaiusap: 1 unread message, from zed. Tell the user.")
    assert note(store, "codex:s1") is None  # once per session


def test_a_persona_with_no_handle_is_told_to_ask_for_one(store):
    bind(store, "alpha", "codex:s1")
    mail(store, "alpha", "codex:s1", "bbbbbb")
    bind(store, "alpha", "codex:s9", moved_from="codex:s1")  # another persona is given the name
    text = note(store, "codex:s1")
    assert text == "kaiusap: 1 unread message, from zed. You hold no handle; ask the user for one to reply. Tell the user."
    assert note(store, "codex:s9") is None  # the later holder isn't told of earlier mail


def test_a_fresh_start_carries_the_mail_and_the_new_session_is_told_again(store):
    bind(store, "alpha", "codex:s1")
    mail(store, "alpha", "codex:s1", "cccccc")
    assert note(store, "codex:s1")  # told in s1
    bind(store, "alpha", "codex:s2", reason="fresh-start", moved_from="codex:s1")
    assert note(store, "codex:s2").startswith("kaiusap: 1 unread message")  # its memory was cleared


def test_a_continuation_with_no_handle_carries_the_mail(store):
    mail(store, "alpha", "codex:s1", "dddddd")  # alpha's persona, already with no handle
    store.append(PERSONAS, {"at": now(), "session_key": "codex:s2", "continues": "codex:s1", "via": "fresh-start", "handle": None})
    assert note(store, "codex:s2").startswith("kaiusap: 1 unread message, from zed. You hold no handle")
