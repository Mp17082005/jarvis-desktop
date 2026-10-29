"""Desktop agent brain: tool routing, confirmation policy, jobs, scheduler, memory, proxy."""
import json
import threading
import time
from datetime import datetime, timedelta

import httpx
import pytest

from jarvis_desktop import agent as agent_mod
from jarvis_desktop.agent import Agent, needs_confirmation, speech_safe, fast_route


# ---------------------------------------------------------------------------- fakes
class FakeLLM:
    """Scripted OpenAI-compatible endpoint. Each script item: tool calls list or final text."""

    def __init__(self, script):
        self.script = list(script)
        self.requests = []

    def __call__(self, request):
        body = json.loads(request.content)
        self.requests.append(body)
        step = self.script.pop(0) if self.script else 'Done.'
        if isinstance(step, str):
            message = {'role': 'assistant', 'content': step}
        else:
            message = {'role': 'assistant', 'content': None, 'tool_calls': [
                {'id': f'c{i}', 'type': 'function', 'function': {'name': n, 'arguments': json.dumps(a)}} for i, (n, a) in enumerate(step)]}
        return httpx.Response(200, json={'choices': [{'message': message}]})


class FakeToolbox:
    def __init__(self):
        self.calls = []
        self.jobs = self.memory = self.scheduler = self._research = None

    def execute(self, name, args, source='user'):
        self.calls.append((name, args, source))
        if name == 'ufo_task':
            return {'ok': True, 'job_id': 'j1', 'status': 'started'}
        if name == 'remember':
            self.memory.remember(args['fact'])
        if name == 'schedule_task':
            return self.scheduler.create(args['task_request'], args['when'], args.get('tool') or 'ufo_task', {'request': args['task_request']})
        return {'ok': True}

    def close(self):
        pass


class FakeJobs:
    announce = None

    def start(self):
        return self

    def close(self):
        pass


def make(tmp_path, script, autonomy='balanced', answers=(), clock=None):
    llm = FakeLLM(script)
    asked = []
    replies = list(answers)

    def confirm(question):
        asked.append(question)
        return replies.pop(0) if replies else False
    cfg = {'base_url': 'https://llm.test/v1', 'api_key': 'sk-test', 'chat_model': 'claude-sonnet-5', 'autonomy': autonomy,
           'language': 'English', 'data_dir': tmp_path, 'ufo_home': None}
    tb = FakeToolbox()
    a = Agent(cfg, lambda t: None, confirm, transport=httpx.MockTransport(llm), toolbox=tb, jobs=FakeJobs(),
              clock=clock or time.time, start_background=False)
    return a, llm, tb, asked


# ---------------------------------------------------------------------------- routing
def test_time_tool_round_trip_and_no_sampling_params(tmp_path):
    a, llm, tb, _ = make(tmp_path, [[('time', {})], 'It is **10:30** right now. 🙂'])
    reply = a.handle('what time is it')
    assert reply == 'It is 10:30 right now.'
    assert tb.calls == [('time', {}, 'user')]
    body = llm.requests[0]
    assert body['model'] == 'claude-sonnet-5'
    assert not {'temperature', 'top_p', 'n'} & set(body)
    names = {t['function']['name'] for t in body['tools']}
    assert {'youtube_play', 'youtube_control', 'open_app', 'open_url', 'ufo_task', 'schedule_task', 'remember',
            'system_volume', 'web_research', 'job_status', 'cancel_job', 'list_schedules', 'cancel_schedule',
            'workbook_read', 'workbook_write', 'workbook_create', 'weather', 'time'} <= names
    system = body['messages'][0]['content']
    assert 'youtube_play' in system and 'ufo_task' in system and 'Current local time' in system
    assert llm.requests[1]['messages'][-1]['role'] == 'tool'


