import asyncio,json
from pathlib import Path
from dotenv import dotenv_values
import httpx,websockets
c=dotenv_values(Path(__file__).resolve().parents[1]/'.env')
async def main():
    base=c['JARVIS_LLM_BASE_URL']; key=c['JARVIS_LLM_API_KEY']
    async with httpx.AsyncClient(timeout=30) as h:
        r=await h.post(base+'/realtime/client_secrets',headers={'Authorization':'Bearer '+key},json={'session':{'type':'realtime','model':'gpt-realtime-2.1'}});r.raise_for_status();token=r.json()['value']
    for label,credential in [('ephemeral',token),('server',key)]:
        try:
            async with websockets.connect(base.replace('https://','wss://')+'/realtime?model=gpt-realtime-2.1',additional_headers={'Authorization':'Bearer '+credential},open_timeout=20) as ws:
                for i in range(2):
                    e=json.loads(await asyncio.wait_for(ws.recv(),15)); print(label,'event',e.get('type'),'code',e.get('error',{}).get('code'))
                    if e.get('type')=='session.created':break
        except Exception as e:
            reason=str(getattr(e,'reason','') or getattr(getattr(e,'rcvd',None),'reason',''))
            for secret in [key,token]:reason=reason.replace(secret,'[redacted]')
            print(label,type(e).__name__,getattr(e,'code',None),'reason',reason[:700])
asyncio.run(main())
