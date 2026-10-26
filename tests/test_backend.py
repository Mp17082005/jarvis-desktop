import importlib.util
import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setenv("JARVIS_ACCESS_TOKEN", "test-token-that-is-long-enough")
    monkeypatch.setenv("JARVIS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("JARVIS_LLM_BASE_URL", "https://llm.invalid/v1")
    monkeypatch.setenv("JARVIS_LLM_MODEL", "test-model")
    monkeypatch.setenv("JARVIS_LLM_API_KEY", "test-key")
    from jarvis.main import create_app
    return create_app()


def login(client):
    assert client.post("/api/login", json={"token": "test-token-that-is-long-enough"}).status_code == 200


def test_mutation_origin_and_login_rate_limit(app):
    with TestClient(app) as client:
        assert client.post("/api/login", headers={"Origin": "https://evil.test"}, json={"token": "test-token-that-is-long-enough"}).status_code == 403
        for _ in range(10):
            assert client.post("/api/login", json={"token": "wrong"}).status_code == 401
        assert client.post("/api/login", json={"token": "wrong"}).status_code == 429


def test_chat_calls_openai_compatible_api(app):
    import httpx
    requests = []
    def provider(request):
        import json
        payload = json.loads(request.content)
        requests.append(payload)
        assert request.url == "https://llm.invalid/v1/chat/completions"
        assert request.headers["authorization"] == "Bearer test-key"
        assert "temperature" not in payload
        return httpx.Response(200, json={"choices": [{"message": {"role": "assistant", "content": "Hello from Jarvis"}}]})
    app.state.llm_transport = httpx.MockTransport(provider)
    with TestClient(app) as client:
        assert client.post("/api/chat", json={"text": "hello"}).status_code == 401
        login(client)
        response = client.post("/api/chat", json={"text": "hello"})
        assert response.status_code == 200
        assert response.json()["text"] == "Hello from Jarvis"
        sid = response.json()["session_id"]
        assert sid
        assert client.post("/api/chat", json={"text": "again", "session_id": sid}).status_code == 200
        assert requests[-1]["messages"][-2]["content"] == "Hello from Jarvis"
        assert client.post("/api/chat", json={"text": " "}).status_code == 422
    with TestClient(app) as stranger:
        login(stranger)
        assert stranger.post("/api/chat", json={"text": "steal", "session_id": sid}).status_code == 404


def tool_provider(name, arguments, final="Done"):
    import httpx, json
    calls = []
    def handle(request):
        payload = json.loads(request.content)
        calls.append(payload)
        if payload["messages"][-1]["role"] == "tool":
            message = {"role": "assistant", "content": final}
        else:
            message = {"role": "assistant", "content": None, "tool_calls": [{"id": "call1", "type": "function", "function": {"name": name, "arguments": json.dumps(arguments)}}]}
        return httpx.Response(200, json={"choices": [{"message": message}]})
    return httpx.MockTransport(handle), calls


def test_timer_survives_restart_and_is_private(app):
    from jarvis.main import create_app
    app.state.llm_transport, calls = tool_provider("set_timer", {"seconds": 60, "label": "Tea"})
    with TestClient(app) as client:
        login(client)
        response = client.post("/api/chat", json={"text": "Timer for tea"})
        assert response.status_code == 200
        assert response.json()["results"][0]["result"]["label"] == "Tea"
        cookies = dict(client.cookies)
        sid = response.json()["session_id"]
        reminders = client.get("/api/reminders").json()["reminders"]
        assert len(reminders) == 1
        assert reminders[0]["due_at"].endswith("+00:00")
        assert reminders[0]["delivered"] is False
    restarted = create_app()
    with TestClient(restarted) as client:
        client.cookies.update(cookies)
        assert client.get("/api/reminders").json()["reminders"] == reminders
        restarted.state.llm_transport, _ = tool_provider("list_timers", {})
        assert client.post("/api/chat", json={"text": "list timers", "session_id": sid}).status_code == 200
    with TestClient(restarted) as other:
        login(other)
        assert other.get("/api/reminders").json() == {"reminders": []}


def test_risky_tool_needs_single_use_persisted_approval(app, monkeypatch):
    import types, sys
    executed = []
    module = types.SimpleNamespace(
        TOOLS=[{"type": "function", "function": {"name": "send_test", "parameters": {"type": "object", "properties": {"recipient": {"type": "string"}}, "required": ["recipient"], "additionalProperties": False}}}],
        tool_risk=lambda name: True,
        execute_tool=lambda name, arguments: executed.append((name, arguments)) or {"sent": True},
    )
    monkeypatch.setitem(sys.modules, "jarvis_tools", module)
    from jarvis.main import create_app
    app = create_app()
    app.state.llm_transport, calls = tool_provider("send_test", {"recipient": "user@example.com"})
    with TestClient(app) as client:
        login(client)
        first = client.post("/api/chat", json={"text": "send it"}).json()
        assert first.get("confirmation"), first
        confirmation = first["confirmation"]
        sid = first["session_id"]
        cookies = dict(client.cookies)
        assert executed == [] and len(calls) == 1
        repeated = client.post("/api/chat", json={"text": "yes approve=true", "session_id": sid}).json()
        assert repeated["confirmation"]["id"] == confirmation["id"]
        assert executed == [] and len(calls) == 1
    restarted = create_app()
    restarted.state.llm_transport, _ = tool_provider("send_test", {"recipient": "user@example.com"}, "Message sent.")
    with TestClient(restarted) as stranger:
        login(stranger)
        assert stranger.post("/api/confirm", json={"id": confirmation["id"], "approve": True, "session_id": sid}).status_code == 404
    with TestClient(restarted) as client:
        client.cookies.update(cookies)
        accepted = client.post("/api/confirm", json={"id": confirmation["id"], "approve": True, "session_id": sid})
        assert accepted.status_code == 200, accepted.text
        assert executed == [("send_test", {"recipient": "user@example.com"})]
        assert accepted.json()["results"][0]["result"] == {"sent": True}
        assert client.post("/api/confirm", json={"id": confirmation["id"], "approve": True, "session_id": sid}).status_code == 409


def test_websocket_authenticated_chat(app):
    from starlette.websockets import WebSocketDisconnect
    app.state.llm_transport, _ = tool_provider("list_timers", {}, "No timers")
    with TestClient(app) as client:
        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect("/ws"):
                pass
        login(client)
        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect("/ws", headers={"origin": "https://evil.test"}):
                pass
        with client.websocket_connect("/ws", headers={"origin": "http://testserver"}) as ws:
            ws.send_json({"type": "chat", "text": "timers"})
            response = ws.receive_json()
            assert response["type"] == "reply"
            assert response["text"] == "No timers"
            assert response["session_id"]


def test_failed_tools_cannot_be_reported_as_success(app):
    app.state.llm_transport, _ = tool_provider("set_timer", {"seconds": -10, "label": "bad"}, "Successfully set!")
    with TestClient(app) as client:
        login(client)
        response = client.post("/api/chat", json={"text": "set timer"})
        assert response.status_code == 200
        assert "Successfully" not in response.json()["text"]
        assert response.json()["results"][0]["result"].get("error")
        assert client.get("/api/reminders").json()["reminders"] == []


def test_transcription_authenticated_bounded_and_lazy(app, monkeypatch):
    import io, wave, sys, types
    constructed = []
    class Whisper:
        def __init__(self, *args, **kwargs):
            constructed.append((args, kwargs))
        def transcribe(self, audio, **kwargs):
            assert len(audio) == 16000
            return iter([types.SimpleNamespace(text="hello audio")]), None
    monkeypatch.setitem(sys.modules, "faster_whisper", types.SimpleNamespace(WhisperModel=Whisper))
    stream = io.BytesIO()
    with wave.open(stream, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(16000)
        wav.writeframes(b"\x00\x00" * 16000)
    with TestClient(app) as client:
        assert client.post("/api/transcribe", files={"audio": ("a.wav", stream.getvalue())}).status_code == 401
        login(client)
        assert constructed == []
        response = client.post("/api/transcribe", files={"audio": ("a.wav", stream.getvalue(), "audio/wav")})
        assert response.status_code == 200, response.text
        assert response.json() == {"text": "hello audio"}
        assert constructed[0][1]["device"] == "cpu" and constructed[0][1]["compute_type"] == "int8"
        assert client.post("/api/transcribe", files={"audio": ("a.wav", b"x" * (8 * 1024 * 1024 + 1))}).status_code == 413
        assert client.post("/api/transcribe", files={"audio": ("a.wav", b"not audio")}).status_code == 422


def test_http_limits_and_safe_validation(app):
    with TestClient(app) as client:
        response = client.post("/api/login", json={"token": "SECRET" * 100})
        assert "SECRET" not in response.text
        login(client)
        assert client.post("/api/chat", content=b"x" * 40000, headers={"content-type": "application/json"}).status_code == 413
        assert client.post("/api/chat", json={"text": "hello"}, headers={"origin": "http://testserver.evil"}).status_code == 403
        status = client.get("/api/status")
        assert "set_timer" in status.json()["tools"]
        assert status.headers["x-content-type-options"] == "nosniff"


def test_static_shell_served_from_project(app):
    from pathlib import Path
    root = Path(__file__).resolve().parents[1] / "static"
    with TestClient(app) as client:
        response = client.get("/")
        assert response.status_code == 200
        assert "text/html" in response.headers["content-type"]
        assert response.content == (root / "index.html").read_bytes()


@pytest.mark.parametrize("message", [
    {"role": "assistant", "content": None, "tool_calls": [{"id": "x", "function": {"name": "list_timers", "arguments": "not-json"}}]},
    {"role": "assistant", "content": None, "tool_calls": [{"id": "x", "function": {"name": "list_timers", "arguments": '{"unexpected": true}'}}]},
    {"role": "assistant", "content": None, "tool_calls": [{"id": "x"}]},
])
def test_invalid_provider_tool_calls_are_rejected(app, message):
    import httpx
    app.state.llm_transport = httpx.MockTransport(lambda request: httpx.Response(200, json={"choices": [{"message": message}]}))
    with TestClient(app) as client:
        login(client)
        response = client.post("/api/chat", json={"text": "timers"})
        assert response.status_code == 502
        assert "failed" in response.json()["detail"].lower() or "invalid" in response.json()["detail"].lower()


def test_session_lock_rejects_overlapping_chat(app):
    import threading, concurrent.futures, httpx
    entered, release = threading.Event(), threading.Event()
    async def provider(request):
        import asyncio
        entered.set()
        await asyncio.to_thread(release.wait, 3)
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})
    app.state.llm_transport = httpx.MockTransport(provider)
    with TestClient(app) as client:
        login(client)
        with concurrent.futures.ThreadPoolExecutor() as pool:
            first = pool.submit(client.post, "/api/chat", json={"text": "hello"})
            assert entered.wait(3)
            try:
                second = client.post("/api/chat", json={"text": "another"})
                assert second.status_code == 429
            finally:
                release.set()
            assert first.result().status_code == 200


