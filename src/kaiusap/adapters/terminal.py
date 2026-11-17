"""Process and pane mechanics for an adapter whose sessions run in a terminal process it records by pid and start
time: a process's start time, the default tmux server's panes, whether a pane holds a process, a start
record's pane and a hook's pane key. Shared, so no adapter reads another; it names no
provider.

`TerminalProcesses` is a mixin. The adapter gives `run`, `limit(seconds)`, `store`, `environ` and
`process_label` (its name in log lines), and resets `_processes = {}`, `_panes = None` and `_parents = None` at
each call.
"""
from datetime import datetime, timezone
import re

from .daemon import linux_start_time, time_locale_env

PS = re.compile(r"^\s*(\w{3}\s+\w{3}\s+\d+\s+\d\d:\d\d:\d\d\s+\d{4})\s*$")
PANE_FORMAT = "\t".join(["#{pane_id}", "#{session_name}", "#{session_attached}", "#{pid}", "#{start_time}", "#{pane_pid}"])


class TerminalProcesses:
    process_label = "terminal"

    def started_at(self, pid):
        """The process's start time, or None when it isn't running or can't be read."""
        import subprocess
        if type(pid) is not int or pid <= 0:
            return None
        if pid not in self._processes:
            self._processes[pid] = None
            try:
                started = linux_start_time(pid, self.run)
                if not started:
                    code, out = self.run(["ps", "-ww", "-o", "lstart=", "-p", str(pid)], self.limit(2), time_locale_env())
                    match = PS.match(out.strip()) if not code else None
                    if match:
                        local = datetime.strptime(" ".join(match[1].split()), "%a %b %d %H:%M:%S %Y").astimezone()
                        started = local.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
                self._processes[pid] = started or None
            except (OSError, subprocess.TimeoutExpired, ValueError, TimeoutError) as exc:
                self.store.log(f"{self.process_label} process {pid}: start time unreadable: {type(exc).__name__}: {exc}")
        return self._processes[pid]

    def panes(self):
        """The default tmux server's panes, by composite key, read once per call."""
        import subprocess
        if self._panes is None:
            self._panes = {}
            try:
                code, out = self.run(["tmux", "-L", "default", "list-panes", "-a", "-F", PANE_FORMAT], self.limit(2), None)
                if not code:
                    for line in out.splitlines():
                        fields = line.split("\t")
                        if len(fields) == 6 and fields[5].isdigit():
                            pane, session, attached, server, started, pane_pid = fields
                            key = f"{server}@{started}:{pane}"
                            self._panes[key] = {"pane": key, "tmux_session": session, "attached": attached not in {"", "0"},
                                                "trusted": None, "pane_pid": int(pane_pid)}
            except (OSError, subprocess.TimeoutExpired, ValueError, TimeoutError):
                pass
        return self._panes

    def holds(self, pid):
        """The pane check: a function of a pane's process id, true when it is `pid` or one of its ancestors."""
        def check(pane_pid):
            if type(pid) is not int or pid <= 0:
                return False
            if self._parents is None:
                code, out = self.run(["ps", "-A", "-o", "pid=,ppid="], self.limit(2), None)
                if code:
                    raise ValueError(f"ps exited {code}")
                self._parents = {}
                for line in out.splitlines():
                    parts = line.split()
                    if len(parts) == 2 and parts[0].isdigit() and parts[1].isdigit():
                        self._parents[int(parts[0])] = int(parts[1])
            target, current = int(pane_pid), pid
            for _ in range(64):
                if current == target:
                    return True
                if current not in self._parents or current <= 1:
                    return False
                current = self._parents[current]
            return False
        return check

    def record_pane(self, record):
        """The pane in a start record, when it is on the running default server and holds the record's process."""
        if not record or not record.get("tmux_pane"):
            return None
        pane = self.panes().get(record["tmux_pane"])
        try:
            return pane if pane and self.holds(record.get("pid"))(pane["pane_pid"]) else None
        except (OSError, ValueError, TimeoutError) as exc:
            self.store.log(f"{self.process_label} pane check for {record['tmux_pane']}: {exc}")
            return None

    def pane_key(self, event, raw_pane):
        """The composite pane key and the tmux session's name, into `event`: one display-message."""
        import subprocess
        try:
            code, out = self.run(["tmux", "-L", "default", "display-message", "-p", "-t", raw_pane,
                                  "#{pid} #{start_time} #{session_name}"], self.limit(2), None)
            server, started, session = out.strip().split(" ", 2)
            if code or server != self.environ["TMUX"].rsplit(",", 2)[1]:
                raise ValueError("non-default tmux server or failed display-message")
            event.update(tmux_pane=f"{server}@{started}:{raw_pane}", tmux_session=session)
        except (OSError, subprocess.TimeoutExpired, ValueError, IndexError, TimeoutError) as exc:
            self.store.log(f"{self.process_label} start pane unavailable: {exc}")
