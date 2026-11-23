"""The base's part of following a start: what a session with no handle is told when kaiusap can place it (a fresh
start's new session, a copy, a clone), the line the user is shown with it, the offer, and the generated name."""
import pytest

from kaiusap.adapters import Identity, Providers, UnavailableProvider
from kaiusap.core import Core
from kaiusap.following import CLONE, COPY, FRESH, TELL_ONCE, UNPLACED_SHOWN, follow
from kaiusap.hooks import merge
from kaiusap.model import PERSONAS, handles, open_offers
from kaiusap.store import identifier, now


class Adapter(UnavailableProvider):
    """A provider whose start step returns a fixed proposal, and that records what the base asks of it."""
    provider = "codex"

    def __init__(self, proposal=None, stays=True):
        self.proposal = proposal or {}
        self.shown_stays = stays
        self.fresh = []

    def follow_start(self, event, store):
        return dict(self.proposal)

    def current(self, event, proposal):
        return True

    def resume_command(self, session_key, cwd):
        return "resume " + session_key.split(":", 1)[1]

    def name_fresh(self, event, handle, name):
        self.fresh.append((event["session_key"], handle, name))
        return []

    def liveness(self, session_key):
        return {"live": False, "can_be_woken": False}

    def mirror(self, binding):
        return []

    def retire(self, binding, left_at, cause=None):
        return []


def bind(store, handle, key, reason="iam", moved_from=None):
    at = now()
    line = {"at": at, "id": identifier(at), "handle": handle, "reason": reason, "session_id": key.split(":", 1)[1],
            "session_key": key, "provider": key.split(":", 1)[0], "moved_from": moved_from, "also_released": None}
    store.append("handles.jsonl", line)
    return line


def start(store, adapter, key="codex:new", kind="clear"):
    event = {"provider": "codex", "kind": kind, "session_id": key.split(":", 1)[1], "session_key": key,
             "cwd": "/w", "at": now()}
    return follow(store, Providers({"codex": adapter}, store), event)


def offers(store, key="codex:new"):
    return [(o["offered"], o["from"]) for o in open_offers(store, session_key=key)]


# ----- a fresh start leaves the handle ---------------------------------------------------------


def test_a_fresh_start_tells_names_offers_and_shows_the_new_session(store):
    bind(store, "alpha", "codex:old")
    adapter = Adapter({"suggest": False, "fresh": {"handle": "alpha", "from": "codex:old"}})
    result = start(store, adapter)
    [suggested] = [r["suggested"] for r in store.rows(PERSONAS) if r.get("suggested")]
    assert result["texts"] == [FRESH.format(handle="alpha", noun="session", id="old", open="resume old",
                                            example=f", for example {suggested}")]
    assert result["shown"] == [f"kaiusap: new conversation. alpha stays with the one you left; to go back: resume old. "
                               f"To carry on as alpha here, say 'you are alpha'; or name this session, for example {suggested}."]
    assert not result["moved"] and handles(store)["alpha"]["session_key"] == "codex:old"
    assert offers(store) == [("alpha", "codex:old")]
    [(key, handle, name)] = adapter.fresh
    assert (key, handle) == ("codex:new", "alpha") and name.startswith("alpha~")


def test_the_generated_name_in_the_fresh_text_is_the_sessions_one_suggestion(store):
    bind(store, "alpha", "codex:old")
    start(store, Adapter({"suggest": False, "fresh": {"handle": "alpha", "from": "codex:old"}}))
    core = Core(store, Identity("codex", "new", "codex:new", "/w", "root"), Adapter())
    assert "suggested_handle" not in core.run("whoami", {})


def test_a_fresh_text_without_the_lock_has_no_example_and_leaves_the_suggestion(store, monkeypatch):
    bind(store, "alpha", "codex:old")
    real = store.locked

    def locked(*args, **kwargs):
        if not args or args[0] == "lock":  # the global lock; a file's append lock still works
            raise TimeoutError("busy")
        return real(*args, **kwargs)

    monkeypatch.setattr(store, "locked", locked)
    result = start(store, Adapter({"suggest": False, "fresh": {"handle": "alpha", "from": "codex:old"}}))
    assert result["texts"][0].endswith("or give it another name.")
    assert not [r for r in store.rows(PERSONAS) if r.get("suggested")]
    assert offers(store) == [("alpha", "codex:old")]  # the offer doesn't wait for the lock