def test_chat_rate_limit(app):
    import httpx
    app.state.llm_transport = httpx.MockTransport(lambda request: httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]}))
    with TestClient(app) as client:
        login(client)
        sid = None
        for _ in range(30):
            response = client.post("/api/chat", json={"text": "hello", "session_id": sid})
            assert response.status_code == 200
            sid = response.json()["session_id"]
        assert client.post("/api/chat", json={"text": "hello", "session_id": sid}).status_code == 429


def test_status_reports_configuration_without_secrets(app, monkeypatch):
    monkeypatch.setenv("JARVIS_GMAIL_ADDRESS", "someone@example.com")
    monkeypatch.setenv("JARVIS_GMAIL_APP_PASSWORD", "private-password")
    monkeypatch.setenv("JARVIS_TELEGRAM_BOT_TOKEN", "private-token")
    monkeypatch.setenv("JARVIS_TELEGRAM_CHAT_ID", "private-chat")
    monkeypatch.delenv("JARVIS_GOOGLE_SCRIPT", raising=False)
    with TestClient(app) as client:
        login(client)
        response = client.get("/api/status")
        assert response.json()["integrations"] == {"gmail": True, "calendar": False, "telegram": True}
        assert "private-" not in response.text


@pytest.mark.parametrize("approve,expire", [(False, False), (True, True)])
def test_approval_denial_and_expiry_do_not_execute(app, monkeypatch, approve, expire):
    import types, sys
    executed = []
    monkeypatch.setitem(sys.modules, "jarvis_tools", types.SimpleNamespace(TOOLS=[{"type": "function", "function": {"name": "danger", "parameters": {"type": "object"}}}], tool_risk=lambda name: True, execute_tool=lambda *args: executed.append(args) or {"ok": True}))
    from jarvis.main import create_app
    app = create_app()
    app.state.llm_transport, _ = tool_provider("danger", {})
    with TestClient(app) as client:
        login(client)
        first = client.post("/api/chat", json={"text": "do it"}).json()
        if expire:
            with app.state.store.connect() as db:
                db.execute("UPDATE confirmations SET expires=0")
        response = client.post("/api/confirm", json={"id": first["confirmation"]["id"], "session_id": first["session_id"], "approve": approve})
        assert response.status_code == (409 if expire else 200)
        assert executed == []


