"""HTTP application. Start with ``uvicorn jarvis.main:app``."""
import importlib.util
import os
import secrets
import time
from fastapi import Depends, FastAPI, HTTPException, Request, Response, WebSocket, WebSocketDisconnect
from pydantic import ValidationError
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel, Field, field_validator
from pathlib import Path
import asyncio
from .llm import completion, public_research, PUBLIC_TOOL_NAMES
from .store import Store
from .speech import Speech
import json
import re

BUILTINS = [
    {"type": "function", "function": {"name": "public_research", "description": "Research the current user's public question with an isolated public-web-only worker. The server uses the exact current user turn, not this generated query or earlier context. No approval is needed for this isolated route. If the turn lacks the subject or details needed, ask for a self-contained question; never fill them from private history.", "parameters": {"type": "object", "properties": {"query": {"type": "string", "minLength": 1, "maxLength": 8000}}, "required": ["query"], "additionalProperties": False}}},
    {"type": "function", "function": {"name": "set_timer", "description": "Set a browser-polled reminder, between 1 second and 7 days.", "parameters": {"type": "object", "properties": {"seconds": {"type": "number", "minimum": 1, "maximum": 604800}, "label": {"type": "string", "maxLength": 200}}, "required": ["seconds", "label"], "additionalProperties": False}}},
    {"type": "function", "function": {"name": "list_timers", "description": "List this user's saved reminders.", "parameters": {"type": "object", "properties": {}, "additionalProperties": False}}},
]
from dotenv import load_dotenv
from collections import defaultdict, deque
from starlette.responses import JSONResponse
from urllib.parse import urlsplit
import threading


def same_origin(request):
    origin = request.headers.get("origin")
    if request.headers.get("sec-fetch-site") == "cross-site":
        return False
    if not origin:
        return True
    parsed = urlsplit(origin)
    return parsed.scheme in ("http", "https") and parsed.netloc.lower() == request.headers.get("host", "").lower() and parsed.scheme == request.url.scheme.replace("ws", "http")


load_dotenv(Path(__file__).resolve().parent.parent / ".env")
COOKIE = "jarvis_session"


class Login(BaseModel):
    token: str = Field(min_length=1, max_length=512)


class LanguageRequest(BaseModel):
    language: str | None = Field(default=None, min_length=2, max_length=63)

    @field_validator("language")
    @classmethod
    def language_tag(cls, value):
        # Validate regular BCP47 syntax without rewriting the caller's identifier.
        pattern = (r"(?:[A-Za-z]{2,3}(?:-[A-Za-z]{3}){0,3}|[A-Za-z]{4}|[A-Za-z]{5,8})"
                   r"(?:-[A-Za-z]{4})?(?:-(?:[A-Za-z]{2}|[0-9]{3}))?"
                   r"(?P<variants>(?:-(?:[A-Za-z0-9]{5,8}|[0-9][A-Za-z0-9]{3}))*)"
                   r"(?P<extensions>(?:-[0-9A-WY-Za-wy-z](?:-[A-Za-z0-9]{2,8})+)*)"
                   r"(?:-x(?:-[A-Za-z0-9]{1,8})+)?")
        if value is None:
            return value
        match = re.fullmatch(pattern, value)
        if not match:
            raise ValueError("Expected a BCP47 language tag")
        variants = match.group("variants").lower().split("-")[1:]
        extensions = [part.lower() for part in match.group("extensions").split("-") if len(part) == 1]
        if len(set(variants)) != len(variants) or len(set(extensions)) != len(extensions):
            raise ValueError("Repeated BCP47 variant or extension")
        return value


class Chat(LanguageRequest):
    text: str = Field(min_length=1, max_length=8000)
    session_id: str | None = Field(default=None, max_length=100)

    @field_validator("text")
    @classmethod
    def nonblank(cls, value):
        if not value.strip():
            raise ValueError("Text cannot be blank")
        return value