def test_youtube_request_runs_without_confirmation_in_balanced(tmp_path):
    a, _, tb, asked = make(tmp_path, [[('youtube_play', {'query': 'lofi hip hop'})], 'Playing lofi hip hop.'])
    assert a.handle('play lofi hip hop on youtube') == 'Playing lofi hip hop.'
    assert tb.calls[0][:2] == ('youtube_play', {'query': 'lofi hip hop'}) and asked == []


def test_simple_ufo_requests_are_fast_routed():
    assert fast_route('ufo_task', {'request': 'open notepad'}) == ('open_app', {'app': 'notepad'})
    assert fast_route('ufo_task', {'request': 'play relaxing jazz music on YouTube'}) == ('youtube_play', {'query': 'relaxing jazz music'})
    assert fast_route('ufo_task', {'request': 'open notepad and type hello and save it'})[0] == 'ufo_task'
    assert fast_route('ufo_task', {'request': 'open outlook and send the report'})[0] == 'ufo_task'


def test_player_shortcut_skips_llm(tmp_path):
    a, llm, tb, _ = make(tmp_path, [])
    assert a.handle('pause') == 'Paused.'
    assert a.handle('skip the ad') == 'Skipped the ad.'
    assert llm.requests == [] and [c[1]['action'] for c in tb.calls] == ['pause', 'skip_ad']


@pytest.mark.parametrize('said,query', [
    ('Play some lofi music on YouTube', 'some lofi music'),
    ('Jarvis, play Believer by Imagine Dragons.', 'Believer by Imagine Dragons'),
    ('play rain sounds on youtube', 'rain sounds'),
    ('put on some jazz', 'some jazz'),
])
def test_play_request_starts_youtube_without_llm(tmp_path, said, query):
    # Two LLM round trips (~3.5 s) used to precede every play request.
    a, llm, tb, asked = make(tmp_path, [])
    reply = a.handle(said)
    assert llm.requests == [] and asked == []
    assert tb.calls == [('youtube_play', {'query': query}, 'user')]
    assert reply.startswith('Playing')


@pytest.mark.parametrize('said', ['play the next episode in Netflix', 'play my voicemail', 'play chess with me',
                                  'what should I play tonight'])
def test_non_youtube_play_requests_still_go_to_the_model(tmp_path, said):
    a, llm, tb, _ = make(tmp_path, ['Sure.'])
    a.handle(said)
    assert len(llm.requests) == 1


def test_play_shortcut_failure_lets_the_model_explain(tmp_path):
    a, llm, tb, _ = make(tmp_path, ["YouTube wants you to sign in first."])
    tb.execute = lambda name, args, source='user': (tb.calls.append((name, args, source)),
                                                    {'ok': False, 'error': 'human_required'})[1]
    assert a.handle('play lofi on youtube') == "YouTube wants you to sign in first."
    assert len(llm.requests) == 1


def test_careful_mode_play_still_confirms(tmp_path):
    a, llm, tb, asked = make(tmp_path, [[('youtube_play', {'query': 'jazz'})], 'Okay.'], autonomy='careful', answers=[False])
    a.handle('play jazz on youtube')
    assert asked and tb.calls == []


def test_parallel_fast_tools_in_one_step(tmp_path):
    a, _, tb, _ = make(tmp_path, [[('weather', {'city': 'Pune'}), ('time', {})], 'Sunny, 10 30.'])
    a.handle('weather in pune and the time')
    assert sorted(c[0] for c in tb.calls) == ['time', 'weather']


