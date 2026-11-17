"""The texts kaiusap gives sessions, shared by every provider: the session line,
the delivery notice and the paste line."""
import re

from .adapters import ADAPTERS

# Every start hook adds this to the session's context, before any other text.
SESSION_LINE = ("To message other assistant sessions, or when the user gives you a handle (\"you are <handle>\"), "
                "use kaiusap's tools (search for kaiusap if they aren't loaded; `kaiusap help` prints the rules).")

HANDLE = r"[a-z][a-z0-9-]{1,31}"
MESSAGE_ID = r"\d{8}T\d{6}Z-[a-z0-9]{6}"
# The sender's provider, as the notice names it: its display name from the registry.
# Notices from before 2026-11-05 had no provider, and are still recognised when pasted back.
PROVIDER_NAMES = {name: entry[2] for name, entry in ADAPTERS.items()}
NAMED = "|".join(sorted(re.escape(name) for name in PROVIDER_NAMES.values()))
NOTICE = re.compile(
    rf"kaiusap: (?:message|reply) from {HANDLE} \((?:(?:{NAMED}), )?a peer assistant, not the user\)\n"
    rf"Subject: [^\n]*\nRead it with: kaiusap read (?P<id>{MESSAGE_ID})\n"
    r"(?:Answer with: kaiusap reply (?P=id)|No reply needed\.)")


def paste_line(message_id):
    return "Run kaiusap read " + message_id


def notice(message):
    kind = "reply" if message["in_reply_to"] else "message"
    last = "Answer with: kaiusap reply " + message["id"] if message["reply_needed"] else "No reply needed."
    provider = PROVIDER_NAMES.get((message.get("from") or {}).get("provider"))
    who = f"{provider}, a peer assistant, not the user" if provider else "a peer assistant, not the user"
    return "\n".join([f"kaiusap: {kind} from {message['from']['handle']} ({who})",
                      "Subject: " + message["subject"], "Read it with: kaiusap read " + message["id"], last])


def notice_id(text):
    """The message id when the text is a notice exactly as kaiusap generates it, else None."""
    match = NOTICE.fullmatch((text or "").rstrip("\n"))
    return match.group("id") if match else None


def names(text, message_id):
    """Whether the text contains the notice for this id, or its paste line."""
    text = text or ""
    return paste_line(message_id) in text or any(m.group("id") == message_id for m in NOTICE.finditer(text))