class Confirm(LanguageRequest):
    id: str = Field(min_length=1, max_length=100)
    approve: bool = Field(strict=True)
    session_id: str = Field(min_length=1, max_length=100)
    voice_phrase: str | None = Field(default=None, min_length=1, max_length=100)


def create_app():
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    store = Store(os.getenv("JARVIS_DATA_DIR", str(Path(__file__).resolve().parent.parent / "data")))
    app.state.store = store
    app.state.llm_transport = None
    config = {"base": os.getenv("JARVIS_LLM_BASE_URL", ""), "key": os.getenv("JARVIS_LLM_API_KEY", ""), "model": os.getenv("JARVIS_LLM_MODEL", "")}
    try:
        import jarvis_tools as external
    except ImportError:
        external = None
    # Native control is available only through the local desktop bridge.
    schemas = BUILTINS + ([schema for schema in external.TOOLS if schema["function"]["name"] not in {"set_timer", "list_timers", "open_app", "run_command"}] if external else [])
    from .desktop import DesktopBridge, register_desktop_routes
    desktop = DesktopBridge()
    desktop.user_lookup = store.session_user
    app.state.desktop = desktop
    schemas += desktop.schemas
    names = {schema["function"]["name"] for schema in schemas}
    locks = {}
    token = os.getenv("JARVIS_ACCESS_TOKEN", "")
    attempts = defaultdict(deque)
    rate_lock = threading.Lock()

    @app.exception_handler(RequestValidationError)
    async def invalid_request(request, exc):
        return JSONResponse({"detail": "Invalid request fields"}, status_code=422)

    @app.middleware("http")
    async def protect(request: Request, call_next):
        if request.method not in ("GET", "HEAD", "OPTIONS") and not same_origin(request):
            return JSONResponse({"detail": "Origin not allowed"}, status_code=403)
        if request.url.path.startswith("/api/") and request.url.path != "/api/login" and not store.session(request.cookies.get(COOKIE, ""), token):
            return JSONResponse({"detail": "Authentication required"}, status_code=401)
        limit = 8 * 1024 * 1024 + 65536 if request.url.path == "/api/transcribe" else 32768
        if request.url.path == "/api/realtime/connect":
            limit = 131072  # JSON envelope/escaping; decoded SDP remains capped at 64 KiB.
        try:
            if int(request.headers.get("content-length", "0")) > limit:
                return JSONResponse({"detail": "Request body too large"}, status_code=413)
        except ValueError:
            return JSONResponse({"detail": "Invalid content length"}, status_code=400)
        # Buffer within a strict cap before parsing, including chunked uploads.
        if request.method not in ("GET", "HEAD", "OPTIONS"):
            chunks, size = [], 0
            async for chunk in request.stream():
                size += len(chunk)
                if size > limit:
                    return JSONResponse({"detail": "Request body too large"}, status_code=413)
                chunks.append(chunk)
            request._body = b"".join(chunks)
        try:
            response = await call_next(request)
        except Exception:
            return JSONResponse({"detail": "Internal operation failed"}, status_code=500)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Cache-Control"] = "no-store"
        response.headers["Content-Security-Policy"] = "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; media-src 'self' blob:; object-src 'none'; base-uri 'self'; frame-ancestors 'none'"
        if request.url.path == "/connect.html":
            response.headers["Content-Security-Policy"] = response.headers["Content-Security-Policy"].replace("connect-src 'self';", "connect-src 'self' http://127.0.0.1:8767;")
        return response

    def limit_login(request):
        key = request.client.host if request.client else "unknown"
        with rate_lock:
            now = time.monotonic()
            for stale in list(attempts):
                if not attempts[stale] or attempts[stale][-1] < now - 60:
                    del attempts[stale]
            queue = attempts[key]
            while queue and queue[0] < now - 60:
                queue.popleft()
            if len(queue) >= 10 or len(attempts) > 10000:
                raise HTTPException(429, "Too many login attempts", headers={"Retry-After": "60"})
            queue.append(now)


    def authenticated(request: Request):
        session = store.session(request.cookies.get(COOKIE, ""), token)
        if not session:
            raise HTTPException(401, "Authentication required")
        return session

    @app.post("/api/login")
    def login(body: Login, request: Request, response: Response):
        limit_login(request)
        if not token:
            raise HTTPException(503, "Access token is not configured")
        if not secrets.compare_digest(body.token.encode(), token.encode()):
            raise HTTPException(401, "Invalid access token")
        sid = store.login(token)
        response.set_cookie(COOKIE, sid, httponly=True, samesite="strict", secure=request.url.scheme == "https", max_age=30 * 86400)
        return {"ok": True}

    @app.get("/api/status")
    def status(session=Depends(authenticated)):
        return {"llm_configured": bool(os.getenv("JARVIS_LLM_BASE_URL") and os.getenv("JARVIS_LLM_MODEL")), "model": os.getenv("JARVIS_LLM_MODEL", ""), "stt": {"available": Speech.available(), "model": "base"}, "tools": sorted(names), "integrations": {"gmail": bool(os.getenv("JARVIS_GOOGLE_SCRIPT") or (os.getenv("JARVIS_GMAIL_ADDRESS") and os.getenv("JARVIS_GMAIL_APP_PASSWORD"))), "calendar": bool(os.getenv("JARVIS_GOOGLE_SCRIPT")), "telegram": bool(os.getenv("JARVIS_TELEGRAM_BOT_TOKEN") and os.getenv("JARVIS_TELEGRAM_CHAT_ID"))}}

    @app.get("/api/reminders")
    def reminders(session=Depends(authenticated)):
        return {"reminders": store.timers(session["id"])}

    async def execute_public(name, args):
        if name not in PUBLIC_TOOL_NAMES or not external:
            return {"error": "Only public research tools are available"}
        try:
            result = await asyncio.to_thread(external.execute_tool, name, args)
            return result if isinstance(result, dict) else {"error": "Tool returned an invalid result"}
        except Exception:
            return {"error": "Public tool execution failed"}

    async def execute(owner, name, args, sid=None, call_id=None):
        if sid and name not in PUBLIC_TOOL_NAMES | {"public_research", "set_timer", "list_timers", "play_music", "stop_music"}:
            # Persist BEFORE execution, even if execution or the next LLM call fails.
            store.taint(sid)
        if name == "public_research":
            return await public_research(config, args.get("query"), schemas, execute_public, app.state.llm_transport)
        if name == "set_timer":
            return store.set_timer(owner, args)
        if name == "list_timers":
            return {"reminders": store.timers(owner)}
        if name in desktop.tool_names:
            return await desktop.execute(owner, name, args, sid, call_id)
        if name not in names or not external:
            return {"error": "Unknown tool"}
        try:
            result = await asyncio.to_thread(external.execute_tool, name, args)
            return result if isinstance(result, dict) else {"error": "Tool returned an invalid result"}
        except Exception:
            return {"error": "Tool execution failed"}

    def approval_response(sid, pending, results=None, language=None):
        # Trusted fixed templates; never ask the model to paraphrase approved args.
        pending = dict(pending)
        lang = (language or "en").split("-")[0].lower()
        labels = {
            "ta": ("அனுமதி தேவை. செயல்", "சரியான விவரங்கள்", "இந்தச் செயல் இன்னும் செய்யப்படவில்லை. முழு விவரங்களையும் கேட்ட பிறகு சொல்லுங்கள்"),
            "hi": ("अनुमति चाहिए। कार्य", "सटीक विवरण", "यह कार्य अभी नहीं किया गया है। पूरा विवरण सुनने के बाद कहें"),
            "es": ("Se requiere aprobación. Acción", "Argumentos exactos", "Esta acción no se ha ejecutado. Después de escuchar el resumen completo, diga"),
            "fr": ("Approbation requise. Action", "Paramètres exacts", "Cette action n'a pas été exécutée. Après le résumé complet, dites"),
        }
        if lang in labels:
            action, arguments, instruction = labels[lang]
            pending["spoken_summary"] = f"{action}: {pending['tool']}. {arguments}: {json.dumps(pending['arguments'], ensure_ascii=False, sort_keys=True)}. {instruction}: {pending['voice_phrase']}."
        if pending["tool"] in desktop.tool_names:
            pending["spoken_summary"] = f"Approval required. Action: {pending['tool']}. Exact arguments: {json.dumps(pending['arguments'], ensure_ascii=False, sort_keys=True)}. Nothing has been executed. Use the Approve or Deny button. Voice approval is disabled for desktop mutations."
        return {"text": pending["spoken_summary"], "session_id": sid, "confirmation": pending, "results": results or []}

    async def run_chat(owner, sid, messages, language=None, *, public_query):
        results = []
        for _ in range(4):
            message = await completion(config, messages, schemas, app.state.llm_transport, language=language)
            calls = message.get("tool_calls") or []
            try:
                if not isinstance(calls, list) or len(calls) > 8:
                    raise ValueError()
                ids = set()
                for call in calls:
                    ident = call["id"]
                    name = call["function"]["name"]
                    raw = call["function"]["arguments"]
                    if not isinstance(ident, str) or not ident or ident in ids or not isinstance(name, str) or not isinstance(raw, str) or len(raw) > 16000:
                        raise ValueError()
                    ids.add(ident)
                    args = json.loads(raw, parse_constant=lambda value: (_ for _ in ()).throw(ValueError()))
                    if not isinstance(args, dict):
                        raise ValueError()
                    schema = next((s["function"]["parameters"] for s in schemas if s["function"]["name"] == name), {})
                    if not set(schema.get("required", [])) <= args.keys():
                        raise ValueError()
                    if schema.get("additionalProperties") is False and args.keys() - schema.get("properties", {}).keys():
                        raise ValueError()
                    if name == "public_research":
                        # Only this HTTP/WebSocket user turn may cross the public
                        # boundary. Never derive it from model args or history.
                        # Record the actual args in the outer tool transcript too.
                        call["function"]["arguments"] = json.dumps({"query": public_query}, ensure_ascii=False)
            except (ValueError, KeyError, TypeError):
                raise HTTPException(502, "Language model returned invalid tool calls") from None
            if not calls:
                text = message.get("content") or "No response was returned."
                messages.append({"role": "assistant", "content": text})
                store.save(sid, messages)
                return {"text": text, "session_id": sid, "results": results}
            messages.append({"role": "assistant", "content": message.get("content"), "tool_calls": calls})
            for call in calls[:8]:
                name = call["function"]["name"]
                try:
                    args = json.loads(call["function"]["arguments"])
                    if not isinstance(args, dict):
                        raise ValueError()
                except (ValueError, TypeError):
                    args = {}
                needs_approval = desktop.needs_approval(name, args) if name in desktop.tool_names else (
                    name in names and name not in {"set_timer", "list_timers", "public_research"} and external.tool_risk(name)
                )
                if needs_approval:
                    pending = store.propose(owner, sid, name, args)
                    if name in desktop.tool_names:
                        desktop.bind_approval(owner, sid, pending["id"], call["id"])
                    messages.append({"role": "tool", "tool_call_id": call["id"], "content": "Not executed: explicit approval is pending."})
                    for skipped in calls[calls.index(call) + 1:]:
                        messages.append({"role": "tool", "tool_call_id": skipped["id"], "content": "Not executed: stopped for approval of earlier action."})
                    messages.append({"role": "assistant", "content": "Waiting for explicit approval via UI."})
                    store.save(sid, messages)
                    return approval_response(sid, pending, results, language)
                result = await execute(owner, name, args, sid, call["id"])
                results.append({"tool": name, "result": result})
                messages.append({"role": "tool", "tool_call_id": call["id"], "content": json.dumps(result)})
                if result.get("error") or result.get("ok") is False or result.get("success") is False:
                    for skipped in calls[calls.index(call) + 1:]:
                        messages.append({"role": "tool", "tool_call_id": skipped["id"], "content": "Skipped after tool failure."})
                    text = "The tool action failed. Review the result; no success is assumed."
                    messages.append({"role": "assistant", "content": text})
                    store.save(sid, messages)
                    return {"text": text, "session_id": sid, "results": results}
        text = "Tool-round limit reached. Review the results before trying again."
        messages.append({"role": "assistant", "content": text})
        store.save(sid, messages)
        return {"text": text, "session_id": sid, "results": results}

    chat_attempts = defaultdict(deque)

    def limit_action(owner):
        now = time.monotonic()
        for key in list(chat_attempts):
            if not chat_attempts[key] or chat_attempts[key][-1] < now - 60:
                del chat_attempts[key]
                if key in locks and not locks[key].locked():
                    del locks[key]
        queue = chat_attempts[owner]
        while queue and queue[0] < now - 60:
            queue.popleft()
        if len(queue) >= 30:
            raise HTTPException(429, "Action rate limit reached", headers={"Retry-After": "60"})
        queue.append(now)

    @app.post("/api/chat")
    async def chat(body: Chat, session=Depends(authenticated)):
        owner = session["id"]
        limit_action(owner)
        lock = locks.setdefault(owner, asyncio.Lock())
        if lock.locked():
            raise HTTPException(429, "Another action is in progress")
        async with lock:
            sid, messages = store.conversation(owner, body.session_id)
            pending = store.pending(owner, sid)
            while pending and pending["tool"] == "public_research":
                # Legacy proposals contain model-derived queries, not trusted
                # user input. Retire them; never execute their saved arguments.
                store.consume(owner, sid, pending["id"])
                messages.append({"role": "assistant", "content": "Cancelled the old public research request without executing it. Research uses only your new question."})
                store.save(sid, messages)
                pending = store.pending(owner, sid)
            if pending:
                return approval_response(sid, pending, language=body.language)
            messages.append({"role": "user", "content": body.text})
            return await run_chat(owner, sid, messages, body.language, public_query=body.text)

    @app.post("/api/confirm")
    async def confirm(body: Confirm, session=Depends(authenticated)):
        owner = session["id"]
        async with locks.setdefault(owner, asyncio.Lock()):
            sid, messages = store.conversation(owner, body.session_id)
            proposed = store.pending(owner, sid)
            if proposed and proposed["id"] == body.id and proposed["tool"] in desktop.tool_names and body.voice_phrase is not None:
                raise HTTPException(403, "Desktop mutations require button approval")
            pending = store.consume(owner, sid, body.id, body.voice_phrase)
            if pending["tool"] == "public_research":
                # Confirmation has no current raw question. Never resurrect a
                # legacy model-derived query, even when its old UI is approved.
                text, results = "Cancelled the old public research request. Nothing was executed. Please ask a new question.", []
            elif body.approve:
                call_id = desktop.approval_call(owner, sid, pending["id"]) if pending["tool"] in desktop.tool_names else "approved_" + pending["id"]
                result = await execute(owner, pending["tool"], pending["arguments"], sid, call_id)
                results = [{"tool": pending["tool"], "result": result}]
                failed = bool(result.get("error") or result.get("success") is False or result.get("ok") is False)
                text = "Action failed. Review the tool result." if failed else "The tool returned a result, but I couldn't summarize it. Review the result below."
                ident = "approved_" + pending["id"]
                messages.extend([
                    {"role": "assistant", "content": None, "tool_calls": [{"id": ident, "type": "function", "function": {"name": pending["tool"], "arguments": json.dumps(pending["arguments"])}}]},
                    {"role": "tool", "tool_call_id": ident, "content": json.dumps(result)},
                ])
                # Save the real outcome before any fallible provider request. Never rerun it.
                store.save(sid, messages)
                try:
                    summary = await completion(config, messages, [], app.state.llm_transport, purpose="confirmation", language=body.language)
                    if not failed and not summary.get("tool_calls") and isinstance(summary.get("content"), str) and summary["content"].strip():
                        text = summary["content"]
                except HTTPException:
                    pass
            else:
                text, results = "Action denied. Nothing was executed.", []
            messages.append({"role": "assistant", "content": text + " " + json.dumps(results)})
            store.save(sid, messages)
            return {"text": text, "session_id": sid, "results": results}

    speech = Speech()
    app.state.speech = speech

    @app.post("/api/transcribe")
    async def transcribe(request: Request, session=Depends(authenticated)):
        async with request.form(max_files=1, max_fields=0, max_part_size=8 * 1024 * 1024) as form:
            audio = form.get("audio")
            if audio is None or not hasattr(audio, "read"):
                raise HTTPException(422, "An audio file is required")
            data = await audio.read(8 * 1024 * 1024 + 1)
            if len(data) > 8 * 1024 * 1024:
                raise HTTPException(413, "Audio file exceeds 8 MB")
            if not data:
                raise HTTPException(422, "Audio file is empty")
        text = await asyncio.to_thread(speech.transcribe, data)
        return {"text": text}

    @app.websocket("/ws")
    async def websocket(ws: WebSocket):
        session = store.session(ws.cookies.get(COOKIE, ""), token)
        if not session or not same_origin(ws):
            await ws.close(code=1008)
            return
        await ws.accept()
        try:
            while True:
                raw = await asyncio.wait_for(ws.receive_text(), timeout=300)
                if len(raw.encode()) > 32768:
                    await ws.close(code=1009)
                    return
                if not store.session(ws.cookies.get(COOKIE, ""), token):
                    await ws.close(code=1008)
                    return
                try:
                    data = json.loads(raw)
                    if not isinstance(data, dict) or data.get("type") != "chat":
                        raise ValueError()
                    body = Chat.model_validate(data)
                    result = await chat(body, session)
                    await ws.send_json({"type": "reply", **result})
                except (ValueError, ValidationError):
                    await ws.send_json({"type": "error", "text": "Invalid chat message"})
                except HTTPException as exc:
                    await ws.send_json({"type": "error", "text": exc.detail})
        except (WebSocketDisconnect, asyncio.TimeoutError):
            return

    from .desktop import LocalOnlyMiddleware, desktop_requested
    if desktop_requested():
        app.add_middleware(LocalOnlyMiddleware)
    register_desktop_routes(app, desktop, authenticated)
    if desktop_requested() and os.getenv("JARVIS_DESKTOP_LOCAL_ONLY", "").lower() == "true":
        from .pairing import register_pairing_routes
        register_pairing_routes(app, store, authenticated)

    from .realtime import register_realtime_routes
    register_realtime_routes(app, authenticated, chat, Chat, same_origin)

    from fastapi.staticfiles import StaticFiles
    from starlette.responses import FileResponse
    package = Path(os.getenv("JARVIS_COMPANION_PACKAGE_PATH", str(Path(__file__).resolve().parent.parent / "data" / "Jarvis-Windows-Companion-final.zip"))).resolve()

    @app.get("/downloads/Jarvis-Windows-Companion.zip")
    def companion_download():
        # Fixed trusted deployment configuration, never a caller-selected path.
        if package.suffix.lower() != ".zip" or not package.is_file():
            raise HTTPException(404, "Companion package is not available")
        return FileResponse(package, media_type="application/zip", filename="Jarvis-Windows-Companion.zip")

    static = Path(__file__).resolve().parent.parent / "static"
    app.mount("/static", StaticFiles(directory=str(static), check_dir=False), name="static")
    app.mount("/", StaticFiles(directory=str(static), html=True, check_dir=False), name="frontend")
    return app


app = create_app()
