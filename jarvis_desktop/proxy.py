"""In-process loopback LLM proxy for Microsoft UFO.

UFO hard-codes sampling parameters (temperature=0, top_p=0, n=1) that some gateways reject
for Claude models. This proxy listens on 127.0.0.1 only, holds the upstream key itself (UFO's
config carries no secret), strips only the parameters in STRIP and forwards everything else
verbatim, including streamed responses.

    handle = start_proxy("https://gateway/v1", key, port=8780)
    ...
    handle.stop()
"""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx

STRIP = frozenset({"temperature", "top_p", "n", "frequency_penalty", "presence_penalty"})
MAX_BODY = 32 * 1024 * 1024  # UFO sends screenshots; bound memory anyway.
_PATHS = ("/v1/chat/completions", "/chat/completions")


def strip_params(body: dict) -> tuple[dict, list[str]]:
    dropped = sorted(k for k in STRIP if k in body)
    return {k: v for k, v in body.items() if k not in STRIP}, dropped


class ProxyHandle:
    def __init__(self, server: ThreadingHTTPServer, thread: threading.Thread, client: httpx.Client):
        self._server, self._thread, self._client = server, thread, client
        self.port = server.server_address[1]
        self.url = f"http://127.0.0.1:{self.port}/v1"

    def stop(self):
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(5)
        self._client.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.stop()


def start_proxy(upstream_base: str = "", key: str = "", port: int = 8780, *, host: str = "127.0.0.1",
                model_override: str = "", transport: httpx.BaseTransport | None = None, provider=None) -> ProxyHandle:
    """Raw passthrough for OpenAI-compatible upstreams, or (provider=...) translation through a
    jarvis_desktop.providers.Provider so UFO's OpenAI-format calls reach Claude, Azure, etc."""
    if host not in ("127.0.0.1", "localhost", "::1"):
        raise ValueError("The proxy only listens on loopback")
    if provider is not None and provider.kind in ("anthropic", "azure"):
        return _start_translating(provider, port, host, model_override)
    if provider is not None:
        upstream_base, key = provider.base, provider.api_key
        transport = transport or provider.transport
    if not upstream_base.startswith(("https://", "http://")):
        raise ValueError("Upstream base must be an http(s) URL")
    upstream = upstream_base.rstrip("/") + "/chat/completions"
    client = httpx.Client(timeout=httpx.Timeout(180, connect=10), transport=transport, follow_redirects=False)

    class Handler(BaseHTTPRequestHandler):
        server_version = "JarvisUFOProxy"
        protocol_version = "HTTP/1.0"  # connection closes after each response; simple streaming

        def log_message(self, *args):  # never log request bodies or headers
            pass

        def _json(self, status, payload):
            data = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            if self.path in ("/health", "/v1/health"):
                return self._json(200, {"ok": True})
            self._json(404, {"error": "not_found"})

        def do_POST(self):
            if self.path.split("?")[0] not in _PATHS:
                return self._json(404, {"error": "not_found"})
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= MAX_BODY:
                    raise ValueError
                body = json.loads(self.rfile.read(length))
                if not isinstance(body, dict):
                    raise ValueError
            except (ValueError, TypeError):
                return self._json(400, {"error": "invalid_json"})
            body, _ = strip_params(body)
            if model_override:
                body["model"] = model_override
            headers = {"Content-Type": "application/json", **({"Authorization": "Bearer " + key} if key else {})}
            try:
                if body.get("stream"):
                    with client.stream("POST", upstream, json=body, headers=headers) as response:
                        self.send_response(response.status_code)
                        self.send_header("Content-Type", response.headers.get("content-type", "text/event-stream"))
                        self.send_header("Cache-Control", "no-cache")
                        self.end_headers()
                        for chunk in response.iter_bytes():  # decoded: we don't forward Content-Encoding
                            self.wfile.write(chunk)
                            self.wfile.flush()
                    return
                response = client.post(upstream, json=body, headers=headers)
            except httpx.HTTPError:
                return self._json(502, {"error": "upstream_unreachable"})
            except (BrokenPipeError, ConnectionResetError):
                return
            self.send_response(response.status_code)
            self.send_header("Content-Type", response.headers.get("content-type", "application/json"))
            self.send_header("Content-Length", str(len(response.content)))
            self.end_headers()
            self.wfile.write(response.content)

    server = ThreadingHTTPServer((host, port), Handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.2}, name="ufo-llm-proxy", daemon=True)
    thread.start()
    return ProxyHandle(server, thread, client)


def _start_translating(provider, port, host, model_override) -> ProxyHandle:
    """Non-streaming translation: OpenAI chat request in -> provider.chat -> OpenAI response out."""
    from .providers import ProviderError

    class Handler(BaseHTTPRequestHandler):
        server_version = "JarvisUFOProxy"
        protocol_version = "HTTP/1.0"

        def log_message(self, *args):
            pass

        def _json(self, status, payload):
            data = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            self._json(200, {"ok": True}) if self.path in ("/health", "/v1/health") else self._json(404, {"error": "not_found"})

        def do_POST(self):
            if self.path.split("?")[0] not in _PATHS:
                return self._json(404, {"error": "not_found"})
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= MAX_BODY:
                    raise ValueError
                body = json.loads(self.rfile.read(length))
                if not isinstance(body, dict) or not isinstance(body.get("messages"), list):
                    raise ValueError
            except (ValueError, TypeError):
                return self._json(400, {"error": {"message": "invalid_json"}})
            body, _ = strip_params(body)
            model = model_override or body.get("model") or ""
            limit = body.get("max_tokens") or body.get("max_completion_tokens") or 4096
            try:
                message = provider.chat(model, body["messages"], tools=body.get("tools"), max_tokens=int(limit))
            except ProviderError as e:
                return self._json(e.status if 400 <= e.status < 600 else 502, {"error": {"message": str(e), "code": e.status}})
            except httpx.HTTPError:
                return self._json(502, {"error": {"message": "upstream_unreachable"}})
            self._json(200, {"id": "jarvis-proxy", "object": "chat.completion", "model": model,
                             "choices": [{"index": 0, "message": message,
                                          "finish_reason": "tool_calls" if message.get("tool_calls") else "stop"}]})

    server = ThreadingHTTPServer((host, port), Handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.2}, name="ufo-llm-proxy", daemon=True)
    thread.start()

    class _Closer:
        def close(self):
            provider.close()
    return ProxyHandle(server, thread, _Closer())
