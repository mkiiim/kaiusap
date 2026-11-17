"""The stdio MCP server: a face over the core, one fresh core per call.

`kaiusap mcp <provider>` names the provider on the command line; nothing here
reads a provider's session variables. Every tool call builds its own store and
adapters, resolves the caller's identity for that call alone, runs the verb the
CLI would run, and answers with the CLI's text and `--json` object. The MCP SDK
is imported only by `serve()`, so no other verb pays for it.
"""
from dataclasses import replace
import sys

from .adapters import ADAPTERS, Identity, Providers
from .cli import render
from .core import Core, rule_lines
from .errors import Error, store_error
from .model import FIELDS, last_directory
from .store import Store

PROVIDERS = tuple(sorted(ADAPTERS))
TOOLS = ("whoami", "who", "iam", "release", "send", "reply", "inbox", "read", "moot", "pause", "unpause", "help")

# The rules each tool carries: the numbers are the rules' own.
RULES_OF = {"send": (1, 2, 3, 7, 9, 10, 11), "reply": (1, 2, 3, 7, 9, 10, 11), "read": (3, 4, 6, 8, 11),
            "inbox": (3, 4, 6, 8, 11), "iam": (5, 9, 11), "release": (5, 9, 11), "moot": (5, 9, 11),
            "unpause": (5, 9, 11), "pause": (11,), "whoami": (11,), "who": (11,)}

SUMMARIES = {
    "whoami": "Show your handle, whether you can be woken and why not, and your unread count.",
    "who": "List the active handles: each one's provider, directory, name, whether it is reachable, and its unread count.",
    "iam": ("Take a handle for this session, on the user's word ('you are <handle>'). A handle a running session holds is refused; "
            "one from a stopped session moves here, and its earlier mail stays with that session unless kaiusap offered you "
            "the handle. The result says which."),
    "release": "Release this session's handle, on the user's word.",
    "send": ("Send a new message to a handle. reply_needed says whether you expect an answer. Body fields: with reply_needed true, "
             "request and expected_output are required; with false, context is. context, current_state, constraints and "
             "starting_points are optional. The result is the outcome line; a stored message comes with a paste line."),
    "reply": ("Answer a message by its id. body is required; reply_needed says whether you expect an answer, and "
              "expected_output is required when it is true. The reply goes to the session that sent the message, "
              "under the handle it holds now; if it holds none, the reply is refused."),
    "inbox": ("List this session's mail, whatever handle it holds or none, unread first; all includes resolved mail, "
              "sent lists what this session sent."),
    "read": "Read a message by id, or by any prefix that matches exactly one.",
    "moot": "Mark a message that asked for an answer as needing none, on the user's word.",
    "pause": "Pause every delivery on this machine (the kill switch); messages are still written and stored.",
    "unpause": "Resume deliveries, on the user's word.",
    "help": "Show the rules for sessions and a one-line summary of each verb.",
}

# Every tool: neither hint set makes Codex's default `auto` approval
# mode ask before a tool's first call, which a turn woken with no terminal can't answer.
# `readOnlyHint` stays unset: `read` records a receipt and the acting verbs write.
ANNOTATIONS = {"destructive_hint": False, "open_world_hint": False}

STRING = {"type": "string"}
BOOLEAN = {"type": "boolean"}
BODY = {name: STRING for name in sorted(FIELDS - {"body"})}


def when(reply_needed, required):
    """A conditional field requirement, as JSON Schema: with `reply_needed` so, these fields are required."""
    return {"if": {"properties": {"reply_needed": {"const": reply_needed}}, "required": ["reply_needed"]},
            "then": {"required": required}}


