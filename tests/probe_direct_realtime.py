"""Check direct provider WebSocket using only ephemeral token."""
import asyncio,json
from pathlib import Path
import httpx,websockets
from dotenv import dotenv_values
c=dotenv_values(Path(__file__).resolve().parents[1]/'.env')
async def main():
    async with httpx.AsyncClient(timeout=30) as h:
        r=await h.post(c['JARVIS_LLM_BASE_URL']+'/realtime/client_secrets',headers={'Authorization':'Bearer '+c['JARVIS_LLM_API_KEY']},json={'session':{'type':'realtime','model':'gpt-realtime-2.1'}})
        r.raise_for_status();token=r.json()['value']
    try:
        async with websockets.connect('wss://api.openai.com/v1/realtime?model=gpt-realtime-2.1',additional_headers={'Authorization':'Bearer '+token},open_timeout=25,max_size=2000000) as ws:
            event=json.loads(await asyncio.wait_for(ws.recv(),20));print('connected',event.get('type'));print('initial error',event.get('error',{}).get('code'),event.get('error',{}).get('message','')[:500])
            await ws.send(json.dumps({'type':'response.create','response':{'instructions':'Say exactly: Jarvis realtime audio is working.'}}))
            size=0; types=set(); transcript=''
            for i in range(300):
                e=json.loads(await asyncio.wait_for(ws.recv(),30));types.add(e['type'])
                if e['type'] in ['response.audio.delta','response.output_audio.delta']:size+=len(e.get('delta',''))
                if e['type'] in ['response.audio_transcript.done','response.output_audio_transcript.done']:transcript=e.get('transcript','')
                if e['type']=='error':print('error_code',e.get('error',{}).get('code'));break
                if e['type']=='response.done':break
            print(json.dumps({'audio_base64_chars':size,'transcript':transcript,'events':sorted(types)}))
    except Exception as e:print('failed',type(e).__name__,getattr(getattr(e,'response',None),'status_code',None))
asyncio.run(main())
