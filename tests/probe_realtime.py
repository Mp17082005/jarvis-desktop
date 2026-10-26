"""Bounded real gateway capability probe. Never print keys or session secrets."""
import asyncio,json
from pathlib import Path
import httpx
import websockets
from dotenv import dotenv_values
ROOT=Path(__file__).resolve().parents[1]
c=dotenv_values(ROOT/'.env')
base=c['JARVIS_LLM_BASE_URL'].rstrip('/')
key=c['JARVIS_LLM_API_KEY']
async def main():
    report=[]
    async with httpx.AsyncClient(timeout=30) as client:
        for path,body in [('/realtime/client_secrets',{'session':{'type':'realtime','model':'gpt-realtime-2.1'}}),('/realtime/sessions',{'model':'gpt-realtime-2.1'})]:
            try:
                r=await client.post(base+path,headers={'Authorization':'Bearer '+key},json=body)
                report.append({'path':path,'status':r.status_code,'response_keys':list(r.json()) if 'json' in r.headers.get('content-type','') else [],'content_type':r.headers.get('content-type')})
            except Exception as e:report.append({'path':path,'error':type(e).__name__})
    for model in ['gpt-realtime-2.1','gpt-realtime-2.1-pool']:
        try:
            async with websockets.connect(base.replace('https://','wss://')+'/realtime?model='+model,additional_headers={'Authorization':'Bearer '+key,'OpenAI-Beta':'realtime=v1'},open_timeout=25,max_size=2000000) as ws:
                event=json.loads(await asyncio.wait_for(ws.recv(),20))
                report.append({'model':model,'event_type':event.get('type'),'session_model':event.get('session',{}).get('model'),'error_code':event.get('error',{}).get('code')})
        except Exception as e:
            report.append({'model':model,'error':type(e).__name__,'status':getattr(getattr(e,'response',None),'status_code',None)})
    print(json.dumps(report,indent=2))
    (ROOT/'data/realtime-capability.json').write_text(json.dumps(report,indent=2))
asyncio.run(main())
