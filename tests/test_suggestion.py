"""The one-time generated name: the first call after which a session holds no handle and has no open offer
suggests a name, records it, and no later call suggests another."""
from io import StringIO
import json
import re

import pytest

from conftest import FakeProvider
from kaiusap.adapters import Identity
from kaiusap.cli import main
from kaiusap.core import SUGGESTION, Core
from kaiusap.errors import Error
from kaiusap.model import PERSONAS, handles
from kaiusap.store import now

NAME = r"[a-z]+-[a-z]+-[1-9][0-9]{3}"


def session(store, key="a", classification="root", parent_key=None):
    return Core(store, Identity("codex", key, f"codex:{key}", "/fixture", classification, parent_key), FakeProvider())


def suggested_lines(store):
    return [row for row in store.rows(PERSONAS) if "suggested" in row]


def test_the_first_call_suggests_a_name_once_and_records_it(store):
    core = session(store)
    first = core.run("whoami", {})
    assert re.fullmatch(NAME, first["suggested_handle"])
    assert [(row["session_key"], row["suggested"]) for row in suggested_lines(store)] == [("codex:a", first["suggested_handle"])]
    assert "suggested_handle" not in core.run("whoami", {})
    assert "suggested_handle" not in core.run("inbox", {})
    assert len(suggested_lines(store)) == 1


def test_a_refused_send_from_a_session_with_no_handle_carries_the_suggestion(store):
    core = session(store)
    with pytest.raises(Error) as refused:
        core.run("send", {"to": "beta", "subject": "Review", "reply_needed": False, "fields": {"request": "Look"}})
    assert "has no handle" in str(refused.value)
    assert re.fullmatch(NAME, refused.value.suggested_handle)
    assert "suggested_handle" not in core.run("whoami", {})


def test_a_session_holding_a_handle_is_suggested_nothing(store):
    core = session(store)
    core.run("iam", {"handle": "alpha"})
    assert "suggested_handle" not in core.run("whoami", {})
    assert suggested_lines(store) == []


def test_an_open_offer_comes_first_and_leaves_the_suggestion_unused(store):
    session(store, "b").run("iam", {"handle": "alpha"})
    store.append(PERSONAS, {"at": now(), "session_key": "codex:a", "offered": "alpha", "from": "codex:b"})
    assert "suggested_handle" not in session(store).run("whoami", {})
    assert suggested_lines(store) == []


def test_release_suggests_nothing_but_leaves_the_suggestion_for_a_later_call(store):
    core = session(store)
    core.run("iam", {"handle": "alpha"})
    assert "suggested_handle" not in core.run("release", {})
    later = core.run("whoami", {})
    assert re.fullmatch(NAME, later["suggested_handle"]) and later["suggested_handle"] != "alpha"


@pytest.mark.parametrize("classification,parent_key", [("spawned", "codex:p"), ("unknown", None), ("plain_shell", None)])
def test_spawned_agents_unknown_callers_and_plain_shells_are_suggested_nothing(store, classification, parent_key):
    core = Core(store, Identity("codex", "a", "codex:a" if classification != "unknown" else None, "/fixture",
                                classification, parent_key), FakeProvider())
    assert "suggested_handle" not in core.run("whoami", {})
    assert suggested_lines(store) == []


def test_the_generator_is_told_every_handle_bound_offered_or_suggested(store, monkeypatch):
    from kaiusap import names
    session(store, "b").run("iam", {"handle": "alpha"})
    session(store, "c").run("iam", {"handle": "gamma"})
    session(store, "c").run("release", {})
    session(store, "d").run("whoami", {})
    earlier = suggested_lines(store)[0]["suggested"]
    seen = {}

    def generate(unavailable=(), rng=None):
        seen["unavailable"] = set(unavailable)
        return "calm-lake-1234"
    monkeypatch.setattr(names, "generate", generate)
    assert session(store).run("whoami", {})["suggested_handle"] == "calm-lake-1234"
    assert {"alpha", "gamma", earlier} <= seen["unavailable"]


def test_taking_the_suggestion_with_iam_ends_it(store):
    core = session(store)
    name = core.run("whoami", {})["suggested_handle"]
    assert core.run("iam", {"handle": name})["handle"] == name
    assert handles(store)[name]["session_key"] == "codex:a"
    assert "suggested_handle" not in core.run("whoami", {})


