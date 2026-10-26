"""SQLite durable local queue; never automatically replays a started action."""
from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
import sqlite3
import time
import uuid
import os
import threading

_LOCKS = {}
_LOCKS_GUARD = threading.Lock()


@contextmanager
def _execution_lock(path):
    with _LOCKS_GUARD:
        lock = _LOCKS.setdefault(str(path).casefold(), threading.Lock())
    if not lock.acquire(blocking=False):
        yield False
        return
    handle = None
    acquired = False
    try:
        handle = open(path, 'a+b')
        handle.seek(0)
        if os.name == 'nt':
            import msvcrt
            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                acquired = True
            except OSError:
                pass
        else:
            import fcntl
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
            except OSError:
                pass
        yield acquired
    finally:
        if handle:
            if acquired and os.name == 'nt':
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            handle.close()
        lock.release()



def sanitize_result(value):
    import re
    if isinstance(value, dict):
        return {str(k): '[REDACTED]' if re.search(r'password|secret|token|authorization|cookie|api.?key', str(k), re.I) else sanitize_result(v) for k, v in value.items()}
    if isinstance(value, list):
        return [sanitize_result(v) for v in value]
    if isinstance(value, str):
        value = re.sub(r'(?i)Bearer\s+[^\s,;]+', 'Bearer [REDACTED]', value)
        return re.sub(r'(?i)(password|secret|token|api.?key)(\s*[:=]\s*)[^\s,;]+', r'\1\2[REDACTED]', value)
    return value


