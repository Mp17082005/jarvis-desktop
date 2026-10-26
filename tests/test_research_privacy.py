"""Raw user-turn isolation and retirement of unsafe legacy approvals."""
import json

import httpx
import pytest
from fastapi.testclient import TestClient
from test_backend import app, login, tool_provider
from test_public_research import call, response, fake_external


def seed_conversation(app, client):
    owner = app.state.store.session(client.cookies.get('jarvis_session'), 'test-token-that-is-long-enough')['id']
    sid, _ = app.state.store.conversation(owner)
    app.state.store.save(sid, [{'role': 'user', 'content': 'PRIVATE_MARKER earlier conversation'}])
    app.state.store.taint(sid)
    return owner, sid


def test_new_chat_cancels_legacy_research_without_executing_old_args(app, monkeypatch):
    executed = fake_external(monkeypatch)
    from jarvis.main import create_app
    app = create_app()
    worker_queries = []
    async def research(config, query, schemas, execute, transport=None):
        worker_queries.append(query)
        return {'answer': 'Public result', 'sources': []}
    monkeypatch.setattr('jarvis.main.public_research', research)
    app.state.llm_transport, _ = tool_provider('public_research', {'query': 'PRIVATE_MARKER generated query'})
    with TestClient(app) as client:
        login(client)
        owner, sid = seed_conversation(app, client)
        pending = app.state.store.propose(owner, sid, 'public_research', {'query': 'PRIVATE_MARKER legacy query'})
        result = client.post('/api/chat', json={'session_id': sid, 'text': 'gold price today'})
        assert result.status_code == 200, result.text
        assert 'confirmation' not in result.json()
        assert worker_queries == ['gold price today']
        assert executed == []
        assert app.state.store.pending(owner, sid) is None
        replay = client.post('/api/confirm', json={'session_id': sid, 'id': pending['id'], 'approve': True})
        assert replay.status_code == 409
        assert worker_queries == ['gold price today']
        assert app.state.store.private_tainted(sid)


@pytest.mark.parametrize('approve', [True, False])
def test_legacy_research_confirmation_is_cancelled_not_executed(app, monkeypatch, approve):
    executed = []
    async def research(*args, **kwargs):
        executed.append(args)
        return {'answer': 'Must not run'}
    monkeypatch.setattr('jarvis.main.public_research', research)
    with TestClient(app) as client:
        login(client)
        owner, sid = seed_conversation(app, client)
        pending = app.state.store.propose(owner, sid, 'public_research', {'query': 'PRIVATE_MARKER'})
        result = client.post('/api/confirm', json={'session_id': sid, 'id': pending['id'], 'approve': approve})
        assert result.status_code == 200, result.text
        assert executed == []
        assert result.json()['results'] == []
        assert 'new question' in result.json()['text']
        assert app.state.store.pending(owner, sid) is None
        assert client.post('/api/confirm', json={'session_id': sid, 'id': pending['id'], 'approve': True}).status_code == 409


@pytest.mark.parametrize('same_batch', [True, False])
def test_private_read_then_research_uses_raw_turn_only(app, monkeypatch, same_batch):
    fake_external(monkeypatch)
    import jarvis_tools
    from jarvis.main import create_app
    app = create_app()
    outbound, worker_requests = [], []
    def execute(name, args):
        if name == 'read_file':
            return {'content': 'PRIVATE_MARKER private note'}
        outbound.append((name, args))
        return {'results': [{'snippet': 'Public evidence'}]}
    monkeypatch.setattr(jarvis_tools, 'execute_tool', execute)
    raw_question = 'Read my note, then check the gold price in Chennai.'
    def provider(request):
        payload = json.loads(request.content)
        outer = any(t['function']['name'] == 'public_research' for t in payload.get('tools', []))
        if not outer:
            worker_requests.append(payload)
            if len(worker_requests) == 1:
                return response(call('web_search', {'query': payload['messages'][1]['content']}))
            return response({'content': 'Only public evidence is available.'})
        if payload['messages'][-1]['role'] == 'user':
            message = call('read_file', {'path': 'note.txt'})
            if same_batch:
                research_call = call('public_research', {'query': 'PRIVATE_MARKER'})['tool_calls'][0]
                research_call['id'] = 'research1'
                message['tool_calls'].append(research_call)
            return response(message)
        if not same_batch and not worker_requests:
            return response(call('public_research', {'query': 'PRIVATE_MARKER'}))
        return response({'content': 'Only public evidence is available.'})
    app.state.llm_transport = httpx.MockTransport(provider)
    with TestClient(app) as client:
        login(client)
        result = client.post('/api/chat', json={'text': raw_question})
        assert result.status_code == 200, result.text
        assert 'confirmation' not in result.json()
        assert worker_requests[0]['messages'][1:] == [{'role': 'user', 'content': raw_question}]
        assert 'PRIVATE_MARKER' not in json.dumps(worker_requests)
        assert outbound == [('web_search', {'query': raw_question})]
        assert app.state.store.private_tainted(result.json()['session_id'])


@pytest.mark.parametrize('websocket', [True, False])
def test_ambiguous_followup_does_not_fill_query_from_private_history(app, monkeypatch, websocket):
    executed = fake_external(monkeypatch)
    from jarvis.main import create_app
    app = create_app()
    worker_requests = []
    raw_question = '  What about that?\n'
    def provider(request):
        payload = json.loads(request.content)
        if any(t['function']['name'] == 'public_research' for t in payload.get('tools', [])):
            return response(call('public_research', {'query': 'PRIVATE_MARKER inferred subject'}))
        worker_requests.append(payload)
        return response({'content': 'Please specify the public subject to research.'})
    app.state.llm_transport = httpx.MockTransport(provider)
    with TestClient(app) as client:
        login(client)
        _, sid = seed_conversation(app, client)
        body = {'session_id': sid, 'text': raw_question}
        if websocket:
            with client.websocket_connect('/ws') as ws:
                ws.send_json({'type': 'chat', **body})
                data = ws.receive_json()
                assert data['type'] == 'reply'
        else:
            result = client.post('/api/chat', json=body)
            assert result.status_code == 200, result.text
            data = result.json()
        assert 'confirmation' not in data
        assert data['results'][0]['result']['error']
        assert worker_requests[0]['messages'][1:] == [{'role': 'user', 'content': raw_question}]
        assert 'PRIVATE_MARKER' not in json.dumps(worker_requests)
        assert executed == []


@pytest.mark.parametrize('name,args', [
    ('web_search', {'query': 'PRIVATE_MARKER'}),
    ('read_public_page', {'url': 'https://public.example/?secret=PRIVATE_MARKER'}),
    ('send_email', {'body': 'PRIVATE_MARKER'}),
])
def test_other_pending_actions_still_block_new_public_research(app, monkeypatch, name, args):
    executed = fake_external(monkeypatch)
    from jarvis.main import create_app
    app = create_app()
    app.state.llm_transport, requests = tool_provider('public_research', {'query': 'gold price'})
    with TestClient(app) as client:
        login(client)
        owner, sid = seed_conversation(app, client)
        pending = app.state.store.propose(owner, sid, name, args)
        result = client.post('/api/chat', json={'session_id': sid, 'text': 'gold price'})
        assert result.status_code == 200, result.text
        assert result.json()['confirmation']['id'] == pending['id']
        assert app.state.store.pending(owner, sid)['arguments'] == args
        assert requests == []
        assert executed == []
