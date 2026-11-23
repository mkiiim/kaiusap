"""Provider-independent projections and message validation."""
from datetime import timedelta
import json
import re

from .errors import Error
from .store import instant

DEFAULTS = {"max_depth": 6, "max_per_pair_per_hour": 240, "max_per_hour": 600,
            "chain_join_minutes": 30, "wake_detached": True, "unread_note_days": 30,
            "tmux_session_names": "follow"}
CHOICES = {"tmux_session_names": {"off", "follow"}}  # added 2026-11-08 17:20 EST; "follow" is the default since 2026-11-09 22:33 EST
OPTIONAL = {"context", "current_state", "constraints", "starting_points"}
FIELDS = OPTIONAL | {"request", "expected_output", "body"}
CREDENTIAL_PATTERNS = {
    "AWS access key": r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b",
    "GitHub token": r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})",
    "OpenAI key": r"\bsk-(?:proj-|svcacct-)?[A-Za-z0-9_-]{20,}",
    "Anthropic key": r"\bsk-ant-[A-Za-z0-9_-]{20,}",
    "Slack token": r"\bxox[a-z]-[A-Za-z0-9-]{10,}",
    "private key header": r"-----BEGIN (?:[A-Z0-9 ]+ )?PRIVATE KEY-----",
    "JWT": r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+",
}


def validate(subject, reply_needed, fields, reply=False):
    if type(reply_needed) is not bool:
        raise Error("reply_needed is required. Use --reply-needed yes or no.")
    required = {"body"} if reply else ({"request", "expected_output"} if reply_needed else {"context"})
    if reply and reply_needed:
        required.add("expected_output")
    allowed = required | OPTIONAL
    if set(fields) - allowed:
        raise Error("Fields not allowed for this message: " + ", ".join(sorted(set(fields) - allowed)) + ". Check kaiusap help.")
    if required - set(fields):
        raise Error("Missing required fields: " + ", ".join(sorted(required - set(fields))) + ". Check kaiusap help.")
    if not isinstance(subject, str) or not subject.strip() or len(subject) > 80 or any(c in subject for c in "\r\n\u0085\u2028\u2029"):
        raise Error("Subject must be one nonempty line of at most 80 characters.")
    for name, value in fields.items():
        if not isinstance(value, str) or not value.strip():
            raise Error(f"{name} is empty. Supply a nonempty field.")
        if len(value.encode("utf-8")) > 65536:
            raise Error(f"{name} exceeds 64 KB. Shorten the field.")
    for name, pattern in CREDENTIAL_PATTERNS.items():
        if any(re.search(pattern, value) for value in [subject, *fields.values()]):
            raise Error(f"Text matches a known credential shape ({name}). Remove the credential before sending.")


def apply_line(result, line):
    handle = line["handle"]
    if line.get("also_released") in result:
        result[line["also_released"]] = None
    if line["reason"] == "release":
        result[handle] = None
    elif line["reason"] == "process":
        if result.get(handle):
            result[handle] = {**result[handle], "pid": line["pid"],
                              "process_started_at": line["process_started_at"]}
    else:
        result[handle] = line


def handles(store, before=None):
    """Bindings by handle; with `before`, as they stood before the line with that id."""
    result = {}
    for line in store.rows("handles.jsonl"):
        if before is not None and line.get("id") == before:
            break
        try:
            apply_line(result, line)
        except (KeyError, TypeError):
            continue
    return result


def ended_by(store, line):
    """The bindings a handle line ended: the one it moved from, released, or displaced."""
    previous = handles(store, before=line.get("id"))
    ended = []
    if line.get("reason") == "release" or line.get("moved_from"):
        ended.append(previous.get(line["handle"]))
    if line.get("also_released"):
        ended.append(previous.get(line["also_released"]))
    return [binding for binding in ended if binding]


