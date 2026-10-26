"""Actual neural TTS gateway fallback probe; bounded samples only."""
import asyncio,json
from pathlib import Path
import httpx
from dotenv import dotenv_values
ROOT=Path(__file__).resolve().parents[1];c=dotenv_values(ROOT/'.env')
async def main():
    async with httpx.AsyncClient(timeout=60) as h:
        for model in ['tts-mai-voice-2','tts-dragon-omni']:
            try:
                r=await h.post(c['JARVIS_LLM_BASE_URL']+'/audio/speech',headers={'Authorization':'Bearer '+c['JARVIS_LLM_API_KEY']},json={'model':model,'input':'Hello there. This is a neural voice, not the browser reader.','voice':'alloy','response_format':'mp3'})
                print(model,r.status_code,r.headers.get('content-type'),'bytes',len(r.content))
                if r.is_success and len(r.content)>1000 and 'json' not in r.headers.get('content-type',''):
                    (ROOT/'data'/('probe-'+model+'.mp3')).write_bytes(r.content)
            except Exception as e:print(model,type(e).__name__)
asyncio.run(main())
