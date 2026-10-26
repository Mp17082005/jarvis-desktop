"""Any key type: OpenAI, Claude direct, Azure, Gemini, LiteLLM/OpenRouter/Groq..., local Ollama.

Each provider is exercised through a mock that only answers when the request has that provider's
real URL, auth header and body shape, so a wrong mapping fails here instead of in a user's setup.
"""
import json

import httpx
import pytest

from jarvis_desktop.providers import PROVIDERS, Provider, detect_provider, verify

TOOL = {"type": "function", "function": {"name": "get_time", "description": "time",
                                         "parameters": {"type": "object", "properties": {}, "additionalProperties": False}}}


def openai_reply(content="OK", tool=False):
    msg = {"role": "assistant", "content": None if tool else content}
    if tool:
        msg["tool_calls"] = [{"id": "c1", "type": "function", "function": {"name": "get_time", "arguments": "{}"}}]
    return httpx.Response(200, json={"choices": [{"message": msg}]})


# ------------------------------------------------------------------ detection
@pytest.mark.parametrize("base,key,kind", [
    ("https://api.openai.com/v1", "sk-x", "openai"),
    ("https://api.anthropic.com", "sk-ant-x", "anthropic"),
    ("https://myres.openai.azure.com/openai/v1", "k", "azure"),
    ("https://myres.services.ai.azure.com/api/projects/p", "k", "azure"),
    ("https://generativelanguage.googleapis.com/v1beta/openai", "AIza", "gemini"),
    ("https://openrouter.ai/api/v1", "sk-or-x", "openrouter"),
    ("https://api.groq.com/openai/v1", "gsk_x", "groq"),
    ("http://localhost:11434/v1", "", "ollama"),
    ("https://llm.mycompany.com/v1", "sk-x", "litellm"),
    ("", "sk-ant-abc", "anthropic"),
    ("", "sk-proj-abc", "openai"),
])
def test_detect_provider(base, key, kind):
    assert detect_provider(base, key) == kind


# ------------------------------------------------------------------ OpenAI-compatible family
@pytest.mark.parametrize("kind", ["openai", "litellm", "openrouter", "groq", "gemini", "deepseek", "mistral", "xai"])
def test_openai_compatible_uses_bearer_and_chat_completions(kind):
    seen = {}
    base = PROVIDERS[kind].default_base or "https://gateway.example.com/v1"

    def handle(req):
        seen.update(url=str(req.url), auth=req.headers.get("authorization"), body=json.loads(req.content))
        return openai_reply("OK")
    p = Provider(kind, base, "key-123", transport=httpx.MockTransport(handle))
    assert p.chat("m", [{"role": "user", "content": "hi"}])["content"] == "OK"
    assert seen["url"] == base + "/chat/completions"
    assert seen["auth"] == "Bearer key-123"
    assert "temperature" not in seen["body"]


def test_ollama_needs_no_key_and_sends_no_auth():
    seen = {}

    def handle(req):
        seen["auth"] = req.headers.get("authorization")
        seen["url"] = str(req.url)
        return openai_reply("OK")
    p = Provider("ollama", "http://localhost:11434/v1", "", transport=httpx.MockTransport(handle))
    assert p.chat("llama3.1", [{"role": "user", "content": "hi"}])["content"] == "OK"
    assert seen == {"auth": None, "url": "http://localhost:11434/v1/chat/completions"}


def test_reasoning_models_get_max_completion_tokens():
    bodies = []
    p = Provider("openai", "https://api.openai.com/v1", "k",
                 transport=httpx.MockTransport(lambda r: (bodies.append(json.loads(r.content)), openai_reply())[1]))
    p.chat("gpt-5.5", [{"role": "user", "content": "hi"}], max_tokens=50)
    p.chat("gpt-4o-mini", [{"role": "user", "content": "hi"}], max_tokens=50)
    assert bodies[0].get("max_completion_tokens") == 50 and "max_tokens" not in bodies[0]
    assert bodies[1].get("max_tokens") == 50


def test_retries_with_max_completion_tokens_when_provider_demands_it():
    bodies = []

    def handle(req):
        body = json.loads(req.content)
        bodies.append(body)
        if "max_tokens" in body:
            return httpx.Response(400, json={"error": {"message": "Unsupported parameter: 'max_tokens'. Use 'max_completion_tokens' instead."}})
        return openai_reply()
    p = Provider("litellm", "https://gw/v1", "k", transport=httpx.MockTransport(handle))
    assert p.chat("some-new-model", [{"role": "user", "content": "hi"}])["content"] == "OK"
    assert "max_completion_tokens" in bodies[-1]


# ------------------------------------------------------------------ Azure
@pytest.mark.parametrize("base,expected", [
    ("https://res.openai.azure.com/openai/v1", "https://res.openai.azure.com/openai/v1/chat/completions"),
    ("https://res.openai.azure.com", "https://res.openai.azure.com/openai/v1/chat/completions"),
    ("https://res.services.ai.azure.com/api/projects/myproj", "https://res.services.ai.azure.com/openai/v1/chat/completions"),
    ("https://res.openai.azure.com/openai/deployments/gpt4o",
     "https://res.openai.azure.com/openai/deployments/gpt4o/chat/completions?api-version=2024-10-21"),
])
def test_azure_urls_and_api_key_header(base, expected):
    seen = {}

    def handle(req):
        seen.update(url=str(req.url), key=req.headers.get("api-key"), auth=req.headers.get("authorization"))
        return openai_reply()
    Provider("azure", base, "az-key", transport=httpx.MockTransport(handle)).chat("gpt4o", [{"role": "user", "content": "hi"}])
    assert seen == {"url": expected, "key": "az-key", "auth": None}


