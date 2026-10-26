import json
import re
import httpx
from fastapi.testclient import TestClient
from test_backend import app, login, tool_provider
from test_public_research import fake_external, response


def test_voice_phrase_is_exact_bound_stable_and_not_chat_approval(app, monkeypatch):
    executed = fake_external(monkeypatch)
    from jarvis.main import create_app
    app = create_app()
    full_body = 'Long message: ' + 'important detail ' * 100
    app.state.llm_transport, _ = tool_provider('send_email', {'body': full_body})
    with TestClient(app) as client:
        login(client)
        data = client.post('/api/chat', json={'text': 'Send message'}).json()
        c = data['confirmation']
        assert re.fullmatch(r'approve [0-9]{4}', c['voice_phrase'])
        assert full_body in c['spoken_summary']
        assert 'send_email' in c['spoken_summary']
        repeated = client.post('/api/chat', json={'session_id': data['session_id'], 'text': c['voice_phrase']}).json()
        assert repeated['confirmation'] == c
        assert executed == []
        cookies = dict(client.cookies)
    restarted = create_app()
    restarted.state.llm_transport = httpx.MockTransport(lambda req: response({'content': 'Message result received.'}))
    with TestClient(restarted) as client:
        client.cookies.update(cookies)
        payload = {'id': c['id'], 'session_id': data['session_id'], 'approve': True}
        for bad in ['yes', c['voice_phrase'] + ' please', 'approve 99999']:
            assert client.post('/api/confirm', json={**payload, 'voice_phrase': bad}).status_code == 422
            assert executed == []
        accepted = client.post('/api/confirm', json={**payload, 'voice_phrase': c['voice_phrase']})
        assert accepted.status_code == 200, accepted.text
        assert executed == [('send_email', {'body': full_body})]
        assert client.post('/api/confirm', json=payload).status_code == 409


def test_confirm_synthesizes_actual_result_with_tools_disabled(app, monkeypatch):
    fake_external(monkeypatch)
    from jarvis.main import create_app
    app = create_app()
    app.state.llm_transport, _ = tool_provider('web_search', {'query': 'gold rate'})
    with TestClient(app) as client:
        login(client)
        data = client.post('/api/chat', json={'text': 'search'}).json()
        requests = []
        def summarize(req):
            payload = json.loads(req.content)
            requests.append(payload)
            assert 'tools' not in payload
            assert payload['messages'][-1]['role'] == 'tool'
            assert 'INR 100' in payload['messages'][-1]['content']
            return response({'content': 'The dated quote is 100 rupees per gram for 22-carat gold, not a current price.'})
        app.state.llm_transport = httpx.MockTransport(summarize)
        result = client.post('/api/confirm', json={'id': data['confirmation']['id'], 'session_id': data['session_id'], 'approve': True})
        assert result.status_code == 200
        assert len(requests) == 1
        assert '100 rupees' in result.json()['text']


def test_confirmation_summary_localized_without_changing_exact_arguments(app, monkeypatch):
    fake_external(monkeypatch)
    from jarvis.main import create_app
    app = create_app()
    app.state.llm_transport, _ = tool_provider('send_email', {'body': 'Exact English body'})
    with TestClient(app) as client:
        login(client)
        data = client.post('/api/chat', json={'text': 'அனுப்பு', 'language': 'ta-IN'}).json()
        summary = data['confirmation']['spoken_summary']
        assert 'அனுமதி' in summary
        assert 'Exact English body' in summary
        assert 'send_email' in summary
        seen = []
        def provider(req):
            payload = json.loads(req.content)
            seen.append(payload)
            assert 'ta-IN' in payload['messages'][0]['content']
            return response({'content': 'முடிவு கிடைத்தது.'})
        app.state.llm_transport = httpx.MockTransport(provider)
        payload = {'id': data['confirmation']['id'], 'session_id': data['session_id'], 'approve': True, 'language': 'ta-IN'}
        result = client.post('/api/confirm', json=payload).json()
        assert result['text'] == 'முடிவு கிடைத்தது.'
        assert len(seen) == 1


def test_provider_failure_after_confirmation_preserves_result_without_retry(app, monkeypatch):
    executed = fake_external(monkeypatch)
    from jarvis.main import create_app
    app = create_app()
    app.state.llm_transport, _ = tool_provider('send_email', {'body': 'once'})
    with TestClient(app) as client:
        login(client)
        data = client.post('/api/chat', json={'text': 'send'}).json()
        app.state.llm_transport = httpx.MockTransport(lambda req: httpx.Response(503))
        payload = {'id': data['confirmation']['id'], 'session_id': data['session_id'], 'approve': True}
        confirmed = client.post('/api/confirm', json=payload)
        assert confirmed.status_code == 200
        assert "couldn't summarize" in confirmed.json()['text']
        assert confirmed.json()['results']
        assert executed == [('send_email', {'body': 'once'})]
        assert client.post('/api/confirm', json=payload).status_code == 409


def test_summary_model_cannot_schedule_additional_actions(app, monkeypatch):
    executed = fake_external(monkeypatch)
    from jarvis.main import create_app
    app = create_app()
    app.state.llm_transport, _ = tool_provider('send_email', {'body': 'once'})
    with TestClient(app) as client:
        login(client)
        data = client.post('/api/chat', json={'text': 'send'}).json()
        from test_public_research import call
        app.state.llm_transport = httpx.MockTransport(lambda req: response(call('send_email', {'body': 'unapproved second action'})))
        payload = {'id': data['confirmation']['id'], 'session_id': data['session_id'], 'approve': True}
        confirmed = client.post('/api/confirm', json=payload)
        assert confirmed.status_code == 200
        assert executed == [('send_email', {'body': 'once'})]
        assert "couldn't summarize" in confirmed.json()['text']
