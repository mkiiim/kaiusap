"""Provider boundary: the provider contract and the registry the base calls through."""
# Provider's method `list` hides the built-in in its class body; string
# annotations resolve `list[str]` against the module, on every Python.
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from ..errors import Error


# Closes each adapter's "can't tell which session is calling" refusal: an assistant kaiusap can't place
# tells the user rather than reading the store itself.
UNIDENTIFIED_CLOSE = " Tell the user; don't read kaiusap's files unless they ask."
SPAWNED_AGENT_TEXT = 'You are a spawned agent and may only look (whoami, who, inbox, read, help, pause). Report to the agent that started you, which sends.'


@dataclass(frozen=True)
class Identity:
    provider: str | None = None
    session_id: str | None = None
    session_key: str | None = None
    cwd: str | None = None
    classification: str = "plain_shell"
    parent_key: str | None = None
    pid: int | None = None
    process_started_at: str | None = None
    agent_id: str | None = None
    reason: str | None = None


class Provider(Protocol):
    """The provider obligations, and what the base calls besides: the hook-time ones,
    the call bracket and `setup`'s items. Every one is called directly."""
    provider: str  # the session key's prefix, as in the registry
    harness_maker: str  # who made the provider's harness, lowercase, as `sessions` shows it
    delivers: bool
    session_variable: str | None  # the shell variable that marks one of its sessions
    hook_events: frozenset[str]  # the provider hook events `kaiusap hook <p> <event>` handles; others are refused
    record_only_events: frozenset[str]  # of those, the ones whose hook writes its record and nothing else
    start_events: frozenset[str]  # of those, the start hook's: 3 s, following and the session line
    prompt_events: frozenset[str]  # of those, the prompt hook's, which adds the unread-mail note on every call
    screen_events: frozenset[str]  # of those, the tool-call screens decided by `pre_tool`, before any store work
    setup_flags: frozenset[str]  # the `kaiusap setup` flags its `setup_items` read from `setup.options`, as keys

    @classmethod
    def claims(cls, environment: dict) -> bool: ...
    @classmethod
    def pre_tool(cls, payload: dict) -> dict | None: ...  # a screen event's output: None when nothing is decided, else the hook output
    @classmethod
    def screen_timeout(cls, event: str, payload: dict) -> dict | None: ...  # the output when a screen can't be decided in time (or fails): its refusal, so the call fails closed; None for nothing
    @classmethod
    def setup_items(cls, setup) -> list[tuple[int, dict]]: ...  # (its place in setup's order, the item)
    @classmethod
    def setup_command(cls, setup) -> str | None: ...  # the provider's own command; not on the PATH and nothing done: its items are skipped
    def begin(self) -> None: ...  # a CLI or MCP call starts, before its identity lookup (idempotent within a call)
    def reset(self) -> None: ...  # drop this call's caches; the call continues
    def finish(self, catch_up: bool = True) -> None: ...  # the call ends
    def identity(self, environment: dict, store) -> Identity: ...
    def mcp_identity(self, meta: dict | None, stated=None) -> Identity: ...  # `stated`: the identity argument, or None; raises Error when the caller can't be identified
    def directory(self, identity: Identity) -> str | None: ...
    def events(self, payload: dict, deadline: float | None = None) -> dict | None: ...
    def hook_deadline(self, at: float): ...
    def hook_record(self, name: str, event: dict, deadline: float, recorded: bool = False, native=None) -> None: ...
    def hook_output(self, event: dict, state) -> dict: ...
    def marker_fields(self, state, event: dict) -> dict: ...
    def where(self, session_key: str) -> dict: ...
    def liveness(self, session_key: str) -> dict: ...  # with `live`: whether the start hook's handle suggestions count the session as live
    def unidentified(self, identity: Identity) -> dict: ...
    def deliver(self, store, message: dict, deadline: float | None = None) -> dict: ...
    def list(self) -> list[dict]: ...
    def tell(self, text: str, event: str = "UserPromptSubmit") -> dict: ...
    def mirror(self, binding: dict) -> list[str]: ...
    def retire(self, binding: dict, left_at: str, cause: str | None = None) -> list[str]: ...
    def refuse_before_acting(self, identity: Identity) -> None: ...
    def follow_start(self, event: dict, store) -> dict | None: ...
    def current(self, event: dict, proposal: dict) -> bool: ...
    def tmux_start(self, event: dict) -> None: ...
    def context(self, session_key: str) -> dict | None: ...  # `sessions`' context use: {used, limit, limit_source, model, model_maker, at}, or None
    def harness(self, session_key: str) -> str | None: ...  # `sessions`' harness_maker: who made the provider's harness
    def harness_version(self, session_key: str) -> str | None: ...  # `sessions`' harness_version: the version running a live session now, else None
    def activity(self, session_key: str) -> dict | None: ...  # `sessions`' activity: {state, waiting_on, observed_at} for a live session, else None