# ---------------------------------------------------------------------------- confirmation policy
@pytest.mark.parametrize('autonomy,name,args,expected', [
    ('balanced', 'ufo_task', {'request': 'open notepad and write hello'}, False),
    ('balanced', 'ufo_task', {'request': 'email the report to Sam'}, True),
    ('balanced', 'ufo_task', {'request': 'uninstall Zoom'}, True),
    ('balanced', 'ufo_task', {'request': 'submit the form'}, True),
    ('balanced', 'ufo_task', {'request': 'order a pizza'}, True),
    ('balanced', 'schedule_task', {'task_request': 'play news', 'when': {'every_minutes': 60}}, True),
    ('balanced', 'youtube_play', {'query': 'x'}, False),
    ('balanced', 'open_app', {'app': 'excel'}, False),
    ('balanced', 'system_volume', {'level': 20}, False),
    ('balanced', 'time', {}, False),
    ('careful', 'youtube_play', {'query': 'x'}, True),
    ('careful', 'open_app', {'app': 'excel'}, True),
    ('careful', 'ufo_task', {'request': 'open notepad'}, True),
    ('careful', 'web_research', {'question': 'x'}, False),
    ('careful', 'time', {}, False),
    ('autonomous', 'ufo_task', {'request': 'uninstall Zoom'}, False),
    ('autonomous', 'ufo_task', {'request': 'install VLC'}, False),
    ('autonomous', 'ufo_task', {'request': 'send a message to mom'}, True),
    ('autonomous', 'ufo_task', {'request': 'buy the cheapest charger'}, True),
    ('autonomous', 'ufo_task', {'request': 'delete old screenshots'}, True),
    ('autonomous', 'ufo_task', {'request': 'pay the electricity bill'}, True),
    ('autonomous', 'schedule_task', {'task_request': 'play news', 'when': {'every_minutes': 60}}, False),
    ('autonomous', 'schedule_task', {'task_request': 'send the daily report', 'when': {'every_minutes': 60}}, True),
    # Words that merely contain a risky stem in past tense / as a noun are not requests to do it.
    ('balanced', 'ufo_task', {'request': 'type: Installed Jarvis works and save it'}, False),
    ('balanced', 'ufo_task', {'request': 'open the file about removed items'}, False),
    ('balanced', 'ufo_task', {'request': 'open my ordered list in notepad'}, False),
    ('balanced', 'ufo_task', {'request': 'make the formatted heading bold'}, False),
    ('balanced', 'ufo_task', {'request': 'install VLC'}, True),
    ('balanced', 'ufo_task', {'request': 'remove the old shortcuts'}, True),
    ('balanced', 'ufo_task', {'request': 'reformat nothing, just format the C drive'}, True),
    ('autonomous', 'ufo_task', {'request': 'write deleted scenes in notepad'}, False),
    ('autonomous', 'ufo_task', {'request': 'delete old screenshots'}, True),
])
def test_confirmation_matrix(autonomy, name, args, expected):
    assert needs_confirmation(name, args, autonomy) is expected


def test_declined_consequential_task_is_not_run(tmp_path):
    a, llm, tb, asked = make(tmp_path, [[('ufo_task', {'request': 'email the budget to Sam'})]], answers=[False])
    assert a.handle('email the budget to Sam') == "Okay, I won't."
    assert tb.calls == []
    assert asked == ['Should I do this on your desktop: email the budget to Sam?']
    assert len(llm.requests) == 1


def test_approved_task_runs_and_careful_reads_back_exact_action(tmp_path):
    a, _, tb, asked = make(tmp_path, [[('youtube_play', {'query': 'rain sounds'})], 'Playing rain sounds.'], autonomy='careful', answers=[True])
    assert a.handle('play rain sounds') == 'Playing rain sounds.'
    assert asked == ['Should I play rain sounds on YouTube?'] and tb.calls[0][0] == 'youtube_play'


def test_careful_player_shortcut_still_confirms(tmp_path):
    a, llm, tb, asked = make(tmp_path, [[('youtube_control', {'action': 'pause'})], 'Paused.'], autonomy='careful', answers=[False])
    assert a.handle('pause') == "Okay, I won't."
    assert asked == ['Should I pause the YouTube video?'] and tb.calls == []


