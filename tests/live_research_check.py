"""Opt-in actual provider research check, no fabricated source data."""
import json
import os
from pathlib import Path
import httpx
from dotenv import dotenv_values
ROOT = Path(__file__).resolve().parents[1]
base = os.getenv('JARVIS_TEST_URL', 'http://127.0.0.1:8765')
client = httpx.Client(base_url=base, timeout=240, headers={'Origin':base})
client.post('/api/login',json={'token':dotenv_values(ROOT/'.env')['JARVIS_ACCESS_TOKEN']}).raise_for_status()
response = client.post('/api/chat', json={'text':'What is the gold price in India today? Research current sources. Tell me the 22 and 24 carat price per gram in rupees, the source date, and whether taxes are included. If today cannot be verified, say that clearly. Give the answer, not just links.', 'language':'en-IN'})
response.raise_for_status()
result = response.json()
assert not result.get('confirmation'), 'Public research unexpectedly requires manual approval'
assert result.get('text'), 'Missing spoken answer'
assert result.get('results'), 'No tool evidence: current answer must use research'
path = ROOT/'data/live-gold-research.json'
path.write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
print(json.dumps({'answer':result['text'],'evidence_file':str(path),'top_level_tools':[r.get('tool') for r in result['results']]},ensure_ascii=False))