# How far back the end of a session's file is read for its context use, in steps.
TAIL_STEP = 64 * 1024
TAIL_LIMIT = 1024 * 1024


def tail_records(path, limit=TAIL_LIMIT, step=TAIL_STEP):
    """The JSON objects of a JSONL file's last lines, newest first, reading back at most `limit` bytes.
    A line cut by the limit, a line that isn't a JSON object and a file that can't be read are skipped."""
    import json
    try:
        with open(path, "rb") as handle:
            end = handle.seek(0, 2)
            position, head = end, b""
            while position > 0 and end - position < limit:
                size = min(step, position, limit - (end - position))
                position -= size
                handle.seek(position)
                lines = (handle.read(size) + head).split(b"\n")
                head = lines.pop(0) if position > 0 else b""
                for line in reversed(lines):
                    try:
                        record = json.loads(line)
                    except ValueError:
                        continue
                    if isinstance(record, dict):
                        yield record
    except (OSError, TypeError, ValueError):
        return


# Who made a model (`sessions`' `context.model_maker`): the first table entry whose name starts the model id
# (or a part of it after a `/` or `.`, as in `google/gemini-…` or `us.anthropic.claude-…`), lowercase. A model
# the table doesn't name has no maker, whoever served it.
MODEL_MAKERS = (
    ("claude", "anthropic"), ("gpt", "openai"), ("chatgpt", "openai"), ("codex", "openai"), ("o1", "openai"),
    ("o3", "openai"), ("o4", "openai"), ("gemini", "google"), ("gemma", "google"), ("grok", "xai"),
    ("llama", "meta"), ("mistral", "mistral"), ("devstral", "mistral"), ("codestral", "mistral"),
    ("deepseek", "deepseek"), ("qwen", "alibaba"), ("kimi", "moonshotai"), ("glm", "zhipu"),
    ("big-pickle", "opencode"),
)
# Model routes that are themselves makers: a provider's own name, when the model id isn't in the table.
MAKER_ROUTES = {"anthropic": "anthropic", "openai": "openai", "google": "google", "google-vertex": "google",
                "xai": "xai", "mistral": "mistral", "deepseek": "deepseek", "moonshotai": "moonshotai"}


def model_maker(model, route=None):
    """Who made `model`, from MODEL_MAKERS; else `route` when it is itself a maker (MAKER_ROUTES); else None."""
    import re
    if isinstance(model, str) and model:
        for part in re.split(r"[/.]", model.lower()):
            for prefix, maker in MODEL_MAKERS:
                if part.startswith(prefix) and (len(part) == len(prefix) or not part[len(prefix)].isalpha()):
                    return maker
    return MAKER_ROUTES.get(route.lower()) if isinstance(route, str) else None


def context_use(used, limit, model, at, route=None):
    """The `context` value, or None when `used` isn't a count. `limit_source` is "provider" when the provider
    gave the limit, else null with the limit (no limit is inferred). `model_maker` from `model_maker`; `route`
    is the provider the request went through, when the session's provider serves several."""
    if type(used) is not int or used < 0:
        return None
    limit = limit if type(limit) is int and limit > 0 else None
    model = model if isinstance(model, str) and model else None
    return {"used": used, "limit": limit, "limit_source": "provider" if limit else None,
            "model": model, "model_maker": model_maker(model, route), "at": at if isinstance(at, str) else None}