class JobQueue:
    def __init__(self, db_path, engine):
        self.path = Path(db_path).absolute()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.engine = engine
        self._stop = threading.Event()
        self.lock_path = self.path.with_suffix(self.path.suffix + '.desktop.lock')
        with self._db() as db:
            db.execute('''CREATE TABLE IF NOT EXISTS jobs (
                id TEXT PRIMARY KEY, owner TEXT NOT NULL, idem TEXT NOT NULL,
                payload_hash TEXT NOT NULL, action TEXT NOT NULL, args TEXT NOT NULL,
                state TEXT NOT NULL, result TEXT, created REAL NOT NULL,
                depends_on TEXT, cancel_requested INTEGER NOT NULL DEFAULT 0,
                UNIQUE(owner, idem))''')
        with _execution_lock(self.lock_path) as acquired:
            if acquired:
                self._recover()

    def _recover(self):
        with self._db() as db:
            db.execute("UPDATE jobs SET state='interrupted', result=? WHERE state='running'", (json.dumps({'ok': False, 'error': 'interrupted', 'message': 'Worker stopped; side effect may have happened. Not retried.'}),))

    @contextmanager
    def _db(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    @staticmethod
    def _public(row):
        return {'id': row['id'], 'owner': row['owner'], 'action': row['action'], 'state': row['state'], 'result': json.loads(row['result']) if row['result'] else None, 'created': row['created'], 'depends_on': row['depends_on'], 'cancel_requested': bool(row['cancel_requested'])}

    def submit(self, action, args, owner, idempotency_key, depends_on=None):
        if any(not isinstance(v, str) or not v.strip() or len(v) > 200 for v in (action, owner, idempotency_key)) or not isinstance(args, dict):
            raise ValueError('Invalid job identity or arguments')
        if depends_on is not None and not isinstance(depends_on, str):
            raise ValueError('Dependency must be a job id')
        payload = json.dumps({'action': action, 'args': args, 'depends_on': depends_on}, sort_keys=True, allow_nan=False, separators=(',', ':'))
        if len(payload.encode()) > 65536:
            raise ValueError('Job payload exceeds 64KB')
        digest = hashlib.sha256(payload.encode()).hexdigest()
        with self._db() as db:
            db.execute('BEGIN IMMEDIATE')
            old = db.execute('SELECT * FROM jobs WHERE owner=? AND idem=?', (owner, idempotency_key)).fetchone()
            if old:
                if old['payload_hash'] != digest:
                    raise ValueError('Idempotency key already used for different arguments')
                return self._public(old)
            if db.execute("SELECT COUNT(*) FROM jobs WHERE state IN ('queued','running')").fetchone()[0] >= 100:
                raise ValueError('Queue is full (100 pending jobs)')
            if depends_on is not None and not db.execute('SELECT 1 FROM jobs WHERE id=? AND owner=?', (depends_on, owner)).fetchone():
                raise KeyError('Dependency not found')
            job_id = str(uuid.uuid4())
            db.execute('INSERT INTO jobs VALUES (?,?,?,?,?,?,?,?,?,?,?)', (job_id, owner, idempotency_key, digest, action, json.dumps(args), 'queued', None, time.time(), depends_on, 0))
        return self.get(job_id, owner)

    def get(self, job_id, owner):
        with self._db() as db:
            row = db.execute('SELECT * FROM jobs WHERE id=? AND owner=?', (job_id, owner)).fetchone()
            if row is None:
                raise KeyError('Job not found')
            return self._public(row)

    def cancel(self, job_id, owner):
        with self._db() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT * FROM jobs WHERE id=? AND owner=?', (job_id, owner)).fetchone()
            if row is None:
                raise KeyError('Job not found')
            if row['state'] == 'queued':
                db.execute("UPDATE jobs SET state='cancelled', cancel_requested=1, result=? WHERE id=?", (json.dumps({'ok': False, 'error': 'cancelled', 'message': 'Cancelled before execution.'}), job_id))
            elif row['state'] == 'running':
                db.execute('UPDATE jobs SET cancel_requested=1 WHERE id=?', (job_id,))
        return self.get(job_id, owner)

    def request_stop(self):
        """Ask the running action (if cooperative) to stop; no process is killed."""
        self._stop.set()

    def _cancel_check(self, job_id):
        def check():
            if self._stop.is_set():
                return True
            try:
                with self._db() as db:
                    row = db.execute('SELECT cancel_requested FROM jobs WHERE id=?', (job_id,)).fetchone()
                return bool(row and row['cancel_requested'])
            except sqlite3.Error:
                return False
        return check

    def worker(self, stop_event=None, poll_interval=0.25):
        """Foreground worker; caller owns thread/process lifetime. Never auto-started."""
        stop_event = stop_event or threading.Event()
        while not stop_event.is_set():
            if self.run_next() is None:
                stop_event.wait(poll_interval)

    def run_next(self):
        with _execution_lock(self.lock_path) as acquired:
            if not acquired:
                return None
            self._recover()
            return self._run_locked()

    def _run_locked(self):
        with self._db() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute("SELECT * FROM jobs WHERE state='queued' ORDER BY created, rowid LIMIT 1").fetchone()
            if row is None:
                return None
            if row['depends_on']:
                dependency = db.execute('SELECT state FROM jobs WHERE id=?', (row['depends_on'],)).fetchone()
                if dependency['state'] in ('queued', 'running'):
                    return None
                if dependency['state'] != 'succeeded':
                    db.execute("UPDATE jobs SET state='cancelled', result=? WHERE id=?", (json.dumps({'ok': False, 'error': 'dependency_failed'}), row['id']))
                    return self._public(db.execute('SELECT * FROM jobs WHERE id=?', (row['id'],)).fetchone())
            db.execute("UPDATE jobs SET state='running' WHERE id=?", (row['id'],))
        try:
            args = json.loads(row['args'])
            cancellable = getattr(self.engine, 'execute_cancellable', None)
            if callable(cancellable):
                result = cancellable(row['action'], args, self._cancel_check(row['id']))
            else:
                result = self.engine.execute(row['action'], args)
            if not isinstance(result, dict) or not isinstance(result.get('ok'), bool):
                raise ValueError('Invalid engine result')
            result = sanitize_result(result)
            encoded = json.dumps(result, allow_nan=False)
            if len(encoded.encode()) > 65536:
                result = {'ok': False, 'error': 'result_too_large', 'message': 'Action ran; result exceeded 64KB. Do not retry automatically.'}
        except Exception:
            result = {'ok': False, 'error': 'action_failed', 'message': 'Action failed; side effect may have happened. Do not retry automatically.'}
        with self._db() as db:
            requested = db.execute('SELECT cancel_requested FROM jobs WHERE id=?', (row['id'],)).fetchone()['cancel_requested'] or self._stop.is_set()
            # A completed success stays success (effects are not undone); any failure after a cancel request is cancelled.
            state = 'succeeded' if result.get('ok') else 'cancelled' if requested else 'failed'
            db.execute('UPDATE jobs SET state=?, result=? WHERE id=?', (state, json.dumps(result), row['id']))
        return self.get(row['id'], row['owner'])
