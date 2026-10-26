import sqlite3
import pytest


class Engine:
    def __init__(self):
        self.calls = []
    def execute(self, name, args):
        self.calls.append((name, args))
        return {'ok': True, 'value': args}


def test_recovery_dependencies_cancel_and_bound(tmp_path):
    from companion.queue import JobQueue
    engine = Engine()
    path = tmp_path / 'jobs.db'
    queue = JobQueue(path, engine)
    first = queue.submit('open_app', {}, 'alice', 'first')
    child = queue.submit('open_app', {}, 'alice', 'child', depends_on=first['id'])
    with sqlite3.connect(path) as db:
        db.execute("UPDATE jobs SET state='running' WHERE id=?", (first['id'],))
    queue = JobQueue(path, engine)
    assert queue.get(first['id'], 'alice')['state'] == 'interrupted'
    assert queue.run_next()['state'] == 'cancelled'
    assert queue.get(child['id'], 'alice')['result']['error'] == 'dependency_failed'
    assert not engine.calls
    job = queue.submit('open_app', {}, 'alice', 'cancel')
    assert queue.cancel(job['id'], 'alice')['state'] == 'cancelled'
    with pytest.raises(KeyError):
        queue.submit('open_app', {}, 'bob', 'foreign', depends_on=first['id'])
    for n in range(100):
        queue.submit('open_app', {}, 'alice', str(n))
    with pytest.raises(ValueError):
        queue.submit('open_app', {}, 'alice', 'overflow')


def test_serial_execution_live_cancel_and_safe_results(tmp_path):
    from companion.queue import JobQueue
    import threading
    entered, release = threading.Event(), threading.Event()
    class Blocking:
        def execute(self, name, args):
            entered.set()
            assert release.wait(5)
            return {'ok': True, 'token': 'secret-token', 'message': 'Bearer secret-value'}
    path = tmp_path / 'jobs.db'
    q = JobQueue(path, Blocking())
    a = q.submit('open_app', {}, 'alice', 'a')
    b = q.submit('open_app', {}, 'alice', 'b')
    thread = threading.Thread(target=q.run_next)
    thread.start()
    assert entered.wait(5)
    other = JobQueue(path, Engine())
    assert other.get(a['id'], 'alice')['state'] == 'running'
    assert other.run_next() is None
    assert other.cancel(a['id'], 'alice')['cancel_requested'] is True
    release.set()
    thread.join(5)
    final = q.get(a['id'], 'alice')
    assert final['state'] == 'succeeded'  # cancellation is not undo
    assert 'secret' not in str(final['result'])
    assert q.get(b['id'], 'alice')['state'] == 'queued'
    class Broken:
        def execute(self, *args):
            raise RuntimeError('password=private')
    other.engine = Broken()
    assert other.run_next()['result']['error'] == 'action_failed'
    other.engine = type('Huge', (), {'execute': lambda *args: {'ok': True, 'text': 'a' * 70000}})()
    other.submit('open_app', {}, 'alice', 'huge')
    assert other.run_next()['result']['error'] == 'result_too_large'
    with pytest.raises(ValueError):
        other.submit('open_app', {'text': 'x'*70000}, 'alice', 'large')
    with pytest.raises(ValueError):
        other.submit('open_app', {}, '', '')


def test_queue_cli_and_worker_stop(tmp_path):
    from companion.queue import JobQueue
    import threading
    import subprocess
    import sys
    q = JobQueue(tmp_path / 'jobs.db', Engine())
    q.submit('capabilities', {}, 'alice', 'worker')
    stop = threading.Event()
    thread = threading.Thread(target=q.worker, args=(stop,))
    thread.start()
    import time
    for _ in range(100):
        if q.submit('capabilities', {}, 'alice', 'worker')['state'] == 'succeeded': break
        time.sleep(0.01)
    stop.set()
    thread.join(5)
    assert not thread.is_alive()
    assert q.submit('capabilities', {}, 'alice', 'worker')['state'] == 'succeeded'
    result = subprocess.run([sys.executable, '-m', 'companion', '--help'], capture_output=True, text=True)
    assert result.returncode == 0 and 'submit' in result.stdout


def test_restart_after_real_worker_crash_never_replays(tmp_path):
    from companion.queue import JobQueue
    import subprocess
    import sys
    path = tmp_path / 'jobs.db'
    marker = tmp_path / 'side-effect.txt'
    q = JobQueue(path, Engine())
    job = q.submit('open_app', {}, 'alice', 'crash')
    code = "from companion.queue import JobQueue; from pathlib import Path; import os; class_placeholder"
    code = code.replace('class_placeholder', 'Engine = type(\"Engine\", (), {\"execute\": lambda *a: (Path(' + repr(str(marker)) + ').write_text(\"done\"), os._exit(23))}); JobQueue(' + repr(str(path)) + ', Engine()).run_next()')
    result = subprocess.run([sys.executable, '-c', code], capture_output=True, text=True)
    assert result.returncode == 23, result.stderr
    assert marker.read_text() == 'done'
    reopened = JobQueue(path, Engine())
    assert reopened.get(job['id'], 'alice')['state'] == 'interrupted'
    assert reopened.submit('open_app', {}, 'alice', 'crash')['id'] == job['id']
    assert reopened.run_next() is None


def test_durable_idempotent_owner_scoped_execution(tmp_path):
    from companion.queue import JobQueue
    engine = Engine()
    queue = JobQueue(tmp_path / 'jobs.db', engine)
    job = queue.submit('open_app', {'app': 'chrome'}, 'alice', 'one')
    assert job['state'] == 'queued'
    assert queue.submit('open_app', {'app': 'chrome'}, 'alice', 'one')['id'] == job['id']
    with pytest.raises(ValueError):
        queue.submit('open_app', {'app': 'edge'}, 'alice', 'one')
    with pytest.raises(KeyError):
        queue.get(job['id'], 'bob')
    assert queue.run_next()['state'] == 'succeeded'
    assert queue.run_next() is None
    assert len(engine.calls) == 1
    reopened = JobQueue(tmp_path / 'jobs.db', engine)
    assert reopened.get(job['id'], 'alice')['result']['ok']
    assert reopened.submit('open_app', {'app': 'chrome'}, 'alice', 'one')['state'] == 'succeeded'
