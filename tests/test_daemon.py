import json
import time

import pytest

from kaiusap.adapters.daemon import Daemon, DaemonError, Deadline


class Connection:
    def __init__(self):
        self.sent = []
        self.closed = False

    def send(self, data):
        self.sent.append(json.loads(data))

    def recv(self, timeout):
        raise TimeoutError()

    def close(self):
        self.closed = True


def test_expired_request_transmits_nothing():
    daemon = Daemon("fake", "test", Deadline(at=time.monotonic() - 1), "test")
    daemon.connection = Connection()
    with pytest.raises(DaemonError) as caught:
        daemon.request("turn/start", {}, 10)
    assert caught.value.code == "timeout"
    assert daemon.connection.sent == []
    assert not caught.value.sent


@pytest.mark.parametrize("method", ["initialize", "thread/loaded/list", "thread/read", "turn/start"])
def test_request_timeout_keeps_classification_and_handover(method):
    daemon = Daemon("fake", "test", Deadline(1), "test")
    daemon.connection = Connection()
    with pytest.raises(DaemonError) as caught:
        daemon.request(method, {}, 5, protocol=True)
    assert caught.value.code == "timeout" and caught.value.sent


def test_socket_permission_is_sandboxed(monkeypatch):
    import websockets.sync.client
    def denied(*args, **kwargs):
        raise PermissionError("injected")
    monkeypatch.setattr(websockets.sync.client, "unix_connect", denied)
    daemon = Daemon("fake", "test", Deadline(1), "test")
    with pytest.raises(DaemonError) as caught:
        daemon.connect()
    assert caught.value.code == "sandboxed"


def test_blocked_frame_write_is_bounded():
    import threading
    class BlockedConnection(Connection):
        def __init__(self):
            super().__init__()
            self.interrupted = threading.Event()
            self.socket = self
        def shutdown(self, how):
            self.interrupted.set()
        def send(self, data):
            assert self.interrupted.wait(1), "frame transmission exceeded its deadline"
            raise OSError("interrupted write")
    daemon = Daemon("fake", "test", Deadline(2), "test")
    daemon.connection = BlockedConnection()
    began = time.monotonic()
    with pytest.raises(DaemonError) as caught:
        daemon.request("turn/start", {}, .03)
    assert caught.value.code == "timeout" and caught.value.sent
    assert time.monotonic() - began < .3


def test_proc_start_time_reads_stat_and_btime(tmp_path):
    """Field 22 of /proc/<pid>/stat in clock ticks after boot, plus btime, cut to the second."""
    import os
    from kaiusap.adapters.daemon import proc_start_time
    hz = os.sysconf("SC_CLK_TCK")
    (tmp_path / "42").mkdir()
    fields = ["S"] + ["0"] * 18 + [str(473304 * hz + hz // 3)] + ["0"] * 10
    (tmp_path / "42" / "stat").write_text("42 (claude (odd) name) " + " ".join(fields) + "\n")
    (tmp_path / "stat").write_text("cpu  1 2 3\nbtime 1793857262\nprocesses 9\n")
    assert proc_start_time(42, root=tmp_path) == "2026-11-10T17:09:26Z"
    assert proc_start_time(43, root=tmp_path) is None
    (tmp_path / "stat").write_text("cpu  1 2 3\n")
    assert proc_start_time(42, root=tmp_path) is None


def test_linux_start_time_only_with_the_real_runner(monkeypatch):
    import sys
    from kaiusap.adapters import daemon
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(daemon, "proc_start_time", lambda pid: "2026-11-10T17:09:26Z")
    assert daemon.linux_start_time(42, daemon.run_command) == "2026-11-10T17:09:26Z"
    assert daemon.linux_start_time(42, lambda *a: (0, "")) is None
    monkeypatch.setattr(sys, "platform", "darwin")
    assert daemon.linux_start_time(42, daemon.run_command) is None