def binding_history(store):
    """Every binding that has ended, with the time its handle left, oldest first."""
    result, ended = {}, []
    for line in store.rows("handles.jsonl"):
        try:
            for binding in ended_by_state(result, line):
                ended.append((binding, line["at"]))
            apply_line(result, line)
        except (KeyError, TypeError):
            continue
    return ended, result


def ended_by_state(state, line):
    ended = []
    if line.get("reason") == "release" or line.get("moved_from"):
        ended.append(state.get(line["handle"]))
    if line.get("also_released"):
        ended.append(state.get(line["also_released"]))
    if line.get("reason") in {"iam", "fresh-start", "rewind", "moved-directory", "moved-background", "copy"} and not line.get("moved_from"):
        current = state.get(line["handle"])
        if current and current.get("session_key") != line.get("session_key"):
            ended.append(current)
    return [binding for binding in ended if binding]


def load_settings(store):
    """settings.json with the defaults for missing keys, the providers' own keys included; a malformed file or
    value means the defaults. A string value must not be blank."""
    from .adapters import provider_settings
    defaults = {**DEFAULTS, **provider_settings()}
    try:
        data = json.loads((store.root / "settings.json").read_text())
        if not isinstance(data, dict):
            raise ValueError()
        for key, default in defaults.items():
            value = data.get(key, default)
            if type(value) is not type(default) or (type(value) is int and value < 0) or (
                    key in CHOICES and value not in CHOICES[key]) or (type(value) is str and not value.strip()):
                raise ValueError()
        return {**defaults, **data}
    except FileNotFoundError:
        return defaults
    except (ValueError, TypeError):
        store.warnings.append("Malformed settings; using defaults.")
        return defaults


def retirement_name(handle, left_at):
    """The name for a session that lost `handle`: `<handle>~<yymmddhhmmss>`, the local date and time,
    to the second, of the handle line's `at` (amended 2026-11-08 17:36 EST; it was `<handle>~<yyyymmdd>`)."""
    return f"{handle}~{instant(left_at).astimezone().strftime('%y%m%d%H%M%S')}"


def last_directory(store, session_key):
    """The session's last recorded directory: its newest start, cwd or prompt line."""
    best = None
    for relative in ("sessions/starts.jsonl", "sessions/prompts.jsonl"):
        row = store.latest(relative, lambda r: r.get("session_key") == session_key and r.get("cwd"))
        if row and (best is None or row["at"] > best["at"]):
            best = row
    return best["cwd"] if best else None


def prompted_since(store, session_key, created):
    """Whether the session has a prompt record newer than `created`: such a prompt ends its chain.
    Scans backward and stops at lines more than 3 s older than `created`."""
    created = instant(created)
    floor = created - timedelta(seconds=3)
    for row in store.rows("sessions/prompts.jsonl", reverse=True):
        try:
            at = instant(row["at"])
        except (KeyError, ValueError, TypeError, AttributeError):
            continue
        if at < floor:
            return False
        if at > created and row.get("session_key") == session_key:
            return True
    return False


PREVIEW_FIELDS = ("body", "request", "context")
PREVIEW_LIMIT = 256


def preview_text(text):
    """The preview of one field's text: its first two non-blank lines, each with its runs of
    whitespace collapsed to one space and trimmed, joined by a newline, cut to 256 characters.
    When a non-blank line or a character was left out, `…` is added, replacing the last character
    kept only when the result would pass 256. Blank lines and collapsed whitespace are never
    counted as left out."""
    lines = [" ".join(line.split()) for line in text.splitlines()]
    lines = [line for line in lines if line]
    kept = "\n".join(lines[:2])
    if len(lines) <= 2 and len(kept) <= PREVIEW_LIMIT:
        return kept
    return kept[:PREVIEW_LIMIT - 1] + "…"


def preview(message):
    """`(preview_field, preview)` for `messages --preview`: the first of body, request and
    context the message's fields has; both null when it has none or that field isn't text."""
    fields = message.get("fields")
    field = next((name for name in PREVIEW_FIELDS if name in fields), None) if isinstance(fields, dict) else None
    if field is None or not isinstance(fields[field], str):
        return None, None
    return field, preview_text(fields[field])


