"""Stable CLI parsing and text/JSON presentation."""
import json
import os
from pathlib import Path
import sys

from .adapters import Identity, Providers, display_name
from .build import label
from .core import PLAIN_SHELL_STOP, SUGGESTION, Core, VERBS, rule_lines
from .errors import Error, store_error
from .model import FIELDS
from .store import Store, instant

FLAGS = FIELDS | {"to", "subject", "reply_needed", "all", "sent", "reason", "since", "preview"}
ALLOWED = {
    "send": FIELDS - {"body"} | {"to", "subject", "reply_needed"},
    "reply": FIELDS - {"request"} | {"reply_needed"},
    "inbox": {"all", "sent"}, "pause": {"reason"}, "moot": {"reason"}, "messages": {"since", "preview"},
}
POSITIONAL = {"iam": "handle", "reply": "id", "read": "id", "moot": "id"}


def provider_setup_flags():
    """The flags `setup` takes besides the shared ones: each adapter's own."""
    from .adapters import setup_flags
    return setup_flags()


def parse(argv, stdin):
    if not argv or argv[0] not in VERBS:
        raise Error("Unknown or unavailable verb. Run kaiusap help.", 64)
    verb = argv[0]
    args, positionals = {}, []
    index = 1
    stdin_used = False
    while index < len(argv):
        token = argv[index]
        index += 1
        if not token.startswith("--"):
            positionals.append(token)
            continue
        flag, equals, inline = token[2:].partition("=")
        key = flag.replace("-", "_")
        if key not in FLAGS and not (verb == "setup" and key in provider_setup_flags()):
            raise Error(f"Unknown flag --{flag}. Run kaiusap help.", 64)
        if key in args:
            raise Error(f"Repeated flag --{flag}. Supply it once.", 64)
        if key in {"all", "sent", "preview"}:
            if equals:
                raise Error(f"--{flag} takes no value.", 64)
            value = True
        else:
            if equals:
                value = inline
            else:
                if index == len(argv) or argv[index].startswith("--"):
                    raise Error(f"--{flag} needs a value.", 64)
                value = argv[index]
                index += 1
            if key in FIELDS | {"subject"}:
                if value.startswith("@@"):
                    value = value[1:]
                elif value == "@-":
                    if stdin_used:
                        raise Error("Only one field may read stdin.", 64)
                    stdin_used = True
                    value = stdin.read()
                elif value.startswith("@"):
                    try:
                        value = Path(value[1:]).read_text()
                    except (OSError, UnicodeError):
                        raise Error("Field file could not be read. Check its path and encoding.", 64) from None
            if key == "reply_needed":
                if value not in {"yes", "no"}:
                    raise Error("--reply-needed must be yes or no.", 64)
                value = value == "yes"
        args[key] = value
    disallowed = set(args) - ALLOWED.get(verb, set()) - (provider_setup_flags() if verb == "setup" else set())
    if disallowed:
        raise Error("Flags not allowed for this verb: " + ", ".join(sorted(disallowed)) + ". Run kaiusap help.")
    expected = 1 if verb in POSITIONAL else 0
    if len(positionals) > expected:
        raise Error("Unexpected positional argument. Run kaiusap help.", 64)
    if expected:
        if positionals:
            args[POSITIONAL[verb]] = positionals[0]
        elif verb != "iam":  # `iam` with no handle takes a generated name
            raise Error(f"{verb} requires {POSITIONAL[verb]}.")
    if verb in {"send", "reply"}:
        args["fields"] = {k: args.pop(k) for k in list(args) if k in FIELDS}
    return verb, args


def local(at, format="%Y-%m-%d %H:%M"):
    return instant(at).astimezone().strftime(format)


def column(value):
    """A null field in a column renderer (`who`, `sessions`): `-`, so the columns stay aligned."""
    return "-" if value is None else value


def where_from(provider, cwd):
    """`(<display name>, ~/Projects/trade)` in prose, leaving out a null directory."""
    return f"({display_name(provider)}, {cwd})" if cwd is not None else f"({display_name(provider)})"


def waits(waiting_on):
    """The woken line: what a Codex thread was waiting on when the notice arrived."""
    if "approval" in waiting_on and "user_input" in waiting_on:
        return "an approval and the user's input"
    return "an approval" if "approval" in waiting_on else "the user's input"


