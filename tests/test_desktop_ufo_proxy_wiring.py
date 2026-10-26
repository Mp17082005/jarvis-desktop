"""The installed app must start its own UFO LLM proxy; a fresh PC has no other one."""
import socket

import httpx

from jarvis_desktop.agent import Agent


class NullJobs:
    announce = None
    def start(self): pass
    def close(self): pass


class NullScheduler:
    fire = announce = None
    def start(self): pass
    def close(self): pass


def free_port():
    s = socket.socket(); s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]; s.close(); return port


def test_agent_starts_ufo_proxy_with_users_key_and_stops_it(tmp_path, monkeypatch):
    port = free_port()
    monkeypatch.setenv("JARVIS_UFO_PROXY_PORT", str(port))
    ufo = tmp_path / "ufo"; ufo.mkdir()
    seen = {}

    def upstream(request):
        seen["auth"] = request.headers.get("authorization")
        seen["url"] = str(request.url)
        return httpx.Response(200, json={"choices": [{"message": {"role": "assistant", "content": "ok"}}]})

    cfg = {"base_url": "https://provider.example/v1", "api_key": "user-key", "chat_model": "m", "autonomy": "balanced",
           "language": "en", "data_dir": str(tmp_path / "data"), "workspace": str(tmp_path / "ws"), "ufo_home": str(ufo),
           "ufo_proxy_transport": httpx.MockTransport(upstream)}
    agent = Agent(cfg, speak_async=None, confirm=None, jobs=NullJobs(), scheduler=NullScheduler())
    try:
        assert httpx.get(f"http://127.0.0.1:{port}/health", timeout=5).json() == {"ok": True}
        r = httpx.post(f"http://127.0.0.1:{port}/v1/chat/completions", json={"model": "x", "temperature": 0, "messages": []}, timeout=5)
        assert r.status_code == 200
        assert seen == {"auth": "Bearer user-key", "url": "https://provider.example/v1/chat/completions"}
    finally:
        agent.close()
    try:
        httpx.get(f"http://127.0.0.1:{port}/health", timeout=2)
        raise AssertionError("proxy still running after close")
    except httpx.HTTPError:
        pass


def test_no_ufo_no_proxy(tmp_path, monkeypatch):
    port = free_port()
    monkeypatch.setenv("JARVIS_UFO_PROXY_PORT", str(port))
    cfg = {"base_url": "https://provider.example/v1", "api_key": "k", "chat_model": "m", "autonomy": "balanced",
           "language": "en", "data_dir": str(tmp_path / "data"), "workspace": str(tmp_path / "ws"), "ufo_home": None}
    agent = Agent(cfg, speak_async=None, confirm=None, jobs=NullJobs(), scheduler=NullScheduler())
    try:
        try:
            httpx.get(f"http://127.0.0.1:{port}/health", timeout=2)
            raise AssertionError("proxy started without UFO")
        except httpx.HTTPError:
            pass
    finally:
        agent.close()
