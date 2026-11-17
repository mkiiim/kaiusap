"""The provider contract, with its hook-time obligations, run against every registered adapter.

The tests are written against `Provider` (kaiusap/adapters/__init__.py), never against one
adapter's internals. Each provider's specifics come from its fixture, `tests/contract/<p>.py`, which a
provider author writes. It says how to make the provider's inputs, not what the adapter does with them:

- `World(store, tmp_path)`: the provider's fakes for one test, with
  - `adapter`: the adapter under test, built on those fakes (nothing real runs);
  - `session`, `agent`: a root session id and a spawned agent's id, in the provider's formats;
  - `environment()`: the session's own shell environment, which this provider claims;
  - `reachable(name=None)`: make the session live, its provider-side name `name`; called before its
    start input is recorded, so the start record carries whatever reaching it needs;
  - `close()`.
- `INPUTS`: `(event, hook input, kind)` for the root session: every hook event at least once, and the
  base-vocabulary kind each input maps to, or None for an input that is no start or prompt.
- `SPAWNED`: `(event, hook input)` for the spawned agent inside the session.
- With `screen_events`: `SCREENED`, `(event, hook input)` for the spawned agent's acting calls, which a
  screen refuses.
- `PROGRAMS`: `{name: script}`, the fake programs the hook processes find on PATH (tmux, ps, ...).
- With a native part (`adapters/<p>.rs`): `NATIVE`, `(label, event, hook input, programs)` that the native
  part and Python must record byte for byte alike, and `SETTLED`,
  `(event, hook input)` the native part answers alone once Python has settled the session.
"""
import importlib.util
import inspect
import io
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time

import pytest

from kaiusap.adapters import ADAPTERS, BINDING_REASONS, Identity, Provider, Providers, adapter_class, display_name
from kaiusap.core import Core
from kaiusap.errors import Error
from kaiusap.hooks import dispatch
from kaiusap.hookstate import HookState
from kaiusap.model import retirement_name
from kaiusap.notice import notice
from kaiusap.setup import Setup
from kaiusap.store import Store, identifier, instant, now

REPO = Path(__file__).resolve().parent.parent
FIXTURES = Path(__file__).resolve().parent / "contract"
PARTS = REPO / "src/kaiusap/adapters"
HANDLE = "alpha"
AT = "2026-11-09T06:00:00.000001Z"


# Open findings: each test named here fails today for the stated reason and is marked strict, so it
# fails again ("XPASS") as soon as the finding is fixed and its line here is removed. None are open: a
# provider without a native part runs on Python hooks, and codex.rs writes the store's lines.
OPEN_FINDINGS = {}


@pytest.fixture(autouse=True)
def open_findings(request):
    reason = OPEN_FINDINGS.get(request.node.name)
    if reason:
        request.node.add_marker(pytest.mark.xfail(strict=True, reason=reason))


# The event kinds a provider's records may carry, written out here rather than read from the code.
VOCABULARY = {"startup", "resume", "clear", "compact", "fork", "prompt", "cwd", "subagent"}
STARTS = VOCABULARY - {"prompt", "cwd", "subagent"}
# The reason codes, by the outcome they come with: a stored message, an uncertain delivery, or `whoami` only.
STORED = {"paused", "limit", "detached", "offer_pending", "no_socket", "connect_failed", "timeout", "two_processes",
          "not_live", "daemon_not_found", "protocol_unrecognised", "thread_not_loaded", "codex_refused",
          "provider_refused", "not_attempted", "store_failed", "sandboxed", "listing_failed"}
UNCERTAIN = {"no_confirmation"}
REASON_CODES = STORED | UNCERTAIN | {"spawned_agent", "unknown_caller"}
WHERE = {"tmux", "not_in_tmux", "background", "not_running", "unknown"}  # `sessions`' values




