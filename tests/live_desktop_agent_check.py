"""Live check: Agent against the real provider; time + real YouTube playback. Never prints the key."""
import sys
import tempfile
import time
from pathlib import Path

QUERY = sys.argv[1] if len(sys.argv) > 1 else 'play lofi hip hop on youtube'
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def env():
    values = {}
    for line in (ROOT / '.env').read_text(encoding='utf-8').splitlines():
        if '=' in line and not line.lstrip().startswith('#'):
            k, v = line.split('=', 1)
            values[k.strip()] = v.strip().strip("'\"")
    return values


def main():
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    from jarvis_desktop.agent import Agent
    e = env()
    data = Path(tempfile.mkdtemp(prefix='jarvis-live-'))
    cfg = {'base_url': e['JARVIS_LLM_BASE_URL'], 'api_key': e['JARVIS_LLM_API_KEY'], 'chat_model': 'claude-sonnet-5',
           'autonomy': 'balanced', 'language': 'English', 'data_dir': data, 'ufo_home': None}
    asked = []
    agent = Agent(cfg, print, lambda q: asked.append(q) or False)
    try:
        t = time.monotonic()
        print('TIME REPLY:', agent.handle('what time is it'), f'({time.monotonic() - t:.1f}s)', agent.last_tool_calls)
        t = time.monotonic()
        print('PLAY REPLY:', agent.handle(QUERY), f'({time.monotonic() - t:.1f}s)', agent.last_tool_calls)
        yt = agent.tools._youtube
        assert yt is not None, 'youtube tool was not used'
        s1 = yt.execute('state', {})
        time.sleep(5)
        s2 = yt.execute('state', {})
        assert yt.execute('state', {}).get('video') is not None, 'no video'
        print('STATE1:', {k: s1.get(k) for k in ('video', 'title', 'watching', 'ad_showing', 'auto_skips')})
        print('STATE2:', {k: s2.get(k) for k in ('video', 'title', 'watching', 'ad_showing', 'auto_skips')})
        advancing = s1['video'] and s2['video'] and s2['video']['currentTime'] > s1['video']['currentTime'] and not s2['video']['paused']
        print('PLAYBACK ADVANCING:', bool(advancing), 'confirm asked:', asked)
        print('PAUSE REPLY:', agent.handle('pause'))
        print('STATE3 paused:', yt.execute('state', {})['video']['paused'])
    finally:
        agent.close()


if __name__ == '__main__':
    main()
