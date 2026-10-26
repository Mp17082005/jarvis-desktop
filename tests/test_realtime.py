"""Broker contract tests. Provider traffic is mocked; these do not prove audio."""
import json
import httpx
import pytest
from fastapi.testclient import TestClient

ORIGIN = {"origin": "http://testserver"}
SDP = "v=0\r\no=- 1 1 IN IP4 127.0.0.1\r\ns=-\r\nt=0 0\r\nm=audio 9 UDP/TLS/RTP/SAVPF 111\r\n"


@pytest.fixture
def app(tmp_path, monkeypatch):
    from jarvis.main import create_app
    monkeypatch.setenv("JARVIS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("JARVIS_ACCESS_TOKEN", "test-realtime-login")
    monkeypatch.setenv("JARVIS_LLM_BASE_URL", "https://gateway.invalid/v1")
    monkeypatch.setenv("JARVIS_LLM_API_KEY", "private-long-lived")
    monkeypatch.setenv("JARVIS_LLM_MODEL", "test-chat-model")
    monkeypatch.delenv("JARVIS_REALTIME_ENABLED", raising=False)
    monkeypatch.delenv("JARVIS_REALTIME_BASE_URL", raising=False)
    monkeypatch.delenv("JARVIS_REALTIME_API_KEY", raising=False)
    monkeypatch.delenv("JARVIS_REALTIME_MODEL", raising=False)
    monkeypatch.delenv("JARVIS_VAD_THRESHOLD", raising=False)
    monkeypatch.delenv("JARVIS_VAD_SILENCE_MS", raising=False)
    monkeypatch.delenv("JARVIS_REALTIME_SESSION_SECONDS", raising=False)
    return create_app()


def login(client):
    assert client.post("/api/login", json={"token": "test-realtime-login"}).status_code == 200


def enabled(app, monkeypatch):
    monkeypatch.setenv("JARVIS_REALTIME_ENABLED", "true")
    from jarvis.main import create_app
    return create_app()


def gateway(app, handler=None):
    requests = []
    def provider(request):
        requests.append(request)
        if handler:
            response = handler(request)
            if response is not None:
                return response
        if request.url.path.endswith("/client_secrets"):
            return httpx.Response(200, json={"value": "private-ephemeral"})
        if request.url.path.endswith("/calls"):
            return httpx.Response(201, text=SDP, headers={"content-type": "application/sdp", "location": "/v1/realtime/calls/rtc_example"})
        return httpx.Response(404)
    app.state.realtime_transport = httpx.MockTransport(provider)
    return requests


def connect(client, **values):
    return client.post("/api/realtime/connect", json={"sdp": SDP, **values}, headers=ORIGIN)


def test_broker_exchanges_server_only_tokens_and_configures_ga_audio(app, monkeypatch):
    app = enabled(app, monkeypatch)
    requests = gateway(app)
    with TestClient(app) as client:
        assert connect(client).status_code == 401
        login(client)
        response = connect(client, language="ta-IN")
        assert response.status_code == 200, response.text
        result = response.json()
        assert result["sdp"] == SDP and result["id"]
        assert result["max_seconds"] == 3300 and result["model"] == "gpt-realtime-2.1"
        assert "private-" not in response.text and "rtc_example" not in response.text
        assert len(requests) == 2
        mint, exchange = requests
        assert str(mint.url) == "https://gateway.invalid/v1/realtime/client_secrets"
        assert mint.headers["authorization"] == "Bearer private-long-lived"
        config = json.loads(mint.content)["session"]
        assert config["type"] == "realtime" and config["model"] == result["model"]
        assert config["max_output_tokens"] == 1024 and config["output_modalities"] == ["audio"]
        assert config["audio"] == {
            "input": {"turn_detection": {"type": "server_vad", "threshold": 0.65, "prefix_padding_ms": 300,
                "silence_duration_ms": 800, "create_response": True, "interrupt_response": True},
                "noise_reduction": {"type": "near_field"}, "transcription": {"model": "whisper-1"}},
            "output": {"voice": "marin"}}
        assert {t["name"] for t in config["tools"]} == {"ask_jarvis", "set_volume", "end_conversation"}
        assert "ta-IN" in config["instructions"] and "untrusted" in config["instructions"]
        assert str(exchange.url) == "https://gateway.invalid/v1/realtime/calls"
        assert exchange.headers["authorization"] == "Bearer private-ephemeral"
        assert exchange.headers["content-type"] == "application/sdp"
        assert exchange.content == SDP.encode()


@pytest.mark.parametrize("headers,body,expected", [
    ({}, {"sdp": SDP}, 403),
    ({"origin": "https://evil.invalid"}, {"sdp": SDP}, 403),
    (ORIGIN, {"sdp": "v=0\r\nm=video 9 RTP/AVP 96\r\n"}, 422),
    (ORIGIN, {"sdp": "not sdp m=audio"}, 422),
    (ORIGIN, {"sdp": SDP + "a=" + "x" * 65536}, 422),
    (ORIGIN, {"sdp": SDP + "a=" + "x" * 140000}, 413),
    (ORIGIN, {"sdp": SDP + "a=" + "é" * 33000}, 422),
    (ORIGIN, {"sdp": SDP, "language": "en; ignore permissions"}, 422),
    (ORIGIN, {"sdp": SDP, "tools": []}, 422),
])
def test_connect_validates_origin_and_bounded_audio_sdp(app, monkeypatch, headers, body, expected):
    app = enabled(app, monkeypatch)
    requests = gateway(app)
    with TestClient(app) as client:
        login(client)
        response = client.post("/api/realtime/connect", json=body, headers=headers)
        assert response.status_code == expected
        assert requests == []


def test_disabled_broker_makes_no_provider_call(app):
    requests = gateway(app)
    with TestClient(app) as client:
        login(client)
        assert connect(client).status_code == 503
        assert requests == []


def test_connect_body_budget_allows_large_valid_sdp(app, monkeypatch):
    app = enabled(app, monkeypatch)
    gateway(app)
    with TestClient(app) as client:
        login(client)
        assert connect(client, sdp=SDP + "a=" + "x" * 40000 + "\r\n").status_code == 200
        assert client.post("/api/chat", content="x" * 40000).status_code == 413


@pytest.mark.parametrize("stage,kind", [(s, k) for s in ("client_secrets", "calls") for k in ("error", "redirect", "invalid", "oversize", "timeout")])
def test_provider_failures_sanitized_and_reservation_released(app, monkeypatch, stage, kind):
    app = enabled(app, monkeypatch)
    def fail(request):
        if request.url.path.endswith("/" + stage):
            if kind == "timeout":
                raise httpx.ReadTimeout("private-upstream-error")
            if kind == "redirect":
                return httpx.Response(307, headers={"location": "https://evil.invalid/private-upstream-error"})
            if kind == "oversize":
                return httpx.Response(200 if stage == "client_secrets" else 201, text="private-upstream-error" * 10000)
            if kind == "invalid":
                return httpx.Response(200 if stage == "client_secrets" else 201, json={"error": "private-upstream-error"})
            return httpx.Response(500, text="private-upstream-error")
    gateway(app, fail)
    with TestClient(app) as client:
        login(client)
        response = connect(client)
        assert response.status_code == 502
        assert "private-" not in response.text
        gateway(app)
        assert connect(client).status_code == 200


def end(client, ident, headers=ORIGIN):
    return client.post("/api/realtime/end", json={"id": ident}, headers=headers)


def test_owner_bound_session_readback_expiry_and_end(app, monkeypatch):
    app = enabled(app, monkeypatch)
    requests = gateway(app)
    with TestClient(app) as client, TestClient(app) as other:
        login(client); login(other)
        first = connect(client).json()
        status = client.get("/api/realtime/status").json()
        assert status["session"]["id"] == first["id"]
        assert status["session"]["expires_at"] > 0
        assert status["session"]["busy"] is False
        assert other.get("/api/realtime/status").json()["session"] is None
        assert connect(client).status_code == 429
        assert end(other, first["id"]).status_code == 404
        assert end(client, first["id"], {}).status_code == 403
        stopped = end(client, first["id"])
        assert stopped.status_code == 200
        assert stopped.json() == {"ok": True, "provider_hangup": False}
        assert requests[-1].url.path == "/v1/realtime/calls/rtc_example/hangup"
        assert requests[-1].headers["authorization"] == "Bearer private-long-lived"
        assert client.get("/api/realtime/status").json()["session"] is None
        assert end(client, first["id"]).status_code == 404
        second = connect(client).json()
        app.state.realtime.sessions[second["id"]].expires = 0
        assert client.get("/api/realtime/status").json()["session"] is None
        assert connect(client).status_code == 200


@pytest.mark.parametrize("location,attempt", [
    ("https://gateway.invalid/v1/realtime/calls/rtc_safe", True),
    ("/v1/realtime/calls/rtc_safe", True),
    ("https://evil.invalid/v1/realtime/calls/rtc_safe", False),
    ("http://gateway.invalid/v1/realtime/calls/rtc_safe", False),
    ("https://gateway.invalid:444/v1/realtime/calls/rtc_safe", False),
    ("/v1/realtime/calls/../private", False),
    ("/v1/realtime/calls/rtc_safe?token=private", False),
    ("/v1/realtime/calls/rtc_safe%2fprivate", False),
    ("", False),
])
def test_hangup_never_follows_untrusted_location(app, monkeypatch, location, attempt):
    app = enabled(app, monkeypatch)
    def response(request):
        if request.url.path.endswith("/calls"):
            return httpx.Response(201, text=SDP, headers={"location": location})
        if request.url.path.endswith("/hangup"):
            return httpx.Response(200)
    requests = gateway(app, response)
    with TestClient(app) as client:
        login(client)
        ident = connect(client).json()["id"]
        assert end(client, ident).json()["provider_hangup"] is attempt
        assert len(requests) == (3 if attempt else 2)


def test_start_quotas_global_and_hourly(app, monkeypatch):
    monkeypatch.setenv("JARVIS_REALTIME_STARTS_PER_HOUR", "6")
    from contextlib import ExitStack
    app = enabled(app, monkeypatch)
    gateway(app)
    with ExitStack() as stack:
        clients = [stack.enter_context(TestClient(app)) for _ in range(5)]
        for client in clients:
            login(client)
        for client in clients[:4]:
            assert connect(client).status_code == 200
        assert connect(clients[4]).status_code == 429
        first = clients[0]
        for _ in range(5):
            ident = first.get("/api/realtime/status").json()["session"]["id"]
            assert end(first, ident).status_code == 200
            assert connect(first).status_code == 200
        ident = first.get("/api/realtime/status").json()["session"]["id"]
        end(first, ident)
        assert connect(first).status_code == 429


def test_connect_reserves_slot_before_provider_await(app, monkeypatch):
    import asyncio, threading, concurrent.futures
    app = enabled(app, monkeypatch)
    entered, release = threading.Event(), threading.Event()
    async def provider(request):
        if request.url.path.endswith("/client_secrets"):
            entered.set()
            await asyncio.to_thread(release.wait, 5)
            return httpx.Response(200, json={"value": "private-ephemeral"})
        return httpx.Response(201, text=SDP)
    app.state.realtime_transport = httpx.MockTransport(provider)
    with TestClient(app) as client:
        login(client)
        with concurrent.futures.ThreadPoolExecutor() as pool:
            first = pool.submit(connect, client)
            assert entered.wait(3)
            try:
                assert client.get("/api/realtime/status").json()["session"]["busy"] is True
                assert connect(client).status_code == 429
            finally:
                release.set()
            assert first.result().status_code == 200


def tool(client, ident, call_id="call_1", **values):
    return client.post("/api/realtime/tool", headers=ORIGIN, json={
        "id": ident, "call_id": call_id, "name": "ask_jarvis",
        "arguments": {"request": "PRIVATE model generated content"},
        "user_text": "What is the weather in Chennai?", **values})


def chat_provider(app, handler=None):
    requests = []
    def provider(request):
        data = json.loads(request.content)
        requests.append(data)
        message = handler(data) if handler else {"content": "Verified answer"}
        return httpx.Response(200, json={"choices": [{"message": message}]})
    app.state.llm_transport = httpx.MockTransport(provider)
    return requests


def test_tool_bridge_uses_current_user_turn_and_persists_conversation(app, monkeypatch):
    app = enabled(app, monkeypatch)
    gateway(app)
    requests = chat_provider(app)
    with TestClient(app) as client:
        login(client)
        ident = connect(client, language="ta-IN").json()["id"]
        response = tool(client, ident)
        assert response.status_code == 200, response.text
        result = response.json()
        assert result["text"] == "Verified answer" and result["session_id"]
        assert "PRIVATE model" not in json.dumps(requests)
        assert requests[0]["messages"][-1]["content"] == "What is the weather in Chennai?"
        assert tool(client, ident).json() == result
        assert len(requests) == 1
        assert tool(client, ident, user_text="different").status_code == 409
        assert tool(client, ident, "call_2", user_text="follow up").json()["session_id"] == result["session_id"]
        assert any(m.get("content") == "Verified answer" for m in requests[1]["messages"])


@pytest.mark.parametrize("values", [
    {"name": "set_volume"}, {"name": "end_conversation"}, {"name": "confirm"},
    {"user_text": ""}, {"user_text": " "}, {"user_text": "x" * 8001},
    {"call_id": "x" * 129}, {"arguments": {}}, {"arguments": {"request": "x", "approve": True}},
    {"arguments": {"request": "x" * 8001}}, {"arguments": {"request": 12}},
])
def test_tool_bridge_strictly_validates_request(app, monkeypatch, values):
    app = enabled(app, monkeypatch)
    gateway(app)
    calls = chat_provider(app)
    with TestClient(app) as client:
        login(client)
        ident = connect(client).json()["id"]
        assert tool(client, ident, **values).status_code == 422
        assert calls == []


def test_tool_owner_expiry_and_twenty_call_limit(app, monkeypatch):
    app = enabled(app, monkeypatch)
    gateway(app)
    calls = chat_provider(app)
    with TestClient(app) as client, TestClient(app) as other:
        login(client); login(other)
        ident = connect(client).json()["id"]
        assert tool(other, ident).status_code == 404
        assert client.post("/api/realtime/tool", json={"id": ident, "call_id": "x", "name": "ask_jarvis", "arguments": {"request": "x"}, "user_text": "x"}).status_code == 403
        for i in range(20):
            assert tool(client, ident, str(i)).status_code == 200
        assert tool(client, ident, "over_limit").status_code == 429
        assert tool(client, ident, "0").status_code == 200
        assert len(calls) == 20
        app.state.realtime.sessions[ident].expires = 0
        assert tool(client, ident, "0").status_code == 404


def test_tool_confirmation_remains_exact_and_yes_does_not_approve(app, monkeypatch):
    import sys, types
    executed = []
    monkeypatch.setitem(sys.modules, "jarvis_tools", types.SimpleNamespace(
        TOOLS=[{"type": "function", "function": {"name": "danger", "parameters": {"type": "object", "properties": {"recipient": {"type": "string"}}}}}],
        tool_risk=lambda name: True, execute_tool=lambda *args: executed.append(args) or {"ok": True}))
    app = enabled(app, monkeypatch)
    gateway(app)
    chat_provider(app, lambda data: {"tool_calls": [{"id": "danger_call", "function": {"name": "danger", "arguments": '{"recipient":"exact@example.com"}'}}]})
    with TestClient(app) as client:
        login(client)
        ident = connect(client).json()["id"]
        result = tool(client, ident, user_text="send the message").json()
        assert result["confirmation"]["arguments"] == {"recipient": "exact@example.com"}
        assert result["text"] == result["confirmation"]["spoken_summary"]
        again = tool(client, ident, "yes_call", user_text="yes").json()
        assert again["confirmation"] == result["confirmation"]
        assert executed == []


def test_tool_inflight_duplicate_and_other_call_rejected_without_reexecution(app, monkeypatch):
    import asyncio, threading, concurrent.futures
    app = enabled(app, monkeypatch)
    gateway(app)
    entered, release = threading.Event(), threading.Event()
    calls = []
    async def provider(request):
        calls.append(request)
        entered.set()
        await asyncio.to_thread(release.wait, 5)
        return httpx.Response(200, json={"choices": [{"message": {"content": "done"}}]})
    app.state.llm_transport = httpx.MockTransport(provider)
    with TestClient(app) as client:
        login(client)
        ident = connect(client).json()["id"]
        with concurrent.futures.ThreadPoolExecutor() as pool:
            first = pool.submit(tool, client, ident)
            assert entered.wait(3)
            try:
                assert tool(client, ident).status_code == 409
                assert tool(client, ident, "different").status_code == 429
                assert client.get("/api/realtime/status").json()["session"]["busy"] is True
            finally:
                release.set()
            assert first.result().status_code == 200
        assert len(calls) == 1
        assert tool(client, ident).json()["text"] == "done"
        assert len(calls) == 1
        assert client.get("/api/realtime/status").json()["session"]["busy"] is False


def test_failed_tool_call_cached_never_retried(app, monkeypatch):
    app = enabled(app, monkeypatch)
    gateway(app)
    calls = []
    def broken(request):
        calls.append(request)
        return httpx.Response(500, text="private provider error")
    app.state.llm_transport = httpx.MockTransport(broken)
    with TestClient(app) as client:
        login(client)
        ident = connect(client).json()["id"]
        first = tool(client, ident)
        assert first.status_code == 502
        assert "private" not in first.text
        assert tool(client, ident).json() == first.json()
        assert len(calls) == 1


def test_tool_output_is_bounded_and_cached(app, monkeypatch):
    app = enabled(app, monkeypatch)
    gateway(app)
    calls = chat_provider(app, lambda data: {"content": "x" * 40000})
    with TestClient(app) as client:
        login(client)
        ident = connect(client).json()["id"]
        first = tool(client, ident)
        assert first.status_code == 502
        assert len(first.content) < 32768
        assert tool(client, ident).json() == first.json()
        assert len(calls) == 1


@pytest.mark.parametrize("base", ["http://gateway.invalid/v1", "https://user:password@gateway.invalid/v1", "https://gateway.invalid/v1?token=secret", "file:///tmp/api"])
def test_insecure_or_ambiguous_provider_config_disabled(app, monkeypatch, base):
    monkeypatch.setenv("JARVIS_REALTIME_BASE_URL", base)
    app = enabled(app, monkeypatch)
    calls = gateway(app)
    with TestClient(app) as client:
        login(client)
        assert client.get("/api/realtime/status").json()["enabled"] is False
        assert connect(client).status_code == 503
        assert calls == []


@pytest.mark.parametrize("location", ["\nhttps://evil.invalid/v1/realtime/calls/rtc_safe", "\t/v1/realtime/calls/rtc_safe", "https://[invalid/v1/realtime/calls/rtc_safe"])
def test_malformed_location_does_not_break_valid_exchange(app, monkeypatch, location):
    app = enabled(app, monkeypatch)
    calls = gateway(app, lambda request: httpx.Response(201, text=SDP, headers={"location": location}) if request.url.path.endswith("/calls") else None)
    with TestClient(app) as client:
        login(client)
        response = connect(client)
        assert response.status_code == 200
        assert end(client, response.json()["id"]).json()["provider_hangup"] is False
        assert len(calls) == 2


def test_cancelled_connect_does_not_reactivate_ended_session(app, monkeypatch):
    import asyncio, threading, concurrent.futures
    app = enabled(app, monkeypatch)
    entered, release = threading.Event(), threading.Event()
    calls = []
    async def provider(request):
        calls.append(request.url.path)
        if request.url.path.endswith("/client_secrets"):
            entered.set()
            await asyncio.to_thread(release.wait, 5)
            return httpx.Response(200, json={"value": "private-ephemeral"})
        if request.url.path.endswith("/hangup"):
            return httpx.Response(200)
        return httpx.Response(201, text=SDP, headers={"location": "/v1/realtime/calls/rtc_safe"})
    app.state.realtime_transport = httpx.MockTransport(provider)
    with TestClient(app) as client:
        login(client)
        with concurrent.futures.ThreadPoolExecutor() as pool:
            pending = pool.submit(connect, client)
            assert entered.wait(3)
            try:
                ident = client.get("/api/realtime/status").json()["session"]["id"]
                assert end(client, ident).status_code == 200
            finally:
                release.set()
            assert pending.result().status_code == 404
        assert client.get("/api/realtime/status").json()["session"] is None
        assert calls[-1] == "/v1/realtime/calls/rtc_safe/hangup"


@pytest.mark.parametrize("configured,expected", [(None, 3300), ("60", 60), ("1800", 1800), ("3300", 3300), ("59", 3300), ("3301", 3300), ("", 3300), ("nan", 3300), ("inf", 3300), ("600.5", 3300)])
def test_configured_session_duration_matches_reservation_and_api(app, monkeypatch, configured, expected):
    import time
    if configured is None:
        monkeypatch.delenv("JARVIS_REALTIME_SESSION_SECONDS", raising=False)
    else:
        monkeypatch.setenv("JARVIS_REALTIME_SESSION_SECONDS", configured)
    app = enabled(app, monkeypatch)
    gateway(app)
    with TestClient(app) as client:
        login(client)
        before = time.monotonic()
        result = connect(client).json()
        after = time.monotonic()
        assert result["max_seconds"] == expected
        item = app.state.realtime.sessions[result["id"]]
        assert before + expected <= item.expires <= after + expected
        assert client.get("/api/realtime/status").json()["max_seconds"] == expected


def test_authenticated_disabled_status(app):
    with TestClient(app) as client:
        assert client.get("/api/realtime/status").status_code == 401
        login(client)
        response = client.get("/api/realtime/status")
        assert response.status_code == 200
        data = response.json()
        assert data["enabled"] is False
        assert data["model"] == "gpt-realtime-2.1"
        assert data["transport"] == "webrtc" and data["max_seconds"] == 3300
        assert "single-worker" in " ".join(data["limitations"])
        assert "private-long-lived" not in response.text
