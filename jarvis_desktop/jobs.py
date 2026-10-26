"""Background job runner: durable companion.queue.JobQueue + companion.ufo.UFORunner.

Screen-touching UFO jobs are serialized (one worker thread, one screen). Chat and fast tools
keep working while a job runs. Completion is announced through `announce(text)` in plain words.
"""
from __future__ import annotations

import sqlite3
import threading
import uuid
from pathlib import Path

OWNER = 'local'


class UFOEngine:
    """Engine adapter for JobQueue: only `ufo_task` is executed here."""

    def __init__(self, runner):
        self.runner = runner

    def execute_cancellable(self, name, args, cancel_check):
        if name != 'ufo_task':
            return {'ok': False, 'error': 'unsupported_action'}
        return self.runner.run(args, cancel_check=cancel_check)

    def execute(self, name, args):
        return self.execute_cancellable(name, args, None)


def short(text, limit=90):
    import re
    text = ' '.join(str(text).split())
    # Speak file names, not full paths.
    text = re.sub(r'(?i)\b[a-z]:[\\/](?:[^\\/:*?"<>|]*[\\/])*([^\\/\s]+\.\w{1,5})\b', r'\1', text)
    text = text.rstrip('. ')
    return text if len(text) <= limit else text[:limit].rsplit(' ', 1)[0]


_REASONS = {
    'not_completed': 'the desktop agent reported it did not complete',
    'uncertain': 'I could not confirm whether it worked',
    'timeout': 'it took too long and was stopped',
    'failed': 'the desktop agent hit an error',
    'ufo_unavailable': 'desktop automation is not set up on this computer',
    'ufo_busy': 'another desktop task was running',
    'interrupted': 'Jarvis was restarted while it ran',
    'dependency_failed': 'an earlier step failed',
}


def spoken_outcome(job, request):
    state = job.get('state')
    result = job.get('result') or {}
    task = short(request)
    if state == 'succeeded':
        return f'Your desktop task is done: {task}.'
    if state == 'cancelled':
        return f'I stopped the desktop task: {task}.'
    reason = _REASONS.get(result.get('outcome')) or _REASONS.get(result.get('error')) or 'something went wrong'
    return f"I couldn't finish the desktop task {task}, because {reason}."


class JobRunner:
    def __init__(self, data_dir, ufo_home=None, announce=None, *, runner=None, poll_interval=0.25):
        self.path = Path(data_dir) / 'jobs.sqlite3'
        if runner is None:
            from companion.ufo import UFORunner
            runner = UFORunner(home=str(ufo_home) if ufo_home else '', enabled=bool(ufo_home))
        self.runner = runner
        from companion.queue import JobQueue
        self.queue = JobQueue(self.path, UFOEngine(runner))
        self.announce = announce or (lambda text: None)
        self.poll_interval = poll_interval
        self._stop = threading.Event()
        self._idle = threading.Event()
        self._thread = None
        self.current = None

    # ---- lifecycle -----------------------------------------------------------------------
    def start(self):
        if self._thread is None or not self._thread.is_alive():
            self._stop.clear()
            self._thread = threading.Thread(target=self._loop, name='jarvis-jobs', daemon=True)
            self._thread.start()
        return self

    def _loop(self):
        while not self._stop.is_set():
            job = self.run_once()
            if job is None:
                self._idle.set()
                self._stop.wait(self.poll_interval)

    def run_once(self):
        """Run the next queued job (if any) and announce its outcome."""
        try:
            job = self.queue.run_next()
        except Exception:
            return None
        if job is not None:
            self._idle.clear()
            request = (self._args(job['id']) or {}).get('request', 'your task')
            try:
                self.announce(spoken_outcome(job, request))
            except Exception:
                pass
        return job

    def close(self, timeout=10):
        self._stop.set()
        self.queue.request_stop()
        try:
            self.runner.cancel()
        except Exception:
            pass
        if self._thread:
            self._thread.join(timeout)

    # ---- api used by tools ---------------------------------------------------------------
    def submit(self, request, idempotency_key=None):
        job = self.queue.submit('ufo_task', {'request': request}, OWNER, idempotency_key or uuid.uuid4().hex)
        return job

    def _db(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        return db

    def _args(self, job_id):
        import json
        with self._db() as db:
            row = db.execute('SELECT args FROM jobs WHERE id=?', (job_id,)).fetchone()
        db.close()
        return json.loads(row['args']) if row else None

    def pending_before(self, job_id):
        with self._db() as db:
            row = db.execute('SELECT created, rowid FROM jobs WHERE id=?', (job_id,)).fetchone()
            if not row:
                return 0
            n = db.execute("SELECT COUNT(*) FROM jobs WHERE id<>? AND (state='running' OR (state='queued' AND (created<? OR (created=? AND rowid<?))))",
                           (job_id, row['created'], row['created'], row['rowid'])).fetchone()[0]
        db.close()
        return n

    def _latest(self, active_only=False):
        where = "WHERE state IN ('queued','running')" if active_only else ''
        with self._db() as db:
            row = db.execute(f'SELECT id FROM jobs {where} ORDER BY created DESC, rowid DESC LIMIT 1').fetchone()
        db.close()
        return row['id'] if row else None

    def _view(self, job):
        args = self._args(job['id']) or {}
        result = job.get('result') or {}
        return {'job_id': job['id'], 'state': job['state'], 'request': short(args.get('request', ''), 200),
                'outcome': result.get('outcome') or result.get('error'), 'message': result.get('message')}

    def status(self, job_id=None):
        job_id = job_id or self._latest()
        if not job_id:
            return None
        try:
            return self._view(self.queue.get(job_id, OWNER))
        except KeyError:
            return None

    def cancel(self, job_id=None):
        job_id = job_id or self._latest(active_only=True)
        if not job_id:
            return None
        try:
            return self._view(self.queue.cancel(job_id, OWNER))
        except KeyError:
            return None

    def wait_idle(self, timeout):
        """Test helper: wait until the queue has no queued/running jobs."""
        import time
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self._latest(active_only=True) is None:
                return True
            time.sleep(0.1)
        return False