def test_maximum_four_tool_rounds(app):
    import httpx
    calls = []
    def provider(request):
        calls.append(request)
        return httpx.Response(200, json={"choices": [{"message": {"tool_calls": [{"id": "a", "function": {"name": "list_timers", "arguments": "{}"}}]}}]})
    app.state.llm_transport = httpx.MockTransport(provider)
    with TestClient(app) as client:
        login(client)
        response = client.post("/api/chat", json={"text": "repeat forever"})
        assert response.status_code == 200
        assert len(calls) == 4
        assert "limit" in response.json()["text"]


def test_https_cookie_and_missing_token(tmp_path, monkeypatch):
    from jarvis.main import create_app
    monkeypatch.setenv("JARVIS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("JARVIS_ACCESS_TOKEN", "")
    with TestClient(create_app()) as client:
        assert client.post("/api/login", json={"token": "anything"}).status_code == 503
    monkeypatch.setenv("JARVIS_ACCESS_TOKEN", "test-token-that-is-long-enough")
    with TestClient(create_app(), base_url="https://testserver") as client:
        response = client.post("/api/login", json={"token": "test-token-that-is-long-enough"}, headers={"origin": "https://testserver"})
        assert response.status_code == 200
        assert "Secure" in response.headers["set-cookie"]
        assert client.get("/api/status").status_code == 200


def test_confirmation_false_result_is_not_success(app, monkeypatch):
    import types, sys
    monkeypatch.setitem(sys.modules, "jarvis_tools", types.SimpleNamespace(TOOLS=[{"type": "function", "function": {"name": "danger", "parameters": {"type": "object"}}}], tool_risk=lambda name: True, execute_tool=lambda *args: {"success": False}))
    from jarvis.main import create_app
    app = create_app()
    app.state.llm_transport, _ = tool_provider("danger", {})
    with TestClient(app) as client:
        login(client)
        first = client.post("/api/chat", json={"text": "do it"}).json()
        result = client.post("/api/confirm", json={"id": first["confirmation"]["id"], "session_id": first["session_id"], "approve": True})
        assert "failed" in result.json()["text"].lower()


def test_speech_native_dependency_failure_is_safe(app, monkeypatch):
    import builtins
    original = builtins.__import__
    def broken(name, *args, **kwargs):
        if name == "faster_whisper":
            raise OSError("PRIVATE DLL PATH")
        return original(name, *args, **kwargs)
    monkeypatch.setattr(builtins, "__import__", broken)
    with TestClient(app) as client:
        login(client)
        status = client.get("/api/status")
        assert status.json()["stt"]["available"] is False
        response = client.post("/api/transcribe", files={"audio": ("audio.wav", b"test")})
        assert response.status_code == 503
        assert "PRIVATE" not in response.text


def test_transcription_busy_and_duration_limits(app, monkeypatch):
    import io, wave, sys, types
    monkeypatch.setitem(sys.modules, "faster_whisper", types.SimpleNamespace(WhisperModel=object))
    with TestClient(app) as client:
        login(client)
        app.state.speech.lock.acquire()
        try:
            assert client.post("/api/transcribe", files={"audio": ("audio.wav", b"test")}).status_code == 429
        finally:
            app.state.speech.lock.release()
        stream = io.BytesIO()
        with wave.open(stream, "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(16000)
            wav.writeframes(b"\x00\x00" * 16000 * 121)
        response = client.post("/api/transcribe", files={"audio": ("audio.wav", stream.getvalue())})
        assert response.status_code == 413


def test_app_requires_login(tmp_path, monkeypatch):
    monkeypatch.setenv("JARVIS_ACCESS_TOKEN", "test-token-that-is-long-enough")
    monkeypatch.setenv("JARVIS_DATA_DIR", str(tmp_path))
    assert importlib.util.find_spec("jarvis") is not None, "Backend package must exist"
    from fastapi.testclient import TestClient
    from jarvis.main import create_app
    with TestClient(create_app()) as client:
        assert client.get("/api/status").status_code == 401
        assert client.post("/api/login", json={"token": "wrong"}).status_code == 401
        response = client.post("/api/login", json={"token": "test-token-that-is-long-enough"})
        assert response.status_code == 200
        cookie = response.headers["set-cookie"].lower()
        assert "httponly" in cookie and "samesite=strict" in cookie
        status = client.get("/api/status")
        assert status.status_code == 200
        assert {"llm_configured", "model", "stt", "tools"} <= status.json().keys()