def test_schedule_confirmation_reads_time_and_invalid_schedule_not_asked(tmp_path):
    at = (datetime.now().astimezone() + timedelta(hours=2)).replace(microsecond=0).isoformat()
    a, _, tb, asked = make(tmp_path, [[('schedule_task', {'task_request': 'play the news', 'when': {'at': at}})], 'Scheduled.'], answers=[True])
    assert a.handle('play the news in two hours') == 'Scheduled.'
    assert asked[0].startswith('Should I schedule this task: play the news, on ')
    assert tb.calls[0][0] == 'schedule_task' and a.scheduler.list()[0]['task'] == 'play the news'
    b, llm, tb2, asked2 = make(tmp_path / 'b', [[('schedule_task', {'task_request': 'x', 'when': {'every_minutes': 1}})], 'That interval is too short.'])
    assert b.handle('do x every minute') == 'That interval is too short.'
    assert asked2 == [] and tb2.calls == []
    assert 'every_minutes' in llm.requests[1]['messages'][-1]['content']


def test_llm_failure_is_spoken_safely(tmp_path):
    cfg = {'base_url': 'https://llm.test/v1', 'api_key': 'k', 'chat_model': 'm', 'autonomy': 'balanced', 'language': 'English', 'data_dir': tmp_path, 'ufo_home': None}
    a = Agent(cfg, None, None, transport=httpx.MockTransport(lambda r: httpx.Response(500, text='boom secret')),
              toolbox=FakeToolbox(), jobs=FakeJobs(), start_background=False)
    assert a.handle('hello') == agent_mod.LLM_FAILED


# ---------------------------------------------------------------------------- memory
def test_memory_and_facts_persist(tmp_path):
    a, llm, tb, _ = make(tmp_path, [[('remember', {'fact': 'My sister is called Priya'})], 'Got it.'])
    a.handle('remember my sister is called Priya')
    a.close()
    b, llm2, _, _ = make(tmp_path, ['Your sister is Priya.'])
    b.handle("what's my sister's name")
    msgs = llm2.requests[0]['messages']
    assert 'My sister is called Priya' in msgs[0]['content']
    assert msgs[1] == {'role': 'user', 'content': 'remember my sister is called Priya'}
    assert msgs[2] == {'role': 'assistant', 'content': 'Got it.'}


def test_memory_is_bounded(tmp_path):
    a, _, _, _ = make(tmp_path, ['ok'] * 30)
    for i in range(30):
        a.handle(f'message {i}')
    data = json.loads((tmp_path / 'memory.json').read_text(encoding='utf-8'))
    assert len(data['turns']) == 40 and data['turns'][-2]['content'] == 'message 29'


# ---------------------------------------------------------------------------- speech safety
def test_speech_safe():
    raw = "## Result\n- **Done**! See https://example.com/x or [the doc](http://a.b).\n```json\n{\"a\":1}\n```\n🎉 Enjoy `it`"
    out = speech_safe(raw)
    for bad in ('#', '*', 'http', '```', '{', '🎉', '`', '- '):
        assert bad not in out
    assert out.startswith('Result Done!') and 'the doc' in out and 'Enjoy it' in out


# ---------------------------------------------------------------------------- jobs
class FakeRunner:
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.started = threading.Event()
        self.release = threading.Event()
        self.requests = []

    def run(self, args, cancel_check=None):
        self.requests.append(args['request'])
        self.started.set()
        outcome = self.outcomes.pop(0)
        if outcome == 'block':
            while not (cancel_check and cancel_check()):
                time.sleep(0.05)
            return {'ok': False, 'error': 'cancelled', 'outcome': 'cancelled', 'message': 'stopped'}
        return outcome

    def cancel(self):
        pass


def test_job_runner_announces_outcomes(tmp_path):
    from jarvis_desktop.jobs import JobRunner, short
    assert short('Save it as C:\\Users\\me\\Desktop\\jarvis home\\notes.txt then close.') == 'Save it as notes.txt then close'
    said = []
    runner = FakeRunner([{'ok': True, 'outcome': 'completed'}, {'ok': False, 'outcome': 'not_completed'}])
    jobs = JobRunner(tmp_path, None, said.append, runner=runner, poll_interval=0.05).start()
    j1 = jobs.submit('write hello in notepad')
    j2 = jobs.submit('rename the file')
    assert jobs.wait_idle(5)
    time.sleep(0.2)
    assert said == ['Your desktop task is done: write hello in notepad.',
                    "I couldn't finish the desktop task rename the file, because the desktop agent reported it did not complete."]
    assert jobs.status()['job_id'] == j2['id'] and jobs.status(j1['id'])['state'] == 'succeeded'
    jobs.close()