def held(bindings, key):
    return next((name for name, binding in bindings.items() if binding and binding["session_key"] == key), None)


# The handle moves that continue a persona, and the file of continuations with no handle
# and of offers. A move with a handle is read from its `handles.jsonl` line alone, so it is one write.
CONTINUING = {"fresh-start", "rewind", "moved-directory", "moved-background", "copy"}
PERSONAS = "sessions/personas.jsonl"


class Personas:
    """Persona continuations, oldest first: which session a persona went on in after a given time.

    Edges come from `handles.jsonl` lines whose reason continues a persona and that name the session moved from,
    and from `sessions/personas.jsonl` lines that continue a session (`continues`) with no handle move: a
    continuation with no handle, or a `confirmed` offer whose `iam` line was written. Offer lines (`offered`)
    are not continuations."""

    def __init__(self, store):
        self.edges = {}  # session key -> [(at, next session key)], oldest first
        iam_moves = set()  # (from key, to key) of every `iam` line that moved a handle
        for line in store.rows("handles.jsonl"):
            moved = line.get("moved_from") or {}
            source, target = moved.get("session_key"), line.get("session_key")
            if not source or not target or source == target or not isinstance(line.get("at"), str):
                continue
            if line.get("reason") in CONTINUING:
                self.add(source, line["at"], target)
            elif line.get("reason") == "iam":
                iam_moves.add((source, target))
        for line in store.rows(PERSONAS):
            source, target, at = line.get("continues"), line.get("session_key"), line.get("at")
            if not isinstance(source, str) or not isinstance(target, str) or not isinstance(at, str) or source == target:
                continue
            if line.get("via") == "confirmed" and (source, target) not in iam_moves:
                continue  # the `iam` that confirms it never landed
            if line.get("via") in CONTINUING and line.get("handle"):
                continue  # a move with a handle is its handles.jsonl line
            self.add(source, at, target)
        for targets in self.edges.values():
            targets.sort(key=lambda edge: instant(edge[0]))

    def add(self, source, at, target):
        self.edges.setdefault(source, []).append((at, target))

    def sessions_of(self, session_key):
        """Every session whose continuations can lead to `session_key`, itself included: where its persona's mail
        may have been addressed (the hook's projection reads these, then checks each message's time)."""
        reverse = {}
        for source, targets in self.edges.items():
            for _, target in targets:
                reverse.setdefault(target, set()).add(source)
        found, todo = {session_key}, [session_key]
        while todo:
            for source in reverse.get(todo.pop(), ()):
                if source not in found:
                    found.add(source)
                    todo.append(source)
        return found

    def path(self, session_key, at=None):
        """The continuations followed from `session_key` as it was at `at`: [(at, session key)], oldest first."""
        steps, when = [], instant(at) if at else None
        while session_key and len(steps) < 1024:
            following = next(((edge_at, target) for edge_at, target in self.edges.get(session_key, ())
                              if when is None or instant(edge_at) > when), None)
            if not following:
                break
            steps.append(following)
            when, session_key = instant(following[0]), following[1]
        return steps

    def current(self, session_key, at=None):
        """The persona's session now, starting from `session_key` as it was at `at` (an ISO time; None follows
        every continuation): at each session, the first continuation later than the time reached so far."""
        if not session_key:
            return session_key
        seen = 0
        when = instant(at) if at else None
        while seen < 1024:
            seen += 1
            following = next(((edge_at, target) for edge_at, target in self.edges.get(session_key, ())
                              if when is None or instant(edge_at) > when), None)
            if not following:
                return session_key
            when, session_key = instant(following[0]), following[1]
        return session_key

    def recipient(self, message, bindings):
        """The handle that may answer `message` now (`answerable_by`), or None when the
        persona it belongs to holds no handle."""
        return self.holder(message.get("to") or {}, message.get("created"), bindings)

    def sender(self, message, bindings):
        """The handle a reply to `message` goes to now, or None when the sending persona holds none."""
        return self.holder(message.get("from") or {}, message.get("created"), bindings)

    def holder(self, side, at, bindings):
        key = side.get("session_key")
        if not key:
            return None
        return held(bindings, self.current(key, at))