def render(verb, result):
    r = result
    if verb in {"send", "reply"}:
        recipient = r.get("recipient", "the recipient")
        if r["outcome"] == "stored":
            return f"stored ({r['reason']}) [{r['message_id']}]. Paste this into {recipient}'s session:\n{r['paste_line']}"
        if r["outcome"] == "uncertain":
            return f"uncertain ({r['reason']}) [{r['message_id']}]. If it didn't arrive, {recipient}'s next input may bring the unread-mail note (needs the kaiusap plugin)."
        if r["outcome"] == "woken":
            if r.get("waiting_on"):
                return (f"woken: {recipient} accepted it as a turn, but its thread is waiting on {waits(r['waiting_on'])}, "
                        f"so it won't act on this until the user answers there; resume the thread if no terminal shows it. [{r['message_id']}]")
            return f"woken: {recipient} accepted it as a turn. [{r['message_id']}]"
        return f"delivered: posted to {recipient}'s session, which decides when to read it. [{r['message_id']}]"
    if verb == "help":
        return "\n".join([*rule_lines(), "", *(v["verb"] + ": " + v["summary"] for v in r["verbs"]), "", "kaiusap " + label(r["kaiusap"])])
    if verb == "version":
        return "kaiusap " + label(r)
    if verb == "iam":
        if r.get("generated"):
            text = f"kaiusap picked `{r['handle']}`; it now points to this session."
        else:
            text = f"{r['handle']} {'already' if r['already_held'] else 'now'} points to this session."
        if r["moved_from"]:
            old = r["moved_from"]
            text += f" Moved from the {display_name(old['provider'])} session in {old['cwd'] or '(unknown directory)'}, named {old['name'] or '(unknown)'}."
        if r["previous_provider"]:
            provider = display_name(r["session_key"].split(":", 1)[0])
            text += f" {r['handle']} is now a {provider} session."
        if r["released"]:
            text += f" Released {r['released']}."
        if r["process_changed"]:
            text += " Deliveries now go to this terminal."
        if r["unread"]:
            text += f" Unread: {r['unread']} (from {', '.join(r['unread_from'])})."
        if r.get("notice"):
            text += " " + r["notice"]
        return text
    if verb == "release":
        return f"Released {r['handle']}."
    if verb == "moot":
        return f"Marked \"{r['subject']}\" from {r['from']} as needing no answer."
    if verb == "pause":
        return "Deliveries paused. Messages are still written and stored."
    if verb == "unpause":
        return "Deliveries resumed."
    if verb == "read":
        m = r["message"]
        lines = []
        if r["already_read_at"]:
            lines.append(f"You already read this at {local(r['already_read_at'], '%H:%M')}.")
            if m["reply_needed"]:
                lines.append("You replied." if r["replied"] else "You haven't replied.")
        lines += [f"From: {m['from']['handle']}", f"Date: {local(m['created'])}", f"Subject: {m['subject']}"]
        if m["in_reply_to"]:
            lines.append("In reply to: " + m["in_reply_to"])
        lines += [f"\n{key.replace('_', ' ').title()}:\n{value}" for key, value in m["fields"].items()]
        if not r.get("mine", not r["for_handle"]):
            h = r["for_handle"] or m["to"]["handle"]
            lines.append(f"This message belongs to another session; taking {h} won't make it yours. Tell the user.")
        elif r["for_handle"] is None and r.get("no_handle"):
            lines.append("This message is yours, but you hold no handle. Ask the user for one, then read it again to reply.")
        else:
            lines.append("Answer with: kaiusap reply " + m["id"] if m["reply_needed"] else "No reply needed.")
        return "\n".join(lines)
    if verb == "inbox":
        lines = []
        for m in r["messages"]:
            expected = ", answer expected" if m["reply_needed"] else ""
            reply = "reply " if m["in_reply_to"] else ""
            if "to" in m:
                state = {"read_unanswered": "read, not answered", "unread": m["outcome"] + ", not read"}.get(m["state"], m["state"])
                lines.append(f"{local(m['at'])}  to {m['to']}{expected}: {m['subject']}  {state}  [{m['id']}]")
                if m["paste_line"]:
                    lines.append(m["paste_line"])
            else:
                lines.append(f"{m['state']}  {local(m['at'])}  {reply}from {m['from']}{expected}: {m['subject']}  [{m['id']}]")
        return "\n".join(lines)
    if verb == "messages":
        lines = []
        for m in r["messages"]:
            expected = ", answer expected" if m["reply_needed"] else ""
            reply = "reply " if m["in_reply_to"] else ""
            state = m["state"] + (", owed" if m["owed"] else "")
            lines.append(f"{local(m['at'])}  {reply}{m['from']} → {m['to']}{expected}: {m['subject']}  {state}  [{m['id']}]")
            if m.get("preview"):
                lines.extend("  " + line for line in m["preview"].split("\n"))
        return "\n".join(lines)
    if verb == "whoami":
        if r["plain_shell"]:
            return "This is a plain shell, not an assistant session. " + PLAIN_SHELL_STOP
        if r["spawned"]:
            parent = f" of {r['parent_handle']}" if r["parent_handle"] else ""
            return f"You are a spawned agent{parent} {where_from(r['provider'], r['cwd'])}. You may look (whoami, who, inbox, read, help, pause) but not send; report to your parent, which sends."
        text = (f"You are {r['handle']}" if r["handle"] else "You have no handle") + f" {where_from(r['provider'], r['cwd'])}. "
        why = "; ".join(part for part in (r["why_not"], r["fix"]) if part)
        text += ("Can be woken" if r["handle"] else "Can be woken once named") + (": yes." if r["can_be_woken"] else f": unknown. {why}." if r["can_be_woken"] is None and why else f": no. {why}." if why else ": no.")
        if r["handle"]:
            text += f" Unread: {r['unread']}."
        if r["paused"]:
            text += " Deliveries are paused."
        return text
    if verb == "who":
        return "\n".join(f"{h['handle']}  {display_name(h['provider'])}  {column(h['cwd'])}  {column(h['name'])}  " +
                         ("reachable" if h["reachable"] else f"not reachable ({h['why_not']})" if h["why_not"] else "not reachable") +
                         (", detached" if h["detached"] else "") + f"  unread: {h['unread']}  {h['session_key']}" for h in r["handles"])
    if verb == "sessions":
        lines = []
        for s in r["sessions"]:
            where = {"not_in_tmux": "running, not in tmux", "not_running": "not running"}.get(s["where"], s["where"])
            if s["where"] == "tmux":
                where = "; ".join(f"tmux \"{p['tmux_session']}\", {'attached' if p['attached'] else 'detached'}" for p in s["tmux_panes"])
                if s["trusted"] is False:
                    where += " (not yet trusted: send it a prompt)"
            elif s["where"] == "hosted" and s.get("hosts"):
                host = s["hosts"][0]
                where = f"in the {host['name']} desktop app" if host["kind"] == "app" else f"in {host['name']}"
            lines.append(f"{s['handle'] or '(no handle)'}  {display_name(s['provider'])}  {column(s['cwd'])}  {where}  {s['session_key']}")
            if s["command"]:
                lines.append(s["command"])
            if s.get("note"):
                lines.append(s["note"])
        return "\n".join(lines)
    if verb == "setup":
        lines = []
        for item in r["items"]:
            state = "done" if item["done"] else ("warning" if item["item"] == "tmux server" else "missing")
            lines.append(f"{item['item']}: {state}" + (f" ({item['detail']})" if item["detail"] else ""))
            if item["command"]:
                lines.extend(item["command"].splitlines())  # each command or line alone on its own line
            if item["note"]:
                lines.append("Note: " + item["note"])
        return "\n".join(lines)
    raise Error("Unavailable verb.", 64)


