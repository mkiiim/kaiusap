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
    # What the adapter's steps established besides a move: offers it made in its text. The base writes
    # them; the adapter decides them.
    record_offers(store, key, proposal.get("offers") or [], deadline)
    shown = []
    if not line and not held(handles(store), key):
        placed = place(store, providers, adapter, event, proposal, deadline)
        if placed:
            if placed[0]:
                texts.append(placed[0])
            shown.append(placed[1])
        # A placed session gets no directory suggestion (its proposal says so), but still the tmux-name one.
        text = suggest(store, providers, event, {**proposal, "suggest": False} if placed else proposal)
        if text:
            texts.append(text)
    return {"texts": texts, "shown": shown, "moved": line is not None, "line": line, "proposal": proposal}


# What a session that holds no handle is told when kaiusap knows where it came from, and the line the user is
# shown with it. `{noun}` is the provider's word for a session.
FRESH = ("This is a new conversation. It is not {handle}: {handle} stays with the conversation left in this terminal, "
         "{noun} {id}. To go back to it: {open}. This {noun} has no handle. The user can say 'you are {handle}' to "
         "carry on as {handle} here, or give it another name{example}.")
FRESH_SHOWN = ("kaiusap: new conversation. {handle} stays with the one you left; to go back: {open}. To carry on as "
               "{handle} here, say 'you are {handle}'; or name this {noun}{example}.")
COPY = ("This conversation is a copy of {handle}'s. It has no handle. To carry on as {handle} here, the user says "
        "'you are {handle}'.")
COPY_SHOWN = ("kaiusap: this conversation is a copy of {handle}'s and holds no handle. To carry on as {handle} here, "
              "say 'you are {handle}'.")
CLONE = ("This conversation is not {handle}'s current one. {handle} went on in {noun} {id}; what was said there isn't "
         "here. To carry on as {handle} from there: {open}. To continue from here instead, the user says 'you are {handle}'.")
CLONE_SHOWN = ("kaiusap: this conversation is not {handle}'s current one. {handle} went on elsewhere; to go to it: "
               "{open}. To continue as {handle} here, say 'you are {handle}'.")
UNPLACED_SHOWN = "kaiusap: this conversation holds no handle. If it is a copy, the original keeps its handle."
# Ends the text to the session when the provider's line for the user doesn't stay on screen.
TELL_ONCE = " Tell the user this once, in your first reply."
# The moves a clone is left behind by: a binding with one of these reasons that names this session as its source.
CLONE_REASONS = {"copy", "moved-background"}


def place(store, providers, adapter, event, proposal, deadline=None):
    """The text and the shown line for a session with no handle that kaiusap can place: the new session of a
    fresh start (`fresh`), a copy the user wasn't moved into (`copy_of`), a copy kaiusap can't place
    (`unplaced`), or a clone, which the base finds itself. Writes the offer, and for a fresh start the generated
    name and the session's name. None when none applies."""
    key = event["session_key"]
    noun = adapter.session_noun
    fresh, copy_of = proposal.get("fresh"), proposal.get("copy_of")
    if fresh:
        handle, source = fresh["handle"], fresh["from"]
        # The offer first: the generated name waits for the lock, and is the part that may be left out.
        record_offers(store, key, [{"handle": handle, "from": source}], deadline)
        name = generated(store, key, deadline)
        example = f", for example {name}" if name else ""
        fields = {"handle": handle, "noun": noun, "id": source.split(":", 1)[1], "example": example,
                  "open": reopen(providers, source, event.get("cwd"))}
        from .model import retirement_name
        try:
            for warning in adapter.name_fresh(event, handle, retirement_name(handle, event["at"])) or []:
                store.log(f"start {key}: {warning}")
        except (OSError, TimeoutError) as exc:
            store.log(f"start {key}: fresh name not set: {type(exc).__name__}: {exc}")
        text, line = FRESH.format(**fields), FRESH_SHOWN.format(**fields)
    elif copy_of:
        handle = copy_of["handle"]
        record_offers(store, key, [{"handle": handle, "from": copy_of["from"]}], deadline)
        text, line = COPY.format(handle=handle), COPY_SHOWN.format(handle=handle)
    elif proposal.get("unplaced"):
        # The adapter's own text was told already; only the line for the user is added.
        return (None if adapter.shown_stays else TELL_ONCE.strip()), UNPLACED_SHOWN
    else:
        found = clone_of(store, key)
        if not found:
            return None
        handle, current = found["handle"], found["session_key"]
        record_offers(store, key, [{"handle": handle, "from": current}], deadline)
        fields = {"handle": handle, "noun": noun, "id": current.split(":", 1)[1],
                  "open": reopen(providers, current, event.get("cwd"))}
        text, line = CLONE.format(**fields), CLONE_SHOWN.format(**fields)
    if not adapter.shown_stays:
        text += TELL_ONCE
    return text, line


