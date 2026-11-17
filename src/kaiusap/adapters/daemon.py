"""The Codex app-server daemon: discovery and a JSON-RPC client over its Unix socket.

`websockets` is imported here only, so hooks and every verb that doesn't talk to
the daemon never load it.
"""
import json
import os
import time

VERSION_LIMIT, CONNECT_LIMIT, REQUEST_LIMIT, TURN_LIMIT = 3, 2, 5, 10


class DaemonError(Exception):
    """A protocol, timeout or socket failure, with its reason and transmission state."""

    def __init__(self, code, detail="", sent=False):
        super().__init__(detail or code)
        self.code = code
        self.detail = detail
        self.sent = sent


class RpcError(Exception):
    """A JSON-RPC error answer from the daemon."""

    def __init__(self, error):
        self.code = error.get("code")
        self.message = str(error.get("message", ""))
        self.data = error.get("data")
        super().__init__(self.message)


class Deadline:
    def __init__(self, seconds=None, at=None):
        self.at = at if at is not None else (time.monotonic() + seconds if seconds is not None else None)

    def remaining(self):
        return None if self.at is None else self.at - time.monotonic()

    def expired(self):
        remaining = self.remaining()
        return remaining is not None and remaining <= 0

    def limit(self, seconds):
        """The shorter of a step's own limit and the time left."""
        remaining = self.remaining()
        return seconds if remaining is None else max(0.0, min(seconds, remaining))


def find_daemon(run, deadline, environ=None):
    """`codex app-server daemon version` with CODEX_HOME removed: (socket path, app-server version)."""
    import subprocess
    env = {k: v for k, v in (os.environ if environ is None else environ).items() if k != "CODEX_HOME"}
    limit = deadline.limit(VERSION_LIMIT)
    if limit <= 0:
        raise DaemonError("daemon_not_found", "no time left for codex app-server daemon version")
    try:
        code, out = run(["codex", "app-server", "daemon", "version"], limit, env)
    except (OSError, subprocess.TimeoutExpired, ValueError) as exc:
        raise DaemonError("daemon_not_found", f"codex app-server daemon version failed: {type(exc).__name__}") from None
    if code != 0:
        raise DaemonError("daemon_not_found", f"codex app-server daemon version exited {code}")
    start = out.find("{")
    try:
        info = json.loads(out[start:]) if start >= 0 else None
    except ValueError:
        info = None
    if not isinstance(info, dict):
        raise DaemonError("daemon_not_found", "codex app-server daemon version printed no JSON object")
    path, version = info.get("socketPath"), info.get("appServerVersion")
    if not isinstance(path, str) or not os.path.exists(path):
        raise DaemonError("daemon_not_found", "the daemon's socket is missing")
    return path, version if isinstance(version, str) else None