def open_offers(store, offered=None, session_key=None, source=None):
    """Open offers: offer lines of `sessions/personas.jsonl`, the latest per session and handle, that no
    later move of the handle and no later start of the offered session (a resume) has closed. Filtered by the
    handle offered, the session offered it, and the session it would come from."""
    latest = {}
    for row in store.rows(PERSONAS):
        if not isinstance(row.get("offered"), str) or not isinstance(row.get("at"), str):
            continue
        if (offered and row["offered"] != offered) or (session_key and row.get("session_key") != session_key) or (
                source and row.get("from") != source):
            continue
        pair = (row.get("session_key"), row["offered"])
        if pair not in latest or instant(row["at"]) > instant(latest[pair]["at"]):
            latest[pair] = row
    if not latest:
        return []
    moved, restarted = {}, {}
    for line in store.rows("handles.jsonl"):
        if isinstance(line.get("at"), str) and line.get("handle"):
            moved.setdefault(line["handle"], []).append(instant(line["at"]))
    for row in store.rows("sessions/starts.jsonl"):
        if row.get("source") == "resume" and isinstance(row.get("at"), str):
            restarted.setdefault(row.get("session_key"), []).append(instant(row["at"]))
    found = []
    for (key, handle), row in latest.items():
        since = instant(row["at"])
        if any(at > since for at in moved.get(handle, ())) or any(at > since for at in restarted.get(key, ())):
            continue
        found.append(row)
    return found


