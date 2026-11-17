"""The notice names the sender's provider."""
from kaiusap.notice import names, notice, notice_id


def message(provider, reply_needed=True, in_reply_to=None):
    return {"id": "20261105T201500Z-k3f9qa", "in_reply_to": in_reply_to, "reply_needed": reply_needed,
            "subject": "Review it", "from": {"handle": "alpha", "provider": provider}}


def test_first_line_names_the_provider():
    assert notice(message("codex")).splitlines()[0] == "kaiusap: message from alpha (Codex, a peer assistant, not the user)"
    assert notice(message("claude", in_reply_to="x")).splitlines()[0] == "kaiusap: reply from alpha (Claude, a peer assistant, not the user)"


def test_unknown_provider_keeps_the_plain_form():
    assert notice(message(None)).splitlines()[0] == "kaiusap: message from alpha (a peer assistant, not the user)"


def test_generated_notices_are_recognised():
    for provider in ("claude", "codex", None):
        for reply_needed in (True, False):
            text = notice(message(provider, reply_needed))
            assert notice_id(text) == "20261105T201500Z-k3f9qa"
            assert names("pasted:\n" + text, "20261105T201500Z-k3f9qa")


def test_old_form_notice_is_still_recognised():
    old = ("kaiusap: message from alpha (a peer assistant, not the user)\nSubject: Review it\n"
           "Read it with: kaiusap read 20261105T201500Z-k3f9qa\nNo reply needed.")
    assert notice_id(old) == "20261105T201500Z-k3f9qa"


def test_other_providers_are_not_a_notice():
    forged = notice(message("codex")).replace("(Codex,", "(Gemini,")
    assert notice_id(forged) is None