def test_job_cancel_announces_stopped_and_chat_still_works(tmp_path):
    from jarvis_desktop.jobs import JobRunner
    said = []
    runner = FakeRunner(['block'])
    jobs = JobRunner(tmp_path, None, None, runner=runner, poll_interval=0.05)
    cfg = {'base_url': 'https://llm.test/v1', 'api_key': 'k', 'chat_model': 'm', 'autonomy': 'autonomous', 'language': 'English', 'data_dir': tmp_path, 'ufo_home': None}
    llm = FakeLLM([[('ufo_task', {'request': 'tidy my desktop icons'})], 'Started, I will tell you when it is done.', [('time', {})], 'It is noon.',
                   [('cancel_job', {})], 'Stopping it.'])
    a = Agent(cfg, None, lambda q: True, transport=httpx.MockTransport(llm), jobs=jobs, start_background=False)
    a.on_event(said.append)
    jobs.start()
    assert a.handle('tidy my desktop icons') == 'Started, I will tell you when it is done.'
    assert runner.started.wait(5)
    assert a.handle('what time is it') == 'It is noon.'  # chat works while the job runs
    job_result = json.loads(llm.requests[1]['messages'][-1]['content'])
    assert job_result['ok'] and job_result['job_id']
    assert a.handle('stop that task') == 'Stopping it.'
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and not said:
        time.sleep(0.05)
    assert said == ['I stopped the desktop task: tidy my desktop icons.']
    a.close()


def test_ufo_engine_rejects_other_actions():
    from jarvis_desktop.jobs import UFOEngine
    assert UFOEngine(FakeRunner([])).execute('open_app', {})['ok'] is False


# ---------------------------------------------------------------------------- scheduler
class Clock:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t


def _scheduler(tmp_path, clock, fired, said):
    from jarvis_desktop.scheduler import Scheduler
    return Scheduler(tmp_path, lambda tool, args: fired.append((tool, args)) or {'ok': True}, said.append, clock=clock, tz_name='Asia/Kolkata')


def test_one_shot_fires_once_and_is_idempotent(tmp_path):
    clock, fired, said = Clock(1_800_000_000.0), [], []
    s = _scheduler(tmp_path, clock, fired, said)
    at = datetime.fromtimestamp(clock.t + 600).astimezone().isoformat()
    created = s.create('write the report', {'at': at}, 'ufo_task', {'request': 'write the report'})
    assert created['ok'] and created['when'].startswith('on ')
    assert s.tick() == []
    clock.t += 601
    keys = s.tick()
    assert keys == [f'schedule:{created["schedule_id"]}:{int(clock.t - 1)}'] and fired == [('ufo_task', {'request': 'write the report'})]
    assert s.tick() == [] and len(fired) == 1
    s2 = _scheduler(tmp_path, clock, fired, said)  # restart: still not refired
    assert s2.tick() == [] and len(fired) == 1
    assert s.list() == [] and said == ['Running your scheduled task: write the report.']


def test_concurrent_ticks_claim_slot_once(tmp_path):
    clock, fired, said = Clock(1_800_000_000.0), [], []
    s = _scheduler(tmp_path, clock, fired, said)
    s.create('ping', {'every_minutes': 5}, 'youtube_play', {'query': 'news'})
    clock.t += 301
    from jarvis_desktop.scheduler import Scheduler
    others = [Scheduler(tmp_path, s.fire, said.append, clock=clock, tz_name='Asia/Kolkata') for _ in range(4)]
    threads = [threading.Thread(target=o.tick) for o in others]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert len(fired) == 1 and len(s.fires()) == 1


