"""External query arguments must not bypass explicit approval."""
import json
import httpx
import pytest
from fastapi.testclient import TestClient

@pytest.mark.parametrize('name,args', [('web_search', {'query':'PRIVATE_MARKER'}), ('weather', {'city':'PRIVATE_MARKER'}), ('read_public_page', {'url':'https://example.com/?secret=PRIVATE_MARKER'})])
def test_external_queries_require_approval_after_private_read(tmp_path, monkeypatch, name, args):
    monkeypatch.setenv('JARVIS_ACCESS_TOKEN','private-test-token')
    monkeypatch.setenv('JARVIS_DATA_DIR',str(tmp_path/'db'))
    monkeypatch.setenv('JARVIS_WORKSPACE_DIR',str(tmp_path))
    monkeypatch.setenv('JARVIS_LLM_BASE_URL','https://example.invalid/v1')
    monkeypatch.setenv('JARVIS_LLM_MODEL','test')
    (tmp_path/'note.txt').write_text('PRIVATE_MARKER')
    import jarvis_tools
    outbound=[]
    monkeypatch.setattr(jarvis_tools, '_http', lambda *a, **k: outbound.append(a) or '{}')
    from jarvis.main import create_app
    app=create_app()
    def provider(request):
        messages=json.loads(request.content)['messages']
        if messages[-1]['role']=='tool':
            tool,arguments=name,args
        else:
            tool,arguments='read_file',{'path':'note.txt'}
        return httpx.Response(200,json={'choices':[{'message':{'role':'assistant','content':None,'tool_calls':[{'id':'call'+str(len(messages)),'type':'function','function':{'name':tool,'arguments':json.dumps(arguments)}}]}}]})
    app.state.llm_transport=httpx.MockTransport(provider)
    with TestClient(app) as client:
        assert client.post('/api/login',json={'token':'private-test-token'}).status_code==200
        result=client.post('/api/chat',json={'text':'Read my note'}).json()
        assert outbound==[], 'Private data leaked before approval'
        assert result['confirmation']['tool']==name
        assert result['confirmation']['arguments']==args
