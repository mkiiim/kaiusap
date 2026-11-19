"""Generated handles: their form, and that a name the store has seen as a handle is never generated."""
import random
import re

import pytest

from kaiusap import names
from kaiusap.model import PERSONAS

HANDLE = r"[a-z][a-z0-9-]{1,31}"


def test_every_word_is_lowercase_letters_and_no_word_is_in_both_lists():
    for word in names.ADJECTIVES + names.NOUNS:
        assert re.fullmatch(r"[a-z]{1,9}", word), word
    assert len(set(names.ADJECTIVES)) == len(names.ADJECTIVES)
    assert len(set(names.NOUNS)) == len(names.NOUNS)
    assert not set(names.ADJECTIVES) & set(names.NOUNS)


def test_a_name_is_an_adjective_a_noun_and_four_digits_and_a_valid_handle():
    rng = random.Random(7)
    for _ in range(500):
        name = names.generate(rng=rng)
        adjective, noun, digits = name.split("-")
        assert adjective in names.ADJECTIVES and noun in names.NOUNS
        assert re.fullmatch(r"[1-9][0-9]{3}", digits)
        assert re.fullmatch(HANDLE, name)


def test_the_longest_possible_name_fits_the_handle_rule():
    longest = f"{max(names.ADJECTIVES, key=len)}-{max(names.NOUNS, key=len)}-9999"
    assert len(longest) <= 32


def test_a_name_in_the_unavailable_set_is_skipped():
    first = names.generate(rng=random.Random(3))
    assert names.generate({first}, rng=random.Random(3)) != first


def test_no_free_name_is_a_lookup_error(monkeypatch):
    monkeypatch.setattr(names, "ADJECTIVES", ("calm",))
    monkeypatch.setattr(names, "NOUNS", ("lake",))
    every = {f"calm-lake-{n}" for n in range(1000, 10000)}
    with pytest.raises(LookupError):
        names.generate(every, rng=random.Random(1))


def test_handles_ever_bound_offered_or_suggested_are_unavailable(pair, store):
    a, b = pair
    b.run("release", {})
    store.append(PERSONAS, {"at": "2026-11-18T00:00:00.000-05:00", "session_key": "codex:c", "offered": "gamma",
                            "from": "codex:a"})
    store.append(PERSONAS, {"at": "2026-11-18T00:00:01.000-05:00", "session_key": "codex:d",
                            "suggested": "calm-lake-1234"})
    assert {"alpha", "beta", "gamma", "calm-lake-1234"} <= names.unavailable(store)


def test_an_empty_store_has_nothing_unavailable(store):
    assert names.unavailable(store) == set()


def test_free_never_returns_a_name_the_store_has_seen(store, monkeypatch):
    monkeypatch.setattr(names, "ADJECTIVES", ("calm",))
    monkeypatch.setattr(names, "NOUNS", ("lake",))
    store.append(PERSONAS, {"at": "2026-11-18T00:00:01.000-05:00", "session_key": "codex:d",
                            "suggested": "calm-lake-1234"})
    rng = random.Random(0)
    for _ in range(200):
        assert names.free(store, rng) != "calm-lake-1234"