class UnavailableProvider:
    """No provider facts are fabricated while an adapter is absent (Claude until step 3).

    Tests inject identities and observations at this boundary. The stub refuses
    session-dependent operations; plain-shell operations work.
    """
    delivers = False
    harness_maker = None
    session_variable = None
    hook_events = frozenset()
    record_only_events = frozenset()
    start_events = frozenset()
    prompt_events = frozenset()
    screen_events = frozenset()
    setup_flags = frozenset()

    @classmethod
    def claims(cls, environment):
        return False

    @classmethod
    def pre_tool(cls, payload):
        return None

    @classmethod
    def screen_timeout(cls, event, payload):
        return None

    @classmethod
    def setup_items(cls, setup):
        return []

    @classmethod
    def setup_command(cls, setup):
        return None

    def begin(self):
        return None

    def reset(self):
        return None

    def finish(self, catch_up=True):
        return None

    def identity(self, environment, store):
        if environment.get("CLAUDE_CODE_SESSION_ID") or environment.get("CODEX_THREAD_ID"):
            raise Error("Provider identity is not implemented for this provider yet. Use the core test fixtures until its adapter is implemented.")
        return Identity()

    def mcp_identity(self, meta, stated=None):
        raise Error("Provider identity is not implemented for this provider yet.")

    def directory(self, identity):
        return None

    def events(self, payload, deadline=None):
        return None

    def hook_deadline(self, at):
        return at

    def hook_record(self, name, event, deadline, recorded=False, native=None):
        return None

    def hook_output(self, event, state):
        return {}

    def marker_fields(self, state, event):
        return {}

    def where(self, session_key):
        return {"cwd": None, "name": None, "where": "unknown", "tmux_panes": [], "command": None,
                "trusted": None, "detached": False, "note": None}

    def liveness(self, session_key):
        raise Error("Provider liveness is not implemented for this provider yet.")

    def unidentified(self, identity):
        return {"can_be_woken": False, "why_not": identity.reason, "reason_code": "unknown_caller", "fix": None}

    def deliver(self, store, message, deadline=None):
        # Temporary seam (step 1): no attempt event and no provider interaction.
        return {"outcome": "stored", "message_id": message["id"],
                "reason_code": "not_attempted", "reason": "not attempted",
                "paste_line": "Run kaiusap read " + message["id"],
                "warnings": ["Delivery is not implemented for this provider yet."]}

    def list(self):
        return []

    def tell(self, text, event="UserPromptSubmit"):
        return {}

    def mirror(self, binding):
        return ["Provider name mirroring is not implemented for this provider yet."]

    def retire(self, binding, left_at, cause=None):
        return ["Provider name retirement is not implemented for this provider yet."]

    def refuse_before_acting(self, identity):
        if identity.classification in {"spawned", "unknown"}:
            raise Error("This caller cannot act. Report to the parent, which sends, or install the provider adapter to establish identity.")

    def follow_start(self, event, store):
        return None

    def current(self, event, proposal):
        return False

    def tmux_start(self, event):
        return None

    def context(self, session_key):
        return None

    def harness(self, session_key):
        return None

    def harness_version(self, session_key):
        return None

    def activity(self, session_key):
        return None



# `activity`: the states and the waits a reading may name, in this order.
ACTIVITY_STATES = ("idle", "working", "blocked", "error")
ACTIVITY_WAITS = ("approval", "user_input")


def activity_reading(state, waiting_on=()):
    """A `sessions` row's `activity`: `{state, waiting_on, observed_at}`, read now. `waiting_on` is kept
    only for `blocked`, in ACTIVITY_WAITS' order."""
    from datetime import datetime, timezone
    if state not in ACTIVITY_STATES:
        raise ValueError(f"unknown activity state {state!r}")
    waits = [name for name in ACTIVITY_WAITS if name in waiting_on] if state == "blocked" else []
    at = datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    return {"state": state, "waiting_on": waits, "observed_at": at}


BINDING_REASONS = {"iam", "fresh-start", "rewind", "moved-directory", "moved-background"}

# The registry: each provider's adapter, by the session key's prefix, as (module, class, display name).
# The modules are imported only when an adapter is built, so the hook entry pays for none it doesn't use.
# The display name is what kaiusap shows the user (notices, `who`, `sessions`),
# so showing it imports no adapter.
ADAPTERS = {
    "codex": ("kaiusap.adapters.codex", "CodexAdapter", "Codex"),
    "claude": ("kaiusap.adapters.claude", "ClaudeAdapter", "Claude"),
    "opencode": ("kaiusap.adapters.opencode", "OpenCodeAdapter", "OpenCode"),
    "opencode2": ("kaiusap.adapters.opencode2", "OpenCode2Adapter", "OpenCode 2"),
}


# Each provider's own settings, by provider: key -> default. Declared here, beside the registry, so reading
# settings imports no adapter; the base checks and merges them as its own (`model.load_settings`).
SETTINGS = {
    "opencode2": {"opencode2_command": "opencode"},  # the first word of OpenCode 2's resume command in `sessions`
}


def provider_settings():
    """Every registered provider's settings with their defaults."""
    return {key: default for name in ADAPTERS for key, default in SETTINGS.get(name, {}).items()}


