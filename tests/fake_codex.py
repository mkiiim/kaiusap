"""A fake Codex daemon (WebSocket JSON-RPC over a temporary Unix socket) and a fake `codex` executable."""
import json
import os
import stat
import threading

from websockets.sync.server import unix_serve


class FakeDaemon:
    """Scripted thread store: `threads` maps id -> thread/read answer; `loaded` lists loaded ids.

    `turn` decides turn/start: "ok" answers with a turn id, "refuse" answers a JSON-RPC
    error, "not_found" answers "thread not found", "hang" never answers, "drop" closes
    the connection, "malformed" answers without a turn id. `read` decides thread/read the
    same way: "ok" answers from `threads` (a thread's `status` defaults to idle), "error"
    answers a JSON-RPC error, "drop" closes the connection. `delays` maps a method to
    the seconds its answer is held back, for time-limit tests.
    """

    def __init__(self, path, threads=None, loaded=None, turn="ok", initialize="ok", page=50, delays=None, read="ok"):
        self.path, self.threads, self.loaded, self.turn, self.initialize, self.page = path, threads or {}, loaded or [], turn, initialize, page
        self.read = read
        self.delays = dict(delays or {})
        self.requests, self.names, self.turns, self.connections = [], {}, [], 0
        self.closed = threading.Event()
        self.active = set()
        self.condition = threading.Condition()
        self.server = unix_serve(self.handle, path)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def stop(self):
        self.closed.set()
        self.server.shutdown()
        with self.condition:
            connections = list(self.active)
        for connection in connections:
            connection.close()
        self.thread.join(timeout=1)

    def wait_idle(self):
        with self.condition:
            return self.condition.wait_for(lambda: not self.active, timeout=1)

    def answer(self, request):
        method, params = request.get("method"), request.get("params") or {}
        self.requests.append(request)
        if method == "initialize":
            if self.initialize == "error":
                return {"error": {"code": -32600, "message": "nope"}}
            if self.initialize == "malformed":
                return {"result": {"unexpected": True}}
            return {"result": {"userAgent": "codex-app-server/0.166.0", "codexHome": "/x", "platformFamily": "unix", "platformOs": "macos"}}
        if method == "thread/loaded/list":
            cursor = int(params.get("cursor") or 0)
            page = self.loaded[cursor:cursor + self.page]
            next_cursor = str(cursor + self.page) if cursor + self.page < len(self.loaded) else None
            return {"result": {"data": page, "nextCursor": next_cursor}}
        if method == "thread/read":
            if self.read == "error":
                return {"error": {"code": -32603, "message": "internal error"}}
            if self.read == "drop":
                return "drop"
            thread = self.threads.get(params.get("threadId"))
            if thread is None:
                return {"error": {"code": -32600, "message": "thread not loaded: " + str(params.get("threadId"))}}
            return {"result": {"thread": {"id": params["threadId"], "cwd": "/fixture", "name": None, "forkedFromId": None,
                                          "parentThreadId": None, "status": {"type": "idle"}, **thread}}}
        if method == "thread/name/set":
            if self.threads.get(params.get("threadId"), {}).get("archived"):
                return {"error": {"code": -32600, "message": "thread is archived"}}
            self.names[params["threadId"]] = params["name"]
            self.threads.setdefault(params["threadId"], {})["name"] = params["name"]
            return {"result": {}}
        if method == "turn/start":
            self.turns.append(params)
            if self.turn == "refuse":
                return {"error": {"code": -32600, "message": "turn refused: workspace switching"}}
            if self.turn == "not_found":
                return {"error": {"code": -32600, "message": "thread not found: " + params["threadId"]}}
            if self.turn == "hang":
                return "hang"
            if self.turn == "drop":
                return "drop"
            if self.turn == "malformed":
                return {"result": {"turn": {"status": "queued"}}}
            return {"result": {"turn": {"id": "turn-1", "items": [], "status": "inProgress"}}}
        return {"error": {"code": -32601, "message": "method not found"}}

    def handle(self, connection):
        with self.condition:
            self.connections += 1
            self.active.add(connection)
        try:
            for raw in connection:
                request = json.loads(raw)
                if "id" not in request:
                    self.requests.append(request)
                    continue
                # A broadcast server request with a colliding id, which kaiusap must ignore.
                connection.send(json.dumps({"id": request["id"], "method": "item/commandExecution/requestApproval", "params": {}}))
                connection.send(json.dumps({"method": "thread/status/changed", "params": {}}))
                answer = self.answer(request)
                if request.get("method") in self.delays:
                    self.closed.wait(self.delays[request["method"]])
                if answer == "hang":
                    self.closed.wait(5)
                    continue
                if answer == "drop":
                    connection.close()
                    return
                connection.send(json.dumps({"id": request["id"], **answer}))
        except Exception:
            pass
        finally:
            with self.condition:
                self.active.discard(connection)
                self.condition.notify_all()


def fake_codex(directory, socket_path, version="0.166.0", env_file=None, exit_code=0, output=None):
    """Write a fake `codex` executable into `directory` that prints `daemon version` JSON."""
    directory.mkdir(parents=True, exist_ok=True)
    script = directory / "codex"
    payload = output if output is not None else json.dumps({"status": "running", "backend": "pid", "managedCodexPath": "codex",
                                                              "managedCodexVersion": version, "socketPath": str(socket_path),
                                                              "cliVersion": version, "appServerVersion": version})
    lines = ["#!/bin/sh"]
    if env_file:
        lines.append(f"/usr/bin/env > {env_file}")
    lines.append(f"/bin/cat <<'JSON'\n{payload}\nJSON")
    lines.append(f"exit {exit_code}")
    script.write_text("\n".join(lines) + "\n")
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return script
