"""OpenCode 2's adapter: the service client, against a fake service on localhost."""
import base64
import json
import os
import re
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from kaiusap.adapters.opencode2 import Service, ServiceError, message_id, registration_path
from kaiusap.store import Store

PASSWORD = "s3cret-not-for-logs"


class FakeService:
    """The service's HTTP API as kaiusap reads it: Basic auth, user `opencode`; a repeated prompt id 409."""

    def __init__(self):
        self.requests = []
        self.sessions = {"ses_a": {"id": "ses_a", "title": "New session", "parentID": None},
                         "ses_child": {"id": "ses_child", "title": "child", "parentID": "ses_a"}}
        self.admitted = set()
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def answer(self, status, body=None):
                data = b"" if body is None else json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def handle_any(self, method):
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length)) if length else None
                fake.requests.append((method, self.path, body, self.headers.get("Authorization")))
                expected = "Basic " + base64.b64encode(f"opencode:{PASSWORD}".encode()).decode()
                if self.headers.get("Authorization") != expected:
                    return self.answer(401, {"message": "Unauthorized"})
                path, _, query = self.path.partition("?")
                parts = path.split("/")
                if path == "/api/info":
                    return self.answer(200, {"version": "2.2.7", "pid": 4242})
                if path == "/api/session" and method == "GET":
                    rows = [s for s in fake.sessions.values() if "parentID=null" not in query or s["parentID"] is None]
                    return self.answer(200, {"data": rows, "cursor": {}})
                if len(parts) == 4 and parts[2] == "session":
                    session = fake.sessions.get(parts[3])
                    if session is None:
                        return self.answer(404, {"message": f"Session not found: {parts[3]}"})
                    if method == "GET":
                        return self.answer(200, {"data": session})
                    if method == "PATCH":
                        session.update(body)
                        return self.answer(204)
                if len(parts) == 5 and parts[4] == "prompt" and method == "POST":
                    if body["id"] in fake.admitted:
                        return self.answer(409, {"message": "conflict"})
                    fake.admitted.add(body["id"])
                    return self.answer(200, {"data": {"id": body["id"]}})
                if len(parts) == 5 and parts[4] == "context":
                    return self.answer(200, {"data": [{"role": "assistant"}]})
                return self.answer(404, {"message": "no route"})

            def do_GET(self):
                self.handle_any("GET")

            def do_POST(self):
                self.handle_any("POST")

            def do_PATCH(self):
                self.handle_any("PATCH")

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def fake():
    service = FakeService()
    yield service
    service.close()


def register(tmp_path, url, password=PASSWORD):
    path = tmp_path / "state" / "opencode" / "service.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"id": "svc", "version": "2.2.7", "url": url, "pid": 4242, "password": password}))
    os.chmod(path, 0o600)
    return {"XDG_STATE_HOME": str(tmp_path / "state"), "HOME": str(tmp_path)}


def test_the_registration_path_follows_xdg_state_home(tmp_path):
    assert registration_path({"XDG_STATE_HOME": "/s", "HOME": "/h"}) == "/s/opencode/service.json"
    assert registration_path({"HOME": "/h"}) == "/h/.local/state/opencode/service.json"


def test_the_registration_is_read_without_its_password(tmp_path, fake):
    service = Service(register(tmp_path, fake.url))
    assert service.registration() == {"id": "svc", "version": "2.2.7", "url": fake.url, "pid": 4242}
    assert PASSWORD not in repr(vars(service))


def test_requests_authenticate_and_use_the_api(tmp_path, fake):
    service = Service(register(tmp_path, fake.url))
    assert service.info() == {"version": "2.2.7", "pid": 4242}
    assert service.session("ses_a")["title"] == "New session"
    assert [s["id"] for s in service.sessions()] == ["ses_a"]  # roots only
    assert {s["id"] for s in service.sessions(roots=False)} == {"ses_a", "ses_child"}
    service.set_title("ses_a", "alpha")
    assert fake.sessions["ses_a"]["title"] == "alpha"
    assert service.context("ses_a") == [{"role": "assistant"}]


def test_a_prompt_carries_the_tag_and_a_repeat_is_a_conflict(tmp_path, fake):
    service = Service(register(tmp_path, fake.url))
    message = {"id": "20261112T145728Z-65zsb8", "created": "2026-11-12T14:57:28.123Z"}
    mid = message_id(message)
    service.prompt("ses_a", mid, "kaiusap: message from beta", message["id"])
    method, path, body, _ = fake.requests[-1]
    assert (method, path) == ("POST", "/api/session/ses_a/prompt")
    assert body == {"id": mid, "text": "kaiusap: message from beta", "metadata": {"kaiusap": message["id"]}, "delivery": "steer"}
    with pytest.raises(ServiceError) as repeat:
        service.prompt("ses_a", mid, "again", message["id"])
    assert (repeat.value.code, repeat.value.status) == ("conflict", 409)


