"""Live provider matrix: real keys, real endpoints. Never prints a key.

For each provider kind available on this machine: setup-style verify() (chat + tool call), then a
real Jarvis agent turn that must call a tool (time), then the UFO proxy translating a UFO-shaped
request (temperature/top_p/n + image) for that provider.
"""
import base64, json, os, socket, sys, time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import httpx
from jarvis_desktop.providers import Provider, verify
from jarvis_desktop.proxy import start_proxy


def load_env(path):
    out = {}
    for line in Path(path).read_text(errors="ignore").splitlines():
        if "=" in line and not line.startswith("#"):
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip().strip("'\"")
    return out


env = {**load_env(Path(os.environ["LOCALAPPDATA"]) / "hermes/.env"), **load_env(ROOT / ".env")}
gw_base, gw_key = env["JARVIS_LLM_BASE_URL"], env["JARVIS_LLM_API_KEY"]
cases = [
    ("litellm", gw_base, gw_key, "claude-sonnet-5"),
    ("litellm", gw_base, gw_key, "gpt-5.5"),
    # Anthropic Messages API (x-api-key, /v1/messages) - served here by the gateway's Anthropic route.
    ("anthropic", gw_base.removesuffix("/v1"), gw_key, "claude-sonnet-5"),
    ("azure", env["AZURE_FOUNDRY_BASE_URL"], env["AZURE_FOUNDRY_API_KEY"], "gpt-5.5"),
    ("ollama", "http://localhost:11434/v1", "", "llama3.1"),
]
secrets = [gw_key, env["AZURE_FOUNDRY_API_KEY"]]
image = ROOT / "data" / "vision-probe" / "image.png"


def port():
    s = socket.socket(); s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]; s.close(); return p


def agent_turn(kind, base, key, model):
    from jarvis_desktop.agent import Agent

    class Null:
        announce = fire = None
        def start(self): pass
        def close(self): pass
    import tempfile
    d = Path(tempfile.mkdtemp(prefix="jarvis-prov-"))
    cfg = {"provider": kind, "base_url": base, "api_key": key, "chat_model": model, "autonomy": "balanced",
           "language": "en", "data_dir": str(d / "data"), "workspace": str(d / "ws"), "ufo_home": None}
    a = Agent(cfg, speak_async=None, confirm=lambda q: False, jobs=Null(), scheduler=Null())
    try:
        t = time.time()
        reply = a.handle("What time is it right now?")
        return {"reply": reply[:90], "tool_used": [c[0] for c in a.last_tool_calls], "seconds": round(time.time() - t, 1)}
    finally:
        a.close()


def proxy_turn(kind, base, key, model):
    prov = Provider(kind, base, key, timeout=120)
    h = start_proxy(provider=prov, port=port(), model_override=model)
    try:
        content = [{"type": "text", "text": "What two shapes are in this image? Answer in five words."}]
        if image.exists():
            content.append({"type": "image_url", "image_url": {"url": "data:image/png;base64," + base64.b64encode(image.read_bytes()).decode()}})
        r = httpx.post(h.url + "/chat/completions", timeout=120, json={
            "model": "gpt-4o", "temperature": 0, "top_p": 0, "n": 1, "max_tokens": 300,
            "messages": [{"role": "system", "content": "You are a desktop agent."}, {"role": "user", "content": content}]})
        return {"status": r.status_code, "answer": (r.json()["choices"][0]["message"].get("content") or "")[:80] if r.status_code == 200 else r.text[:120]}
    finally:
        h.stop()


results = []
for kind, base, key, model in cases:
    row = {"provider": kind, "model": model}
    t = time.time()
    ok, msg, det = verify(kind, base, key, model)
    row.update(verify_ok=ok, verify_msg=msg, tools=det.get("tools"), verify_s=round(time.time() - t, 1))
    if ok:
        try:
            row["agent"] = agent_turn(kind, base, key, model)
        except Exception as e:
            row["agent"] = {"error": type(e).__name__, "detail": str(e)[:150]}
        if kind != "ollama":
            try:
                row["ufo_proxy"] = proxy_turn(kind, base, key, model)
            except Exception as e:
                row["ufo_proxy"] = {"error": type(e).__name__, "detail": str(e)[:150]}
    results.append(row)
text = json.dumps(results, indent=2)
assert not any(s and s in text for s in secrets), "secret leaked"
print(text)
(ROOT / "data").mkdir(exist_ok=True)
(ROOT / "data" / "provider-matrix.json").write_text(text)