def clone_of(store, key):
    """The current binding whose handle moved on from this session by a copy, a background move, or an `iam`
    that answered an offer; None when there is none."""
    bindings = handles(store)
    confirmed = None
    for binding in bindings.values():
        if not binding or (binding.get("moved_from") or {}).get("session_key") != key:
            continue
        if binding.get("reason") in CLONE_REASONS:
            return binding
        if binding.get("reason") == "iam":
            if confirmed is None:
                confirmed = {(row.get("session_key"), row.get("continues")) for row in store.rows(PERSONAS)
                             if row.get("via") == "confirmed"}
            if (binding["session_key"], key) in confirmed:
                return binding
    return None


def generated(store, key, deadline=None):
    """The generated name a fresh session is told, which is its one suggestion: picked and recorded under the
    global lock. None when it can't be had in time, so the session's first call suggests one as usual."""
    from . import names
    try:
        with store.locked(deadline=min(time.monotonic() + 1, deadline) if deadline else time.monotonic() + 1):
            if any(row.get("session_key") == key and isinstance(row.get("suggested"), str) for row in store.rows(PERSONAS)):
                return None
            name = names.free(store)
            store.append(PERSONAS, {"at": now(), "session_key": key, "suggested": name}, deadline=deadline)
            return name
    except (TimeoutError, OSError, LookupError) as exc:
        store.log(f"start {key}: no generated name: {type(exc).__name__}: {exc}")
        return None


def reopen(providers, session_key, cwd):
    """How to open a session again: its provider's resume command, with a `cd` when its directory isn't `cwd`."""
    try:
        command = providers.for_key(session_key).resume_command(session_key, cwd)
    except Exception:
        command = None
    return command or "resume it from its provider's session list"


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
    # Only a /clear whose session left kaiusap couldn't find: one it found says what it found, or nothing.
    if event.get("kind") == "clear" and proposal.get("suggest", True) and not proposal.get("told"):
        for handle, binding in bindings.items():
            if binding and binding["session_key"] != event["session_key"] and not alive(providers, binding) \
                    and same(last_directory(store, binding["session_key"]), event.get("cwd")):
                candidates.append(handle)
    if candidates:
        # "You are <handle>" here continues that persona, so this suggestion is an offer.
        record_offers(store, event["session_key"], [{"handle": c, "from": bindings[c]["session_key"]} for c in candidates])
        if len(candidates) == 1:
            handle = candidates[0]
            return (f"This conversation began with a /clear. {handle}'s session was in this directory; if that is the "
                    f"conversation left, {handle} stays with it: {reopen(providers, bindings[handle]['session_key'], event.get('cwd'))}. "
                    f"The user can say 'you are {handle}' to carry on as {handle} here.")
        names = " or ".join(candidates)
        return (f"This conversation began with a /clear. The sessions of {names} were in this directory; if one of them "
                "is the conversation left, its handle stays with it. The user can say 'you are <handle>' to carry on "
                "as one of them here.")
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
