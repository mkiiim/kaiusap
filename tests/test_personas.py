"""A message belongs to the persona that held its recipient handle when it was sent. These
check the resolver, `Personas`, against the store's records directly."""
from kaiusap.model import PERSONAS, Personas, handles

T = ["2026-11-11T10:00:0%d.000000Z" % i for i in range(10)]


def line(store, at, handle, reason, key, moved_from=None, also_released=None):
    store.append("handles.jsonl", {
        "at": at, "id": f"id-{at}-{handle}", "handle": handle, "reason": reason,
        "session_id": key.split(":")[1] if key else None, "session_key": key, "provider": "claude",
        "moved_from": {"session_key": moved_from} if moved_from else None, "also_released": also_released})


def persona(store, at, key, continues, via, handle=None):
    store.append(PERSONAS, {"at": at, "session_key": key, "continues": continues, "via": via, "handle": handle})


def message(at, to_key, from_key="claude:z"):
    return {"id": "m", "created": at, "to": {"handle": "alpha", "session_key": to_key},
            "from": {"handle": "zed", "session_key": from_key}}


def resolver(store):
    return Personas(store)


def test_no_continuation_keeps_the_session(store):
    line(store, T[0], "alpha", "iam", "claude:s1")
    assert resolver(store).current("claude:s1", T[1]) == "claude:s1"


def test_a_fresh_start_carries_earlier_mail_but_not_later_mail_to_the_old_session(store):
    line(store, T[0], "alpha", "iam", "claude:s1")
    line(store, T[2], "alpha", "fresh-start", "claude:s2", moved_from="claude:s1")
    personas = resolver(store)
    assert personas.current("claude:s1", T[1]) == "claude:s2"  # sent before the /clear: follows it
    # Sent to s1 after the persona moved on: s1 is a clone by then, and keeps it.
    assert personas.current("claude:s1", T[3]) == "claude:s1"


def test_a_rewind_returns_to_the_earlier_session_in_recorded_order(store):
    line(store, T[0], "alpha", "iam", "claude:s1")
    line(store, T[2], "alpha", "fresh-start", "claude:s2", moved_from="claude:s1")
    line(store, T[4], "alpha", "rewind", "claude:s1", moved_from="claude:s2")
    personas = resolver(store)
    assert personas.current("claude:s1", T[1]) == "claude:s1"  # s1 -> s2 -> s1
    assert personas.current("claude:s2", T[3]) == "claude:s1"
    assert personas.current("claude:s1", T[5]) == "claude:s1"


def test_an_iam_move_does_not_continue_a_persona(store):
    line(store, T[0], "alpha", "iam", "claude:s1")
    line(store, T[2], "alpha", "iam", "codex:t1", moved_from="claude:s1")
    assert resolver(store).current("claude:s1", T[1]) == "claude:s1"


def test_a_continuation_with_no_handle_counts_and_a_handle_move_is_read_from_handles_only(store):
    persona(store, T[2], "claude:s2", "claude:s1", "fresh-start")  # no handle: its own record
    persona(store, T[4], "claude:s3", "claude:s2", "fresh-start", handle="alpha")  # a handle move with no handles line
    personas = resolver(store)
    assert personas.current("claude:s1", T[1]) == "claude:s2"
    assert personas.current("claude:s2", T[3]) == "claude:s2"


def test_a_confirmed_offer_counts_only_once_its_iam_landed(store):
    line(store, T[0], "alpha", "iam", "claude:s1")
    persona(store, T[2], "claude:s2", "claude:s1", "confirmed", handle="alpha")
    assert resolver(store).current("claude:s1", T[1]) == "claude:s1"  # the iam never landed
    line(store, T[3], "alpha", "iam", "claude:s2", moved_from="claude:s1")
    assert resolver(store).current("claude:s1", T[1]) == "claude:s2"


def test_offer_lines_are_not_continuations(store):
    store.append(PERSONAS, {"at": T[2], "session_key": "claude:s2", "offered": "alpha", "from": "claude:s1"})
    assert resolver(store).current("claude:s1", T[1]) == "claude:s1"


def test_the_recipient_follows_a_handle_change_in_the_same_persona(store):
    line(store, T[0], "alpha", "iam", "claude:s1")
    line(store, T[2], "beta", "iam", "claude:s1", also_released="alpha")  # "you are beta" in alpha's session
    personas = resolver(store)
    assert personas.recipient(message(T[1], "claude:s1"), handles(store)) == "beta"


def test_a_persona_with_no_handle_has_no_recipient(store):
    line(store, T[0], "alpha", "iam", "claude:s1")
    line(store, T[2], "alpha", "release", None)
    assert resolver(store).recipient(message(T[1], "claude:s1"), handles(store)) is None


def test_a_later_holder_of_the_name_does_not_get_earlier_mail(store):
    line(store, T[0], "alpha", "iam", "claude:s1")
    line(store, T[2], "alpha", "iam", "claude:s9", moved_from="claude:s1")  # another persona takes the name
    personas = resolver(store)
    bindings = handles(store)
    assert personas.recipient(message(T[1], "claude:s1"), bindings) is None  # s1 holds nothing now
    assert personas.recipient(message(T[3], "claude:s9"), bindings) == "alpha"


def test_a_reply_goes_to_the_sending_persona_under_its_current_handle(store):
    line(store, T[0], "zed", "iam", "claude:z")
    line(store, T[2], "zed", "fresh-start", "claude:z2", moved_from="claude:z")
    line(store, T[3], "zeta", "iam", "claude:z2", also_released="zed")
    assert resolver(store).sender(message(T[1], "claude:s1", "claude:z"), handles(store)) == "zeta"