def test_the_message_id_is_stable_and_has_opencode_s_form():
    message = {"id": "20261112T145728Z-65zsb8", "created": "2026-11-12T14:57:28.123Z"}
    mid = message_id(message)
    assert mid == message_id(dict(message)) and re.fullmatch(r"msg_[0-9a-f]{12}[0-9A-Za-z]{14}", mid)
    assert mid != message_id({**message, "id": "20261112T145728Z-zzzzzz"})
    # The creation time in ms, times 0x1000 plus a counter of 1, cut to 48 bits as OpenCode's `create` cuts it.
    assert int(mid[4:16], 16) == (1794495448123 * 0x1000 + 1) & 0xFFFFFFFFFFFF


def test_failures_map_to_reason_codes_and_never_carry_the_password(tmp_path, fake):
    with pytest.raises(ServiceError) as none:
        Service({"XDG_STATE_HOME": str(tmp_path / "nowhere")}).info()
    assert none.value.code == "connect_failed"
    with pytest.raises(ServiceError) as missing:
        Service(register(tmp_path, fake.url)).session("ses_gone")
    assert (missing.value.code, missing.value.status) == ("provider_refused", 404)
    assert str(missing.value) == "Session not found: ses_gone"
    with pytest.raises(ServiceError) as wrong:
        Service(register(tmp_path, fake.url, password="wrong")).info()
    assert (wrong.value.code, wrong.value.status) == ("provider_refused", 401)
    fake.close()
    with pytest.raises(ServiceError) as refused:
        Service(register(tmp_path, fake.url)).info()
    assert refused.value.code == "connect_failed"
    for error in (none.value, missing.value, wrong.value, refused.value):
        assert PASSWORD not in str(error) and PASSWORD not in repr(error.args)
        # No chained exception carries the request, whose headers hold the password.
        assert error.__cause__ is None and (error.__suppress_context__ or error.__context__ is None)


def test_the_adapter_imports_no_http_module_until_it_makes_a_request():
    # The prompt hook has 50 ms; urllib.request and its http/ssl imports cost about 17 ms, so the adapter
    # imports them only in a request (found in the live test, 2026-11-12).
    import subprocess
    import sys
    script = ("import sys, kaiusap.adapters.opencode2; "
              "print(sorted(m for m in ('urllib.request', 'http.client', 'ssl') if m in sys.modules))")
    out = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, check=True).stdout
    assert out.strip() == "[]"


def test_a_delivered_notice_records_its_mention(store, tmp_path):
    # The admitted notice is the mention (`reminded`), so the
    # native part can answer the notice's prompt alone; a repeat the service refuses (409) counts the same.
    import importlib.util
    from pathlib import Path
    from kaiusap.store import identifier, now
    path = Path(__file__).parent / "contract" / "opencode2.py"
    spec = importlib.util.spec_from_file_location("contract_fixture_opencode2_mention", path)
    fixture = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fixture)
    world = fixture.World(store, tmp_path)
    try:
        world.reachable()
        key = f"opencode2:{fixture.SESSION}"
        stamp = now()
        store.append("handles.jsonl", {"at": stamp, "id": "bind-1", "handle": "alpha", "reason": "iam", "provider": "opencode2",
                                       "session_id": fixture.SESSION, "session_key": key, "by_session": fixture.SESSION,
                                       "by_session_key": key, "pid": None, "process_started_at": None, "moved_from": None,
                                       "via": None, "also_released": None})
        message = {"id": identifier(stamp), "created": stamp, "reply_needed": False, "in_reply_to": None, "subject": "S",
                   "from": {"handle": "beta", "provider": "opencode2"},
                   "to": {"handle": "alpha", "session_id": fixture.SESSION, "session_key": key}}
        assert world.adapter.deliver(store, message, deadline=None)["outcome"] == "woken"
        reminded = [e for e in store.events() if e["event"] == "reminded"]
        assert [(e["message_id"], e["handle"], e["session_key"], e["binding"]) for e in reminded] == [(message["id"], "alpha", key, "bind-1")]
    finally:
        world.close()