def test_recurring_catch_up_grace(tmp_path):
    clock, fired, said = Clock(1_800_000_000.0), [], []
    s = _scheduler(tmp_path, clock, fired, said)
    sid = s.create('stretch', {'every_minutes': 10}, 'ufo_task', {'request': 'x'})['schedule_id']
    clock.t += 600 * 5 + 30            # five slots missed but last one within grace: fire once
    assert len(s.tick()) == 1 and len(fired) == 1
    clock.t += 3 * 3600                 # asleep 3 hours: newest slot is within 10 min, still fires once
    assert len(s.tick()) == 1 and len(fired) == 2
    assert s.list()[0]['schedule_id'] == sid


def test_one_shot_missed_beyond_grace_is_skipped(tmp_path):
    clock, fired, said = Clock(1_800_000_000.0), [], []
    s = _scheduler(tmp_path, clock, fired, said)
    at = datetime.fromtimestamp(clock.t + 600).astimezone().isoformat()
    s.create('call mom', {'at': at}, 'ufo_task', {'request': 'x'})
    clock.t += 600 + 3601
    assert s.tick() == [] and fired == [] and s.list() == []
    assert 'missed' in said[0]
    clock2, fired2 = Clock(1_800_000_000.0), []
    s2 = _scheduler(tmp_path / 'b', clock2, fired2, [])
    s2.create('call dad', {'at': at}, 'ufo_task', {'request': 'x'})
    clock2.t += 600 + 3500              # within the hour: fires
    assert len(s2.tick()) == 1 and len(fired2) == 1


def test_cancel_and_invalid_schedule(tmp_path):
    clock, fired, said = Clock(1_800_000_000.0), [], []
    s = _scheduler(tmp_path, clock, fired, said)
    sid = s.create('x', {'cron': '0 7 * * *'}, 'youtube_play', {'query': 'news'})['schedule_id']
    assert s.cancel(sid)['ok'] and not s.cancel(sid)['ok']
    clock.t += 2 * 86400
    assert s.tick() == [] and fired == []
    assert s.create('x', {'every_minutes': 2}, 'ufo_task', {})['error'] == 'invalid_schedule'
    assert s.create('x', {'at': '2020-01-01T00:00:00+05:30'}, 'ufo_task', {})['error'] == 'invalid_schedule'


def test_scheduled_fire_through_agent_does_not_reconfirm(tmp_path):
    clock = Clock(time.time())
    a, _, tb, asked = make(tmp_path, [], autonomy='careful', clock=clock)
    said = []
    a.on_event(said.append)
    a.scheduler.clock = clock
    created = a.scheduler.create('play the news', {'every_minutes': 5}, 'youtube_play', {'query': 'news'})
    clock.t += 301
    a.scheduler.tick()
    assert tb.calls == [('youtube_play', {'query': 'news'}, 'schedule')] and asked == []
    assert said == ['Running your scheduled task: play the news.']


# ---------------------------------------------------------------------------- tools
def test_toolbox_validation_and_fast_tools(tmp_path):
    from jarvis_desktop.tools import Toolbox
    opened = []
    tb = Toolbox({'data_dir': tmp_path}, url_opener=opened.append, volume=lambda level: {'ok': True, 'level': level},
                 public_tool=lambda n, a: {'success': True, 'results': [{'title': 't', 'url': 'https://a.example/x'}]} if n == 'web_search' else {'success': True, 'content': 'Gold is 7000 INR per gram on 25 Oct'},
                 research=lambda q, ev: 'Gold is about 7000 rupees per gram.')
    assert tb.execute('open_url', {'url': 'javascript:alert(1)'})['ok'] is False
    assert tb.execute('open_url', {'url': 'file:///c:/x'})['ok'] is False
    assert tb.execute('open_url', {'url': 'https://example.com/a'}) == {'ok': True, 'status': 'opened', 'host': 'example.com'} and opened
    assert tb.execute('system_volume', {'level': 30}) == {'ok': True, 'level': 30}
    assert tb.execute('time', {})['ok']
    assert tb.execute('nope', {})['error'] == 'unknown_tool'
    assert tb.execute('time', {'x': 1})['error'] == 'invalid_arguments'
    r = tb.execute('web_research', {'question': 'gold price in Pune today'})
    assert r == {'ok': True, 'answer': 'Gold is about 7000 rupees per gram.', 'sources': ['a.example']}
    assert tb.execute('ufo_task', {'request': 'x'})['error'] == 'unavailable'


