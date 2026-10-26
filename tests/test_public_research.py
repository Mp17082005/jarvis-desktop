"""Isolated public research uses a real backend and mocked external boundaries."""
import json
import sys
import types

import httpx
import pytest
from fastapi.testclient import TestClient
from test_backend import app, login, tool_provider


def call(name, arguments):
    return {"role": "assistant", "tool_calls": [{"id": "c1", "type": "function", "function": {"name": name, "arguments": json.dumps(arguments)}}]}


def response(message):
    return httpx.Response(200, json={"choices": [{"message": message}]})


def fake_external(monkeypatch):
    executed = []
    def execute(name, args):
        executed.append((name, args))
        return {"results": [{"title": "Market report", "url": "https://public.example/rates", "snippet": "22 carat gold, 1 gram: INR 100. Published 2001-01-01."}]}
    schemas = []
    for name, arg in [("web_search", "query"), ("read_public_page", "url"), ("read_file", "path"), ("send_email", "body")]:
        schemas.append({"type": "function", "function": {"name": name, "parameters": {"type": "object", "properties": {arg: {"type": "string"}}, "required": [arg], "additionalProperties": False}}})
    monkeypatch.setitem(sys.modules, "jarvis_tools", types.SimpleNamespace(TOOLS=schemas, tool_risk=lambda name: name != "read_file", execute_tool=execute))
    return executed


def test_public_research_is_automatic_isolated_and_returns_sources(app, monkeypatch):
    executed = fake_external(monkeypatch)
    from jarvis.main import create_app
    app = create_app()
    requests = []
    def provider(request):
        payload = json.loads(request.content)
        requests.append(payload)
        if len(requests) == 1:
            return response(call("public_research", {"query": "gold rate in Chennai"}))
        if len(requests) == 2:
            assert payload["messages"][1:] == [{"role": "user", "content": "gold rate in Chennai"}]
            assert {t["function"]["name"] for t in payload["tools"]} == {"web_search", "read_public_page"}
            return response(call("web_search", {"query": "gold rate in Chennai"}))
        if len(requests) == 3:
            return response({"content": "The source is from 2001, so I cannot verify today's price."})
        return response({"content": "I couldn't verify today's gold price; the available quote is outdated."})
    app.state.llm_transport = httpx.MockTransport(provider)
    with TestClient(app) as client:
        login(client)
        owner = app.state.store.session(client.cookies.get('jarvis_session'), 'test-token-that-is-long-enough')['id']
        sid, _ = app.state.store.conversation(owner)
        app.state.store.save(sid, [{'role': 'user', 'content': 'OUTER_ONLY_CONTEXT'}])
        result = client.post("/api/chat", json={"text": "gold rate in Chennai", 'session_id': sid})
        assert result.status_code == 200, result.text
        data = result.json()
        assert "confirmation" not in data
        research = data["results"][0]
        assert research["tool"] == "public_research"
        assert research["result"]["sources"] == ["https://public.example/rates"]
        assert research["result"]["evidence"][0]["tool"] == "web_search"
        assert "OUTER_ONLY_CONTEXT" not in json.dumps(requests[1:3])
        assert "outdated" in data["text"]
        assert executed == [("web_search", {"query": "gold rate in Chennai"}), ("read_public_page", {"url": "https://public.example/rates"})]


def test_private_taint_survives_history_trimming_and_restart(app, monkeypatch):
    executed = fake_external(monkeypatch)
    from jarvis.main import create_app
    app = create_app()
    app.state.llm_transport, _ = tool_provider("read_file", {"path": "note.txt"})
    with TestClient(app) as client:
        login(client)
        data = client.post("/api/chat", json={"text": "Read note"}).json()
        sid, cookies = data["session_id"], dict(client.cookies)
        app.state.store.save(sid, [{"role": "user", "content": "new public question"}])
    restarted = create_app()
    raw_question = "  What's the gold rate in Chennai?\n"
    requests, worker_requests = [], []
    def provider(request):
        payload = json.loads(request.content)
        requests.append(payload)
        outer = any(t['function']['name'] == 'public_research' for t in payload.get('tools', []))
        if outer:
            if payload['messages'][-1]['role'] == 'tool':
                return response({'content': 'The source is outdated; today is unverified.'})
            return response(call('public_research', {'query': 'PRIVATE_MARKER'}))
        worker_requests.append(payload)
        if len(worker_requests) == 1:
            return response(call('web_search', {'query': payload['messages'][1]['content']}))
        return response({'content': 'The source is outdated; today is unverified.'})
    restarted.state.llm_transport = httpx.MockTransport(provider)
    with TestClient(restarted) as client:
        client.cookies.update(cookies)
        result = client.post('/api/chat', json={'text': raw_question, 'session_id': sid})
        assert result.status_code == 200, result.text
        data = result.json()
        assert 'confirmation' not in data
        assert restarted.state.store.private_tainted(sid)
        assert worker_requests[0]['messages'][1:] == [{'role': 'user', 'content': raw_question}]
        assert 'PRIVATE_MARKER' not in json.dumps(worker_requests)
        assert executed == [('read_file', {'path': 'note.txt'}), ('web_search', {'query': raw_question}), ('read_public_page', {'url': 'https://public.example/rates'})]
        actual_call = requests[-1]['messages'][-2]['tool_calls'][0]
        assert json.loads(actual_call['function']['arguments']) == {'query': raw_question}
        with restarted.state.store.connect() as db:
            saved = json.loads(db.execute('SELECT messages FROM conversations WHERE id=?', (sid,)).fetchone()['messages'])
        saved_call = next(m['tool_calls'][0] for m in saved if m.get('tool_calls'))
        assert json.loads(saved_call['function']['arguments']) == {'query': raw_question}
