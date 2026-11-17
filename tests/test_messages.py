"""`kaiusap messages`: every handle's mail, read-only, with `--since` polling."""
from io import StringIO
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys

import pytest

from conftest import send
from kaiusap.adapters import Identity, Providers
from kaiusap.cli import local, main
from datetime import timedelta

from kaiusap.core import SINCE_OVERLAP, Core, VERBS
from kaiusap.model import MailView, preview_text
from kaiusap.store import instant


def shifted(at, seconds):
    """`at` moved by `seconds`, in the store's form."""
    return (instant(at) + timedelta(seconds=seconds)).isoformat(timespec="microseconds").replace("+00:00", "Z")

KEYS = ["id", "at", "from", "from_session_key", "to", "to_session_key", "subject", "reply_needed",
        "in_reply_to", "chain_id", "depth", "state", "owed", "answerable_by",
        "from_persona_session_key", "to_persona_session_key", "reply_to", "outcome", "reason_code", "changed_at"]
AT = re.compile(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{6}Z")
KEY = {"alpha": "codex:a", "beta": "claude:b", "gamma": "codex:g"}


def message(store, mid, created, sender="alpha", to="beta", reply_needed=True, in_reply_to=None, subject="Review contract",
            fields=None):
    if fields is None:
        fields = {"request": "Review it", "expected_output": "Mismatches"} if reply_needed else {"context": "FYI"}
    store.save_message({"schema": 1, "id": mid, "created": created,
                        "from": {"handle": sender, "session_id": KEY[sender].split(":")[1], "provider": KEY[sender].split(":")[0],
                                 "session_key": KEY[sender]},
                        "to": {"handle": to, "session_id": KEY[to].split(":")[1], "session_key": KEY[to]},
                        "reply_needed": reply_needed, "in_reply_to": in_reply_to, "chain_id": mid, "depth": 0,
                        "subject": subject, "fields": fields})
    return mid


def event(store, at, name, mid, **fields):
    store.append(f"events/{at[:7]}.jsonl", {"at": at, "event": name, "message_id": mid, **fields})


def receipt(store, at, mid, handle="beta", receipt=True):
    event(store, at, "read", mid, handle=handle, reader_session="x", reader_session_key="codex:x", receipt=receipt)


def delivery(store, at, mid, outcome="delivered", reason_code=None):
    event(store, at, "delivery", mid, to_session="b", to_session_key="claude:b", outcome=outcome,
          reason_code=reason_code, reason=None, provider_version=None, deadline=None)


def run(store, *argv, environ=None, provider=None):
    out, err = StringIO(), StringIO()
    code = main(["messages", *argv], store=store, provider=provider, stdin=StringIO(""), stdout=out, stderr=err,
                environ={} if environ is None else environ)
    return code, out.getvalue(), err.getvalue()


def listing(store, *argv, **kwargs):
    code, out, err = run(store, *argv, "--json", **kwargs)
    assert code == 0 and err == "", (out, err)
    result = json.loads(out)
    assert result["schema"] == 1 and AT.fullmatch(result["as_of"])
    return result


def by_id(result):
    return {row["id"]: row for row in result["messages"]}


def snapshot(root):
    return {str(p.relative_to(root)): p.read_bytes() for p in root.rglob("*") if p.is_file()} if root.exists() else None


# ----- states, owed and outcome ---------------------------------------------------

def test_every_state_and_owed(store):
    unread = message(store, "20261029T140000Z-unread", "2026-10-29T14:00:00.000000Z")
    read = message(store, "20261029T140100Z-readxx", "2026-10-29T14:01:00.000000Z")
    receipt(store, "2026-10-29T15:00:00.000000Z", read)
    other = message(store, "20261029T140200Z-otherx", "2026-10-29T14:02:00.000000Z")
    receipt(store, "2026-10-29T15:01:00.000000Z", other, handle="gamma", receipt=False)  # not the recipient's receipt
    answered = message(store, "20261029T140300Z-answer", "2026-10-29T14:03:00.000000Z")
    reply = message(store, "20261029T150000Z-replyx", "2026-10-29T15:02:00.000000Z", sender="beta", to="alpha",
                    reply_needed=False, in_reply_to=answered, subject="Re: Review contract")
    moot = message(store, "20261029T140400Z-mootxx", "2026-10-29T14:04:00.000000Z")
    receipt(store, "2026-10-29T15:03:00.000000Z", moot)
    event(store, "2026-10-29T15:04:00.000000Z", "moot", moot, by_session="b", by_session_key="claude:b", reason=None)
    fyi_read = message(store, "20261029T140500Z-fyirdx", "2026-10-29T14:05:00.000000Z", reply_needed=False)
    receipt(store, "2026-10-29T15:05:00.000000Z", fyi_read)
    fyi_unread = message(store, "20261029T140600Z-fyiunr", "2026-10-29T14:06:00.000000Z", reply_needed=False)
    rows = by_id(listing(store))
    expected = {unread: ("unread", True), read: ("read", True), other: ("unread", True), answered: ("answered", False),
                reply: ("unread", False), moot: ("moot", False), fyi_read: ("read", False), fyi_unread: ("unread", False)}
    assert {mid: (row["state"], row["owed"]) for mid, row in rows.items()} == expected
    view = MailView(store)
    for m in view.messages:  # the one definition is the MailView method
        assert view.owed(m) is expected[m["id"]][1]


def test_row_shape_and_no_body(store):
    mid = message(store, "20261029T140000Z-shapex", "2026-10-29T14:00:00.000000Z")
    reply = message(store, "20261029T150000Z-replyx", "2026-10-29T15:00:00.000000Z", sender="beta", to="alpha",
                    reply_needed=False, in_reply_to=mid, subject="Re: Review contract")
    result = listing(store)
    assert set(result) == {"schema", "as_of", "messages"}
    first, second = result["messages"]
    assert list(first) == KEYS
    assert first == {"id": mid, "at": "2026-10-29T14:00:00.000000Z", "from": "alpha", "from_session_key": "codex:a",
                     "to": "beta", "to_session_key": "claude:b", "subject": "Review contract", "reply_needed": True,
                     "in_reply_to": None, "chain_id": mid, "depth": 0, "state": "answered", "owed": False,
                     "answerable_by": None, "from_persona_session_key": "codex:a", "to_persona_session_key": "claude:b",
                     "reply_to": None, "outcome": "stored", "reason_code": "not_attempted", "changed_at": "2026-10-29T15:00:00.000000Z"}
    assert second["in_reply_to"] == mid and second["from"] == "beta" and second["to_session_key"] == "codex:a"
    assert "fields" not in json.dumps(result) and "Review it" not in json.dumps(result)


def test_released_handles_are_listed(pair, store):
    a, b = pair
    to_beta = send(a)["message_id"]
    from_beta = send(b, to="alpha", reply_needed=False, fields={"context": "FYI"})["message_id"]
    b.run("release", {})
    rows = by_id(listing(store))
    assert set(rows) == {to_beta, from_beta}
    assert rows[to_beta]["to"] == "beta" and rows[from_beta]["from"] == "beta"


def test_outcome_projection(store):
    delivered = message(store, "20261029T140000Z-dlvrdx", "2026-10-29T14:00:00.000000Z")
    event(store, "2026-10-29T14:00:01.000000Z", "attempt", delivered, to_session="b", to_session_key="claude:b", binding="x")
    delivery(store, "2026-10-29T14:00:02.000000Z", delivered, "stored", "listing_failed")
    delivery(store, "2026-10-29T14:30:00.000000Z", delivered, "delivered")  # the latest delivery line wins
    unattempted = message(store, "20261029T140100Z-nolinx", "2026-10-29T14:01:00.000000Z")
    delivery(store, "2026-10-29T14:01:01.000000Z", unattempted, "stored", "paused")  # without an attempt
    attempted = message(store, "20261029T140200Z-attmpt", "2026-10-29T14:02:00.000000Z")
    event(store, "2026-10-29T14:02:01.000000Z", "attempt", attempted, to_session="b", to_session_key="claude:b", binding="x")
    neither = message(store, "20261029T140300Z-neithr", "2026-10-29T14:03:00.000000Z")
    rows = by_id(listing(store))
    assert {mid: (row["outcome"], row["reason_code"]) for mid, row in rows.items()} == {
        delivered: ("delivered", None), unattempted: ("stored", "paused"),
        attempted: ("uncertain", "no_confirmation"), neither: ("stored", "not_attempted")}
    view = MailView(store)
    for m in view.messages:  # the same projection as `inbox --sent`
        assert (rows[m["id"]]["outcome"], rows[m["id"]]["reason_code"]) == view.outcome(m)


def test_oldest_first_by_created(store):
    # File names sort one way, creation times the other: the order follows `created`.
    message(store, "20261029T140000Z-bbbbbb", "2026-10-29T14:00:00.500000Z")
    message(store, "20261029T140000Z-aaaaaa", "2026-10-29T14:00:00.900000Z")
    message(store, "20261029T130000Z-cccccc", "2026-10-29T13:00:00.000000Z")
    message(store, "20261101T090000Z-dddddd", "2026-11-01T09:00:00.000000Z")
    ids = [row["id"] for row in listing(store)["messages"]]
    assert ids == ["20261029T130000Z-cccccc", "20261029T140000Z-bbbbbb", "20261029T140000Z-aaaaaa", "20261101T090000Z-dddddd"]


# ----- --since, as_of and changed_at ------------------------------------------------

BASE = "2026-10-29T16:00:00.000000Z"


def base_store(store):
    old = message(store, "20261029T140000Z-oldmsg", "2026-10-29T14:00:00.000000Z")
    still = message(store, "20261029T140100Z-still1", "2026-10-29T14:01:00.000000Z")
    receipt(store, "2026-10-29T15:00:00.000000Z", still)
    delivery(store, "2026-10-29T14:01:01.000000Z", still)
    return old, still


@pytest.mark.parametrize("change", ["new", "read", "moot", "reply", "attempt", "delivery", "other_reader"])
def test_since_relists_a_changed_message(store, change):
    old, still = base_store(store)
    assert listing(store, "--since", BASE)["messages"] == []
    later = "2026-11-02T10:00:00.000000Z"  # another month's event file: the set isn't narrowed by --since
    expected = [old]
    if change == "new":
        expected = [message(store, "20261102T100000Z-newmsg", later)]
    elif change == "read":
        receipt(store, later, old)
    elif change == "moot":
        event(store, later, "moot", old, by_session="b", by_session_key="claude:b", reason=None)
    elif change == "reply":
        expected = [old, message(store, "20261102T100000Z-replyx", later, sender="beta", to="alpha",
                                 reply_needed=False, in_reply_to=old, subject="Re: Review contract")]
    elif change == "attempt":
        event(store, later, "attempt", old, to_session="b", to_session_key="claude:b", binding="x")
    elif change == "delivery":
        delivery(store, later, old, "stored", "paused")
    elif change == "other_reader":
        receipt(store, later, old, handle="gamma", receipt=False)  # not the recipient's receipt: no change
        expected = []
    rows = listing(store, "--since", BASE)["messages"]
    assert [row["id"] for row in rows] == expected
    assert all(row["changed_at"] == later for row in rows)
    assert still not in [row["id"] for row in rows]


def test_changed_at_is_the_latest_change(store):
    mid = message(store, "20261029T140000Z-latest", "2026-10-29T14:00:00.000000Z")
    assert by_id(listing(store))[mid]["changed_at"] == "2026-10-29T14:00:00.000000Z"
    delivery(store, "2026-10-29T14:20:00.000000Z", mid)
    receipt(store, "2026-10-29T14:10:00.000000Z", mid)  # written after, but earlier in time
    assert by_id(listing(store))[mid]["changed_at"] == "2026-10-29T14:20:00.000000Z"
    event(store, "2026-10-29T14:30:00.000000Z", "reminded", mid, handle="beta")  # not a change that counts
    assert by_id(listing(store))[mid]["changed_at"] == "2026-10-29T14:20:00.000000Z"
    event(store, "2026-10-29T14:40:00.000000Z", "attempt", mid, to_session="b", to_session_key="claude:b", binding="x")
    assert by_id(listing(store))[mid]["changed_at"] == "2026-10-29T14:40:00.000000Z"  # it moves the outcome


def test_since_overlaps_by_60_seconds(store):
    """`--since T` lists rows changed after T less 60 s, so a record stamped before its write
    was visible still shows."""
    assert SINCE_OVERLAP == timedelta(seconds=60)
    created = "2026-10-29T14:00:00.000000Z"
    message(store, "20261029T140000Z-exactx", created)
    for seconds, listed in [(-1, True), (0, True), (30, True), (59.999999, True), (60, False), (60.000001, False), (61, False)]:
        assert bool(listing(store, "--since", shifted(created, seconds))["messages"]) is listed, seconds


@pytest.mark.parametrize("seconds,listed", [(59, True), (60, False), (61, False)])
def test_overlap_applies_to_every_change(store, seconds, listed):
    """A change on an old message stamped up to 60 s before the --since time is listed; one more
    than 60 s before it isn't."""
    since = "2026-10-30T12:00:00.000000Z"
    changes = {}
    for index, name in enumerate(("read", "moot", "reply", "attempt", "delivery")):
        mid = changes[name] = message(store, f"20261029T14000{index}Z-{name[:6]:x<6}", "2026-10-29T14:00:00.000000Z")
        at = shifted(since, -seconds)
        if name == "read":
            receipt(store, at, mid)
        elif name == "moot":
            event(store, at, "moot", mid, by_session="b", by_session_key="claude:b", reason=None)
        elif name == "reply":
            # The reply is its own row too; it is checked with the others below.
            changes["the reply itself"] = message(store, "20261030T115900Z-replyx", at, sender="beta", to="alpha",
                                                  reply_needed=False, in_reply_to=mid, subject="Re: Review contract")
        elif name == "attempt":
            event(store, at, "attempt", mid, to_session="b", to_session_key="claude:b", binding="x")
        else:
            delivery(store, at, mid)
    rows = by_id(listing(store, "--since", since))
    for name, mid in changes.items():
        assert (mid in rows) is listed, name


def test_a_record_stamped_before_the_last_as_of_still_shows(store):
    """The race the 60 s overlap covers: a send stamps `created`, then checks its chain before saving,
    so a poll can read the store between the two and miss the message."""
    message(store, "20261029T140000Z-before", "2026-10-29T14:00:00.000000Z")
    first = listing(store)
    assert [row["id"] for row in first["messages"]] == ["20261029T140000Z-before"]
    late = shifted(first["as_of"], -0.5)  # stamped before the poll's as_of, visible only after its read
    mid = message(store, "20261029T235959Z-latexx", late)
    rows = listing(store, "--since", first["as_of"])["messages"]
    assert [row["id"] for row in rows] == [mid] and instant(rows[0]["changed_at"]) < instant(first["as_of"])


@pytest.mark.parametrize("since,listed", [
    # the message changed at 14:00:00Z; --since lists it up to 60 s later
    ("2026-10-29T14:00:59Z", True), ("2026-10-29T10:01:00-04:00", False), ("2026-10-29T10:00:59-04:00", True),
    ("20261029T140059Z", True), ("2026-10-29T14:01:00+00:00", False), ("2026-10-28", True), ("2026-10-31", False),  # a date alone: local midnight
])
def test_since_takes_any_iso_8601_time(store, since, listed):
    message(store, "20261029T140000Z-isoxxx", "2026-10-29T14:00:00.000000Z")
    assert bool(listing(store, "--since", since)["messages"]) is listed
    assert bool(listing(store, f"--since={since}")["messages"]) is listed


def test_since_without_an_offset_is_local_time(store, monkeypatch):
    monkeypatch.setenv("TZ", "America/Toronto")
    import time
    time.tzset()
    try:
        message(store, "20261029T140000Z-localx", "2026-10-29T14:00:00.000000Z")  # 10:00 in Toronto (EDT)
        assert listing(store, "--since", "2026-10-29T10:00:59")["messages"]
        assert not listing(store, "--since", "2026-10-29T10:01:00")["messages"]
    finally:
        monkeypatch.delenv("TZ")
        time.tzset()


def test_as_of_passed_back_verbatim(pair, store):
    """Real changes within a second of each other: each poll from the last as_of carries the new
    state; with the 60 s overlap, earlier rows may repeat, so rows are looked up by id."""
    a, b = pair
    first = send(a)["message_id"]
    initial = listing(store)
    assert [row["id"] for row in initial["messages"]] == [first]
    assert instant(initial["as_of"]) >= instant(initial["messages"][0]["changed_at"])
    assert [row["id"] for row in listing(store, "--since", initial["as_of"])["messages"]] == [first]  # the overlap
    b.run("read", {"id": first})
    after_read = by_id(listing(store, "--since", initial["as_of"]))
    assert (after_read[first]["state"], after_read[first]["owed"]) == ("read", True)
    assert instant(after_read[first]["changed_at"]) > instant(initial["as_of"])
    previous = listing(store, "--since", initial["as_of"])["as_of"]
    reply = b.run("reply", {"id": first, "reply_needed": True, "fields": {"body": "Done", "expected_output": "OK"}})["message_id"]
    after_reply = listing(store, "--since", previous)
    assert [(row["id"], row["state"], row["owed"]) for row in after_reply["messages"]] == [
        (first, "answered", False), (reply, "unread", True)]
    a.run("moot", {"id": reply})
    after_moot = listing(store, "--since", after_reply["as_of"])
    moot = by_id(after_moot)[reply]
    assert (moot["state"], moot["owed"]) == ("moot", False)
    assert instant(moot["changed_at"]) > instant(after_reply["as_of"])
    # Once the overlap has passed, an unchanged store lists nothing.
    assert listing(store, "--since", shifted(after_moot["as_of"], 61))["messages"] == []


@pytest.mark.parametrize("argv", [["--since", "yesterday"], ["--since=2026-13-01"], ["--since="], ["--since", "2026-10-29T25:00:00Z"],
                                  ["--since"]])
def test_malformed_since_exits_64(store, argv):
    code, out, err = run(store, *argv)
    assert code == 64 and out == "" and err.startswith("kaiusap: ") and "\n" == err[-1] and err.count("\n") == 1
    code, out, err = run(store, *argv, "--json")
    assert code == 64 and err == "" and json.loads(out)["code"] == 64


def test_arguments_it_does_not_take(store):
    assert run(store, "--all")[0] == 1
    assert run(store, "extra")[0] == 64
    assert main(["inbox", "--since", BASE], store=store, identity=Identity(), stdin=StringIO(""),
                stdout=StringIO(), stderr=StringIO(), environ={}) == 1


# ----- text output --------------------------------------------------------------------

def test_text_output(store):
    mid = message(store, "20261029T180200Z-k3f9qa", "2026-10-29T18:02:00.000000Z", sender="alpha", to="beta",
                  subject="Review the API contract")
    reply = message(store, "20261030T091500Z-p2m8rd", "2026-10-30T09:15:00.000000Z", sender="beta", to="alpha",
                    reply_needed=False, in_reply_to=mid, subject="Re: Review the API contract")
    receipt(store, "2026-10-30T10:00:00.000000Z", reply, handle="alpha")
    fresh = message(store, "20261031T100000Z-fresh1", "2026-10-31T10:00:00.000000Z", subject="Rerun the backtest")
    code, out, err = run(store)
    assert code == 0 and err == ""
    assert out.splitlines() == [
        f"{local('2026-10-29T18:02:00.000000Z')}  alpha → beta, answer expected: Review the API contract  answered  [{mid}]",
        f"{local('2026-10-30T09:15:00.000000Z')}  reply beta → alpha: Re: Review the API contract  read  [{reply}]",
        f"{local('2026-10-31T10:00:00.000000Z')}  alpha → beta, answer expected: Rerun the backtest  unread, owed  [{fresh}]"]


def test_text_output_in_local_time(store, monkeypatch):
    monkeypatch.setenv("TZ", "America/Toronto")
    import time
    time.tzset()
    try:
        mid = message(store, "20261029T180200Z-k3f9qa", "2026-10-29T18:02:00.000000Z", sender="alpha", to="beta",
                      subject="Review the API contract")
        assert run(store)[1] == f"2026-10-29 14:02  alpha → beta, answer expected: Review the API contract  unread, owed  [{mid}]\n"
    finally:
        monkeypatch.delenv("TZ")
        time.tzset()


def test_an_empty_store_prints_nothing(store):
    assert run(store) == (0, "", "")
    assert listing(store)["messages"] == []
    assert not store.root.exists()


# ----- from any shell, writing nothing and calling nothing -------------------------------

@pytest.fixture
def tripwires(tmp_path):
    """Fake tmux, ps, claude and codex on the PATH that record any run, so a call to one shows."""
    record = tmp_path / "ran"
    binary_dir = Path(os.environ["PATH"].split(os.pathsep)[0])
    for name in ("tmux", "ps", "claude", "codex"):
        path = binary_dir / name
        path.write_text(f"#!/bin/sh\necho {name} \"$@\" >> '{record}'\nexit 1\n")
        path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return record


ENVIRONMENTS = [
    {},
    {"CODEX_THREAD_ID": "t1", "CODEX_SESSION_ID": "t1"},
    {"CLAUDE_CODE_SESSION_ID": "s1", "CLAUDE_PID": "1"},
    {"CODEX_THREAD_ID": "t1", "CLAUDE_CODE_SESSION_ID": "s1"},
    {"TMUX": f"/private/tmp/tmux-{os.getuid()}/default,1,0", "TMUX_PANE": "%1"},
]


@pytest.mark.parametrize("environ", ENVIRONMENTS, ids=["plain", "codex", "claude", "both", "tmux"])
def test_writes_nothing_and_calls_nothing_from_any_shell(pair, store, tripwires, environ):
    a, b = pair
    mid = send(a)["message_id"]
    b.run("read", {"id": mid})
    before = snapshot(store.root)
    assert before and "handles.jsonl" in before and "markers/changed" in before
    for argv in ([], ["--json"], ["--since", "2026-01-01T00:00:00Z"], ["--since", "2026-01-01T00:00:00Z", "--json"]):
        code, out, err = run(store, *argv, environ=environ, provider=Providers.default(store))
        assert code == 0 and err == "" and mid in out
        assert snapshot(store.root) == before
    assert not tripwires.exists()
    assert not (store.root / "sessions/codex/terminals.jsonl").exists()


def test_through_the_core_runs_no_catch_up(store):
    calls = []

    class Adapter:
        def begin(self):
            calls.append("begin")

        def reset(self):
            calls.append("reset")

        def finish(self, catch_up=True):
            calls.append(("finish", catch_up))

        def __getattr__(self, name):
            raise AssertionError(f"messages called the adapter's {name}")

    providers = Providers({"codex": Adapter(), "claude": Adapter()}, store)
    result = Core(store, Identity(), providers).run("messages", {})
    assert result["schema"] == 1 and result["messages"] == []
    assert ("finish", False) in calls and ("finish", True) not in calls


def test_as_a_subprocess_from_a_plain_shell(tmp_path):
    root = tmp_path / "never-created"
    env = {"PATH": os.environ["PATH"], "HOME": os.environ["HOME"], "PYTHONPATH": "src", "KAIUSAP_HOME": str(root)}
    result = subprocess.run([sys.executable, "-m", "kaiusap", "messages", "--json"], capture_output=True, text=True, env=env,
                            cwd=Path(__file__).resolve().parents[1])
    assert result.returncode == 0 and result.stderr == ""
    output = json.loads(result.stdout)
    assert output["messages"] == [] and AT.fullmatch(output["as_of"])
    assert not root.exists()


def test_help_lists_the_verb(store):
    assert "messages" in VERBS
    out = StringIO()
    assert main(["help", "--json"], store=store, stdin=StringIO(""), stdout=out, stderr=StringIO(), environ={}) == 0
    assert "messages" in [v["verb"] for v in json.loads(out.getvalue())["verbs"]]
    out = StringIO()
    assert main(["help"], store=store, stdin=StringIO(""), stdout=out, stderr=StringIO(), environ={}) == 0
    assert any(line.startswith("messages: ") for line in out.getvalue().splitlines())


# ----- preview (--preview) -------------------------------------------------------------

def test_preview_picks_the_first_of_body_request_context(store):
    asks = message(store, "20261029T140000Z-asksxx", "2026-10-29T14:00:00.000000Z",
                   fields={"request": "Check the orders endpoint.", "expected_output": "Mismatches", "context": "Contract moved."})
    fyi = message(store, "20261029T140100Z-fyixxx", "2026-10-29T14:01:00.000000Z", reply_needed=False,
                  fields={"context": "The contract changed.", "current_state": "Draft"})
    reply = message(store, "20261029T150000Z-replyx", "2026-10-29T15:00:00.000000Z", sender="beta", to="alpha",
                    reply_needed=False, in_reply_to=asks, subject="Re: Review contract",
                    fields={"body": "No mismatches.", "context": "Ran it twice."})
    bare = message(store, "20261029T150100Z-barexx", "2026-10-29T15:01:00.000000Z", fields={"expected_output": "x"})
    rows = by_id(listing(store, "--preview"))
    assert {mid: (row["preview_field"], row["preview"]) for mid, row in rows.items()} == {
        asks: ("request", "Check the orders endpoint."), fyi: ("context", "The contract changed."),
        reply: ("body", "No mismatches."), bare: (None, None)}
    assert all(list(row) == KEYS + ["preview_field", "preview"] for row in rows.values())


@pytest.mark.parametrize("fields", [{"body": 42}, {"body": None}, {"body": ["a"]}, {"request": {"x": 1}, "context": "text"}])
def test_preview_of_a_field_that_is_not_text_is_null(store, fields):
    message(store, "20261029T140000Z-oddxxx", "2026-10-29T14:00:00.000000Z", fields=fields)
    [row] = listing(store, "--preview")["messages"]
    assert row["preview_field"] is None and row["preview"] is None


@pytest.mark.parametrize("text, expected", [
    ("Short line", "Short line"),
    ("one\ntwo", "one\ntwo"),
    ("one\ntwo\nthree", "one\ntwo…"),  # … is added when it fits
    ("x" * 250 + "\ny\nz", "x" * 250 + "\ny…"),
    ("one\n\n   \n\t\ntwo", "one\ntwo"),
    ("\n\n  one\ntwo", "one\ntwo"),
    ("  a \t  b  \n   c\u00a0\u00a0d  ", "a b\nc d"),
    ("one\r\ntwo\r\n", "one\ntwo"),
    ("one\ntwo\n\n  \n", "one\ntwo"),  # trailing blank lines aren't left out
    ("x" * 256, "x" * 256),
    ("x" * 257, "x" * 255 + "…"),
    ("x" * 300 + "\nmore", "x" * 255 + "…"),
    ("a" * 100 + "\n" + "b" * 155, "a" * 100 + "\n" + "b" * 155),  # 256 with the newline
    ("a" * 100 + "\n" + "b" * 156, "a" * 100 + "\n" + "b" * 154 + "…"),
    ("a" * 100 + "   " + "b" * 155, "a" * 100 + " " + "b" * 155),  # collapsing alone leaves nothing out
    ("", ""),
    (" \n\t", ""),
])
def test_preview_text(text, expected):
    result = preview_text(text)
    assert result == expected
    assert len(result) <= 256


def test_preview_ellipsis_marks_a_cut():
    # A complete text may end in a … of its own; an incomplete one always ends in ….
    assert preview_text("ends with …") == "ends with …"
    assert preview_text("one\ntwo\nthree").endswith("…")
    assert not preview_text("x" * 256).endswith("…")


def test_without_preview_the_rows_have_neither_key(store):
    message(store, "20261029T140000Z-plainx", "2026-10-29T14:00:00.000000Z")
    for result in (listing(store), listing(store, "--since", "2026-10-29T13:00:00Z")):
        assert result["messages"] and all(list(row) == KEYS for row in result["messages"])
    assert "Review it" not in run(store)[1]


def test_preview_with_since_lists_the_same_rows(store):
    old = message(store, "20261029T140000Z-oldxxx", "2026-10-29T14:00:00.000000Z", fields={"request": "Old", "expected_output": "x"})
    new = message(store, "20261029T160000Z-newxxx", "2026-10-29T16:00:00.000000Z", fields={"request": "New", "expected_output": "x"})
    receipt(store, "2026-10-29T16:30:00.000000Z", old)
    since = "2026-10-29T15:00:00Z"
    plain, previewed = listing(store, "--since", since), listing(store, "--since", since, "--preview")
    assert [r["id"] for r in plain["messages"]] == [r["id"] for r in previewed["messages"]] == [old, new]
    assert [r["preview"] for r in previewed["messages"]] == ["Old", "New"]
    for a, b in zip(plain["messages"], previewed["messages"]):
        assert {k: v for k, v in b.items() if k not in {"preview", "preview_field"}} == a


def test_preview_leaves_state_owed_and_changed_at(store):
    mid = message(store, "20261029T140000Z-stayxx", "2026-10-29T14:00:00.000000Z")
    for _ in range(2):
        [row] = listing(store, "--preview")["messages"]
        assert (row["state"], row["owed"], row["changed_at"]) == ("unread", True, "2026-10-29T14:00:00.000000Z")
    [row] = listing(store)["messages"]
    assert (row["state"], row["owed"], row["changed_at"]) == ("unread", True, "2026-10-29T14:00:00.000000Z")
    assert row["id"] == mid


@pytest.mark.parametrize("environ", ENVIRONMENTS, ids=["plain", "codex", "claude", "both", "tmux"])
def test_preview_writes_nothing_and_calls_nothing(pair, store, tripwires, environ):
    a, b = pair
    mid = send(a)["message_id"]
    before = snapshot(store.root)
    for argv in (["--preview"], ["--preview", "--json"], ["--since", "2026-01-01T00:00:00Z", "--preview", "--json"]):
        code, out, err = run(store, *argv, environ=environ, provider=Providers.default(store))
        assert code == 0 and err == "" and mid in out
        assert snapshot(store.root) == before
    assert not tripwires.exists()
    assert not (store.root / "sessions/codex/terminals.jsonl").exists()
    [row] = listing(store, "--preview")["messages"]
    assert row["state"] == "unread"


def test_preview_through_the_core_calls_no_adapter(store):
    message(store, "20261029T140000Z-corexx", "2026-10-29T14:00:00.000000Z")
    calls = []

    class Adapter:
        def begin(self):
            calls.append("begin")

        def reset(self):
            calls.append("reset")

        def finish(self, catch_up=True):
            calls.append(("finish", catch_up))

        def __getattr__(self, name):
            raise AssertionError(f"messages called the adapter's {name}")

    before = snapshot(store.root)
    result = Core(store, Identity(), Providers({"codex": Adapter(), "claude": Adapter()}, store)).run("messages", {"preview": True})
    assert result["messages"][0]["preview"] == "Review it"
    assert ("finish", False) in calls and ("finish", True) not in calls
    assert snapshot(store.root) == before


def test_preview_text_output(store):
    mid = message(store, "20261029T180200Z-k3f9qa", "2026-10-29T18:02:00.000000Z",
                  fields={"request": "Check the orders\n\n endpoint   now.\nThen this.\nNot shown.", "expected_output": "x"})
    bare = message(store, "20261029T180300Z-barexx", "2026-10-29T18:03:00.000000Z", fields={"expected_output": "x"})
    code, out, err = run(store, "--preview")
    assert code == 0 and err == ""
    assert out.splitlines() == [
        f"{local('2026-10-29T18:02:00.000000Z')}  alpha → beta, answer expected: Review contract  unread, owed  [{mid}]",
        "  Check the orders",
        "  endpoint now.…",
        f"{local('2026-10-29T18:03:00.000000Z')}  alpha → beta, answer expected: Review contract  unread, owed  [{bare}]"]
    assert run(store)[1].splitlines() == [out.splitlines()[0], out.splitlines()[3]]


def test_preview_flag_takes_no_value_and_only_on_messages(store):
    for argv in (["--preview=x"], ["--preview="], ["--preview=x", "--json"]):
        code, out, err = run(store, *argv)
        assert code == 64
    code, out, err = run(store, "--preview", "--preview")
    assert code == 64
    for verb in (["inbox", "--preview"], ["who", "--preview"], ["read", "x", "--preview"]):
        assert main(verb, store=store, identity=Identity(), stdin=StringIO(""),
                    stdout=StringIO(), stderr=StringIO(), environ={}) == 1
