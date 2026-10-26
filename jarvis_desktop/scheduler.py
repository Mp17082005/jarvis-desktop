"""Durable scheduler: SQLite schedules + jarvis/schedule.py timing rules.

- Ticks every 5 s on a daemon thread (or call `tick()` directly with an injectable clock).
- Each fire slot is claimed exactly once with key `schedule:{id}:{slot}` (UNIQUE constraint),
  so restarts or concurrent ticks never double-fire.
- A slot missed by up to one hour (PC asleep, Jarvis closed) still fires once; older missed slots
  are skipped and the schedule advances to its next future slot.
- Firing executes the stored tool directly; it was approved when the schedule was created, so
  no confirmation is asked again. Each fire is announced.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

GRACE_SECONDS = 3600
TICK_SECONDS = 5


def _tz_name():
    try:
        from tzlocal import get_localzone_name  # optional
        return get_localzone_name()
    except Exception:
        pass
    import datetime
    offset = datetime.datetime.now().astimezone().utcoffset()
    return 'Asia/Kolkata' if offset == datetime.timedelta(hours=5, minutes=30) else 'UTC'


class Scheduler:
    def __init__(self, data_dir, fire, announce=None, *, clock=time.time, tz_name=None, grace=GRACE_SECONDS):
        """fire(tool, args) -> result dict; announce(text)."""
        self.path = Path(data_dir) / 'schedules.sqlite3'
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.fire = fire
        self.announce = announce or (lambda text: None)
        self.clock = clock
        self.tz_name = tz_name or _tz_name()
        self.grace = grace
        self._stop = threading.Event()
        self._thread = None
        self._tick_lock = threading.Lock()
        with self._db() as db:
            db.execute('''CREATE TABLE IF NOT EXISTS schedules (
                id TEXT PRIMARY KEY, description TEXT NOT NULL, spec TEXT NOT NULL, tz TEXT NOT NULL,
                tool TEXT NOT NULL, args TEXT NOT NULL, anchor REAL NOT NULL, next_run REAL,
                active INTEGER NOT NULL DEFAULT 1, created REAL NOT NULL)''')
            db.execute('''CREATE TABLE IF NOT EXISTS fires (
                fire_key TEXT PRIMARY KEY, schedule_id TEXT NOT NULL, slot REAL NOT NULL,
                fired_at REAL NOT NULL, status TEXT NOT NULL, result TEXT)''')

    @contextmanager
    def _db(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    # ---- validation helpers --------------------------------------------------------------
    def validate(self, when):
        from jarvis.schedule import validate_schedule, spoken
        spec, tz, first = validate_schedule(when, self.tz_name, self.clock())
        return spec, tz, first, self.describe_when(spec, tz, first)

    @staticmethod
    def describe_when(spec, tz, first):
        from jarvis.schedule import spoken
        when = spoken(first, tz)
        if 'every_minutes' in spec:
            return f'every {spec["every_minutes"]} minutes, first on {when}'
        if 'cron' in spec:
            return f'repeating, next on {when}'
        return f'on {when}'

    # ---- CRUD ----------------------------------------------------------------------------
    def create(self, description, when, tool, args):
        from jarvis.schedule import ScheduleError
        try:
            spec, tz, first, phrase = self.validate(when)
        except ScheduleError as exc:
            return {'ok': False, 'error': 'invalid_schedule', 'message': str(exc)}
        sid = uuid.uuid4().hex[:8]
        with self._db() as db:
            if db.execute('SELECT COUNT(*) FROM schedules WHERE active=1').fetchone()[0] >= 50:
                return {'ok': False, 'error': 'too_many_schedules'}
            db.execute('INSERT INTO schedules VALUES (?,?,?,?,?,?,?,?,?,?)', (sid, str(description)[:500], json.dumps(spec), tz, tool,
                                                                            json.dumps(args), self.clock(), first, 1, self.clock()))
        return {'ok': True, 'schedule_id': sid, 'when': phrase}

    def list(self):
        from jarvis.schedule import spoken
        with self._db() as db:
            rows = db.execute('SELECT * FROM schedules WHERE active=1 ORDER BY next_run').fetchall()
        return [{'schedule_id': r['id'], 'task': r['description'], 'next': spoken(r['next_run'], r['tz']),
                 'repeats': 'at' not in json.loads(r['spec'])} for r in rows]

    def cancel(self, schedule_id):
        with self._db() as db:
            n = db.execute('UPDATE schedules SET active=0 WHERE id=? AND active=1', (str(schedule_id),)).rowcount
        return {'ok': bool(n), 'schedule_id': schedule_id} if n else {'ok': False, 'error': 'not_found'}

    def fires(self, schedule_id=None):
        with self._db() as db:
            q = 'SELECT * FROM fires' + (' WHERE schedule_id=?' if schedule_id else '') + ' ORDER BY slot'
            return [dict(r) for r in db.execute(q, (schedule_id,) if schedule_id else ())]

    # ---- firing --------------------------------------------------------------------------
    def tick(self):
        """Fire every due slot once. Returns list of fired fire_keys."""
        from jarvis.schedule import next_run
        with self._tick_lock:
            now = self.clock()
            fired = []
            with self._db() as db:
                due = db.execute('SELECT * FROM schedules WHERE active=1 AND next_run IS NOT NULL AND next_run<=?', (now,)).fetchall()
            for row in due:
                spec, slot = json.loads(row['spec']), row['next_run']
                # Collapse a backlog to the most recent due slot (never fire a burst of catch-ups).
                following = next_run(spec, row['tz'], slot, anchor=row['anchor'])
                while following is not None and following <= now:
                    slot, following = following, next_run(spec, row['tz'], following, anchor=row['anchor'])
                if slot < now - self.grace:  # missed by more than the grace window: skip it
                    slot = following
                    with self._db() as db:
                        db.execute('UPDATE schedules SET next_run=?, active=? WHERE id=?', (slot, int(slot is not None), row['id']))
                    self._announce_missed(row)
                    continue
                key = f'schedule:{row["id"]}:{int(slot)}'
                with self._db() as db:
                    try:
                        db.execute('INSERT INTO fires VALUES (?,?,?,?,?,?)', (key, row['id'], slot, now, 'claimed', None))
                    except sqlite3.IntegrityError:
                        claimed = False
                    else:
                        claimed = True
                    db.execute('UPDATE schedules SET next_run=?, active=? WHERE id=?', (following, int(following is not None), row['id']))
                if not claimed:
                    continue
                fired.append(key)
                self._run(row, key)
            return fired

    def _announce_missed(self, row):
        try:
            self.announce(f'I missed the scheduled task {row["description"]} because the computer was off or asleep, so I skipped it.')
        except Exception:
            pass

    def _run(self, row, key):
        try:
            self.announce(f'Running your scheduled task: {row["description"]}.')
        except Exception:
            pass
        try:
            result = self.fire(row['tool'], json.loads(row['args']))
        except Exception:
            result = {'ok': False, 'error': 'fire_failed'}
        status = 'started' if isinstance(result, dict) and result.get('ok') else 'failed'
        with self._db() as db:
            db.execute('UPDATE fires SET status=?, result=? WHERE fire_key=?', (status, json.dumps(result, default=str)[:4000], key))
        if status == 'failed':
            try:
                self.announce(f"The scheduled task {row['description']} couldn't start.")
            except Exception:
                pass

    # ---- thread --------------------------------------------------------------------------
    def start(self, interval=TICK_SECONDS):
        if self._thread is None or not self._thread.is_alive():
            self._stop.clear()

            def loop():
                while not self._stop.is_set():
                    try:
                        self.tick()
                    except Exception:
                        pass
                    self._stop.wait(interval)
            self._thread = threading.Thread(target=loop, name='jarvis-scheduler', daemon=True)
            self._thread.start()
        return self

    def close(self):
        self._stop.set()
        if self._thread:
            self._thread.join(5)
