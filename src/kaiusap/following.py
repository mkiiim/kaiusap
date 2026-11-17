"""Follow a start: the rules every provider shares (record first, one move per start under the global lock, the guards,
the told texts, the suggestions). The provider's own steps are adapter code."""
import time

from .model import PERSONAS, handles, held, last_directory
from .store import identifier, now


def follow(store, providers, event, deadline=None, settings=None):
    deadline = deadline if deadline is not None else time.monotonic() + 3
    try:
        return _follow(store, providers, event, deadline, settings)
    finally:
        # A hook's call ends without the pending-name catch-up; a move's own
        # name is mirrored while following.
        providers.named(event["provider"]).finish(catch_up=False)


def _follow(store, providers, event, deadline=None, settings=None):
    """Apply an adapter's proposal for a start event; returns {texts, moved, line}.

    The caller (the start hook, step 4) has already appended the start record.
    """
    adapter = providers.named(event["provider"])
    event = {**event, "deadline": deadline}
    proposal = adapter.follow_start(event, store) or {}
    texts, line = [], None
    move = proposal.get("move")
    key = event["session_key"]
    if move:
        try:
            with store.locked(deadline=min(time.monotonic() + 1, deadline) if deadline else time.monotonic() + 1):
                bindings = handles(store)
                holding = held(bindings, key)
                source = bindings.get(move["handle"])
                if holding:
                    store.log(f"start {key}: no move, it already holds {holding}")
                    texts.append(f"You hold {holding}; you may also be {move['handle']}, which this conversation continued. Tell the user.")
                elif not source or source["session_key"] != move["from"]:
                    store.log(f"start {key}: no move, {move['handle']} is no longer bound to {move['from']}")
                elif not adapter.current(event, proposal):
                    store.log(f"start {key}: no move, the start record is no longer current")
                    return {"texts": [], "moved": False, "line": None, "proposal": proposal}
                else:
                    at = now()
                    proposed_line = {"at": at, "id": identifier(at), "handle": move["handle"], "reason": move["reason"],
                            "session_id": event["session_id"], "session_key": key, "provider": event["provider"],
                            "by_session": event["session_id"], "by_session_key": key,
                            "pid": event.get("pid"), "process_started_at": event.get("process_started_at"),
                            "moved_from": {"session_id": source["session_id"], "provider": source["provider"], "session_key": source["session_key"]},
                            "via": move.get("via"), "also_released": None}
                    store.append("handles.jsonl", proposed_line, deadline=deadline)
                    line = proposed_line
                    texts.append(proposal["text"])
        except (TimeoutError, OSError) as exc:
            store.log(f"start {key}: couldn't follow: {type(exc).__name__}: {exc}")
            if proposal.get("failed"):
                texts.append(proposal["failed"])
                # A move kaiusap couldn't make is an offer, so "you are <handle>" continues the persona.
                record_offers(store, key, [{"handle": move["handle"], "from": move["from"]}], deadline)
        if line:
            for warning in providers.mirror(line):
                store.log(f"start {key}: {warning}")
    elif proposal.get("text"):
        texts.append(proposal["text"])
    # What the adapter's steps established besides a move: offers it made in its text, and a continuation
    # of a persona that held no handle. The base writes them; the adapter decides them.
    record_offers(store, key, proposal.get("offers") or [], deadline)
    continued = proposal.get("persona")
    if continued and not line:
        try:
            with store.locked(deadline=min(time.monotonic() + 1, deadline) if deadline else time.monotonic() + 1):
                store.append(PERSONAS, {"at": now(), "session_key": key, "continues": continued["continues"],
                                        "via": continued["via"], "handle": None}, deadline=deadline)
        except (TimeoutError, OSError) as exc:
            store.log(f"start {key}: persona line not written: {type(exc).__name__}: {exc}")
    if not line and not held(handles(store), key):
        text = suggest(store, providers, event, proposal)
        if text:
            texts.append(text)
    return {"texts": texts, "moved": line is not None, "line": line, "proposal": proposal}


def record_offers(store, key, offers, deadline=None):
    """Write an offer line for each handle this session was told it may be, best effort."""
    for offer in offers:
        try:
            store.append(PERSONAS, {"at": now(), "session_key": key, "offered": offer["handle"], "from": offer["from"]},
                         deadline=deadline)
        except (TimeoutError, OSError) as exc:
            store.log(f"start {key}: offer of {offer.get('handle')} not written: {type(exc).__name__}: {exc}")


def suggest(store, providers, event, proposal):
    """The handle suggestions for a session that starts with no handle."""
    if event.get("kind") == "fork":
        return None
    bindings = handles(store)
    candidates = []
    # A /clear kaiusap followed (its persona continued, with no handle) isn't "a clear no handle followed", so it
    # gets no offer here; one session never continues two personas.
    if event.get("kind") == "clear" and proposal.get("suggest", True) and not proposal.get("told") and not proposal.get("persona"):
        for handle, binding in bindings.items():
            if binding and binding["session_key"] != event["session_key"] and not alive(providers, binding) \
                    and same(last_directory(store, binding["session_key"]), event.get("cwd")):
                candidates.append(handle)
    if candidates:
        # A /clear kaiusap couldn't follow is still the persona, so this suggestion is an offer.
        record_offers(store, event["session_key"], [{"handle": c, "from": bindings[c]["session_key"]} for c in candidates])
        names = " or ".join("`" + c + "`" for c in candidates)
        whose = f"{candidates[0]}'s session was" if len(candidates) == 1 else "their sessions were"
        return (f"You may be {names}: this conversation began with a /clear kaiusap couldn't follow, and {whose} in "
                f"this directory. Ask the user whether this is {'that assistant' if len(candidates) == 1 else 'one of them'} continuing.")
    session = proposal.get("tmux_session") or event.get("tmux_session")
    binding = bindings.get(session) if session else None
    if binding and binding["session_key"] != event["session_key"] and not alive(providers, binding):
        # The two choices and what each does with the mail.
        try:
            command = providers.where(binding["session_key"]).get("command")
        except Exception:
            command = None
        resume = f" ({command})" if command else " (from its provider's own list)"
        return (f"You may be `{session}` (this tmux session is named {session}), whose session isn't running. Ask the user: "
                f"to carry on as that same assistant, with its memory and its messages, quit and resume it{resume}; to take "
                f"only the name here, say 'you are {session}': its earlier messages stay with the old session.")
    return None


def alive(providers, binding):
    """Whether the binding's session is live for these suggestions: the adapter's `live`, which applies its
    provider's rule. A liveness check that fails counts as live, so nothing is suggested."""
    try:
        live = providers.liveness(binding["session_key"])
    except Exception:
        return True
    return live.get("live", live["can_be_woken"] or live.get("reason_code") == "detached")


def same(a, b):
    import os
    return bool(a and b) and os.path.normpath(a) == os.path.normpath(b)