def load(name):
    path = FIXTURES / f"{name}.py"
    if not path.exists():
        pytest.fail(f"{name} is registered but has no contract fixture: write tests/contract/{name}.py (see test_contract.py)")
    spec = importlib.util.spec_from_file_location(f"contract_fixture_{name}", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def native_part(name):
    return (PARTS / f"{name}.rs").exists()


def test_every_registered_provider_has_a_contract_fixture():
    required = {"World", "INPUTS", "SPAWNED", "PROGRAMS"}
    problems = []
    for name in ADAPTERS:
        module = load(name)
        wanted = required | ({"NATIVE", "SETTLED"} if native_part(name) else set()) | (
            {"SCREENED"} if adapter_class(name).screen_events else set())
        if missing := sorted(wanted - set(vars(module))):
            problems.append(f"tests/contract/{name}.py lacks {missing}")
    stale = sorted(p.stem for p in FIXTURES.glob("*.py") if p.stem not in ADAPTERS)
    assert not stale, f"contract fixtures for providers not registered: {stale}"
    assert not problems, "\n".join(problems)


@pytest.fixture(params=sorted(ADAPTERS))
def provider(request):
    return request.param


@pytest.fixture
def fixture(provider):
    return load(provider)


@pytest.fixture
def world(fixture, store, tmp_path):
    made = fixture.World(store, tmp_path)
    yield made
    made.close()


def key(provider, world, agent=False):
    return f"{provider}:{world.session}" + (f"/{world.agent}" if agent else "")


def hook(adapter, event, payload, deadline=None):
    """The adapter's part of one hook call, as the base asks for it: the event, then its records."""
    deadline = deadline or time.monotonic() + 5
    mapped = adapter.events({**payload, "hook_event_name": event}, deadline=deadline)
    if mapped and mapped.get("kind") != "unrecognised":
        adapter.hook_record(event, mapped, deadline)
    return mapped


def start(fixture, world):
    """Record the root session's startup, as its start hook does."""
    event, payload, _ = next(i for i in fixture.INPUTS if i[2] == "startup")
    return hook(world.adapter, event, payload)


def rows(store, relative):
    return list(store.rows(relative))


# ----- registry -------------------------------------------------------------------------------


def test_the_registry_entry(provider):
    kind = adapter_class(provider)
    assert kind.provider == provider, "the registry name is the session key's prefix"
    shown = display_name(provider)
    assert isinstance(shown, str) and shown.strip() == shown and shown and "\n" not in shown
    message = {"id": "20261109T000000Z-abcdef", "in_reply_to": None, "reply_needed": False, "subject": "S",
               "from": {"handle": "beta", "provider": provider}}
    assert notice(message).startswith(f"kaiusap: message from beta ({shown}, a peer assistant")


def test_the_adapter_implements_every_obligation(provider):
    # As test_boundary's introspection check, plus the kind of each member.
    kind = adapter_class(provider)
    declared = {name for name, value in vars(Provider).items() if not name.startswith("_") and (
        callable(value) or isinstance(value, classmethod))} | set(Provider.__annotations__)
    missing = sorted(name for name in declared if not hasattr(kind, name))
    assert not missing, f"{kind.__name__} lacks {missing}"
    for name in ("claims", "setup_items", "pre_tool", "screen_timeout"):
        assert isinstance(inspect.getattr_static(kind, name), classmethod), f"{name} is a classmethod"
    for name in declared - set(Provider.__annotations__) - {"claims", "setup_items", "pre_tool", "screen_timeout"}:
        assert callable(getattr(kind, name)), name
    assert isinstance(kind.delivers, bool)
    assert isinstance(kind.harness_maker, str) and re.fullmatch(r"[a-z][a-z0-9-]*", kind.harness_maker)
    assert kind.session_variable is None or isinstance(kind.session_variable, str)
    assert isinstance(kind.hook_events, frozenset) and kind.hook_events and all(isinstance(e, str) for e in kind.hook_events)
    for name in ("record_only_events", "start_events", "prompt_events", "screen_events"):
        assert isinstance(getattr(kind, name), frozenset) and getattr(kind, name) <= kind.hook_events, name
    # Each event has at most one role: the start hook's 3 s, the prompt hook's note, a screen, or record only.
    roles = [kind.start_events, kind.prompt_events, kind.screen_events, kind.record_only_events]
    assert sum(len(role) for role in roles) == len(frozenset().union(*roles)), "an event with two roles"


# ----- identity and claims --------------------------------------------------------------------


def test_claims(provider, world, store, tmp_path):
    kind = adapter_class(provider)
    assert kind.claims(world.environment()) is True
    assert kind.claims({}) is False
    for other in sorted(set(ADAPTERS) - {provider}):
        other_world = load(other).World(Store(tmp_path / f"other-{other}"), tmp_path / f"other-{other}")
        try:
            environment = other_world.environment()
        finally:
            other_world.close()
        assert kind.claims(environment) is False, f"{provider} claims {other}'s shell"
        # Two claims at once: kaiusap can't tell which session is calling.
        with pytest.raises(Error):
            Providers.default(store).identity({**environment, **world.environment()}, store)


def test_identity(provider, fixture, world, store):
    start(fixture, world)
    identity = world.adapter.identity(world.environment(), store)
    assert isinstance(identity, Identity)
    assert (identity.provider, identity.session_id, identity.session_key, identity.classification) == (
        provider, world.session, key(provider, world), "root")


def test_refusal_unidentified_mcp_identity_and_directory(provider, world, store):
    adapter = world.adapter
    spawned = Identity(provider, world.session, key(provider, world, True), "/fixture", "spawned",
                       key(provider, world), agent_id=world.agent)
    with pytest.raises(Error):
        adapter.refuse_before_acting(spawned)
    assert adapter.refuse_before_acting(Identity(provider, world.session, key(provider, world), "/fixture", "root")) is None
    unknown = adapter.unidentified(Identity(provider, world.session, None, "/fixture", "unknown", reason="the reason"))
    assert unknown["can_be_woken"] is False and unknown["reason_code"] == "unknown_caller"
    try:
        identity = adapter.mcp_identity(None)  # an MCP call that carries nothing to identify it
    except Error:
        pass
    else:
        assert isinstance(identity, Identity) and identity.classification != "root"
    directory = adapter.directory(Identity(provider, world.session, key(provider, world), None, "root"))
    assert directory is None or isinstance(directory, str)


# ----- events and records ---------------------------------------------------------------------------


def test_every_hook_event_maps_to_the_base_vocabulary(provider, fixture, world, store):
    kind = adapter_class(provider)
    assert {event for event, _, _ in fixture.INPUTS} == kind.hook_events, "INPUTS covers every hook event"
    kinds = {expected for _, _, expected in fixture.INPUTS}
    assert {"startup", "prompt"} <= kinds and kinds - {None} <= VOCABULARY
    before = time.time()
    for event, payload, expected in fixture.INPUTS:
        mapped = world.adapter.events({**payload, "hook_event_name": event}, deadline=time.monotonic() + 5)
        if expected is None:
            assert mapped is None, (event, mapped)
            continue
        assert mapped["kind"] == expected, (event, payload)
        assert mapped["provider"] == provider and mapped["session_id"] == world.session
        assert mapped["session_key"] == key(provider, world, agent=expected == "subagent")
        assert mapped["cwd"] == payload["cwd"]
        assert before - 1 <= instant(mapped["at"]).timestamp() <= time.time() + 1
        unidentified = {k: v for k, v in payload.items() if k != "session_id"}
        assert world.adapter.events({**unidentified, "hook_event_name": event}) is None


def test_start_and_prompt_events_map_to_their_kinds(provider, fixture, world, store):
    # The hook entry takes its budget and steps from these sets, so a start event
    # gives a start or nothing, and a prompt event a prompt or nothing.
    kind = adapter_class(provider)
    assert kind.start_events and kind.prompt_events, "the base needs a start hook and a prompt hook"
    seen = set()
    for event, payload, _ in fixture.INPUTS:
        if event not in kind.start_events | kind.prompt_events:
            continue
        seen.add(event)
        mapped = world.adapter.events({**payload, "hook_event_name": event}, deadline=time.monotonic() + 5)
        allowed = STARTS if event in kind.start_events else {"prompt"}
        assert mapped is None or mapped["kind"] in allowed, (event, mapped and mapped["kind"])
    assert seen == kind.start_events | kind.prompt_events, "INPUTS covers every start and prompt event"


def test_screens_decide_before_the_store(provider, fixture, store, tmp_path):
    # A tool-call screen is decided by the adapter's class before any store work; a root caller is
    # never refused, and the hook entry imports no store for it.
    kind = adapter_class(provider)
    screens = [(event, payload) for event, payload, _ in fixture.INPUTS if event in kind.screen_events]
    assert {event for event, _ in screens} == kind.screen_events
    for event, payload in screens:
        assert not kind.pre_tool({**payload, "hook_event_name": event})
        for caller in (payload, {**payload, "agent_id": "a"}):
            script = (f"import sys; from kaiusap.hooks import main; code = main([{provider!r}, {event!r}]); "
                      "assert 'kaiusap.store' not in sys.modules; sys.exit(code)")
            env = {**os.environ, "KAIUSAP_HOME": str(tmp_path / "screen")}
            result = subprocess.run([sys.executable, "-c", script], input=json.dumps(caller), text=True,
                                    capture_output=True, env=env)
            assert (result.returncode, result.stderr) == (0, ""), result.stderr
            assert not (tmp_path / "screen").exists()


def test_screens_fail_closed(provider, fixture, store, tmp_path):
    # A screen that can't be decided in time, or fails, prints the adapter's
    # refusal (`screen_timeout`), never nothing; a root caller is never refused. Without screens it is None.
    kind = adapter_class(provider)
    inputs = [(event, payload) for event, payload, _ in fixture.INPUTS] + list(fixture.SPAWNED)
    if not kind.screen_events:
        assert all(kind.screen_timeout(event, payload) is None for event, payload in inputs)
        return
    assert {event for event, _ in fixture.SCREENED} == kind.screen_events, "SCREENED covers every screen event"
    for event, payload in fixture.SCREENED:
        refusal = kind.pre_tool({**payload, "hook_event_name": event})
        assert refusal and refusal != kind.pre_tool({**payload, "agent_id": None}), "SCREENED is refused"
        timeout = kind.screen_timeout(event, payload)
        assert isinstance(timeout, dict) and timeout and timeout != refusal, (event, timeout)
        assert kind.screen_timeout(event, {k: v for k, v in payload.items() if k != "agent_id"}) is None
    for event, payload, _ in fixture.INPUTS:  # root callers
        assert kind.screen_timeout(event, payload) is None, (event, payload)
    if not native_part(provider):
        return
    # The native part prints the same refusal, byte for byte, when Python gives no answer: too slow to
    # start (killed at the screen's 1 s), or exiting without output. A root caller still gets nothing.
    environment = {**os.environ, "KAIUSAP_HOME": str(store.root)}
    for number, fault in enumerate(("import time\ntime.sleep(3)\n", "import os\nos._exit(0)\n")):
        shadow = tmp_path / f"fault-{number}"
        shadow.mkdir()
        (shadow / "sitecustomize.py").write_text(fault)
        for event, payload in fixture.SCREENED:
            for caller, expected in ((payload, json.dumps(kind.screen_timeout(event, payload)) + "\n"),
                                     ({k: v for k, v in payload.items() if k != "agent_id"}, "")):
                result = subprocess.run([str(native_binary()), "hook", provider, event], input=json.dumps(caller),
                                        text=True, capture_output=True, timeout=10,
                                        env={**environment, "PYTHONPATH": str(shadow)})
                assert (result.returncode, result.stdout, result.stderr) == (0, expected, ""), (fault, caller)


def test_spawned_agents_write_no_prompt_and_only_a_subagent_start(provider, fixture, world, store):
    for event, payload in fixture.SPAWNED:
        mapped = hook(world.adapter, event, payload)
        if mapped is not None:
            assert mapped["kind"] == "subagent" and mapped["session_key"] == key(provider, world, True), (event, mapped)
    assert not rows(store, "sessions/prompts.jsonl")
    assert all(r["source"] == "subagent" for r in rows(store, "sessions/starts.jsonl"))


def test_records_have_the_store_shape(provider, fixture, world, store):
    # Start and subagent lines in starts.jsonl, prompt lines in prompts.jsonl, each with its event's key and time.
    for event, payload, expected in fixture.INPUTS:
        starts, prompts = len(rows(store, "sessions/starts.jsonl")), len(rows(store, "sessions/prompts.jsonl"))
        mapped = hook(world.adapter, event, payload)
        new_starts = rows(store, "sessions/starts.jsonl")[starts:]
        new_prompts = rows(store, "sessions/prompts.jsonl")[prompts:]
        if expected is None:
            assert not new_starts and not new_prompts, (event, new_starts, new_prompts)
            continue
        for row in new_starts + new_prompts:
            assert {"at", "provider", "session_id", "session_key", "source"} <= set(row), row
            assert (row["provider"], row["session_id"], row["at"]) == (provider, world.session, mapped["at"]), row
            assert row["session_key"] == mapped["session_key"]
        if expected in STARTS:
            assert [r["source"] for r in new_starts] == [expected] and not new_prompts
            assert new_starts[0]["cwd"] == payload["cwd"]
        elif expected == "subagent":
            assert [(r["source"], r.get("agent_id")) for r in new_starts] == [("subagent", world.agent)] and not new_prompts
        else:
            assert [r["source"] for r in new_prompts] == ["prompt"]
            assert all(r["source"] == "cwd" for r in new_starts)  # a directory change, never a start


@pytest.mark.parametrize("event", ["NotAHookEvent", "SessionStart", "UserPromptSubmit", "PreToolUse", "SubagentStart"])
def test_events_outside_hook_events_are_refused(provider, event, store, monkeypatch):
    if event in adapter_class(provider).hook_events:
        pytest.skip(f"{event} is one of {provider}'s hook events")
    monkeypatch.setenv("KAIUSAP_HOME", str(store.root))
    payload = {"session_id": "s", "cwd": "/fixture", "prompt": "Hello", "source": "startup", "agent_id": "a"}
    with pytest.raises(ValueError):
        dispatch(provider, event, payload, time.monotonic() + 3)
    from kaiusap.hooks import main
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
    out = io.StringIO()
    monkeypatch.setattr(sys, "stdout", out)
    assert main([provider, event]) == 0 and out.getvalue() == ""
    assert "Error" in (store.root / "hook-errors.log").read_text()
    assert not (store.root / "sessions").exists()


# ----- hook output and hook markers ------------------------------------------------------------


def test_hook_output_and_marker_fields_are_dicts(provider, fixture, world, store):
    kind = adapter_class(provider)
    for event, payload, expected in fixture.INPUTS:
        if expected is None or event in kind.record_only_events:
            continue
        mapped = hook(world.adapter, event, payload)
        state = HookState(store, provider, mapped["session_id"])
        output = world.adapter.hook_output(mapped, state)
        fields = world.adapter.marker_fields(state, mapped)
        assert isinstance(output, dict) and isinstance(fields, dict)
        json.dumps(output), json.dumps(fields)
        state.save(mapped, fields)
        assert json.loads(state.marker.read_text())["version"] == 1
        told = world.adapter.tell("kaiusap: a note", event)
        assert isinstance(told, dict) and isinstance(told["hookSpecificOutput"], dict)  # the base merges this key


# ----- deliver, liveness, where, list -------------------------------------------------------------


def message(provider, world):
    stamp = now()
    return {"schema": 1, "id": identifier(stamp), "created": stamp, "reply_needed": False, "in_reply_to": None,
            "from": {"handle": "beta", "session_id": "sender", "session_key": f"{provider}:sender", "provider": provider},
            "to": {"handle": HANDLE, "session_id": world.session, "session_key": key(provider, world)},
            "subject": "Subject", "chain_id": stamp, "depth": 0, "fields": {"context": "Body"}}


def check_outcome(result, allowed):
    assert result["outcome"] in allowed, result
    if result["outcome"] == "stored":
        assert result["reason_code"] in STORED and isinstance(result["reason"], str), result
    elif result["outcome"] == "uncertain":
        assert result["reason_code"] in UNCERTAIN and isinstance(result["reason"], str), result
    else:
        assert result["reason_code"] is None, result
    assert isinstance(result.get("warnings", []), list)


def deliver(world, store, provider, deadline):
    began = time.monotonic()
    result = world.adapter.deliver(store, message(provider, world), deadline=deadline)
    assert time.monotonic() - began < 20 + 2, "the whole delivery has 20 s"
    return result


def test_deliver_outcomes(provider, fixture, world, store):
    check_outcome(deliver(world, store, provider, time.monotonic() + 20), {"stored"})  # nothing is running
    world.reachable()
    start(fixture, world)
    world.adapter.reset()
    check_outcome(deliver(world, store, provider, time.monotonic() + 20), {"woken", "delivered"})
    world.adapter.reset()
    check_outcome(deliver(world, store, provider, time.monotonic()), {"stored", "uncertain"})  # no time left


def test_liveness_where_and_list(provider, fixture, world, store):
    session = key(provider, world)
    down = world.adapter.liveness(session)
    assert down["can_be_woken"] is False and down["reason_code"] in REASON_CODES
    world.reachable()
    start(fixture, world)
    world.adapter.reset()
    up = world.adapter.liveness(session)
    assert up["can_be_woken"] is True and up["reason_code"] is None
    world.adapter.reset()
    where = world.adapter.where(session)
    assert where["where"] in WHERE and isinstance(where["tmux_panes"], list)
    assert {"cwd", "name", "command", "trusted"} <= set(where)
    world.adapter.reset()
    listed = world.adapter.list()
    assert isinstance(listed, list) and all(row["session_key"].startswith(provider + ":") for row in listed)
    assert session in {row["session_key"] for row in listed}


def test_context_is_none_or_its_shape(provider, fixture, world, store):
    """`context` never raises and returns None or {used, limit, limit_source, model, at}, for a session
    the provider can see and for one it has never heard of."""
    world.reachable()
    start(fixture, world)
    for session in (key(provider, world), f"{provider}:00000000-0000-0000-0000-000000000000"):
        world.adapter.reset()
        found = world.adapter.context(session)
        assert found is None or (set(found) == {"used", "limit", "limit_source", "model", "model_maker", "at"}
                                 and (found["model_maker"] is None or re.fullmatch(r"[a-z][a-z0-9-]*", found["model_maker"]))
                                 and type(found["used"]) is int and found["used"] >= 0
                                 and (found["limit"] is None) == (found["limit_source"] is None)
                                 and found["limit_source"] in {None, "provider"})


def test_harness_version_is_none_or_a_string_and_none_when_not_live(provider, fixture, world, store):
    """`harness_version` is the version running a live session now; never raises, and None for a session
    that isn't live or that the provider has never heard of."""
    session = key(provider, world)
    assert world.adapter.harness_version(session) is None  # nothing is running
    world.reachable()
    start(fixture, world)
    for probe in (session, f"{provider}:00000000-0000-0000-0000-000000000000"):
        world.adapter.reset()
        found = world.adapter.harness_version(probe)
        assert found is None or (isinstance(found, str) and found.strip() == found and found), found
    # A fixture that fakes the running version says which: then the live session reports exactly it.
    expected = getattr(fixture, "HARNESS_VERSION", None)
    if expected:
        world.adapter.reset()
        assert world.adapter.harness_version(session) == expected



def test_activity_is_none_or_a_reading_and_none_when_not_live(provider, fixture, world, store):
    """`activity` is what a live session is doing now, `{state, waiting_on, observed_at}`, or None; never
    raises, and None for a session that isn't live or that the provider has never heard of."""
    from datetime import datetime
    session = key(provider, world)
    assert world.adapter.activity(session) is None  # nothing is running
    world.reachable()
    start(fixture, world)
    for probe in (session, f"{provider}:00000000-0000-0000-0000-000000000000"):
        world.adapter.reset()
        found = world.adapter.activity(probe)
        assert found is None or (set(found) == {"state", "waiting_on", "observed_at"}
                                 and found["state"] in {"idle", "working", "blocked", "error"}
                                 and isinstance(found["waiting_on"], list)
                                 and set(found["waiting_on"]) <= {"approval", "user_input"}
                                 and bool(found["waiting_on"]) == (found["state"] == "blocked")
                                 and found["observed_at"].endswith("Z")
                                 and datetime.fromisoformat(found["observed_at"].replace("Z", "+00:00"))), found
    # A fixture whose fake reports a state says which; the others report None until their signal is proven.
    world.adapter.reset()
    found = world.adapter.activity(session)
    assert (found and found["state"]) == getattr(fixture, "ACTIVITY", None)


# ----- mirror and retire ------------------------------------------------------------------------


def test_mirror_and_retire(provider, fixture, world, store):
    binding = {"at": now(), "id": identifier(), "handle": HANDLE, "reason": "iam", "session_id": world.session,
               "session_key": key(provider, world), "provider": provider}
    assert isinstance(world.adapter.mirror(binding), list)  # nothing is running: warnings, never a failure
    assert isinstance(world.adapter.retire(binding, now(), cause="release"), list)
    world.adapter.reset()
    world.reachable(name=HANDLE)
    start(fixture, world)
    identity = world.adapter.identity(world.environment(), store)
    providers = Providers({provider: world.adapter}, store)
    taken = Core(store, identity, providers).run("iam", {"handle": HANDLE})
    assert all(isinstance(w, str) for w in taken["warnings"])
    named = [e for e in store.events() if e["event"] == "named"]
    assert [(e["kind"], e["name"]) for e in named] == [("handle", HANDLE)], "named once, after iam"
    Core(store, identity, providers).run("release", {})
    left_at = list(store.rows("handles.jsonl"))[-1]["at"]
    retired = [e["name"] for e in store.events() if e["event"] == "named" and e["kind"] == "retired"]
    assert retired == [retirement_name(HANDLE, left_at)]
    assert re.fullmatch(rf"{HANDLE}~\d{{12}}", retired[0])
    assert retired[0] == HANDLE + "~" + instant(left_at).astimezone().strftime("%y%m%d%H%M%S")


# ----- follow a start and currency ----------------------------------------------------------------


def test_follow_start_and_currency(provider, fixture, world, store):
    for event, payload, expected in fixture.INPUTS:
        if expected not in STARTS:
            continue
        mapped = hook(world.adapter, event, payload)
        mapped = {**mapped, "deadline": time.monotonic() + 3, "entered": time.monotonic()}
        proposal = world.adapter.follow_start(mapped, store)
        assert proposal is None or isinstance(proposal, dict), proposal
        proposal = proposal or {}
        move = proposal.get("move")
        if move:
            assert {"handle", "from", "to", "reason", "via"} <= set(move) and move["reason"] in BINDING_REASONS
        assert proposal.get("text") is None or isinstance(proposal["text"], str)
        assert type(world.adapter.current(mapped, proposal)) is bool
        world.adapter.reset()


# ----- setup items and the call bracket -------------------------------------------------------------


def test_setup_items_have_the_interface_shape_and_unique_places(store, tmp_path):
    def run(*args):
        raise OSError("the contract tests run no command")
    empty = tmp_path / "empty-bin"
    empty.mkdir()
    setup = Setup(store, environ={"PATH": str(empty)}, run=run, home=tmp_path / "home", repo="<repo>")
    places = []
    for name in ADAPTERS:
        items = adapter_class(name).setup_items(setup)
        # Of two providers for one harness, setup shows one; the
        # other gives no items. Every other provider gives its own.
        assert isinstance(items, list) and (items or adapter_class(name).setup_flags), f"{name} gives its setup items"
        for place, item in items:
            assert type(place) is int
            places.append(place)
            assert set(item) == {"item", "done", "detail", "command", "note"}, item
            assert isinstance(item["item"], str) and type(item["done"]) is bool
            # `command` is null when done; an item not done may still have none (OpenCode's version with
            # no opencode on the PATH, the tmux warning).
            assert item["command"] is None if item["done"] else item["command"] is None or isinstance(item["command"], str), item
    assert len(places) == len(set(places)), f"places in setup's order are shared: {sorted(places)}"


def test_the_call_bracket(provider, world):
    adapter = world.adapter
    assert adapter.begin() is None and adapter.begin() is None  # idempotent within a call
    assert adapter.reset() is None
    assert adapter.finish() is None
    assert adapter.begin() is None and adapter.finish(catch_up=False) is None


# ----- the native part and its fast path --------------------------------------------------------------


def native_binary():
    binary = Path(sys.executable).parent / "kaiusap"
    assert binary.exists(), "build native/hook.rs into the test environment before running the contract tests"
    return binary


def test_native_parts_belong_to_registered_providers():
    assert {p.stem for p in PARTS.glob("*.rs")} <= set(ADAPTERS)


def test_the_native_entry_hands_a_name_without_a_part_to_python(store, monkeypatch):
    # A provider without a part, or a name not in the table, goes straight to Python.
    monkeypatch.setenv("KAIUSAP_HOME", str(store.root))
    name = "nopart"
    assert not native_part(name) and name not in ADAPTERS
    result = subprocess.run([str(native_binary()), "hook", name, "UserPromptSubmit"], input="{}", text=True,
                            capture_output=True, timeout=10)
    assert result.returncode == 0 and not result.stdout
    log = (store.root / "hook-errors.log").read_text()
    assert "invalid hook arguments" not in log, "the native entry dropped the hook instead of starting Python: " + log
    # Python refuses a name not in ADAPTERS, logs it and exits 0.
    assert "ValueError: hook failed" in log


NO_PART_ADAPTER = """
import json, os, time  # the adapter's events() records that Python ran it, and how long it had

class Adapter:
    provider = "nopart"
    hook_events = frozenset({"Begin", "Ask"})
    start_events = frozenset({"Begin"})
    prompt_events = frozenset({"Ask"})
    screen_events = record_only_events = frozenset()

    def __init__(self, store):
        self.store = store

    def hook_deadline(self, at):
        return at

    def events(self, payload, deadline=None):
        with open(os.environ["NOPART_RAN"], "w") as stream:
            json.dump({"event": payload["hook_event_name"], "left": deadline - time.monotonic()}, stream)
        return None

    def finish(self, catch_up=True):
        pass
"""


def test_a_registered_provider_without_a_part_runs_on_python_hooks(store, tmp_path):
    # A native part is optional. Without one the engine hands every hook to Python with the start
    # hook's deadline, and Python applies the adapter's own budget (its `start_events`), from the hook's start.
    shadow = tmp_path / "shadow"
    shadow.mkdir()
    (shadow / "nopart_adapter.py").write_text(NO_PART_ADAPTER)
    (shadow / "sitecustomize.py").write_text(
        "import kaiusap.adapters\nkaiusap.adapters.ADAPTERS['nopart'] = ('nopart_adapter', 'Adapter', 'Nopart')\n")
    ran, log = tmp_path / "ran.json", store.root / "hook-errors.log"
    environment = {**os.environ, "KAIUSAP_HOME": str(store.root), "PYTHONPATH": str(shadow), "NOPART_RAN": str(ran)}
    assert not native_part("nopart")

    def hook(*args):
        result = subprocess.run([str(native_binary()), "hook", *args], input='{"x": 1}', text=True,
                                capture_output=True, timeout=10, env=environment)
        assert result.returncode == 0 and result.stdout == "" and result.stderr == ""
        return json.loads(ran.read_text()) if ran.exists() else None

    started = hook("nopart", "Begin")
    assert started and started["event"] == "Begin" and 1.5 < started["left"] <= 2.9, started  # 3 s less the output reserve
    ran.unlink()
    prompt = hook("nopart", "Ask")
    # 50 ms from the hook's start: Python either runs within what is left of it or stops, logged, before the adapter.
    assert (prompt and prompt["event"] == "Ask" and prompt["left"] <= .05) or (
        not prompt and "TimeoutError: hook deadline reached" in log.read_text()), (prompt, log.read_text() if log.exists() else "")
    assert "invalid hook arguments" not in (log.read_text() if log.exists() else "")
    ran.unlink(missing_ok=True)
    # Malformed arguments are still refused by the engine, without Python.
    assert hook("nopart") is None and hook("nopart", "Ask", "extra") is None
    assert log.read_text().count("invalid hook arguments") == 2


NO_PART_SCREEN = """
import time

class Adapter:
    provider = "nopart"
    hook_events = frozenset({"Begin", "Check"})
    start_events = frozenset({"Begin"})
    screen_events = frozenset({"Check"})
    prompt_events = record_only_events = frozenset()

    @classmethod
    def pre_tool(cls, payload):
        if payload.get("fault") == "raise":
            raise RuntimeError("fixture screen failure")
        if payload.get("fault") == "slow":
            time.sleep(5)
        return {"decided": True} if payload.get("agent") else None

    @classmethod
    def screen_timeout(cls, event, payload):
        return {"refused": True} if event in cls.screen_events and payload.get("agent") else None
"""


def test_a_provider_without_a_part_fails_its_screens_closed_on_python_alone(store, tmp_path):
    # Decision 10 (2026-11-09): without a part the engine has no refusal to print, so Python fails a screen
    # closed by itself, within the screen's own 1 s from the hook's start (not the 3 s the engine gives it).
    shadow = tmp_path / "shadow"
    shadow.mkdir()
    (shadow / "nopart_adapter.py").write_text(NO_PART_SCREEN)
    (shadow / "sitecustomize.py").write_text(
        "import kaiusap.adapters\nkaiusap.adapters.ADAPTERS['nopart'] = ('nopart_adapter', 'Adapter', 'Nopart')\n")
    environment = {**os.environ, "KAIUSAP_HOME": str(store.root), "PYTHONPATH": str(shadow)}

    def check(data):
        began = time.monotonic()
        result = subprocess.run([str(native_binary()), "hook", "nopart", "Check"], input=json.dumps(data), text=True,
                                capture_output=True, timeout=10, env=environment)
        assert result.returncode == 0 and result.stderr == ""
        return (json.loads(result.stdout) if result.stdout else None), time.monotonic() - began

    assert check({"agent": 1})[0] == {"decided": True}
    assert check({"agent": 1, "fault": "raise"})[0] == {"refused": True}
    refused, elapsed = check({"agent": 1, "fault": "slow"})
    assert refused == {"refused": True} and .8 < elapsed < 1.5, elapsed
    assert check({"fault": "raise"})[0] is None  # a root caller is never refused
    assert check({"fault": "slow"})[0] is None


def write_programs(directory, programs):
    if directory.exists():
        shutil.rmtree(directory)
    directory.mkdir()
    for name, body in programs.items():
        path = directory / name
        data = body if isinstance(body, bytes) else ("#!/bin/sh\n" + body + "\n").encode()
        path.write_bytes(data if data.startswith(b"#!") else b"#!/bin/sh\n" + data)
        path.chmod(0o700)


def sessions(root):
    base = root / "sessions"
    return {str(p.relative_to(base)): p.read_bytes() for p in sorted(base.rglob("*")) if p.is_file()} if base.exists() else {}


def delta(before, after):
    return {name: data[len(before.get(name, b"")):] if data.startswith(before.get(name, b"")) else b"CHANGED " + data
            for name, data in after.items() if before.get(name) != data}


def at_the_hook(changes, at):
    """The records written at the hook's time (a record's `at` is its hook's); other lines, such as a
    start hook's terminal-map observations stamped when they were read, are counted, not compared."""
    kept = {}
    for name, data in changes.items():
        lines = data.splitlines(keepends=True)
        stamped = [line for line in lines if (json.loads(line) if line.strip().startswith(b"{") else {}).get("at") == at]
        kept[name] = (b"".join(stamped), len(lines) - len(stamped))
    return kept


def log_of(root):
    path = root / "hook-errors.log"
    return path.read_text() if path.exists() else ""


def hook_through_python(monkeypatch, root, provider, event, payload, at, entry=None):
    """One hook call through the base's Python entry, its time fixed at `at`; with `entry`, the native part
    writes the fixed-shape records it owns, as when native hands a call to Python."""
    monkeypatch.setenv("KAIUSAP_HOME", str(root))
    monkeypatch.setenv("KAIUSAP_HOOK_AT", at)
    if entry:
        monkeypatch.setenv("KAIUSAP_NATIVE_ENTRY", str(entry))
        monkeypatch.setenv("KAIUSAP_HOOK_END", str(time.time() + 5))
    else:
        monkeypatch.delenv("KAIUSAP_NATIVE_ENTRY", raising=False)
    before = sessions(root)
    try:
        dispatch(provider, event, payload, time.monotonic() + 5)
        outcome = None
    except Exception as exc:
        outcome = type(exc).__name__
    return outcome, at_the_hook(delta(before, sessions(root)), at)


def mismatch(provider, label, python, native):
    lines = [f"{provider} {label}: the native part's records differ from Python's"]
    if python[0] != native[0]:
        lines.append(f"  Python's hook {python[0] or 'returned'}, native's {native[0] or 'returned'}")
    for name in sorted(set(python[1]) | set(native[1])):
        (p, others), (n, native_others) = python[1].get(name, (b"", 0)), native[1].get(name, (b"", 0))
        if others != native_others:
            lines.append(f"  {name}: {others} other lines from Python, {native_others} with native")
        if p != n:
            same = [json.loads(x) for x in p.splitlines()] == [json.loads(x) for x in n.splitlines()] if p and n else False
            lines.append(f"  {name} ({'the same JSON, other bytes' if same else 'other records'}):\n"
                         f"    Python: {p!r}\n    native: {n!r}")
    return "\n".join(lines)


def native_cases():
    cases = []
    for name in sorted(ADAPTERS):
        if native_part(name) and (FIXTURES / f"{name}.py").exists():
            cases.extend(pytest.param(name, *case, id=f"{name}-{case[0]}") for case in load(name).NATIVE)
    return cases


@pytest.mark.parametrize("provider,label,event,payload,programs", native_cases())
def test_native_records_match_python(provider, label, event, payload, programs, tmp_path, monkeypatch):
    # The records native writes for an input Python handed it (`__hook_record`) are Python's, byte for byte.
    entry = native_binary()
    for attempt in range(3):
        write_programs(tmp_path / "bin", programs)
        monkeypatch.setenv("PATH", str(tmp_path / "bin"))
        python = hook_through_python(monkeypatch, tmp_path / f"python-{attempt}", provider, event, payload, AT)
        native = hook_through_python(monkeypatch, tmp_path / f"native-{attempt}", provider, event, payload, AT, entry)
        if python == native or "deadline" not in log_of(tmp_path / f"native-{attempt}"):
            break  # a native helper out of its 50 ms is a slow machine, not a difference: try again
    assert python == native, mismatch(provider, label, python, native)


@pytest.mark.parametrize("provider", [name for name in sorted(ADAPTERS) if native_part(name)])
def test_the_native_cases_reach_the_native_part(provider, tmp_path, monkeypatch):
    # The comparison above means something only if Python hands some of the fixture's inputs to native.
    import kaiusap.hooks
    calls = []
    helper = kaiusap.hooks.native_record
    monkeypatch.setattr(kaiusap.hooks, "native_record", lambda *args: calls.append(args[2]) or helper(*args))
    for label, event, payload, programs in load(provider).NATIVE:
        write_programs(tmp_path / "bin", programs)
        monkeypatch.setenv("PATH", str(tmp_path / "bin"))
        root = tmp_path / label
        outcome, written = hook_through_python(monkeypatch, root, provider, event, payload, AT, native_binary())
        if calls and outcome is None and any(data for data, _ in written.values()):
            return
    pytest.fail(f"no NATIVE input of {provider}'s reached its native part with a record written")


def settled_cases():
    cases = []
    for name in sorted(ADAPTERS):
        if native_part(name) and (FIXTURES / f"{name}.py").exists():
            cases.extend(pytest.param(name, *case, id=f"{name}-{i}") for i, case in enumerate(load(name).SETTLED))
    return cases


@pytest.mark.parametrize("provider,event,payload", settled_cases())
def test_native_fast_path_matches_python_and_needs_no_python(provider, event, payload, tmp_path, monkeypatch):
    # Once Python's marker says the session is settled, the part's `clean` answers alone
    # (the marker round-trips), and what it records is what Python records for the same input.
    entry = native_binary()
    world = load(provider)
    write_programs(tmp_path / "bin", world.PROGRAMS)
    monkeypatch.setenv("PATH", str(tmp_path / "bin"))
    shadow = tmp_path / "shadow"
    shadow.mkdir()
    (shadow / "kaiusap.py").write_text('raise RuntimeError("Python must not run")')
    for attempt in range(3):
        native_root, python_root = tmp_path / f"native-{attempt}", tmp_path / f"python-{attempt}"
        monkeypatch.setenv("KAIUSAP_HOME", str(native_root))
        store = Store(native_root)
        stamp = now()
        session = payload["session_id"]
        store.append("handles.jsonl", {"at": stamp, "id": identifier(stamp), "handle": HANDLE, "reason": "iam",
                                       "provider": provider, "session_id": session, "session_key": f"{provider}:{session}",
                                       "by_session": session, "by_session_key": f"{provider}:{session}", "pid": None,
                                       "process_started_at": None, "moved_from": None, "via": None, "also_released": None})
        for _ in range(3):
            dispatch(provider, event, payload, time.monotonic() + 3)
        shutil.copytree(native_root, python_root)
        before = sessions(native_root)
        result = subprocess.run([str(entry), "hook", provider, event], input=json.dumps(payload), text=True,
                                capture_output=True, timeout=10, env={**os.environ, "PYTHONPATH": str(shadow)})
        log = log_of(native_root)
        if "deadline" not in log:
            break
    assert result.returncode == 0 and result.stdout == "" and result.stderr == ""
    assert log == "", "the native part didn't answer alone: " + log
    native = delta(before, sessions(native_root))
    stamps = {json.loads(line)["at"] for data in native.values() for line in data.splitlines()}
    assert len(stamps) <= 1
    at = stamps.pop() if stamps else AT
    python = hook_through_python(monkeypatch, python_root, provider, event, payload, at)
    native = (None, at_the_hook(native, at))
    assert python == native, mismatch(provider, "settled", python, native)