def test_a_provider_whose_line_passes_asks_the_session_to_tell_the_user(store):
    bind(store, "alpha", "codex:old")
    result = start(store, Adapter({"suggest": False, "fresh": {"handle": "alpha", "from": "codex:old"}}, stays=False))
    assert result["texts"][0].endswith(TELL_ONCE)


def test_a_fresh_start_after_a_session_with_no_handle_says_and_writes_nothing(store):
    result = start(store, Adapter({"suggest": False}))
    assert result["texts"] == [] and result["shown"] == []
    assert list(store.rows(PERSONAS)) == []


# ----- copies --------------------------------------------------------------------------------


def test_a_copy_kaiusap_can_place_is_told_offered_and_shown(store):
    bind(store, "alpha", "codex:old")
    result = start(store, Adapter({"suggest": False, "copy_of": {"handle": "alpha", "from": "codex:old"}}), kind="fork")
    assert result["texts"] == [COPY.format(handle="alpha")]
    assert result["shown"] == ["kaiusap: this conversation is a copy of alpha's and holds no handle. To carry on as "
                               "alpha here, say 'you are alpha'."]
    assert offers(store) == [("alpha", "codex:old")]


def test_a_copy_kaiusap_cant_place_keeps_its_text_and_gets_the_shown_line(store):
    result = start(store, Adapter({"text": "copy text", "suggest": False, "unplaced": True}), kind="fork")
    assert result["texts"] == ["copy text"] and result["shown"] == [UNPLACED_SHOWN]
    assert offers(store) == []


def test_a_copy_the_user_is_moved_into_takes_the_handle(store):
    bind(store, "alpha", "codex:old")
    adapter = Adapter({"suggest": False, "text": "moved",
                       "move": {"handle": "alpha", "from": "codex:old", "to": "codex:new", "reason": "copy", "via": None}})
    result = start(store, adapter, kind="fork")
    assert result["moved"] and result["texts"] == ["moved"] and result["shown"] == []
    line = handles(store)["alpha"]
    assert (line["session_key"], line["reason"]) == ("codex:new", "copy")


# ----- clones --------------------------------------------------------------------------------


def test_a_session_a_copy_moved_on_from_is_a_clone(store):
    bind(store, "alpha", "codex:new", reason="copy",
         moved_from={"session_id": "orig", "provider": "codex", "session_key": "codex:orig"})
    result = start(store, Adapter(), key="codex:orig", kind="resume")
    assert result["texts"] == [CLONE.format(handle="alpha", noun="session", id="new", open="resume new")]
    assert result["shown"][0].startswith("kaiusap: this conversation is not alpha's current one.")
    assert offers(store, "codex:orig") == [("alpha", "codex:new")]


def test_a_session_left_by_an_offered_iam_is_a_clone_and_a_plain_iam_isnt(store):
    moved = {"session_id": "orig", "provider": "codex", "session_key": "codex:orig"}
    bind(store, "alpha", "codex:new", moved_from=moved)
    assert start(store, Adapter(), key="codex:orig", kind="resume")["texts"] == []
    store.append(PERSONAS, {"at": now(), "session_key": "codex:new", "continues": "codex:orig", "via": "confirmed",
                            "handle": "alpha"})
    assert start(store, Adapter(), key="codex:orig", kind="resume")["texts"][0].startswith(
        "This conversation is not alpha's current one.")


# ----- the hook output -------------------------------------------------------------------------


def test_a_shown_line_merges_beside_the_text_to_the_session():
    output = {"hookSpecificOutput": {"tell": "to the session"}}
    merge(output, {"systemMessage": "to the user"})
    merge(output, {"hookSpecificOutput": {"toast": "to the user"}})
    assert output == {"hookSpecificOutput": {"tell": "to the session", "toast": "to the user"}, "systemMessage": "to the user"}


@pytest.mark.parametrize("reason", ["copy"])
def test_a_copy_move_continues_the_persona(store, reason):
    from kaiusap.model import Personas
    bind(store, "alpha", "codex:old")
    bind(store, "alpha", "codex:new", reason=reason,
         moved_from={"session_id": "old", "provider": "codex", "session_key": "codex:old"})
    assert Personas(store).current("codex:old") == "codex:new"