SCHEMAS = {
    "send": {"properties": {"to": STRING, "subject": {"type": "string", "maxLength": 80}, "reply_needed": BOOLEAN, **BODY},
             "required": ["to", "subject", "reply_needed"],
             "allOf": [when(True, ["request", "expected_output"]), when(False, ["context"])]},
    "reply": {"properties": {"id": STRING, "body": STRING, "reply_needed": BOOLEAN,
                             **{name: STRING for name in sorted(FIELDS - {"body", "request"})}},
              "required": ["id", "body", "reply_needed"],
              "allOf": [when(True, ["expected_output"])]},
    "iam": {"properties": {"handle": STRING}, "required": ["handle"]},
    "read": {"properties": {"id": STRING}, "required": ["id"]},
    "moot": {"properties": {"id": STRING, "reason": STRING}, "required": ["id"]},
    "inbox": {"properties": {"all": BOOLEAN, "sent": BOOLEAN}},
    "pause": {"properties": {"reason": STRING}},
}

# Which tools run without the caller's identity (as the CLI's `help` and `who`), and
# which are refused when a Claude caller can't be identified: everything but
# `read` and `pause`, which run as from a plain shell, as for an unknown Codex caller.
IDENTITY_FREE = {"help", "who"}
# The identity argument: a provider's plugin may write the caller's id into a call's arguments. The base
# removes it from every call before the schema check and hands it to the adapter; no schema lists it.
IDENTITY_ARGUMENT = "_kaiusap_session"
NEEDS_IDENTITY = {"whoami", "inbox", "send", "reply", "iam", "release", "moot", "unpause"}


def version():
    """The server's version for the provider (`serverInfo`): the package version, with the commit as build
    metadata when the build recorded one."""
    from . import build
    running = build.running()
    return running["version"] + (f"+{running['commit'][:7]}" if running["commit"] else "")


def instructions():
    """The rules as `kaiusap help` prints them."""
    return "\n".join(rule_lines())


def description(tool):
    """The tool's summary, then exactly the rules assigned to it, numbered as in `help`."""
    return "\n".join([SUMMARIES[tool], *rule_lines(RULES_OF.get(tool, ()))])


def schema(tool):
    base = SCHEMAS.get(tool, {"properties": {}})
    result = {"type": "object", "properties": base["properties"], "required": base.get("required", []),
              "additionalProperties": False}
    if "allOf" in base:
        result["allOf"] = base["allOf"]  # the fields required conditionally; the core checks them too
    return result


def arguments(tool, given):
    """Map a tool's arguments to the core's verb and arguments, refusing what the CLI would refuse."""
    given = dict(given or {})
    allowed = schema(tool)["properties"]
    unknown = sorted(set(given) - set(allowed))
    if unknown:
        raise Error("Arguments not allowed for this tool: " + ", ".join(unknown) + ". Check kaiusap help.")
    for name, value in list(given.items()):
        expected = allowed[name]["type"]
        if expected == "boolean" and value in ("true", "false"):
            # Some models write a boolean as its JSON spelling in quotes; exactly these two are read.
            given[name] = value = value == "true"
        if (expected == "boolean" and type(value) is not bool) or (expected == "string" and not isinstance(value, str)):
            raise Error(f"{name} must be a {expected}. Check kaiusap help.")
    missing = [name for name in schema(tool)["required"] if name not in given]
    if missing:
        raise Error(f"{tool} requires {', '.join(missing)}.")
    if tool == "send":
        return "send", {"to": given.pop("to", None), "subject": given.pop("subject", None),
                        "reply_needed": given.pop("reply_needed", None), "fields": given}
    if tool == "reply":
        return "reply", {"id": given.pop("id", None), "reply_needed": given.pop("reply_needed", None), "fields": given}
    return tool, given


def identify(tool, provider, providers, store, meta, stated=None):
    """The caller of this call: resolved afresh by the adapter, from `_meta` and the identity argument."""
    if tool in IDENTITY_FREE:
        return Identity(), None
    adapter = providers.named(provider)
    warning = None
    try:
        identity = adapter.mcp_identity(meta, stated)
    except Error as exc:
        if tool in NEEDS_IDENTITY:
            raise
        # `read` and `pause` from an unidentified caller (a Claude process with no current
        # record, a Codex call without `_meta.threadId`) run as from a plain shell, and say why.
        identity, warning = Identity(), str(exc)
    return replace(identity, cwd=directory(identity, adapter, store)), warning