# ------------------------------------------------------------------ Anthropic (Claude direct)
def test_anthropic_translates_messages_tools_and_tool_results():
    seen = {}

    def handle(req):
        seen.update(url=str(req.url), key=req.headers.get("x-api-key"), ver=req.headers.get("anthropic-version"),
                    auth=req.headers.get("authorization"), body=json.loads(req.content))
        return httpx.Response(200, json={"content": [{"type": "text", "text": "Checking."},
                                                     {"type": "tool_use", "id": "tu_2", "name": "get_time", "input": {"tz": "IST"}}],
                                         "stop_reason": "tool_use"})
    p = Provider("anthropic", "https://api.anthropic.com", "sk-ant-k", transport=httpx.MockTransport(handle))
    history = [
        {"role": "system", "content": "You are Jarvis."},
        {"role": "user", "content": "time?"},
        {"role": "assistant", "content": None, "tool_calls": [{"id": "tu_1", "type": "function",
                                                               "function": {"name": "get_time", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "tu_1", "content": "{\"time\": \"9:00\"}"},
        {"role": "user", "content": "and in IST?"},
    ]
    msg = p.chat("claude-sonnet-4-5", history, tools=[TOOL], max_tokens=300)
    assert seen["url"] == "https://api.anthropic.com/v1/messages"
    assert seen["key"] == "sk-ant-k" and seen["ver"] and seen["auth"] is None
    body = seen["body"]
    assert body["system"] == "You are Jarvis." and body["max_tokens"] == 300
    assert body["tools"] == [{"name": "get_time", "description": "time",
                              "input_schema": TOOL["function"]["parameters"]}]
    roles = [m["role"] for m in body["messages"]]
    assert roles == ["user", "assistant", "user"]  # strictly alternating; tool result + next user turn merged
    assert body["messages"][1]["content"] == [{"type": "tool_use", "id": "tu_1", "name": "get_time", "input": {}}]
    assert body["messages"][2]["content"][0] == {"type": "tool_result", "tool_use_id": "tu_1", "content": "{\"time\": \"9:00\"}"}
    assert body["messages"][2]["content"][1] == {"type": "text", "text": "and in IST?"}
    assert msg["content"] == "Checking."
    assert msg["tool_calls"] == [{"id": "tu_2", "type": "function", "function": {"name": "get_time", "arguments": "{\"tz\": \"IST\"}"}}]


# ------------------------------------------------------------------ verify (what the setup window calls)
def test_verify_reports_chat_and_tool_support():
    def handle(req):
        body = json.loads(req.content)
        return openai_reply(tool=bool(body.get("tools")))
    ok, msg, details = verify("openai", "https://api.openai.com/v1", "k", "gpt-4o-mini", transport=httpx.MockTransport(handle))
    assert ok and details["tools"] is True and "can use tools" in msg


def test_verify_warns_when_model_cannot_call_tools():
    ok, msg, details = verify("ollama", "http://localhost:11434/v1", "", "tiny", transport=httpx.MockTransport(lambda r: openai_reply()))
    assert ok and details["tools"] is False and "tool" in msg


@pytest.mark.parametrize("status,phrase", [(401, "rejected"), (404, "not found"), (429, "rate limited")])
def test_verify_explains_failures_without_leaking_key(status, phrase):
    def handle(req):
        return httpx.Response(status, json={"error": {"message": "bad key secret-key-999"}})
    ok, msg, _ = verify("anthropic", "https://api.anthropic.com", "secret-key-999", "claude", transport=httpx.MockTransport(handle))
    assert not ok and phrase in msg and "secret-key-999" not in msg


def test_verify_requires_key_except_ollama():
    assert verify("openai", "", "", "m")[0] is False
    assert "API key" in verify("openai", "", "", "m")[1]


def test_local_models_get_a_long_timeout_and_a_specific_message(monkeypatch):
    import jarvis_desktop.providers as prov
    seen = {}
    real = prov.Provider

    def spy(*a, **kw):
        seen["timeout"] = kw.get("timeout")
        return real(*a, **kw)
    monkeypatch.setattr(prov, "Provider", spy)

    def slow(req):
        raise httpx.ReadTimeout("slow", request=req)
    ok, msg, _ = prov.verify("ollama", "http://localhost:11434/v1", "", "llama3.1", transport=httpx.MockTransport(slow))
    assert seen["timeout"] >= 180
    assert not ok and "local model" in msg.lower()


# ------------------------------------------------------------------ the agent uses the provider
def test_agent_talks_to_claude_direct_via_messages_api(tmp_path):
    from jarvis_desktop.agent import Agent

    class Null:
        announce = fire = None
        def start(self): pass
        def close(self): pass

    seen = []

    def handle(req):
        seen.append(str(req.url))
        return httpx.Response(200, json={"content": [{"type": "text", "text": "Hello from Claude."}], "stop_reason": "end_turn"})
    cfg = {"provider": "anthropic", "base_url": "https://api.anthropic.com", "api_key": "sk-ant-x", "chat_model": "claude-sonnet-4-5",
           "autonomy": "balanced", "language": "en", "data_dir": str(tmp_path / "d"), "workspace": str(tmp_path / "w"), "ufo_home": None}
    agent = Agent(cfg, speak_async=None, confirm=None, transport=httpx.MockTransport(handle), jobs=Null(), scheduler=Null())
    try:
        assert agent.handle("say hello") == "Hello from Claude."
    finally:
        agent.close()
    assert seen == ["https://api.anthropic.com/v1/messages"]
