"""Desktop boundary tests never operate a physical desktop or microphone."""
import json
import threading
import time

import httpx
import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

TOKEN = "desktop-test-token-not-a-real-secret"


def make_app(monkeypatch, tmp_path, *, enabled=True):
    monkeypatch.setenv("JARVIS_ACCESS_TOKEN", TOKEN)
    monkeypatch.setenv("JARVIS_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("JARVIS_WORKSPACE_DIR", str(tmp_path / "workspace"))
    monkeypatch.setenv("JARVIS_DESKTOP_ENABLED", str(enabled).lower())
    monkeypatch.setenv("JARVIS_DESKTOP_LOCAL_ONLY", "true")
    monkeypatch.setenv("JARVIS_LLM_BASE_URL", "https://llm.invalid/v1")
    monkeypatch.setenv("JARVIS_LLM_MODEL", "test")
    from jarvis.main import create_app
    return create_app()


def login(client):
    assert client.post("/api/login", json={"token": TOKEN}).status_code == 200


@pytest.mark.parametrize("peer,host,origin", [
    ("203.0.113.10", "127.0.0.1", None),
    ("127.0.0.1", "evil.example", None),
    ("127.0.0.1", "localhost.evil.example", None),
    ("127.0.0.1", "127.0.0.1", "https://evil.example"),
    ("127.0.0.1", "127.0.0.1", "http://localhost"),
])
def test_desktop_rejects_remote_or_spoofed_all_http(monkeypatch, tmp_path, peer, host, origin):
    app = make_app(monkeypatch, tmp_path)
    headers = {"host": host, "authorization": "Bearer " + TOKEN}
    if origin:
        headers["origin"] = origin
    with TestClient(app, base_url="http://127.0.0.1", client=(peer, 45000)) as client:
        for path in ("/", "/api/status", "/api/desktop/status"):
            assert client.get(path, headers=headers).status_code == 403
        assert client.post("/api/login", headers=headers, json={"token": TOKEN}).status_code == 403


@pytest.mark.parametrize("base,peer", [("http://localhost", "127.0.0.1"), ("http://127.0.0.1", "127.0.0.1"), ("http://[::1]", "::1")])
def test_local_allowed_but_authenticated(monkeypatch, tmp_path, base, peer):
    app = make_app(monkeypatch, tmp_path)
    with TestClient(app, base_url=base, client=(peer, 40000)) as client:
        assert client.get("/api/status").status_code == 401
        login(client)
        assert client.get("/api/status").status_code == 200
        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect(base.replace("http", "ws") + "/ws", headers={"origin": "https://evil.example"}):
                pass
        with client.websocket_connect(base.replace("http", "ws") + "/ws", headers={"origin": base}):
            pass


def test_public_hides_desktop_and_existing_launch_tools(monkeypatch, tmp_path):
    app = make_app(monkeypatch, tmp_path, enabled=False)
    with TestClient(app) as client:
        login(client)
        names = client.get("/api/status").json()["tools"]
        assert "open_app" not in names
        assert not any(name.startswith("desktop_") for name in names)
        assert client.get("/api/desktop/status").status_code == 404
        assert client.post("/api/desktop/wake-control", json={"enabled": True}).status_code in (404, 405)


class FakeEngine:
    def __init__(self, workspace):
        self.thread = threading.get_ident()
        self.calls = []
        self.closed = False

    def capabilities(self):
        return {"scope": "test_machine", "apps": {"notepad": {"available": True}}}

    def execute(self, name, args):
        assert self.thread == threading.get_ident()
        self.calls.append((name, args))
        return {"ok": True, "executed": name, "arguments": args}

    def close(self):
        assert self.thread == threading.get_ident()
        self.closed = True


class FakeQueue:
    def __init__(self, db_path, engine):
        self.engine, self.jobs, self.keys = engine, {}, {}
        self.lock = threading.RLock()

    def submit(self, action, args, owner, idempotency_key, depends_on=None):
        with self.lock:
            key = (owner, idempotency_key)
            if key in self.keys:
                job = self.jobs[self.keys[key]]
                if (job["action"], job["args"]) != (action, args):
                    raise ValueError("idempotency conflict")
                return dict(job)
            ident = str(len(self.jobs) + 1)
            job = {"id": ident, "owner": owner, "action": action, "args": args, "status": "queued"}
            self.jobs[ident] = job
            self.keys[key] = ident
            return dict(job)

    def get(self, ident, owner):
        with self.lock:
            job = self.jobs.get(ident)
            return dict(job) if job and job["owner"] == owner else None

    def run_next(self):
        with self.lock:
            job = next((j for j in self.jobs.values() if j["status"] == "queued"), None)
            if not job:
                return None
            job["status"] = "running"
        result = self.engine.execute(job["action"], job["args"])
        with self.lock:
            job.update(status="succeeded", result=result)
            return dict(job)


@pytest.fixture
def desktop_backend(monkeypatch):
    import sys
    import types
    schemas = [{"type": "function", "function": {"name": name, "description": name,
        "parameters": {"type": "object", "properties": {"value": {"type": "string"}}, "required": ["value"], "additionalProperties": False}}}
        for name in ("open_app", "create_workbook")]
    monkeypatch.setitem(sys.modules, "companion.actions", types.SimpleNamespace(
        ACTIONS=schemas, ActionEngine=FakeEngine, action_risk=lambda name: name != "open_app"))
    monkeypatch.setitem(sys.modules, "companion.queue", types.SimpleNamespace(JobQueue=FakeQueue))


def provider(name, args):
    def handle(request):
        payload = json.loads(request.content)
        if payload["messages"][-1]["role"] == "tool" or not payload.get("tools"):
            message = {"role": "assistant", "content": "Result received"}
        else:
            message = {"role": "assistant", "content": None, "tool_calls": [{"id": "stable-call-1", "type": "function",
                "function": {"name": name, "arguments": json.dumps(args)}}]}
        return httpx.Response(200, json={"choices": [{"message": message}]})
    return httpx.MockTransport(handle)


def test_desktop_low_risk_tool_single_thread_dedup_and_job_scope(monkeypatch, tmp_path, desktop_backend):
    app = make_app(monkeypatch, tmp_path)
    app.state.llm_transport = provider("desktop_action", {"action": "open_app", "arguments": {"value": "notepad"}})
    with TestClient(app, base_url="http://127.0.0.1", client=("127.0.0.1", 40000)) as client:
        login(client)
        status = client.get("/api/desktop/status")
        assert status.status_code == 200, status.text
        assert status.json()["enabled"] is True
        assert "desktop_action" in client.get("/api/status").json()["tools"]
        first = client.post("/api/chat", json={"text": "open notepad"}).json()
        assert "confirmation" not in first
        result = first["results"][0]["result"]
        assert result["ok"] is True, result
        job_id = result["job_id"]
        repeat = client.post("/api/chat", json={"text": "same provider call retry", "session_id": first["session_id"]}).json()
        assert repeat["results"][0]["result"]["job_id"] == job_id
        assert len(app.state.desktop.engine.calls) == 1
        assert client.get("/api/desktop/jobs/" + job_id).json()["status"] == "succeeded"
        client.cookies.clear()
        login(client)
        assert client.get("/api/desktop/jobs/" + job_id).status_code == 404
    assert app.state.desktop.engine.closed


@pytest.mark.parametrize("action", ["shell", "delete", "send_message", "keyboard", "mouse"])
def test_unknown_actions_denied_without_execution(monkeypatch, tmp_path, desktop_backend, action):
    app = make_app(monkeypatch, tmp_path)
    app.state.llm_transport = provider("desktop_action", {"action": action, "arguments": {}})
    with TestClient(app, base_url="http://127.0.0.1", client=("127.0.0.1", 40000)) as client:
        login(client)
        reply = client.post("/api/chat", json={"text": "do action"}).json()
        assert "confirmation" not in reply
        assert reply["results"][0]["result"]["ok"] is False
        assert app.state.desktop.engine.calls == []


def test_workbook_approval_persists_exact_arguments_button_only(monkeypatch, tmp_path, desktop_backend):
    app = make_app(monkeypatch, tmp_path)
    args = {"action": "create_workbook", "arguments": {"value": "exact.xlsx"}}
    app.state.llm_transport = provider("desktop_action", args)
    with TestClient(app, base_url="http://127.0.0.1", client=("127.0.0.1", 40000)) as client:
        login(client)
        reply = client.post("/api/chat", json={"text": "create workbook"}).json()
        pending = reply["confirmation"]
        assert pending["arguments"] == args
        assert app.state.desktop.engine.calls == []
        body = {"id": pending["id"], "approve": True, "session_id": reply["session_id"]}
        assert client.post("/api/confirm", json={**body, "voice_phrase": pending["voice_phrase"]}).status_code == 403
        result = client.post("/api/confirm", json=body)
        assert result.status_code == 200, result.text
        assert result.json()["results"][0]["result"]["ok"] is True
        assert app.state.desktop.engine.calls == [("create_workbook", {"value": "exact.xlsx"})]
        assert any(json.loads(key[1]) == [reply["session_id"], "stable-call-1"] for key in app.state.desktop.queue.keys)
        assert client.post("/api/confirm", json=body).status_code == 409


def test_real_durable_queue_contract_and_reopen(monkeypatch, tmp_path, desktop_backend):
    import sys
    sys.modules.pop("companion.queue", None)
    import asyncio
    app = make_app(monkeypatch, tmp_path)
    bridge = app.state.desktop
    async def check():
        await bridge.start()
        try:
            args = {"action": "open_app", "arguments": {"value": "notepad"}}
            first = await bridge.execute("alice", "desktop_action", args, "session", "call", timeout=.5)
            assert first.get("ok") is True, first
            assert bridge.get(first["job_id"], "bob") is None
            from companion.queue import JobQueue
            reopened = JobQueue(tmp_path / "data" / "desktop-jobs.sqlite3", bridge.engine)
            assert reopened.get(first["job_id"], "alice")["state"] == "succeeded"
            again = await bridge.execute("alice", "desktop_action", args, "session", "call", timeout=.5)
            assert again["job_id"] == first["job_id"]
            assert len(bridge.engine.calls) == 1
        finally:
            await bridge.close()
    asyncio.run(check())


class FakeWake:
    instances = []

    def __init__(self, model_path=None):
        self.listening = False
        self.starts = 0
        self.events = []
        self.instances.append(self)

    def status(self):
        return {"available": True, "listening": self.listening, "reason": "ready"}

    def start(self):
        self.starts += 1
        self.listening = True
        return True

    def stop(self):
        self.listening = False
        self.events.clear()

    def pop_event(self):
        return self.events.pop(0) if self.events else None


def test_wake_requires_explicit_consent_and_owner_polling(monkeypatch, tmp_path, desktop_backend):
    import sys
    import types
    monkeypatch.setitem(sys.modules, "companion.launcher", types.SimpleNamespace(WakeController=FakeWake))
    app = make_app(monkeypatch, tmp_path)
    with TestClient(app, base_url="http://127.0.0.1", client=("127.0.0.1", 40000)) as client:
        assert client.get("/api/desktop/wake").status_code == 401
        login(client)
        status = client.get("/api/desktop/status").json()
        assert status["wake_available"] is True
        wake = FakeWake.instances[-1]
        assert wake.starts == 0
        assert client.get("/api/desktop/wake").json()["event"] is None
        assert wake.starts == 0
        assert client.post("/api/desktop/wake-control", json={"enabled": "true"}).status_code == 422
        enabled = client.post("/api/desktop/wake-control", json={"enabled": True})
        assert enabled.status_code == 200, enabled.text
        assert enabled.json()["wake_enabled"] is True
        assert wake.starts == 1
        wake.events.append({"wake": "jarvis"})
        cookies = dict(client.cookies)
        client.cookies.clear()
        login(client)
        assert client.get("/api/desktop/wake").json()["event"] is None
        assert client.post("/api/desktop/wake-control", json={"enabled": True}).status_code == 409
        client.cookies.clear()
        client.cookies.update(cookies)
        assert client.get("/api/desktop/wake").json()["event"] == {"wake": "jarvis"}
        assert client.post("/api/desktop/wake-control", json={"enabled": False}).json()["wake_enabled"] is False
        assert wake.listening is False
        assert client.post("/api/desktop/wake-control", json={"enabled": True}).status_code == 200
    assert wake.listening is False


def test_disabled_desktop_never_imports_companion(monkeypatch, tmp_path):
    import jarvis.desktop as module
    original = module.importlib.import_module
    def guarded(name, *args, **kwargs):
        assert not name.startswith("companion")
        return original(name, *args, **kwargs)
    monkeypatch.setattr(module.importlib, "import_module", guarded)
    app = make_app(monkeypatch, tmp_path, enabled=False)
    assert app.state.desktop.schemas == []


def test_wake_lease_expires_without_authenticated_poll(monkeypatch, tmp_path, desktop_backend):
    import sys
    import types
    monkeypatch.setitem(sys.modules, "companion.launcher", types.SimpleNamespace(WakeController=FakeWake))
    app = make_app(monkeypatch, tmp_path)
    with TestClient(app, base_url="http://127.0.0.1", client=("127.0.0.1", 40000)) as client:
        login(client)
        assert client.post("/api/desktop/wake-control", json={"enabled": True}).status_code == 200
        bridge = app.state.desktop
        bridge._wake_heartbeat = time.monotonic() - 60
        deadline = time.monotonic() + 3
        while bridge.wake.listening and time.monotonic() < deadline:
            time.sleep(.05)
        assert bridge.wake.listening is False
        assert client.get("/api/desktop/wake").json()["event"] is None
        assert bridge.wake.starts == 1


def test_missing_local_only_flag_fails_closed(monkeypatch, tmp_path, desktop_backend):
    make_app(monkeypatch, tmp_path)
    monkeypatch.delenv("JARVIS_DESKTOP_LOCAL_ONLY")
    from jarvis.main import create_app
    app = create_app()
    with TestClient(app, base_url="http://127.0.0.1", client=("127.0.0.1", 40000)) as client:
        login(client)
        assert client.get("/api/desktop/status").json()["enabled"] is False
        assert "desktop_action" not in client.get("/api/status").json()["tools"]
        assert client.post("/api/desktop/wake-control", json={"enabled": True}).status_code == 503


def test_direct_remote_websocket_denied_even_valid_cookie(monkeypatch, tmp_path, desktop_backend):
    app = make_app(monkeypatch, tmp_path)
    with TestClient(app, base_url="http://127.0.0.1", client=("127.0.0.1", 40000)) as client:
        login(client)
        cookies = dict(client.cookies)
        remote = TestClient(app, base_url="http://127.0.0.1", client=("203.0.113.1", 45000))
        remote.cookies.update(cookies)
        with pytest.raises(WebSocketDisconnect):
            with remote.websocket_connect("ws://127.0.0.1/ws", headers={"origin": "http://127.0.0.1"}):
                pass


def test_real_workbook_bridge_and_persisted_approval(monkeypatch, tmp_path):
    app = make_app(monkeypatch, tmp_path)
    args = {"action": "create_workbook", "arguments": {"path": "bridge.xlsx", "sheet": "Data", "rows": [["name", "count"], ["test", 3]]}}
    app.state.llm_transport = provider("desktop_action", args)
    with TestClient(app, base_url="http://127.0.0.1", client=("127.0.0.1", 40000)) as client:
        login(client)
        reply = client.post("/api/chat", json={"text": "create workbook"}).json()
        pending = reply["confirmation"]
        assert pending["arguments"] == args
        assert "button" in pending["spoken_summary"].lower()
        assert not (tmp_path / "workspace" / "bridge.xlsx").exists()
        cookies = dict(client.cookies)
    restarted = make_app(monkeypatch, tmp_path)
    restarted.state.llm_transport = provider("desktop_capabilities", {})
    with TestClient(restarted, base_url="http://127.0.0.1", client=("127.0.0.1", 40000)) as client:
        client.cookies.update(cookies)
        response = client.post("/api/confirm", json={"id": pending["id"], "approve": True, "session_id": reply["session_id"]})
        assert response.status_code == 200, response.text
        assert response.json()["results"][0]["result"]["ok"] is True, response.text
        from openpyxl import load_workbook
        book = load_workbook(tmp_path / "workspace" / "bridge.xlsx")
        assert list(book["Data"].values) == [("name", "count"), ("test", 3)]
        book.close()


@pytest.mark.parametrize("headers", [
    {"x-forwarded-for": "127.0.0.1"}, {"x-forwarded-host": "localhost"}, {"forwarded": "for=127.0.0.1"},
    {"host": "localhost@evil.example"}, {"host": "localhost:bad"}, {"origin": "null"},
    [("host", "localhost"), ("host", "evil.example")], {"sec-fetch-site": "cross-site"},
])
def test_forwarded_and_malformed_local_authorities_denied(monkeypatch, tmp_path, headers):
    app = make_app(monkeypatch, tmp_path)
    with TestClient(app, base_url="http://127.0.0.1", client=("127.0.0.1", 40000)) as client:
        assert client.get("/", headers=headers).status_code == 403


def test_bounded_wait_returns_job_without_replay(monkeypatch, tmp_path, desktop_backend):
    import asyncio
    app = make_app(monkeypatch, tmp_path)
    bridge = app.state.desktop
    class SlowEngine(FakeEngine):
        def execute(self, name, args):
            time.sleep(.15)
            return super().execute(name, args)
    bridge.engine_factory = SlowEngine
    async def check():
        await bridge.start()
        try:
            args = {"action": "open_app", "arguments": {"value": "notepad"}}
            first = await bridge.execute("alice", "desktop_action", args, "sid", "call", timeout=0)
            assert first["pending"] is True
            second = await bridge.execute("alice", "desktop_action", args, "sid", "call", timeout=1)
            assert second["job_id"] == first["job_id"]
            assert second["ok"] is True
            assert len(bridge.engine.calls) == 1
            conflict = await bridge.execute("alice", "desktop_action", {"action": "open_app", "arguments": {"value": "different"}}, "sid", "call", timeout=0)
            assert conflict["ok"] is False
        finally:
            await bridge.close()
    asyncio.run(check())