def test_a_second_session_is_suggested_a_different_name(store):
    first = session(store, "a").run("whoami", {})["suggested_handle"]
    second = session(store, "b").run("whoami", {})["suggested_handle"]
    assert first != second


def run_cli(store, *argv):
    out, err = StringIO(), StringIO()
    code = main([*argv], store=store, provider=FakeProvider(), stdin=StringIO(""), stdout=out, stderr=err, environ={})
    return code, out.getvalue(), err.getvalue()


def test_the_cli_prints_the_suggestion_after_the_result_and_after_a_refusal(store):
    code, out, _ = run_cli(store, "whoami")
    name = suggested_lines(store)[0]["suggested"]
    assert code == 0 and out.endswith(SUGGESTION.format(name) + "\n")
    code, out, _ = run_cli(store, "whoami")
    assert "suggests" not in out


def test_the_cli_refusal_carries_the_suggestion_in_text_and_json(store, tmp_path):
    from kaiusap.store import Store
    code, _, err = run_cli(store, "send", "--to", "beta", "--subject", "Review", "--reply-needed", "no", "--request", "Look")
    name = suggested_lines(store)[0]["suggested"]
    assert code == 1 and err.splitlines()[-1] == SUGGESTION.format(name)
    other = Store(tmp_path / "other")
    code, out, _ = run_cli(other, "--json", "send", "--to", "beta", "--subject", "Review", "--reply-needed", "no",
                           "--request", "Look")
    refusal = json.loads(out)
    assert code == 1 and refusal["suggested_handle"] == suggested_lines(other)[0]["suggested"]


def test_iam_with_no_handle_takes_a_free_generated_name(store):
    core = session(store)
    result = core.run("iam", {})
    assert result["generated"] is True and re.fullmatch(NAME, result["handle"])
    assert handles(store)[result["handle"]]["session_key"] == "codex:a"
    assert "suggested_handle" not in result and suggested_lines(store) == []


def test_iam_with_no_handle_is_refused_for_a_session_that_holds_one(store):
    core = session(store)
    core.run("iam", {"handle": "alpha"})
    with pytest.raises(Error) as refused:
        core.run("iam", {})
    assert str(refused.value) == "This session is alpha. iam with no handle only names a session that has none."
    assert held_by(store, "codex:a") == ["alpha"]


def held_by(store, key):
    return [name for name, binding in handles(store).items() if binding and binding["session_key"] == key]


def test_iam_with_no_handle_never_takes_a_name_already_bound_or_suggested(store, monkeypatch):
    from kaiusap import names
    session(store, "b").run("iam", {"handle": "alpha"})
    session(store, "d").run("whoami", {})
    earlier = suggested_lines(store)[0]["suggested"]
    seen = {}

    def generate(unavailable=(), rng=None):
        seen["unavailable"] = set(unavailable)
        return "calm-lake-1234"
    monkeypatch.setattr(names, "generate", generate)
    assert session(store).run("iam", {})["handle"] == "calm-lake-1234"
    assert {"alpha", earlier} <= seen["unavailable"]


def test_iam_with_no_handle_when_no_name_is_free_is_refused(store, monkeypatch):
    from kaiusap import names

    def full(store, rng=None):
        raise LookupError("no free generated name")
    monkeypatch.setattr(names, "free", full)
    with pytest.raises(Error) as refused:
        session(store).run("iam", {})
    assert "couldn't find a free generated name" in str(refused.value)
    assert handles(store) == {}


def test_the_cli_iam_with_no_handle_says_which_name_it_picked(store):
    code, out, _ = run_cli(store, "iam")
    name = next(iter(handles(store)))
    assert code == 0 and out.splitlines()[0] == f"kaiusap picked `{name}`; it now points to this session."


def test_the_mcp_iam_with_no_handle_takes_a_generated_name(store, monkeypatch):
    from kaiusap import mcp
    monkeypatch.setattr(mcp, "Store", lambda: store)
    monkeypatch.setattr(mcp, "identify", lambda *a, **k: (Identity("codex", "a", "codex:a", "/fixture", "root"), None))

    class Providers:
        @staticmethod
        def default(store):
            return FakeProvider()
    monkeypatch.setattr(mcp, "Providers", Providers)
    text, result, failed = mcp.call("codex", "iam", {}, {})
    assert not failed and result["generated"] is True
    assert text.startswith(f"kaiusap picked `{result['handle']}`; it now points to this session.")