def setup_flags():
    """Every registered adapter's `kaiusap setup` flags, as keys."""
    return frozenset().union(*(adapter_class(name).setup_flags for name in ADAPTERS))


def display_name(name):
    """The provider's name as kaiusap shows it; a name not registered (a record from a provider no longer
    installed) is shown capitalised, as before the registry carried display names."""
    entry = ADAPTERS.get(name)
    return entry[2] if entry else name.title()


def adapter_class(name):
    """The registered adapter class for a provider name; KeyError for a name not registered."""
    module, cls, _ = ADAPTERS[name]
    # __import__ with a fromlist returns the submodule itself, without importing importlib.
    return getattr(__import__(module, fromlist=(cls,)), cls)


class Providers:
    """One adapter per provider; the base dispatches by the session key's prefix."""

    def __init__(self, adapters, store=None):
        self.adapters = dict(adapters)
        self.store = store

    @classmethod
    def default(cls, store, **overrides):
        adapters = {name: adapter_class(name)(store) for name in ADAPTERS}
        adapters.update(overrides)
        return cls(adapters, store)

    def named(self, name):
        return self.adapters.get(name) or UnavailableProvider()

    def for_key(self, session_key):
        return self.named((session_key or "").split(":", 1)[0])

    def missing(self):
        return [name for name, adapter in self.adapters.items() if isinstance(adapter, UnavailableProvider)]

    def begin(self):
        """A CLI or MCP call starts, before its identity lookup."""
        for adapter in self.adapters.values():
            adapter.begin()

    def reset(self):
        for adapter in self.adapters.values():
            adapter.reset()

    def finish(self, catch_up=True):
        for adapter in self.adapters.values():
            adapter.finish(catch_up=catch_up)

    def identity(self, environment, store):
        """Every registered provider is asked, whether or not its adapter is here: a claim
        for one that isn't is refused by its stand-in, as before the adapter exists."""
        claimed = [name for name in ADAPTERS if adapter_class(name).claims(environment)]
        if len(claimed) > 1:
            variables = " and ".join(adapter_class(name).session_variable for name in claimed)
            raise Error(f"Both {variables} are set, so kaiusap can't tell which session is calling. Run it from the session's own shell, or use the kaiusap tools.")
        if claimed:
            return self.named(claimed[0]).identity(environment, store)
        return Identity()

    def where(self, session_key):
        return self.for_key(session_key).where(session_key)

    def liveness(self, session_key):
        return self.for_key(session_key).liveness(session_key)

    def unidentified(self, identity):
        return self.named(identity.provider).unidentified(identity)

    def delivers(self, session_key):
        return bool(self.for_key(session_key).delivers)

    def deliver(self, store, message, deadline=None):
        return self.for_key(message["to"]["session_key"]).deliver(store, message, deadline)

    def list(self):
        rows = []
        for adapter in self.adapters.values():
            rows.extend(adapter.list())
        return rows

    def context(self, session_key):
        return self.for_key(session_key).context(session_key)

    def harness(self, session_key):
        return self.for_key(session_key).harness(session_key)

    def harness_version(self, session_key):
        return self.for_key(session_key).harness_version(session_key)

    def activity(self, session_key):
        return self.for_key(session_key).activity(session_key)

    def tell(self, text, event="UserPromptSubmit", provider="codex"):
        return self.named(provider).tell(text, event)

    def mirror(self, line):
        """Retire every binding the line ended, then mirror the new binding. Retirements go first so a
        tmux session the retirement renames frees the handle's name for the mirror's rename.
        The adapter is told what ended the binding: Claude retires at once only for `iam` and `release`."""
        from ..model import ended_by
        warnings = []
        if self.store is not None:
            for binding in ended_by(self.store, line):
                warnings.extend(self.for_key(binding["session_key"]).retire(binding, line["at"], cause=line.get("reason")))
        if line.get("reason") in BINDING_REASONS and line.get("session_key"):
            warnings.extend(self.for_key(line["session_key"]).mirror(line))
        return warnings

    def retire(self, binding, left_at, cause=None):
        return self.for_key(binding["session_key"]).retire(binding, left_at, cause=cause)

    def refuse_before_acting(self, identity):
        if identity.provider:
            self.named(identity.provider).refuse_before_acting(identity)

    def follow_start(self, event, store):
        return self.named(event["provider"]).follow_start(event, store)

    def current(self, event, proposal):
        return self.named(event["provider"]).current(event, proposal)
