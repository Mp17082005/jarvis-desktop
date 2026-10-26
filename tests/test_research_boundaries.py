"""Security boundaries for public worker and persisted conversation taint."""
import json
import sqlite3
import httpx
import pytest
from fastapi.testclient import TestClient
from test_backend import app, login, tool_provider
from test_public_research import call, response, fake_external


def test_legacy_conversations_are_conservatively_tainted(tmp_path):
    from jarvis.store import Store
    with sqlite3.connect(tmp_path / 'jarvis.sqlite3') as db:
        db.execute('CREATE TABLE conversations (id TEXT PRIMARY KEY, owner TEXT NOT NULL, messages TEXT NOT NULL)')
        db.execute('INSERT INTO conversations VALUES (?,?,?)', ('old', 'owner', '[]'))
    store = Store(tmp_path)
    assert store.private_tainted('old') is True
    sid, _ = store.conversation('owner')
    assert store.private_tainted(sid) is False
    assert Store(tmp_path).private_tainted('old') is True
    assert store.private_tainted('missing') is True


@pytest.mark.parametrize('private_tool', ['read_file', 'send_email', 'public_research', 'set_timer'])
def test_public_worker_rejects_private_tools_before_execution(app, monkeypatch, private_tool):
    executed = fake_external(monkeypatch)
    from jarvis.main import create_app
    app = create_app()
    requests = []
    def provider(req):
        requests.append(json.loads(req.content))
        if len(requests) == 1:
            return response(call('public_research', {'query': 'public question'}))
        return response(call(private_tool, {'path': 'secrets'}))
    app.state.llm_transport = httpx.MockTransport(provider)
    with TestClient(app) as client:
        login(client)
        data = client.post('/api/chat', json={'text': 'public question'}).json()
        assert 'non-public tool' in data['results'][0]['result']['error']
        assert executed == []


def test_private_taint_is_written_before_failing_execution(app, monkeypatch):
    fake_external(monkeypatch)
    import jarvis_tools
    from jarvis.main import create_app
    app = create_app()
    def failing(name, args):
        with app.state.store.connect() as db:
            rows = db.execute('SELECT private_tainted FROM conversations').fetchall()
            assert rows[0]['private_tainted'] == 1
        raise RuntimeError('Private result may already have been seen')
    monkeypatch.setattr(jarvis_tools, 'execute_tool', failing)
    app.state.llm_transport, _ = tool_provider('read_file', {'path': 'secret'})
    with TestClient(app) as client:
        login(client)
        data = client.post('/api/chat', json={'text': 'read'}).json()
        assert app.state.store.private_tainted(data['session_id'])


def test_worker_has_six_round_hard_limit(app, monkeypatch):
    executed = fake_external(monkeypatch)
    from jarvis.main import create_app
    app = create_app()
    requests = []
    def provider(req):
        p = json.loads(req.content)
        requests.append(p)
        if len(requests) == 1:
            return response(call('public_research', {'query': 'a public fact'}))
        if any(t['function']['name'] == 'public_research' for t in p.get('tools', [])):
            return response({'content': 'The research limit was reached.'})
        return response(call('web_search', {'query': 'a public fact'}))
    app.state.llm_transport = httpx.MockTransport(provider)
    with TestClient(app) as client:
        login(client)
        data = client.post('/api/chat', json={'text': 'research'}).json()
        assert data['results'][0]['result']['incomplete'] is True
        assert len(executed) == 6


def test_worker_cannot_report_unsupported_answer_without_evidence(app, monkeypatch):
    fake_external(monkeypatch)
    from jarvis.main import create_app
    app = create_app()
    requests = []
    def provider(req):
        requests.append(req)
        if len(requests) == 1:
            return response(call('public_research', {'query': 'gold today'}))
        return response({'content': 'Gold is exactly 100 rupees today.'})
    app.state.llm_transport = httpx.MockTransport(provider)
    with TestClient(app) as client:
        login(client)
        data = client.post('/api/chat', json={'text': 'gold today'}).json()
        assert data['results'][0]['result'].get('error')
        assert '100 rupees' not in data['text']
