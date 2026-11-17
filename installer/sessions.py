"""What is open now, counted by kind, read-only. The installer never closes a session.

- Claude Code sessions: `claude agents --json`, the live-session listing Claude's adapter reads; every entry
  with a process id.
- Codex terminals: one `ps -axww -o args=`, each line classified by Codex's own adapter test
  (`kaiusap.adapters.codex.classify_command`).
- OpenCode: the same `ps` lines whose program is the OpenCode command, minus its non-terminal subcommands
  (an interpretation: kaiusap's OpenCode test needs a start record, which a machine without kaiusap has none of).
"""
import json
import os
import subprocess

# OpenCode subcommands that aren't a terminal (`opencode --help`, 1.19.5 and 2.2.7): the service, the server, tools.
OPENCODE_NOT_TERMINAL = {"service", "serve", "web", "mcp", "acp", "run", "upgrade", "uninstall", "auth", "agent",
                         "models", "stats", "export", "import", "github", "pr", "debug", "plugin", "x",
                         "--version", "-v", "--help", "-h", "completion", "generate"}
LISTING_LIMIT = 3


def count(ctx, survey):
    """{kind: count} for each provider found, a kind left out when it can't be read."""
    found = {}
    if survey.claude:
        try:
            code, out = ctx.run(["claude", "agents", "--json"], LISTING_LIMIT)
            rows = json.loads(out) if code == 0 else None
            if isinstance(rows, list):
                found["claude"] = sum(1 for r in rows if isinstance(r, dict) and type(r.get("pid")) is int)
        except (OSError, ValueError, subprocess.TimeoutExpired):
            pass
    if survey.codex or survey.opencode:
        try:
            code, out = ctx.run(["ps", "-axww", "-o", "args="], LISTING_LIMIT)
            lines = out.splitlines() if code == 0 else None
        except (OSError, ValueError, subprocess.TimeoutExpired):
            lines = None
        if lines is not None:
            if survey.codex:
                from kaiusap.adapters.codex import classify_command
                found["codex"] = sum(1 for line in lines if classify_command(line))
            if survey.opencode:
                name = os.path.basename(survey.opencode["command"])
                found["opencode"] = sum(1 for line in lines if opencode_terminal(line, name))
    return found


def opencode_terminal(line, name):
    tokens = line.split()
    if not tokens or os.path.basename(tokens[0]) != name:
        return False
    return len(tokens) == 1 or tokens[1] not in OPENCODE_NOT_TERMINAL


def describe(found, survey):
    """`3 Claude Code sessions, 2 Codex terminals, 1 OpenCode terminal`, or '' when nothing is open."""
    parts = []
    for kind in ("claude", "codex", "opencode"):
        n = found.get(kind, 0)
        if not n:
            continue
        if kind == "claude":
            parts.append(f"{n} Claude Code session{'s' if n != 1 else ''}")
        elif kind == "codex":
            parts.append(f"{n} Codex terminal{'s' if n != 1 else ''}")
        else:
            noun = "terminal" if survey.opencode["provider"] == 2 else "session"
            parts.append(f"{n} OpenCode {noun}{'s' if n != 1 else ''}")
    return ", ".join(parts)


def total(found):
    return sum(found.values())