def main(argv=None, *, store=None, provider=None, identity=None, stdin=None, stdout=None, stderr=None, environ=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    stdin, stdout, stderr = stdin or sys.stdin, stdout or sys.stdout, stderr or sys.stderr
    environ = os.environ if environ is None else environ
    as_json = "--json" in argv
    argv = [arg for arg in argv if arg != "--json"]
    try:
        verb, args = parse(argv, stdin)
        store = store or Store()
        provider = provider or Providers.default(store)
        provider.begin()  # the call's clock starts before its identity lookup
        # Looking at global information doesn't require caller identity; `setup` never looks it up.
        if identity is None:
            identity = Identity() if verb in {"help", "who", "sessions", "messages", "setup", "version"} else provider.identity(environ, store)
        core = Core(store, identity, provider, environ)
        result = core.run(verb, args)
        if as_json:
            print(json.dumps(result, ensure_ascii=False), file=stdout)
        else:
            shown = dict(result)
            text = render(verb, shown)
            if text:
                print(text, file=stdout)
            for warning in result.get("warnings") or []:
                print("Warning: " + warning, file=stdout)
            if result.get("suggested_handle"):
                print(SUGGESTION.format(result["suggested_handle"]), file=stdout)
        return 0
    except (OSError, ValueError) as exc:
        error = store_error(exc)
    except Error as exc:
        error = exc
    text = "kaiusap: " + str(error).replace("\n", " ").replace("\r", " ")
    suggested = getattr(error, "suggested_handle", None)
    if as_json:
        refusal = {"schema": 1, "error": text, "code": error.code}
        if suggested:
            refusal["suggested_handle"] = suggested
        print(json.dumps(refusal), file=stdout)
    else:
        print(text, file=stderr)
        if suggested:
            print(SUGGESTION.format(suggested), file=stderr)
    return error.code
