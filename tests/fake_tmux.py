"""A fake default tmux server for kaiusap's tmux calls, the Claude start hook's pane key included.

It interprets only the commands kaiusap sends, in-process, as an injectable runner: no tmux is run.
"""
from kaiusap.adapters.tmux import DEFAULT_FORMAT


class FakeTmux:
    def __init__(self, pid=7, started=42):
        self.pid, self.started = pid, started
        self.sessions = {}  # session id -> name
        self.panes = {}  # pane id -> (session id, window id)
        self.pane_pids = {}  # pane id -> its process id; 10 when not set (the Claude test runner's process)
        self.window_options = {}  # window id -> the options set on that window
        self.session_options = {}  # session id -> the options set on that tmux session
        self.globals = {"automatic-rename": "on", "automatic-rename-format": DEFAULT_FORMAT}
        self.calls = []  # each call's arguments after `tmux -L default`
        self.fail = set()  # command names that exit 1
        self.raises = None  # an exception every call raises
        self._next = 0

    def add(self, name, panes=1, windows=1, ids=None):
        """A tmux session with `panes` panes spread over `windows` windows; returns the pane ids, which
        `ids` may choose."""
        session = f"${len(self.sessions)}"
        self.sessions[session] = name
        result = []
        for index in range(panes):
            window = f"@{session[1:]}.{index % windows}"
            self.window_options.setdefault(window, {})
            pane = ids[index] if ids else f"%{self._next}"
            self._next += 1
            self.panes[pane] = (session, window)
            result.append(pane)
        return result

    def key(self, pane):
        """The pane key kaiusap records."""
        return f"{self.pid}@{self.started}:{pane}"

    def window(self, pane):
        return self.panes[pane][1]

    def name(self, pane):
        return self.sessions[self.panes[pane][0]]

    def titles(self, pane):
        """The title options set on the pane's tmux session."""
        return self.session_options.get(self.panes[pane][0], {})

    def writes(self):
        return [c for c in self.calls if c[0] in {"set-option", "rename-session"}]

    def __call__(self, args, limit, env):
        assert args[:3] == ["tmux", "-L", "default"] and 0 < limit <= 3
        self.calls.append(args[3:])
        if self.raises:
            raise self.raises
        commands, current = [], []
        for word in args[3:]:
            if word == ";":
                commands.append(current)
                current = []
            else:
                current.append(word)
        commands.append(current)
        out = []
        for command in commands:
            if command[0] in self.fail:
                return 1, ""
            lines = self.one(command)
            if lines is None:
                return 1, ""
            out.extend(lines)
        return 0, "".join(line + "\n" for line in out)

    @staticmethod
    def options(command):
        flags, target, rest = set(), None, []
        words = iter(command[1:])
        for word in words:
            if word == "-t":
                target = next(words)
            elif word.startswith("-") and not rest:
                flags |= set(word[1:])
            else:
                rest.append(word)
        return flags, target, rest

    def expand(self, text, pane):
        session, _ = self.panes[pane]
        loop = "".join(name + ":" for name in self.sessions.values())
        for name, value in (("#{S:#{session_name}:}", loop), ("#{pid}", self.pid), ("#{start_time}", self.started),
                            ("#{pane_id}", pane), ("#{pane_pid}", self.pane_pids.get(pane, 10)), ("#{session_id}", session), ("#{session_name}", self.sessions[session]),
                            ("#{session_attached}", 1)):
            text = text.replace(name, str(value))
        return text

    def one(self, command):
        flags, target, rest = self.options(command)
        if command[0] == "display-message":
            return [self.expand(rest[0], target)] if target in self.panes else None
        if command[0] == "show-options":
            name = rest[0]
            if "g" in flags:
                value = self.globals.get(name)
            else:
                if target not in self.panes:
                    return None
                value = self.window_options[self.window(target)].get(name)
                if value is None and "A" in flags:
                    value = self.globals.get(name)
            return [] if value is None else [value]
        if command[0] == "set-option" and "w" not in flags:
            if target not in self.sessions:
                return None
            self.session_options.setdefault(target, {})[rest[0]] = rest[1]
            return []
        if command[0] == "set-option":
            if target not in self.panes:
                return None
            self.window_options[self.window(target)][rest[0]] = rest[1]
            return []
        if command[0] == "list-panes":
            if "s" in flags:
                if target not in self.panes:
                    return None
                session = self.panes[target][0]
                return [self.expand(rest[-1], p) for p, (s, _) in self.panes.items() if s == session]
            return [self.expand(rest[-1], p) for p in self.panes]
        if command[0] == "rename-session":
            name = rest[0]
            if target not in self.sessions or name in [n for s, n in self.sessions.items() if s != target]:
                return None  # tmux: "duplicate session"
            self.sessions[target] = name
            return []
        raise AssertionError(f"unexpected tmux command {command}")