class MailView:
    """Mail by persona: each message belongs to the persona that held its `to` handle when it was sent.
    `owner` is that persona's current session; `recipient` the handle it holds now."""

    def __init__(self, store, since=None):
        self.store = store
        self._personas = self._bindings = self._holders = None
        self.messages = list(store.messages(since))
        self.events = list(store.events(since))
        self.answered = {m["in_reply_to"] for m in self.messages if m["in_reply_to"]}
        self.replied_at = {}  # message id -> the `created` of each reply naming it
        for m in self.messages:
            if m["in_reply_to"]:
                self.replied_at.setdefault(m["in_reply_to"], []).append(m["created"])
        self.by_message = {}
        for event in self.events:
            self.by_message.setdefault(event.get("message_id"), []).append(event)

    @property
    def personas(self):
        if self._personas is None:
            self._personas = Personas(self.store)
        return self._personas

    @property
    def bindings(self):
        if self._bindings is None:
            self._bindings = handles(self.store)
        return self._bindings

    def owner(self, message):
        """The session key the message's persona holds now (its recipient side)."""
        return self.personas.current(message["to"]["session_key"], message["created"])

    def sender_owner(self, message):
        """The session key of the persona that sent it, as it is now (`inbox --sent`)."""
        return self.personas.current(message["from"]["session_key"], message["created"])

    def recipient(self, message):
        """`answerable_by`: the handle that may reply to or moot it now, or None."""
        return held(self.bindings, self.owner(message))

    def sender(self, message):
        """The handle a reply to it goes to now, or None."""
        return held(self.bindings, self.sender_owner(message))

    def holder_changes(self, session_key):
        """When the handle a session holds changed: each binding of it beginning or ending (`messages --since`)."""
        if self._holders is None:
            self._holders = {}
            for line in self.store.rows("handles.jsonl"):
                if line.get("session_key") and isinstance(line.get("at"), str) and line.get("reason") not in {"release", "process"}:
                    self._holders.setdefault(line["session_key"], []).append(line["at"])
            ended, _ = binding_history(self.store)
            for binding, left_at in ended:
                self._holders.setdefault(binding["session_key"], []).append(left_at)
        return self._holders.get(session_key, [])

    def receipts(self, message):
        # A read is a receipt only when its reader was the message's persona holding a handle; the
        # event records that decision, so every receipt of the message counts, whatever handle it held then.
        return sorted((e for e in self.by_message.get(message["id"], [])
                       if e["event"] == "read" and e.get("receipt")), key=lambda e: instant(e["at"]))

    def state(self, message):
        if message["reply_needed"] and any(e["event"] == "moot" for e in self.by_message.get(message["id"], [])):
            return "moot"
        if message["id"] in self.answered:
            return "answered"
        return "read" if self.receipts(message) else "unread"

    def owed(self, message):
        """The one definition of owed: the recipient handle owes a reply for a `reply_needed: true`
        message that is neither answered nor moot."""
        return message["reply_needed"] is True and self.state(message) in {"unread", "read"}

    def changed_at(self, message):
        """The latest change `messages --since` sees: the message's creation, a receipt by
        the recipient handle, a moot, a reply naming it, or an attempt or delivery line."""
        events = self.by_message.get(message["id"], [])
        # A continuation or a handle change that changes `answerable_by` is a change too.
        # So is one on the sending side, which changes `from_persona_session_key` or `reply_to`.
        moved = []
        for side in ("to", "from"):
            steps = self.personas.path(message[side]["session_key"], message["created"])
            owner = steps[-1][1] if steps else message[side]["session_key"]
            moved += [at for at, _ in steps] + [at for at in self.holder_changes(owner)
                                                if instant(at) > instant(message["created"])]
        times = [message["created"], *(e["at"] for e in self.receipts(message)),
                 *(e["at"] for e in events if e["event"] in {"moot", "attempt", "delivery"}),
                 *self.replied_at.get(message["id"], []), *moved]
        return max(times, key=instant)

    def unread(self, handle):
        """The unread messages whose recipient is `handle` now. A reply or moot is not a read receipt."""
        return [m for m in self.messages if self.recipient(m) == handle and not self.receipts(m)]

    def is_open(self, message):
        """Open mail: unread (not read, answered or moot) or owed."""
        return self.state(message) == "unread" or self.owed(message)

    def unread_for(self, session_key):
        """The unread messages of the persona whose current session is `session_key`, handle or not."""
        return [m for m in self.messages if self.owner(m) == session_key and not self.receipts(m)]

    def outcome(self, message):
        events = self.by_message.get(message["id"], [])
        deliveries = [e for e in events if e["event"] == "delivery"]
        if deliveries:
            last = max(deliveries, key=lambda e: instant(e["at"]))
            return last["outcome"], last["reason_code"]
        if any(e["event"] == "attempt" for e in events):
            return "uncertain", "no_confirmation"
        return "stored", "not_attempted"

    def joined_chain(self, session_key, at, minutes):
        candidates = []
        for e in self.events:
            if e["event"] != "attempt" or e.get("to_session_key") != session_key:
                continue
            age = (instant(at) - instant(e["at"])).total_seconds()
            if not 0 <= age <= minutes * 60:
                continue
            message = next((m for m in self.messages if m["id"] == e["message_id"]), None)
            if message and self.outcome(message)[0] != "stored":
                candidates.append((e, message))
        return max(candidates, key=lambda pair: instant(pair[0]["at"]))[1] if candidates else None


def fresh_pending(events, session_key):
    """The name of a fresh start's new session still to apply: its latest `fresh` naming event, when that set no
    name. None otherwise, or once it has been tried again."""
    fresh = [e for e in events if e.get("event") == "named" and e.get("kind") == "fresh" and e.get("session_key") == session_key]
    if not fresh or fresh[-1].get("name") is not None or any(e.get("retry") for e in fresh):
        return None
    return fresh[-1] if isinstance(fresh[-1].get("wanted"), str) else None
