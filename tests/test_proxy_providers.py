"""UFO only speaks OpenAI chat-completions; the local proxy must reach every provider kind."""
import json
import socket

import httpx

from jarvis_desktop.providers import Provider
from jarvis_desktop.proxy import start_proxy


def free_port():
    s = socket.socket(); s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]; s.close(); return port


def test_proxy_translates_ufo_requests_to_claude_messages_api():
    seen = {}

    def anthropic(req):
        seen.update(url=str(req.url), key=req.headers.get("x-api-key"), body=json.loads(req.content))
        return httpx.Response(200, json={"content": [{"type": "text", "text": "{\"Observation\": \"ok\"}"}], "stop_reason": "end_turn"})
    provider = Provider("anthropic", "https://api.anthropic.com", "sk-ant-k", transport=httpx.MockTransport(anthropic))
    handle = start_proxy(provider=provider, port=free_port())
    try:
        image = "data:image/png;base64,iVBORw0KGgo="
        r = httpx.post(handle.url + "/chat/completions", timeout=10, json={
            "model": "gpt-4o", "temperature": 0, "top_p": 0, "n": 1, "max_tokens": 2000,
            "messages": [{"role": "system", "content": "You are UFO."},
                         {"role": "user", "content": [{"type": "text", "text": "Look"}, {"type": "image_url", "image_url": {"url": image}}]}]})
        assert r.status_code == 200, r.text
        assert r.json()["choices"][0]["message"]["content"] == "{\"Observation\": \"ok\"}"
        assert seen["url"] == "https://api.anthropic.com/v1/messages" and seen["key"] == "sk-ant-k"
        assert seen["body"]["system"] == "You are UFO."
        assert seen["body"]["messages"][0]["content"][1]["type"] == "image"
        assert "temperature" not in seen["body"] and "top_p" not in seen["body"]
    finally:
        handle.stop()


def test_proxy_reaches_azure_with_api_key_header_and_model_override():
    seen = {}

    def azure(req):
        seen.update(url=str(req.url), key=req.headers.get("api-key"), body=json.loads(req.content))
        return httpx.Response(200, json={"choices": [{"message": {"role": "assistant", "content": "ok"}}]})
    provider = Provider("azure", "https://res.openai.azure.com/openai/v1", "az", transport=httpx.MockTransport(azure))
    handle = start_proxy(provider=provider, port=free_port(), model_override="my-deployment")
    try:
        r = httpx.post(handle.url + "/chat/completions", timeout=10, json={"model": "gpt-4o", "temperature": 0, "messages": [{"role": "user", "content": "hi"}]})
        assert r.status_code == 200
        assert seen["url"] == "https://res.openai.azure.com/openai/v1/chat/completions"
        assert seen["key"] == "az" and seen["body"]["model"] == "my-deployment" and "temperature" not in seen["body"]
    finally:
        handle.stop()


def test_proxy_reports_provider_errors_as_openai_style_errors():
    provider = Provider("anthropic", "https://api.anthropic.com", "k",
                        transport=httpx.MockTransport(lambda r: httpx.Response(401, json={"error": {"message": "bad key k"}})))
    handle = start_proxy(provider=provider, port=free_port())
    try:
        r = httpx.post(handle.url + "/chat/completions", timeout=10, json={"model": "m", "messages": [{"role": "user", "content": "x"}]})
        assert r.status_code == 401 and "error" in r.json()
    finally:
        handle.stop()