def directory(identity, adapter, store):
    """The directory `whoami` reports: the adapter's own, read in this call, then the records; never the server's own."""
    key = identity.parent_key or identity.session_key
    if not key:
        return None
    return adapter.directory(identity) or last_directory(store, key)


def stale_warning():
    """A server older than the install: every tool result says when kaiusap was reinstalled under this
    server. Best effort: a check that fails says nothing."""
    from . import build
    try:
        return build.stale_warning()
    except Exception:
        return None


def call(provider, tool, given, meta):
    """Run one tool call to completion: (text, structured object, is_error)."""
    store = Store()
    providers = None
    try:
        given = dict(given or {})
        stated = given.pop(IDENTITY_ARGUMENT, None)  # before the schema check, whatever the provider
        verb, args = arguments(tool, given)
        providers = Providers.default(store)
        providers.begin()  # the call's clock starts before its identity lookup
        identity, warning = identify(tool, provider, providers, store, meta, stated)
        result = Core(store, identity, providers).run(verb, args)
        if warning:
            result.setdefault("warnings", []).append(warning)
        stale = stale_warning()
        if stale:
            result.setdefault("warnings", []).append(stale)
        text = render(verb, dict(result)) or ""
        for line in result.get("warnings") or []:
            text += ("\n" if text else "") + "Warning: " + line
        return text, result, False
    except (OSError, ValueError) as exc:
        error = store_error(exc)
    except Error as exc:
        error = exc
    finally:
        if providers is not None:
            providers.finish(catch_up=False)  # a lookup that ended the call early leaves no connection open
    text = "kaiusap: " + str(error).replace("\n", " ").replace("\r", " ")
    structured = {"schema": 1, "error": text, "code": error.code}
    stale = stale_warning()
    if stale:
        # A refused call carries the notice too: it may be the only call the model makes.
        text += "\nWarning: " + stale
        structured["warnings"] = [stale]
    return text, structured, True


def serve(provider):
    import anyio
    from mcp.server import Server
    from mcp.server.stdio import stdio_server
    import mcp.types as types

    async def list_tools(ctx, params):
        return types.ListToolsResult(tools=[types.Tool(name=tool, description=description(tool), input_schema=schema(tool),
                                                       annotations=types.ToolAnnotations(**ANNOTATIONS))
                                            for tool in TOOLS])

    async def call_tool(ctx, params):
        meta = dict(params.meta or {})
        if params.name not in TOOLS:
            text = f"kaiusap: Unknown tool {params.name}. Run kaiusap help."
            return types.CallToolResult(content=[types.TextContent(text=text)], is_error=True,
                                        structured_content={"schema": 1, "error": text, "code": 64})
        # Each call runs in its own thread with its own store and adapters, so calls
        # the provider sends at once neither block nor see each other.
        text, structured, failed = await anyio.to_thread.run_sync(call, provider, params.name, params.arguments or {}, meta)
        return types.CallToolResult(content=[types.TextContent(text=text)], structured_content=structured, is_error=failed)

    from . import build
    build.running()  # the build this server loaded, read before any reinstall can change the files
    server = Server("kaiusap", version=version(), instructions=instructions(),
                    on_list_tools=list_tools, on_call_tool=call_tool)

    async def run():
        async with stdio_server() as (read_stream, write_stream):
            await server.run(read_stream, write_stream, server.create_initialization_options())

    anyio.run(run)


def main(argv):
    if len(argv) != 1 or argv[0] not in PROVIDERS:
        print("kaiusap: mcp takes one argument, " + " or ".join(PROVIDERS) + ". Each plugin's MCP entry names its provider.", file=sys.stderr)
        return 64
    try:
        serve(argv[0])
    except ImportError as exc:
        print(f"kaiusap: the MCP SDK isn't installed ({exc}). Reinstall kaiusap with uv tool install.", file=sys.stderr)
        return 1
    return 0
