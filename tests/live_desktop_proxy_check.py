"""Live check: jarvis_desktop.proxy on 8781 forwarding to the real gateway with UFO-style params."""
import sys
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main():
    from jarvis_desktop.proxy import start_proxy
    env = {}
    for line in (ROOT / '.env').read_text(encoding='utf-8').splitlines():
        if '=' in line and not line.lstrip().startswith('#'):
            k, v = line.split('=', 1)
            env[k.strip()] = v.strip().strip("'\"")
    with start_proxy(env['JARVIS_LLM_BASE_URL'], env['JARVIS_LLM_API_KEY'], port=8781) as proxy:
        print('HEALTH:', httpx.get('http://127.0.0.1:8781/health').json())
        body = {'model': 'claude-sonnet-5', 'messages': [{'role': 'user', 'content': 'Reply with exactly: proxy ok'}],
                'max_tokens': 20, 'temperature': 0, 'top_p': 0, 'n': 1}
        t = time.monotonic()
        r = httpx.post(proxy.url + '/chat/completions', json=body, timeout=60)
        print('NONSTREAM:', r.status_code, repr(r.json()['choices'][0]['message']['content']), f'{time.monotonic() - t:.1f}s')
        chunks = []
        with httpx.stream('POST', proxy.url + '/chat/completions', json={**body, 'stream': True}, timeout=60) as s:
            for line in s.iter_lines():
                chunks.append(line)
        print('STREAM:', s.status_code, s.headers.get('content-type'), 'events:', sum(1 for c in chunks if c.startswith('data:')), 'done:', any('[DONE]' in c for c in chunks))
        direct = httpx.post(env['JARVIS_LLM_BASE_URL'].rstrip('/') + '/chat/completions', json=body, timeout=60,
                            headers={'Authorization': 'Bearer ' + env['JARVIS_LLM_API_KEY']})
        print('DIRECT WITHOUT PROXY (temperature=0, top_p=0):', direct.status_code)


if __name__ == '__main__':
    main()