def test_workbook_tools_via_action_engine(tmp_path):
    pytest.importorskip('openpyxl')
    from jarvis_desktop.tools import Toolbox
    tb = Toolbox({'data_dir': tmp_path})
    assert tb.execute('workbook_create', {'path': 'budget.xlsx', 'sheet': 'S', 'rows': [['a', 1]]})['ok']
    assert tb.execute('workbook_write', {'path': 'budget.xlsx', 'sheet': 'S', 'start_cell': 'A2', 'rows': [['b', 2]]})['ok']
    assert tb.execute('workbook_read', {'path': 'budget.xlsx', 'sheet': 'S', 'range': 'A1:B2'})['rows'] == [['a', 1], ['b', 2]]


def test_system_volume_powershell_fallback_parses_readback():
    from jarvis_desktop import tools

    class Done:
        returncode, stdout, stderr = 0, '40', ''
    seen = []
    result = tools.set_system_volume(40, runner=lambda argv, **kw: seen.append(argv) or Done())
    if result.get('method') == 'pycaw':
        pytest.skip('pycaw installed')
    assert result == {'ok': True, 'level': 40, 'method': 'core_audio'} and '0.40' in seen[0][-1]
    assert tools.set_system_volume(101)['ok'] is False


# ---------------------------------------------------------------------------- proxy
def test_proxy_strips_sampling_params_and_forwards(tmp_path):
    from jarvis_desktop.proxy import start_proxy
    seen = []

    def upstream(request):
        seen.append((request.url, request.headers.get('authorization'), json.loads(request.content)))
        body = json.loads(request.content)
        if body.get('stream'):
            return httpx.Response(200, headers={'content-type': 'text/event-stream'}, content=b'data: {"x":1}\n\ndata: [DONE]\n\n')
        return httpx.Response(200, json={'choices': [{'message': {'content': 'hi'}}]})
    with start_proxy('https://gw.test/v1/', 'sk-upstream', port=0, transport=httpx.MockTransport(upstream)) as proxy:
        assert httpx.get(f'http://127.0.0.1:{proxy.port}/health').json() == {'ok': True}
        r = httpx.post(proxy.url + '/chat/completions', json={'model': 'claude-sonnet-5', 'messages': [], 'temperature': 0, 'top_p': 0, 'n': 1, 'max_tokens': 5})
        assert r.json()['choices'][0]['message']['content'] == 'hi'
        url, auth, body = seen[0]
        assert str(url) == 'https://gw.test/v1/chat/completions' and auth == 'Bearer sk-upstream'
        assert body == {'model': 'claude-sonnet-5', 'messages': [], 'max_tokens': 5}
        s = httpx.post(proxy.url + '/chat/completions', json={'model': 'm', 'messages': [], 'stream': True, 'temperature': 0.2})
        assert s.headers['content-type'].startswith('text/event-stream') and b'[DONE]' in s.content
        assert 'temperature' not in seen[1][2]
        assert httpx.post(proxy.url + '/chat/completions', content=b'not json', headers={'content-type': 'application/json'}).status_code == 400
        assert httpx.post(f'http://127.0.0.1:{proxy.port}/v1/other', json={}).status_code == 404
    with pytest.raises(ValueError):
        start_proxy('https://gw.test/v1', 'k', port=0, host='0.0.0.0')