def test_a_start_that_comes_with_a_notice_doesn_t_mention_its_message_again(tmp_path, monkeypatch):
    # A notice can be a session's first prompt since the plugin loaded (after a service restart, or
    # a directory unloaded when idle), so the start hook runs with it; the notice is the message's mention.
    import time
    from kaiusap.hooks import dispatch
    from kaiusap.notice import notice
    from kaiusap.store import Store, identifier, now
    monkeypatch.setenv("KAIUSAP_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "no-service"))
    store = Store(tmp_path / "home")
    session, key = "ses_resumed", "opencode2:ses_resumed"
    stamp = now()
    store.append("handles.jsonl", {"at": stamp, "id": "bind-1", "handle": "alpha", "reason": "iam", "provider": "opencode2",
                                   "session_id": session, "session_key": key, "by_session": session, "by_session_key": key,
                                   "pid": None, "process_started_at": None, "moved_from": None, "via": None, "also_released": None})
    message = {"schema": 1, "id": identifier(stamp), "created": stamp, "reply_needed": False, "in_reply_to": None,
               "chain_id": stamp, "depth": 0, "subject": "S", "fields": {"context": "c"},
               "from": {"handle": "beta", "session_id": "b", "session_key": "opencode2:b", "provider": "opencode2"},
               "to": {"handle": "alpha", "session_id": session, "session_key": key}}
    store.save_message(message)
    start = {"session_id": session, "cwd": "/w", "title": "alpha", "parent_id": None, "fork_of": None, "has_history": True}
    told = dispatch("opencode2", "session.start", {**start, "prompt": notice(message)}, time.monotonic() + 3)
    assert "unread message" not in json.dumps(told)
    assert [e["message_id"] for e in store.events() if e["event"] == "reminded"] == [message["id"]]


def test_a_start_with_an_ordinary_prompt_still_mentions_unread_mail(tmp_path, monkeypatch):
    import time
    from kaiusap.hooks import dispatch
    from kaiusap.store import Store, identifier, now
    monkeypatch.setenv("KAIUSAP_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "no-service"))
    store = Store(tmp_path / "home")
    session, key = "ses_resumed", "opencode2:ses_resumed"
    stamp = now()
    store.append("handles.jsonl", {"at": stamp, "id": "bind-1", "handle": "alpha", "reason": "iam", "provider": "opencode2",
                                   "session_id": session, "session_key": key, "by_session": session, "by_session_key": key,
                                   "pid": None, "process_started_at": None, "moved_from": None, "via": None, "also_released": None})
    store.save_message({"schema": 1, "id": identifier(stamp), "created": stamp, "reply_needed": False, "in_reply_to": None,
                        "chain_id": stamp, "depth": 0, "subject": "S", "fields": {"context": "c"},
                        "from": {"handle": "beta", "session_id": "b", "session_key": "opencode2:b", "provider": "opencode2"},
                        "to": {"handle": "alpha", "session_id": session, "session_key": key}})
    start = {"session_id": session, "cwd": "/w", "title": "alpha", "parent_id": None, "fork_of": None, "has_history": True,
             "prompt": "carry on"}
    told = dispatch("opencode2", "session.start", start, time.monotonic() + 3)
    assert "1 unread message, from beta" in json.dumps(told)


def test_kaiusap_outside_opencode_2_finds_its_service_from_the_terminal_record(store, tmp_path):
    # OpenCode 2 under its own XDG directories; a kaiusap elsewhere (a Claude
    # session, the CLI, the map) has another state directory, and finds the service from the terminal's record.
    import importlib.util
    from pathlib import Path
    from kaiusap.adapters.opencode2 import OpenCode2Adapter
    from kaiusap.store import identifier, now
    path = Path(__file__).parent / "contract" / "opencode2.py"
    spec = importlib.util.spec_from_file_location("contract_fixture_opencode2_service", path)
    fixture = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fixture)
    world = fixture.World(store, tmp_path)
    try:
        world.reachable()  # the terminal part's report, written in OpenCode 2's environment
        record = list(store.rows("sessions/opencode2/terminals.jsonl"))[-1]
        assert record["service"] == str(tmp_path / "opencode2-state" / "opencode" / "service.json")
        outside = OpenCode2Adapter(store, run=world.run, environ={"HOME": str(tmp_path / "elsewhere")}, parent_pid=1)
        key = f"opencode2:{fixture.SESSION}"
        assert outside.harness_version(key) == "2.2.7"
        stamp = now()
        message = {"id": identifier(stamp), "created": stamp, "reply_needed": False, "in_reply_to": None, "subject": "S",
                   "from": {"handle": "beta", "provider": "claude"},
                   "to": {"handle": "alpha", "session_id": fixture.SESSION, "session_key": key}}
        assert outside.deliver(store, message)["outcome"] == "woken"
        # With no terminal record naming one, the caller's own state directory, as before: here, nothing.
        bare = OpenCode2Adapter(Store(tmp_path / "other-store"), run=world.run, environ={"HOME": str(tmp_path / "elsewhere")})
        assert bare.service_for(fixture.SESSION) is bare.service
    finally:
        world.close()


@pytest.mark.parametrize("settings, first", [(None, "opencode"), ({"opencode2_command": "opencode2"}, "opencode2"),
                                             ({"opencode2_command": " "}, "opencode")])
def test_the_resume_command_starts_with_the_opencode2_command_setting(store, tmp_path, settings, first):
    # `sessions`: OpenCode 2 beside 1.x runs under another name; a blank value is malformed.
    import importlib.util
    import shlex
    from pathlib import Path
    path = Path(__file__).parent / "contract" / "opencode2.py"
    spec = importlib.util.spec_from_file_location("contract_fixture_opencode2_resume", path)
    fixture = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fixture)
    world = fixture.World(store, tmp_path)
    try:
        world.reachable()
        if settings is not None:
            (store.root / "settings.json").write_text(json.dumps(settings))
        row = world.adapter.where(f"opencode2:{fixture.SESSION}")
        assert row["command"].endswith(f"{first} --session {shlex.quote(fixture.SESSION)}")
    finally:
        world.close()


