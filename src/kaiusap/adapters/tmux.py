"""kaiusap's tmux writes: the window name rule, and the session rename and session titles, on unless
turned off.

Both adapters call these for the pane they know shows the provider session (Claude: its start record's
pane; Codex: a trusted pane showing the thread). Every call passes `-L default`, is capped by the
caller's time limit, and never raises: a failure is logged in hook-errors.log and fails nothing.
`subprocess` is imported only when a call is made, as the constant hooks require.
"""
TMUX = ["tmux", "-L", "default"]


def attach_command(session):
    """The line `sessions` prints to get back to a tmux session. `=` makes tmux match the name exactly;
    the quotes are always written, since zsh expands an unquoted word that starts with `=`."""
    return "tmux -L default attach -t '=" + session.replace("'", "'\\''") + "'"


def attach_pane(panes):
    """The pane whose tmux session `sessions`' attach line names: an attached one first."""
    return next((p for p in panes if p.get("attached")), panes[0]) if panes else None


# tmux's own global automatic-rename-format; kaiusap replaces only this.
DEFAULT_FORMAT = "#{?pane_in_mode,[tmux],#{pane_current_command}}#{?pane_dead,[dead],}"
# The pane title without a leading run of non-printable-ASCII characters and its space (Claude's `✳` or
# spinner), and without everything from the first ` | ` (Codex's further title parts).
WINDOW_FORMAT = "#{s/ [|] .*//:#{s/^[^ -~]+ //:pane_title}}"
# One line per pane of the target's tmux session; the last field lists every tmux session's name,
# each followed by `:`, which tmux never allows in a session name.
# The tmux session's own title options, so a terminal showing it is titled with its name.
SESSION_TITLES = (("set-titles", "on"), ("set-titles-string", "#S"))
SESSION_FORMAT = "\t".join(["#{pid}", "#{start_time}", "#{pane_id}", "#{pane_pid}", "#{session_id}",
                            "#{session_name}", "#{S:#{session_name}:}"])


def failures():
    import subprocess
    return OSError, ValueError, TimeoutError, subprocess.TimeoutExpired


def collision(name):
    """`iam`'s warning when another tmux session already has the name."""
    return f"tmux session `{name}` already exists, so this tmux session keeps its name."


def pane_parts(key):
    """(server pid, server start time, pane id) from a pane key such as `4321@1793606400:%5`, or None."""
    if not isinstance(key, str):
        return None
    server, _, rest = key.partition("@")
    started, _, pane = rest.partition(":")
    if not (server.isdigit() and started.isdigit() and pane.startswith("%")):
        return None
    return server, started, pane


def _limit(limit, seconds):
    value = limit(seconds)  # Claude's limit raises TimeoutError itself; Codex's returns 0
    if value is None or value <= 0:
        raise TimeoutError("no time left for tmux")
    return value


def name_window(run, limit, log, pane_key, holds=None):
    """One read, then set the window's `automatic-rename-format` only when `automatic-rename` is on, the
    window has no format of its own and the global format is tmux's default. True when the format was written.
    `holds`, when given, is called with the pane's process id and must return True for the pane to be
    used: the Claude check that the pane holds the session; a pane that doesn't is left silently.

    The read is one tmux call: the server's pid and start time (a pane key from an older server names
    another pane), the window's effective `automatic-rename` (`-A`, so a global `off` counts), the
    window's own format (no line when it has none) and the global format.
    """
    parts = pane_parts(pane_key)
    if not parts:
        return False
    server, started, pane = parts
    read = [*TMUX, "display-message", "-p", "-t", pane, "#{pid} #{start_time} #{pane_pid}", ";",
            "show-options", "-wvA", "-t", pane, "automatic-rename", ";",
            "show-options", "-wv", "-t", pane, "automatic-rename-format", ";",
            "show-options", "-gwv", "automatic-rename-format"]
    try:
        code, out = run(read, _limit(limit, 2), None)
        if code:
            raise ValueError(f"the read exited {code}")
        lines = out.splitlines()
        first = lines[0].split() if lines else []
        if len(first) != 3 or first[:2] != [server, started]:
            log(f"tmux window name: pane {pane_key} isn't on the running default server; nothing written")
            return False
        if holds is not None and not holds(first[2]):
            return False  # the pane doesn't hold the session: an inherited TMUX_PANE
        # Three lines: no format of the window's own. Four or more: it has one (or a format holds a
        # newline); either way the window is left as it is.
        if len(lines) != 3 or lines[1] != "on" or lines[2] != DEFAULT_FORMAT:
            return False
        code, _ = run([*TMUX, "set-option", "-w", "-t", pane, "automatic-rename-format", WINDOW_FORMAT],
                      _limit(limit, 2), None)
        if code:
            raise ValueError(f"set-option exited {code}")
        return True
    except failures() as exc:
        log(f"tmux window name for {pane_key}: {type(exc).__name__}: {exc}")
        return False


def rename_session(run, limit, log, pane_key, name, holds=None):
    """Rename the pane's tmux session to `name` when that session has exactly one pane, and set that
    tmux session's own `set-titles on` and `set-titles-string "#S"`, so the terminal showing it is titled with
    its name whatever the user's tmux.conf says. One `list-panes -s` read, one `rename-session` and one
    `set-option` call. The titles are set also when the session already has the name or the rename is skipped.
    Returns `iam`'s warning when another tmux session already has the name (the rename is skipped and logged),
    else None. `holds` is the Claude pane check, as for name_window."""
    parts = pane_parts(pane_key)
    if not parts or not name:
        return None
    server, started, pane = parts
    try:
        code, out = run([*TMUX, "list-panes", "-s", "-t", pane, "-F", SESSION_FORMAT], _limit(limit, 2), None)
        if code:
            raise ValueError(f"list-panes exited {code}")
        lines = out.splitlines()
        if len(lines) != 1:
            return None  # more than one pane (or window): never renamed
        fields = lines[0].split("\t")
        if len(fields) != 7 or fields[:3] != [server, started, pane]:
            log(f"tmux session name: pane {pane_key} isn't on the running default server; not renamed")
            return None
        if holds is not None and not holds(fields[3]):
            return None  # the pane doesn't hold the session
        session, current, names = fields[4], fields[5], fields[6].split(":")
        warning = None
        if current != name and name in names:
            warning = collision(name)
            log(f"tmux session name for {pane_key}: {warning}")
        elif current != name:
            code, _ = run([*TMUX, "rename-session", "-t", session, name], _limit(limit, 2), None)
            if code:
                raise ValueError(f"rename-session exited {code}")
        (first, on), (second, string) = SESSION_TITLES
        code, _ = run([*TMUX, "set-option", "-t", session, first, on, ";",
                       "set-option", "-t", session, second, string], _limit(limit, 2), None)
        if code:
            log(f"tmux session titles for {pane_key}: set-option exited {code}")
        return warning
    except failures() as exc:
        log(f"tmux session name for {pane_key}: {type(exc).__name__}: {exc}")
    return None
