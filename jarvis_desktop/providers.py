"""AI provider adapters: one OpenAI-shaped chat interface over every common key type.

The agent speaks the OpenAI chat-completions message/tool format internally; this module maps it
onto each provider's real API so a user can paste whatever key they have:

  openai     api.openai.com                      Bearer key, /chat/completions
  anthropic  api.anthropic.com (Claude direct)   x-api-key + anthropic-version, /v1/messages (translated)
  azure      Azure OpenAI / Azure AI Foundry     api-key header, /openai/v1 or deployment URLs
  gemini     Google AI Studio                    Bearer key, its OpenAI-compatible endpoint
  openrouter, groq, deepseek, mistral, xai, together, fireworks, perplexity: OpenAI-compatible, Bearer
  ollama     local http://localhost:11434/v1     no key
  litellm    any other OpenAI-compatible gateway Bearer key

Keys are only ever sent to the configured provider and never appear in returned messages.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from urllib.parse import urlsplit

import httpx

ANTHROPIC_VERSION = "2023-06-01"
AZURE_API_VERSION = "2024-10-21"


@dataclass(frozen=True)
class ProviderInfo:
    kind: str
    label: str
    default_base: str
    default_model: str
    needs_key: bool = True
    tts_model: str = ""  # "" = no OpenAI-style /audio/speech; the offline Windows voice is used
    notes: str = ""


PROVIDERS: dict[str, ProviderInfo] = {p.kind: p for p in [
    ProviderInfo("openai", "OpenAI", "https://api.openai.com/v1", "gpt-4o-mini", tts_model="tts-1"),
    ProviderInfo("anthropic", "Anthropic (Claude)", "https://api.anthropic.com", "claude-sonnet-4-5"),
    ProviderInfo("azure", "Azure OpenAI / AI Foundry", "https://YOUR-RESOURCE.openai.azure.com/openai/v1", "",
                 notes="Paste your resource endpoint; the model is your deployment name."),
    ProviderInfo("gemini", "Google Gemini", "https://generativelanguage.googleapis.com/v1beta/openai", "gemini-2.5-flash"),
    ProviderInfo("openrouter", "OpenRouter", "https://openrouter.ai/api/v1", "openai/gpt-4o-mini"),
    ProviderInfo("groq", "Groq", "https://api.groq.com/openai/v1", "llama-3.3-70b-versatile"),
    ProviderInfo("deepseek", "DeepSeek", "https://api.deepseek.com/v1", "deepseek-chat"),
    ProviderInfo("mistral", "Mistral", "https://api.mistral.ai/v1", "mistral-large-latest"),
    ProviderInfo("xai", "xAI (Grok)", "https://api.x.ai/v1", "grok-3-mini"),
    ProviderInfo("together", "Together AI", "https://api.together.xyz/v1", "meta-llama/Llama-3.3-70B-Instruct-Turbo"),
    ProviderInfo("fireworks", "Fireworks AI", "https://api.fireworks.ai/inference/v1", "accounts/fireworks/models/llama-v3p3-70b-instruct"),
    ProviderInfo("perplexity", "Perplexity", "https://api.perplexity.ai", "sonar"),
    ProviderInfo("ollama", "Ollama (local, free)", "http://localhost:11434/v1", "llama3.1", needs_key=False,
                 notes="Runs on this PC. Desktop actions need a model with tool support (llama3.1, qwen2.5, qwen3)."),
    ProviderInfo("litellm", "LiteLLM / other OpenAI-compatible", "", "", tts_model="tts-1",
                 notes="Paste your gateway URL ending in /v1."),
]}

_HOSTS = [("api.anthropic.com", "anthropic"), ("openai.azure.com", "azure"), ("services.ai.azure.com", "azure"),
          ("cognitiveservices.azure.com", "azure"), ("generativelanguage.googleapis.com", "gemini"),
          ("openrouter.ai", "openrouter"), ("api.groq.com", "groq"), ("api.deepseek.com", "deepseek"),
          ("api.mistral.ai", "mistral"), ("api.x.ai", "xai"), ("api.together.xyz", "together"),
          ("api.fireworks.ai", "fireworks"), ("api.perplexity.ai", "perplexity"), ("api.openai.com", "openai")]
_KEY_PREFIX = [("sk-ant-", "anthropic"), ("sk-or-", "openrouter"), ("gsk_", "groq"), ("xai-", "xai"), ("AIza", "gemini")]
_REASONING = re.compile(r"^(o\d|gpt-5|gpt-6)", re.I)


def detect_provider(base_url: str = "", key: str = "") -> str:
    """Best guess from the URL host first, then the key prefix."""
    base_url = (base_url or "").strip()
    parts = urlsplit(base_url)
    host = (parts.hostname or "").lower()
    if host:
        for suffix, kind in _HOSTS:
            if host == suffix or host.endswith("." + suffix):
                return kind
        if host in ("localhost", "127.0.0.1", "::1") and parts.port == 11434:
            return "ollama"
        return "litellm"
    key = (key or "").strip()
    for prefix, kind in _KEY_PREFIX:
        if key.startswith(prefix):
            return kind
    return "openai"


class ProviderError(RuntimeError):
    def __init__(self, status: int, message: str = ""):
        super().__init__(message)
        self.status = status


@dataclass
class Provider:
    kind: str
    base_url: str
    api_key: str = ""
    transport: httpx.BaseTransport | None = None
    timeout: float = 90.0
    _client: httpx.Client | None = field(default=None, init=False, repr=False)

    @property
    def client(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(timeout=httpx.Timeout(self.timeout, connect=10), transport=self.transport,
                                        follow_redirects=False)
        return self._client

    def close(self):
        if self._client is not None:
            self._client.close()
            self._client = None

    @property
    def base(self) -> str:
        info = PROVIDERS.get(self.kind)
        return ((self.base_url or (info.default_base if info else "")) or "").strip().rstrip("/")

    def headers(self) -> dict:
        if not self.api_key:
            return {}
        if self.kind == "anthropic":
            return {"x-api-key": self.api_key, "anthropic-version": ANTHROPIC_VERSION}
        if self.kind == "azure":
            return {"api-key": self.api_key}
        return {"Authorization": "Bearer " + self.api_key}

    def _azure_root(self) -> str:
        """URL prefix that '/chat/completions' or '/audio/speech' is appended to (before any query)."""
        b = self.base
        if "/deployments/" in b:
            return b.split("?")[0].removesuffix("/chat/completions")
        if b.endswith("/openai/v1"):
            return b
        p = urlsplit(b)  # bare resource or AI Foundry project URL -> the resource's OpenAI v1 surface
        return f"{p.scheme}://{p.netloc}/openai/v1"

    def url(self, path: str) -> str:
        if self.kind == "azure":
            root = self._azure_root()
            return root + path + (f"?api-version={AZURE_API_VERSION}" if "/deployments/" in root else "")
        if self.kind == "anthropic":
            b = self.base
            return (b if b.endswith("/v1") else b + "/v1") + path
        return self.base + path

    def _raise_for(self, r: httpx.Response):
        if r.status_code < 400:
            return
        try:
            err = r.json().get("error")
            msg = err.get("message") if isinstance(err, dict) else str(err or "")
        except ValueError:
            msg = r.text
        msg = (msg or "")[:300]
        if self.api_key:
            msg = msg.replace(self.api_key, "[key]")
        raise ProviderError(r.status_code, msg)

    # ------------------------------------------------------------- chat
    def chat(self, model: str, messages: list, tools: list | None = None, max_tokens: int = 1024) -> dict:
        """OpenAI-shaped request and response: returns the assistant message (content, tool_calls)."""
        if self.kind == "anthropic":
            return self._anthropic_chat(model, messages, tools, max_tokens)
        payload = {"model": model, "messages": messages}
        if tools:
            payload["tools"] = tools
        limit_key = "max_completion_tokens" if self.kind in ("openai", "azure") and _REASONING.match(model or "") else "max_tokens"
        payload[limit_key] = max_tokens
        r = self.client.post(self.url("/chat/completions"), headers=self.headers(), json=payload)
        if r.status_code == 400 and "max_completion_tokens" in r.text and limit_key == "max_tokens":
            payload["max_completion_tokens"] = payload.pop("max_tokens")
            r = self.client.post(self.url("/chat/completions"), headers=self.headers(), json=payload)
        self._raise_for(r)
        message = r.json()["choices"][0]["message"]
        if not isinstance(message, dict):
            raise ProviderError(502, "invalid response")
        return message

    def _anthropic_chat(self, model, messages, tools, max_tokens):
        system, out = [], []

        def push(role, block):
            if out and out[-1]["role"] == role:
                out[-1]["content"].append(block)
            else:
                out.append({"role": role, "content": [block]})

        for m in messages:
            role, content = m.get("role"), m.get("content")
            if role == "system":
                if isinstance(content, str) and content:
                    system.append(content)
                continue
            if role == "tool":
                push("user", {"type": "tool_result", "tool_use_id": m["tool_call_id"], "content": str(content or "")})
                continue
            if isinstance(content, str) and content:
                push(role, {"type": "text", "text": content})
            elif isinstance(content, list):
                for part in content:
                    if part.get("type") == "text":
                        push(role, {"type": "text", "text": part["text"]})
                    elif part.get("type") == "image_url":
                        data = re.match(r"data:([^;]+);base64,(.*)", part["image_url"]["url"], re.S)
                        if data:
                            push(role, {"type": "image", "source": {"type": "base64", "media_type": data.group(1), "data": data.group(2)}})
            for call in m.get("tool_calls") or []:
                try:
                    args = json.loads(call["function"].get("arguments") or "{}")
                except ValueError:
                    args = {}
                push("assistant", {"type": "tool_use", "id": call["id"], "name": call["function"]["name"], "input": args})
        payload = {"model": model, "max_tokens": max_tokens, "messages": out}
        if system:
            payload["system"] = "\n\n".join(system)
        if tools:
            payload["tools"] = [{"name": t["function"]["name"], "description": t["function"].get("description", ""),
                                 "input_schema": t["function"].get("parameters") or {"type": "object", "properties": {}}}
                                for t in tools]
        r = self.client.post(self.url("/messages"), headers=self.headers(), json=payload)
        self._raise_for(r)
        blocks = r.json().get("content") or []
        text = "".join(b.get("text", "") for b in blocks if b.get("type") == "text")
        calls = [{"id": b["id"], "type": "function", "function": {"name": b["name"], "arguments": json.dumps(b.get("input") or {})}}
                 for b in blocks if b.get("type") == "tool_use"]
        message = {"role": "assistant", "content": text or None}
        if calls:
            message["tool_calls"] = calls
        return message

    # ------------------------------------------------------------- speech
    def speech(self, model: str, voice: str, text: str) -> bytes:
        """OpenAI-style TTS where the provider offers it; raises ProviderError otherwise."""
        if self.kind in ("anthropic", "ollama", "gemini", "groq", "deepseek", "mistral", "xai", "perplexity") or not model:
            raise ProviderError(404, "no speech endpoint")
        last = 500
        bodies = [{"model": model, "voice": v, "input": text} for v in dict.fromkeys([voice, "alloy"]) if v]
        for body in bodies + [{"model": model, "input": text}]:
            r = self.client.post(self.url("/audio/speech"), headers=self.headers(), json=body)
            if r.status_code == 200 and r.content and not r.headers.get("content-type", "").startswith(("application/json", "text/")):
                return r.content
            last = r.status_code
            if r.status_code not in (400, 422):
                break
        raise ProviderError(last, "speech failed")


def provider_from(cfg) -> Provider:
    """Build from an agent cfg dict or a Settings object."""
    get = cfg.get if isinstance(cfg, dict) else (lambda k, d=None: getattr(cfg, k, d))
    base, key = get("base_url", "") or "", get("api_key", "") or ""
    return Provider(kind=get("provider", "") or detect_provider(base, key), base_url=base, api_key=key)


_HINTS = {401: "the API key was rejected", 403: "this key is not allowed to use that model",
          404: "the model, deployment or URL was not found", 429: "rate limited or out of credit"}


def verify(kind: str, base_url: str, api_key: str, model: str, transport=None) -> tuple[bool, str, dict]:
    """Real end-to-end check: a tiny chat AND a tool call. Returns (ok, plain message, details)."""
    info = PROVIDERS.get(kind)
    if info is None:
        return False, f"Unknown provider type '{kind}'.", {}
    if info.needs_key and not api_key:
        return False, "Enter your API key first.", {}
    if not model:
        return False, "Enter a model name.", {}
    local = kind == "ollama"
    p = Provider(kind=kind, base_url=base_url, api_key=api_key, transport=transport, timeout=240 if local else 40)
    details = {"provider": info.label}
    tool = {"type": "function", "function": {"name": "get_time", "description": "Get the current time.",
                                              "parameters": {"type": "object", "properties": {}, "additionalProperties": False}}}
    try:
        details["chat"] = (p.chat(model, [{"role": "user", "content": "Reply with the single word OK."}], max_tokens=16)
                           .get("content") or "").strip()[:40]
        details["tools"] = bool(p.chat(model, [{"role": "user", "content": "What time is it? Use the tool."}],
                                       tools=[tool], max_tokens=200).get("tool_calls"))
    except ProviderError as e:
        hint = _HINTS.get(e.status)
        return False, f"{info.label} returned HTTP {e.status}: " + (hint + "." if hint else str(e)), details
    except httpx.TimeoutException:
        if local:
            return False, ("The local model took too long to answer. It may still be loading; on a PC without a GPU, "
                           "try a smaller model such as qwen2.5:3b, then test again."), details
        return False, "The provider did not answer in time. Check the URL and your internet connection.", details
    except httpx.HTTPError as e:
        return False, f"Could not reach {info.label} ({type(e).__name__}). Check the URL.", details
    except (KeyError, ValueError, TypeError, IndexError):
        return False, "The provider answered in an unexpected format. Check the provider type and URL.", details
    finally:
        p.close()
    if not details["tools"]:
        return True, (f"Connected to {info.label}; '{model}' answered but did not use the test tool. Jarvis can "
                      "talk, but desktop actions need a model with tool calling."), details
    return True, f"Connected to {info.label}. '{model}' answered and can use tools.", details