# ----- following: a fresh start leaves the handle; a copy the user is moved into takes it -----------------


def following_world(store, tmp_path, label):
    import importlib.util
    from pathlib import Path
    path = Path(__file__).parent / "contract" / "opencode2.py"
    spec = importlib.util.spec_from_file_location(f"contract_fixture_opencode2_{label}", path)
    fixture = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fixture)
    return fixture, fixture.World(store, tmp_path)


def hold(store, handle, session):
    from kaiusap.store import identifier, now
    at = now()
    store.append("handles.jsonl", {"at": at, "id": identifier(at), "handle": handle, "reason": "iam", "session_id": session,
                                   "session_key": f"opencode2:{session}", "provider": "opencode2", "moved_from": None,
                                   "also_released": None})


def report(world, fixture, shows):
    world.adapter.events({**fixture.hook_input("tui.route", shows=shows), "hook_event_name": "tui.route"})


def begin(world, fixture, **extra):
    import time
    from kaiusap.adapters import Providers
    from kaiusap.following import follow
    deadline = time.monotonic() + 5
    payload = {**fixture.hook_input("session.start", **extra), "hook_event_name": "session.start"}
    event = world.adapter.events(payload, deadline=deadline)
    world.adapter.hook_record("session.start", event, deadline)
    return event, follow(world.adapter.store, Providers({"opencode2": world.adapter}, world.adapter.store), event, deadline)


def test_new_leaves_the_handle_and_tells_titles_and_offers_the_new_session(store, tmp_path):
    from kaiusap.model import handles, open_offers
    fixture, world = following_world(store, tmp_path, "fresh")
    try:
        world.service.serve(None)
        hold(store, "alpha", fixture.OTHER)
        for shows in (fixture.OTHER, None, fixture.SESSION):
            report(world, fixture, shows)
        event, result = begin(world, fixture)
        assert event["kind"] == "clear"
        [text] = [t for t in result["texts"] if t.startswith("This is a new conversation.")]
        assert f"alpha stays with the conversation left in this terminal, session {fixture.OTHER}." in text
        assert text.endswith("Tell the user this once, in your first reply.")
        assert result["moved"] is False and handles(store)["alpha"]["session_key"] == f"opencode2:{fixture.OTHER}"
        assert [o["offered"] for o in open_offers(store, session_key=f"opencode2:{fixture.SESSION}")] == ["alpha"]
        assert world.service.title.startswith("alpha~")
        assert world.adapter.show("line") == {}
    finally:
        world.close()


def test_a_fork_the_terminal_switches_to_takes_the_handle(store, tmp_path):
    from kaiusap.model import handles
    fixture, world = following_world(store, tmp_path, "fork_moved")
    try:
        world.service.serve(None)
        hold(store, "alpha", fixture.OTHER)
        report(world, fixture, fixture.OTHER)
        report(world, fixture, fixture.SESSION)
        event, result = begin(world, fixture, fork_of=fixture.OTHER)
        assert event["kind"] == "fork" and result["moved"] is True
        line = handles(store)["alpha"]
        assert (line["session_key"], line["reason"]) == (f"opencode2:{fixture.SESSION}", "copy")
        assert world.service.title == "alpha"
    finally:
        world.close()


def test_a_fork_no_terminal_switched_to_is_offered_the_handle(store, tmp_path):
    from kaiusap.model import handles, open_offers
    fixture, world = following_world(store, tmp_path, "fork_offered")
    try:
        world.service.serve(None)
        hold(store, "alpha", fixture.OTHER)
        report(world, fixture, fixture.OTHER)  # the terminal still shows the original
        event, result = begin(world, fixture, fork_of=fixture.OTHER)
        assert result["moved"] is False and handles(store)["alpha"]["session_key"] == f"opencode2:{fixture.OTHER}"
        assert ("This conversation is a copy of alpha's. It has no handle. To carry on as alpha here, the user says "
                "'you are alpha'. Tell the user this once, in your first reply.") in result["texts"]
        assert [o["offered"] for o in open_offers(store, session_key=f"opencode2:{fixture.SESSION}")] == ["alpha"]
    finally:
        world.close()