class Daemon:
    """One WebSocket connection; requests are matched to answers by id, everything with a `method` is ignored."""

    def __init__(self, socket_path, version, deadline, client_version):
        self.socket_path, self.version, self.deadline, self.client_version = socket_path, version, deadline, client_version
        self.connection = None
        self.counter = 0
        self.broken = False  # a write was aborted or the connection dropped: nothing more can be sent

    def connect(self):
        from websockets.exceptions import WebSocketException
        from websockets.sync.client import unix_connect
        limit = self.deadline.limit(CONNECT_LIMIT)
        if limit <= 0:
            raise DaemonError("timeout", "no time left to connect")
        try:
            self.connection = unix_connect(self.socket_path, uri="ws://localhost/", open_timeout=limit, close_timeout=1, legacy=True)
        except TimeoutError:
            raise DaemonError("timeout", "connecting to the daemon took too long") from None
        except PermissionError:
            raise DaemonError("sandboxed", "the session's sandbox denied the daemon socket") from None
        except (OSError, WebSocketException) as exc:
            raise DaemonError("daemon_not_found", f"the daemon's socket refused the connection: {type(exc).__name__}") from None
        return self

    def initialize(self):
        try:
            result = self.request("initialize", {"clientInfo": {"name": "kaiusap", "version": self.client_version}}, REQUEST_LIMIT, protocol=True)
        except RpcError as exc:
            raise DaemonError("protocol_unrecognised", "initialize was refused: " + exc.message) from None
        if not isinstance(result, dict) or not isinstance(result.get("userAgent"), str):
            raise DaemonError("protocol_unrecognised", "the initialize answer had no userAgent")
        self.notify("initialized")
        return result

    def notify(self, method, params=None):
        message = {"method": method}
        if params is not None:
            message["params"] = params
        self._send(message)

    def _send(self, message, end=None):
        import socket
        import threading
        from websockets.exceptions import WebSocketException
        payload = json.dumps(message)
        end = end if end is not None else time.monotonic() + self.deadline.limit(REQUEST_LIMIT)
        remaining = end - time.monotonic()
        if remaining <= 0:
            raise DaemonError("timeout", "no time left to send " + message["method"])
        connection = self.connection
        expired = threading.Event()

        def abort_write():
            expired.set()
            try:
                connection.socket.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

        timer = threading.Timer(remaining, abort_write)
        timer.daemon = True
        timer.start()
        try:
            connection.send(payload)
            if expired.is_set():
                self.broken = True
                raise DaemonError("timeout", "writing " + message["method"] + " took too long", sent=True)
        except (OSError, WebSocketException) as exc:
            self.broken = True
            code = "timeout" if expired.is_set() else "protocol_unrecognised"
            raise DaemonError(code, f"the connection dropped while sending: {type(exc).__name__}", sent=True) from None
        finally:
            timer.cancel()
            timer.join()

    def request(self, method, params, limit, protocol=False):
        """Send a request and wait for its answer: the result, or RpcError for an error answer.

        A malformed answer, a dropped connection or the limit raises DaemonError
        (`timeout` for a limit, `protocol_unrecognised` otherwise). `sent` says
        transmission began, so a turn without an answer may have been submitted.
        """
        from websockets.exceptions import WebSocketException
        self.counter += 1
        request_id = self.counter
        end = time.monotonic() + self.deadline.limit(limit)
        if end <= time.monotonic():
            raise DaemonError("timeout", "no time left to send " + method)
        self._send({"id": request_id, "method": method, "params": params}, end)
        while True:
            wait = end - time.monotonic()
            if wait <= 0:
                raise DaemonError("timeout", f"no answer to {method} in time", sent=True)
            try:
                raw = self.connection.recv(timeout=wait)
            except TimeoutError:
                raise DaemonError("timeout", f"no answer to {method} in time", sent=True) from None
            except (OSError, WebSocketException) as exc:
                self.broken = True
                raise DaemonError("protocol_unrecognised", f"the connection dropped waiting for {method}: {type(exc).__name__}", sent=True) from None
            try:
                message = json.loads(raw)
            except (ValueError, TypeError):
                raise DaemonError("protocol_unrecognised", f"a non-JSON frame answered {method}", sent=True) from None
            if not isinstance(message, dict) or "method" in message or message.get("id") != request_id:
                continue
            if isinstance(message.get("error"), dict):
                raise RpcError(message["error"])
            if "result" not in message:
                raise DaemonError("protocol_unrecognised", f"the answer to {method} had neither result nor error", sent=True)
            return message["result"]

    def loaded_threads(self):
        ids, cursor = [], None
        for _ in range(1000):
            params = {"cursor": cursor} if cursor else {}
            try:
                result = self.request("thread/loaded/list", params, REQUEST_LIMIT, protocol=True)
            except RpcError as exc:
                raise DaemonError("protocol_unrecognised", "thread/loaded/list was refused: " + exc.message) from None
            data = result.get("data") if isinstance(result, dict) else None
            if not isinstance(data, list) or not all(isinstance(item, str) for item in data):
                raise DaemonError("protocol_unrecognised", "thread/loaded/list answered without a list of ids")
            ids.extend(data)
            cursor = result.get("nextCursor")
            if not cursor:
                return ids
        raise DaemonError("protocol_unrecognised", "thread/loaded/list never ended")

    def thread(self, thread_id):
        result = self.request("thread/read", {"threadId": thread_id, "includeTurns": False}, REQUEST_LIMIT)
        thread = result.get("thread") if isinstance(result, dict) else None
        if not isinstance(thread, dict) or thread.get("id") != thread_id:
            raise DaemonError("protocol_unrecognised", "thread/read answered without the thread")
        return thread

    def set_name(self, thread_id, name):
        self.request("thread/name/set", {"threadId": thread_id, "name": name}, REQUEST_LIMIT)

    def usable(self):
        """Whether another request can still be sent on this connection."""
        from websockets.protocol import State
        return self.connection is not None and not self.broken and self.connection.state is State.OPEN

    def close(self):
        if self.connection is not None:
            try:
                self.connection.close_timeout = self.deadline.limit(1)
                self.connection.close()
            except Exception:
                pass
            self.connection = None


def time_locale_env(environ=None):
    """The environment for a `ps` whose `lstart` kaiusap parses: `ps` prints dates in the
    user's locale (`en_CA`: "Thu  5 Nov ..."), so LC_TIME is C and LC_ALL, which overrides it, is dropped."""
    env = {k: v for k, v in (os.environ if environ is None else environ).items() if k != "LC_ALL"}
    env["LC_TIME"] = "C"
    return env


def proc_start_time(pid, root="/proc"):
    """A process's start time from Linux's /proc: field 22 of `<root>/<pid>/stat` (clock ticks
    after boot) plus `btime` from `<root>/stat`, cut to the second, as UTC; None when it can't be read."""
    from datetime import datetime, timezone
    from pathlib import Path
    try:
        stat = Path(root, str(pid), "stat").read_text()
        ticks = int(stat[stat.rindex(")") + 2:].split()[19])  # the fields after `comm` start at field 3
        btime = next(int(line.split()[1]) for line in Path(root, "stat").read_text().splitlines() if line.startswith("btime "))
        seconds = btime + ticks // os.sysconf("SC_CLK_TCK")
    except (OSError, ValueError, IndexError, StopIteration):
        return None
    return datetime.fromtimestamp(seconds, timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def linux_start_time(pid, runner):
    """On Linux, with the real process runner, the start time from /proc, else None (the caller then
    uses `ps` `lstart`). A runner a test supplies keeps `ps`'s answer, so a fake process stays fake."""
    import sys
    if runner is not run_command or not sys.platform.startswith("linux"):
        return None
    return proc_start_time(pid)


def run_command(argv, limit, env=None):
    """The default subprocess runner: (exit code, stdout). Raises TimeoutExpired or OSError."""
    import subprocess
    completed = subprocess.run(argv, capture_output=True, text=True, timeout=limit, env=env, stdin=subprocess.DEVNULL)
    return completed.returncode, completed.stdout
