"""Tool adapters for the desktop agent.

Fast deterministic tools first (YouTube controller, open app/url, workbooks, research, time,
weather, volume); `ufo_task` is the general desktop fallback and runs as a background job.
Every adapter returns a JSON-compatible dict with a boolean `ok`. Authorization (spoken
confirmation) is decided by the agent before `execute` is called, never in here.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import threading
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit

APPS = ['chrome', 'edge', 'excel', 'notepad', 'calculator', 'explorer']
YT_ACTIONS = ['pause', 'resume', 'stop', 'skip_ad', 'volume', 'status']
SCHEDULABLE = ['ufo_task', 'youtube_play', 'youtube_control', 'open_app', 'open_url', 'web_research', 'system_volume']

# Tools that change something on this PC (careful mode confirms all of them).
MUTATING = frozenset({'youtube_play', 'youtube_control', 'open_app', 'open_url', 'workbook_write',
                      'workbook_create', 'ufo_task', 'schedule_task', 'cancel_job', 'cancel_schedule',
                      'system_volume'})
# Tools that touch the screen/browser; executed one at a time.
SCREEN = frozenset({'youtube_play', 'youtube_control', 'open_app', 'open_url'})


def _fn(name, description, properties, required=()):
    return {'type': 'function', 'function': {'name': name, 'description': description, 'parameters': {
        'type': 'object', 'properties': properties, 'required': list(required), 'additionalProperties': False}}}


_S = {'type': 'string'}
_ROWS = {'type': 'array', 'items': {'type': 'array', 'items': {'type': ['string', 'number', 'boolean', 'null']}}}
_WHEN = {'type': 'object', 'description': 'Exactly one key. at: ISO-8601 local datetime WITH offset for a one-off; '
         'cron: 5-field "minute hour day month weekday" local time for repeating; every_minutes: integer 5-10080.',
         'properties': {'at': _S, 'cron': _S, 'every_minutes': {'type': 'integer', 'minimum': 5, 'maximum': 10080}},
         'additionalProperties': False}

SCHEMAS = [
    _fn('youtube_play', 'FAST PATH for any music, song, video or podcast request: search YouTube and start playing the top '
        'video in Jarvis\'s own browser. Skip buttons on ads are clicked automatically.', {'query': {**_S, 'maxLength': 200}}, ['query']),
    _fn('youtube_control', 'Control the YouTube player Jarvis opened: pause, resume, stop, skip_ad, volume (value 0-100, '
        'player volume only), status.', {'action': {'type': 'string', 'enum': YT_ACTIONS},
                                         'value': {'type': 'integer', 'minimum': 0, 'maximum': 100}}, ['action']),
    _fn('open_app', 'FAST PATH: open an installed app.', {'app': {'type': 'string', 'enum': APPS}}, ['app']),
    _fn('open_url', 'FAST PATH: open an http(s) web address in the default browser.', {'url': _S}, ['url']),
    _fn('workbook_read', 'Read a cell range from an .xlsx workbook in Jarvis\'s workspace folder.',
        {'path': _S, 'sheet': _S, 'range': _S}, ['path', 'sheet', 'range']),
    _fn('workbook_write', 'Write rows into an existing .xlsx workbook in the workspace starting at start_cell (overwrites those cells).',
        {'path': _S, 'sheet': _S, 'start_cell': _S, 'rows': _ROWS}, ['path', 'sheet', 'start_cell', 'rows']),
    _fn('workbook_create', 'Create a new .xlsx workbook in the workspace with the given rows (never overwrites).',
        {'path': _S, 'sheet': _S, 'rows': _ROWS}, ['path', 'sheet', 'rows']),
    _fn('web_research', 'Answer a question about current public facts, news or prices from the web. Pass a self-contained '
        'public question; never include private details.', {'question': {**_S, 'maxLength': 1000}}, ['question']),
    _fn('weather', 'Current weather for a city.', {'city': _S}, ['city']),
    _fn('time', 'Current local date and time.', {}),
    _fn('ufo_task', 'SLOW general desktop automation for multi-step work in other apps (e.g. write a note in Notepad and save '
        'it, fill a form, change a setting). Runs in the background and returns a job id; completion is announced later.',
        {'request': {**_S, 'maxLength': 2000}}, ['request']),
    _fn('job_status', 'Status of a background desktop job (latest if job_id omitted).', {'job_id': _S}),
    _fn('cancel_job', 'Stop a background desktop job (latest active if job_id omitted).', {'job_id': _S}),
    _fn('schedule_task', 'Run a task later or repeatedly. Convert the user\'s timing into `when`. tool defaults to ufo_task '
        'with task_request as its request; for music use youtube_play with tool_args {"query": ...}.',
        {'task_request': {**_S, 'maxLength': 500}, 'when': _WHEN, 'tool': {'type': 'string', 'enum': SCHEDULABLE},
         'tool_args': {'type': 'object'}}, ['task_request', 'when']),
    _fn('list_schedules', 'List active scheduled tasks.', {}),
    _fn('cancel_schedule', 'Cancel a scheduled task by id.', {'schedule_id': _S}, ['schedule_id']),
    _fn('remember', 'Store a short long-term fact about the user when they ask you to remember something.',
        {'fact': {**_S, 'maxLength': 300}}, ['fact']),
    _fn('system_volume', 'Set the Windows master volume (0-100).', {'level': {'type': 'integer', 'minimum': 0, 'maximum': 100}}, ['level']),
]
NAMES = frozenset(s['function']['name'] for s in SCHEMAS)


def describe(name, args, schedule_when_text=None):
    """Plain-words read-back of the exact action, used in confirmation questions."""
    a = args or {}
    if name == 'ufo_task':
        return f'do this on your desktop: {a.get("request", "")}'
    if name == 'schedule_task':
        return f'schedule this task: {a.get("task_request", "")}, {schedule_when_text or "at the time you gave"}'
    if name == 'youtube_play':
        return f'play {a.get("query", "")} on YouTube'
    if name == 'youtube_control':
        action = a.get('action')
        return f'set the YouTube volume to {a.get("value")} percent' if action == 'volume' else f'{str(action).replace("_", " ")} the YouTube video'
    if name == 'open_app':
        return f'open {a.get("app")}'
    if name == 'open_url':
        host = urlsplit(str(a.get('url', ''))).hostname or 'that website'
        return f'open the website {host}'
    if name == 'workbook_write':
        return f'write {len(a.get("rows") or [])} rows into {a.get("path")} on sheet {a.get("sheet")} starting at {a.get("start_cell")}, overwriting those cells'
    if name == 'workbook_create':
        return f'create the workbook {a.get("path")}'
    if name == 'cancel_job':
        return 'stop the running desktop task'
    if name == 'cancel_schedule':
        return f'cancel scheduled task {a.get("schedule_id")}'
    if name == 'system_volume':
        return f'set the computer volume to {a.get("level")} percent'
    return f'run {name.replace("_", " ")}'


# ---- Windows master volume ---------------------------------------------------------------
_VOLUME_PS = r'''
$ErrorActionPreference = 'Stop'
Add-Type -TypeDefinition @"
using System; using System.Runtime.InteropServices;
[Guid("5CDF2C82-841E-4546-9722-0CF74078229A"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
interface IAudioEndpointVolume { int f(); int g(); int h(); int i();
  int SetMasterVolumeLevelScalar(float level, Guid ctx); int j(); int GetMasterVolumeLevelScalar(out float level);
  int k(); int l(); int m(); int n(); int SetMute([MarshalAs(UnmanagedType.Bool)] bool mute, Guid ctx); int GetMute(out bool mute); }
[Guid("D666063F-1587-4E43-81F1-B948E807363F"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
interface IMMDevice { int Activate(ref Guid id, int ctx, IntPtr p, [MarshalAs(UnmanagedType.IUnknown)] out object o); }
[Guid("A95664D2-9614-4F35-A746-DE8DB63617E6"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
interface IMMDeviceEnumerator { int f(); int GetDefaultAudioEndpoint(int flow, int role, out IMMDevice d); }
[ComImport, Guid("BCDE0395-E52F-467C-8E3D-C4579291692E")] class MMDeviceEnumeratorComObject { }
public static class JarvisVolume {
  static IAudioEndpointVolume Ep() { var en = (IMMDeviceEnumerator)(new MMDeviceEnumeratorComObject()); IMMDevice dev;
    Marshal.ThrowExceptionForHR(en.GetDefaultAudioEndpoint(0, 1, out dev)); var iid = typeof(IAudioEndpointVolume).GUID; object o;
    Marshal.ThrowExceptionForHR(dev.Activate(ref iid, 23, IntPtr.Zero, out o)); return (IAudioEndpointVolume)o; }
  public static float Set(float v) { var e = Ep(); Marshal.ThrowExceptionForHR(e.SetMasterVolumeLevelScalar(v, Guid.Empty));
    if (v > 0) e.SetMute(false, Guid.Empty); float r; Marshal.ThrowExceptionForHR(e.GetMasterVolumeLevelScalar(out r)); return r; }
}
"@
[Console]::Out.Write([math]::Round([JarvisVolume]::Set(LEVEL) * 100))
'''


def set_system_volume(level, runner=subprocess.run):
    """Set master volume via pycaw when installed, else Core Audio through PowerShell; reads back the level."""
    if type(level) is not int or not 0 <= level <= 100:
        return {'ok': False, 'error': 'invalid_level'}
    try:
        from pycaw.pycaw import AudioUtilities  # optional dependency
        endpoint = AudioUtilities.GetSpeakers().EndpointVolume
        endpoint.SetMasterVolumeLevelScalar(level / 100, None)
        if level:
            endpoint.SetMute(0, None)
        return {'ok': True, 'level': round(endpoint.GetMasterVolumeLevelScalar() * 100), 'method': 'pycaw'}
    except Exception:
        pass
    if os.name != 'nt':
        return {'ok': False, 'error': 'unsupported_os'}
    script = _VOLUME_PS.replace('LEVEL', f'{level / 100:.2f}')
    try:
        done = runner(['powershell.exe', '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass', '-Command', script],
                      capture_output=True, text=True, timeout=30, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    except (OSError, subprocess.TimeoutExpired):
        return {'ok': False, 'error': 'volume_failed', 'message': 'Could not run the volume control.'}
    out = (done.stdout or '').strip()
    if done.returncode == 0 and out.isdigit():
        return {'ok': abs(int(out) - level) <= 1, 'level': int(out), 'method': 'core_audio'}
    return {'ok': False, 'error': 'no_audio_device' if 'Element not found' in (done.stderr or '') or '0x80070490' in (done.stderr or '') else 'volume_failed',
            'message': 'No audio output device is available or volume could not be set.'}


def _valid_url(url):
    if not isinstance(url, str) or len(url) > 2000 or any(c.isspace() or ord(c) < 32 for c in url):
        return False
    try:
        p = urlsplit(url)
    except ValueError:
        return False
    return p.scheme in ('http', 'https') and bool(p.hostname) and not p.username and not p.password


class Toolbox:
    """Executes tools. Collaborators are injectable for tests."""

    def __init__(self, cfg, *, jobs=None, scheduler=None, memory=None, youtube=None, engine=None,
                 research=None, public_tool=None, url_opener=None, volume=None, clock=time.time):
        self.cfg = cfg
        self.jobs = jobs
        self.scheduler = scheduler
        self.memory = memory
        self._youtube = youtube
        self._engine = engine
        self._research = research            # callable(question, evidence) -> answer text (LLM synthesis)
        self._public_tool = public_tool      # callable(name, args) -> dict (jarvis_tools.execute_tool)
        self._url_opener = url_opener
        self._volume = volume or set_system_volume
        self.clock = clock
        self._screen = threading.Lock()
        self._lazy = threading.Lock()

    # ---- lazily constructed collaborators -------------------------------------------------
    @property
    def data_dir(self):
        return Path(self.cfg['data_dir'])

    def engine(self):
        with self._lazy:
            if self._engine is None:
                from companion.actions import ActionEngine
                workspace = Path(self.cfg.get('workspace') or self.data_dir / 'workspace')
                workspace.mkdir(parents=True, exist_ok=True)
                self._engine = ActionEngine(workspace)
            return self._engine

    def youtube(self):
        with self._lazy:
            if self._youtube is None:
                from companion.youtube import YouTubeController
                self._youtube = YouTubeController(self.data_dir / 'browser-profile', headless=bool(self.cfg.get('youtube_headless', False)))
            return self._youtube

    def public_tool(self, name, args):
        if self._public_tool is None:
            import jarvis_tools
            self._public_tool = jarvis_tools.execute_tool
        return self._public_tool(name, args)

    # ---- dispatcher ----------------------------------------------------------------------
    def execute(self, name, args, *, source='user'):
        if name not in NAMES:
            return {'ok': False, 'error': 'unknown_tool'}
        if not isinstance(args, dict):
            return {'ok': False, 'error': 'invalid_arguments'}
        try:
            if name in SCREEN:
                with self._screen:
                    return getattr(self, '_t_' + name)(**args)
            return getattr(self, '_t_' + name)(**args)
        except TypeError:
            return {'ok': False, 'error': 'invalid_arguments'}
        except Exception as exc:  # never leak stack traces into speech
            return {'ok': False, 'error': 'tool_failed', 'message': type(exc).__name__}

    # ---- fast tools ----------------------------------------------------------------------
    def _t_youtube_play(self, query):
        if not isinstance(query, str) or not query.strip() or len(query) > 200:
            return {'ok': False, 'error': 'invalid_arguments'}
        result = self.youtube().execute('play_youtube', {'query': query.strip()})
        return {k: v for k, v in result.items() if k != 'url'}

    def _t_youtube_control(self, action, value=None):
        if action not in YT_ACTIONS:
            return {'ok': False, 'error': 'invalid_arguments'}
        yt = self.youtube()
        if action == 'volume':
            if type(value) is not int or not 0 <= value <= 100:
                return {'ok': False, 'error': 'invalid_arguments', 'message': 'volume needs value 0 to 100'}
            return yt.execute('set_volume', {'volume': value})
        if action == 'status':
            return yt.execute('state', {})
        return yt.execute(action, {})

    def _t_open_app(self, app):
        if app not in APPS:
            return {'ok': False, 'error': 'invalid_arguments'}
        result = self.engine().execute('open_app', {'app': app})
        result.pop('pid', None)
        return result

    def _t_open_url(self, url):
        if not _valid_url(url):
            return {'ok': False, 'error': 'invalid_url', 'message': 'Only http and https addresses can be opened.'}
        if self._url_opener:
            self._url_opener(url)
        else:
            import webbrowser
            if not webbrowser.open(url, new=2):
                return {'ok': False, 'error': 'open_failed'}
        return {'ok': True, 'status': 'opened', 'host': urlsplit(url).hostname}

    def _workbook(self, action, args):
        return self.engine().execute(action, args)

    def _t_workbook_read(self, path, sheet, range):
        return self._workbook('read_range', {'path': path, 'sheet': sheet, 'range': range})

    def _t_workbook_write(self, path, sheet, start_cell, rows):
        return self._workbook('write_range', {'path': path, 'sheet': sheet, 'start_cell': start_cell, 'rows': rows})

    def _t_workbook_create(self, path, sheet, rows):
        return self._workbook('create_workbook', {'path': path, 'sheet': sheet, 'rows': rows})

    def _t_time(self):
        now = datetime.fromtimestamp(self.clock()).astimezone()
        return {'ok': True, 'local_time': now.strftime('%H:%M'), 'date': now.strftime('%A %d %B %Y'), 'iso': now.isoformat()}

    def _t_weather(self, city):
        result = self.public_tool('weather', {'city': city})
        ok = bool(result.get('success'))
        return {'ok': ok, **{k: v for k, v in result.items() if k in ('city', 'country', 'current', 'units', 'error')}}

    def _t_web_research(self, question):
        if not isinstance(question, str) or not question.strip():
            return {'ok': False, 'error': 'invalid_arguments'}
        search = self.public_tool('web_search', {'query': question[:400]})
        evidence = [{'tool': 'web_search', 'result': search}]
        urls = []
        for item in (search.get('results') or [])[:6] if search.get('success') else []:
            url = item.get('url') if isinstance(item, dict) else None
            if isinstance(url, str) and url.startswith('https://') and urlsplit(url).hostname not in {urlsplit(u).hostname for u in urls}:
                urls.append(url)
            if len(urls) == 2:
                break
        if urls:
            from concurrent.futures import ThreadPoolExecutor
            with ThreadPoolExecutor(2) as pool:
                pages = list(pool.map(lambda u: self.public_tool('read_public_page', {'url': u}), urls))
            evidence += [{'tool': 'read_public_page', 'url': u, 'result': _trim(p)} for u, p in zip(urls, pages)]
        if not any(e['result'].get('success') for e in evidence):
            return {'ok': False, 'error': 'no_evidence', 'message': 'No public sources could be retrieved; a current answer cannot be verified.'}
        if self._research is None:
            return {'ok': True, 'evidence': evidence}
        answer = self._research(question, evidence)
        return {'ok': True, 'answer': answer, 'sources': [urlsplit(u).hostname for u in urls]}

    def _t_system_volume(self, level):
        return self._volume(level)

    def _t_remember(self, fact):
        if self.memory is None or not isinstance(fact, str) or not fact.strip():
            return {'ok': False, 'error': 'unavailable'}
        self.memory.remember(fact.strip()[:300])
        return {'ok': True, 'remembered': fact.strip()[:300]}

    # ---- background jobs -----------------------------------------------------------------
    def _t_ufo_task(self, request):
        if self.jobs is None:
            return {'ok': False, 'error': 'unavailable', 'message': 'Desktop automation is not set up.'}
        if not isinstance(request, str) or not request.strip() or len(request) > 2000:
            return {'ok': False, 'error': 'invalid_arguments'}
        job = self.jobs.submit(request.strip())
        ahead = self.jobs.pending_before(job['id'])
        return {'ok': True, 'job_id': job['id'], 'status': 'started' if not ahead else 'queued', 'jobs_ahead': ahead,
                'note': 'Runs in the background; completion will be announced.'}

    def _t_job_status(self, job_id=None):
        if self.jobs is None:
            return {'ok': False, 'error': 'unavailable'}
        job = self.jobs.status(job_id)
        return {'ok': True, **job} if job else {'ok': False, 'error': 'no_jobs'}

    def _t_cancel_job(self, job_id=None):
        if self.jobs is None:
            return {'ok': False, 'error': 'unavailable'}
        job = self.jobs.cancel(job_id)
        return {'ok': True, **job} if job else {'ok': False, 'error': 'no_active_job'}

    # ---- schedules -----------------------------------------------------------------------
    def schedule_target(self, args):
        """(tool, tool_args) a schedule will fire."""
        tool = args.get('tool') or 'ufo_task'
        tool_args = args.get('tool_args')
        if not tool_args:
            key = {'ufo_task': 'request', 'youtube_play': 'query', 'web_research': 'question'}.get(tool)
            tool_args = {key: args['task_request']} if key else {}
        return tool, tool_args

    def _t_schedule_task(self, task_request, when, tool=None, tool_args=None):
        if self.scheduler is None:
            return {'ok': False, 'error': 'unavailable'}
        target, target_args = self.schedule_target({'task_request': task_request, 'tool': tool, 'tool_args': tool_args})
        if target not in SCHEDULABLE or not isinstance(target_args, dict):
            return {'ok': False, 'error': 'invalid_arguments'}
        return self.scheduler.create(task_request, when, target, target_args)

    def _t_list_schedules(self):
        if self.scheduler is None:
            return {'ok': False, 'error': 'unavailable'}
        return {'ok': True, 'schedules': self.scheduler.list()}

    def _t_cancel_schedule(self, schedule_id):
        if self.scheduler is None:
            return {'ok': False, 'error': 'unavailable'}
        return self.scheduler.cancel(schedule_id)

    def close(self):
        for item in (self._youtube, self._engine):
            try:
                if item is not None:
                    item.close()
            except Exception:
                pass


def _trim(result, limit=6000):
    text = json.dumps(result)
    if len(text) <= limit:
        return result
    content = result.get('content') or result.get('text') or ''
    return {'success': result.get('success'), 'title': result.get('title'), 'content': str(content)[:limit]}


CONSEQUENTIAL_BALANCED = re.compile(r'\b(send|sending|sends|e-?mail|e-?mails|e-?mailing|message|messages|messaging|pay|paying|pays|payment|'
                                    r'buy|buying|buys|purchase|purchasing|order|ordering|delete|deleting|remove|removing|'
                                    r'uninstall|uninstalling|install|installing|format|formatting|submit|submitting|transfer|transferring)\b', re.I)
CONSEQUENTIAL_AUTONOMOUS = re.compile(r'\b(send|sending|sends|pay|paying|pays|payment|buy|buying|buys|purchase|purchasing|delete|deleting)\b', re.I)
