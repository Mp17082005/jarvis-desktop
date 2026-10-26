"""UFO runner tests use an inert fake UFO checkout; no real desktop or provider call."""
import ctypes
import json
import sys
import threading
import time

import pytest

from companion.actions import ACTIONS, ActionEngine, action_risk
from companion.ufo import UFORunner

FAKE = r'''
import argparse, json, os, subprocess, sys, time
p = argparse.ArgumentParser()
p.add_argument('--task'); p.add_argument('--mode'); p.add_argument('--request'); p.add_argument('--plan', default=''); p.add_argument('--log-level')
a = p.parse_args()
json.dump({'argv': sys.argv[1:], 'env': sorted(os.environ), 'stdin': sys.stdin.read(), 'cwd': os.getcwd()}, open('seen-' + a.task + '.json', 'w'))
log = os.path.join('logs', a.task)
os.makedirs(log, exist_ok=True)
def evaluation(value):
    open(os.path.join(log, 'evaluation.log'), 'w').write(json.dumps({'complete': value, 'type': 'evaluation_result', 'reason': 'ignore me'}) + '\n')
r = a.request
if r.startswith('hang'):
    child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)'])
    open('child.pid', 'w').write(str(child.pid))
    time.sleep(120)
elif r.startswith('silent'):
    sys.exit(0)
elif r.startswith('crash'):
    evaluation('yes'); sys.exit(3)
elif r.startswith('no'):
    evaluation('no')
elif r.startswith('unsure'):
    evaluation('unsure')
elif r.startswith('garbage'):
    open(os.path.join(log, 'evaluation.log'), 'w').write('{not json\n')
else:
    evaluation('yes')
'''


def alive(pid):
    handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)
    if not handle:
        return False
    code = ctypes.c_ulong()
    ctypes.windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
    ctypes.windll.kernel32.CloseHandle(handle)
    return code.value == 259


def wait_for(path):
    end = time.monotonic() + 15
    while not path.exists() and time.monotonic() < end:
        time.sleep(0.05)
    assert path.exists()


def make_home(root):
    (root / 'ufo').mkdir(parents=True)
    (root / 'ufo' / '__init__.py').write_text('')
    (root / 'ufo' / '__main__.py').write_text(FAKE)
    return root


@pytest.fixture
def home(tmp_path):
    return make_home(tmp_path / 'ufo')


def runner(home, **kw):
    return UFORunner(home=str(home), python=sys.executable, enabled=True, **{'timeout': 20, **kw})


def seen(home):
    return [json.loads(p.read_text()) for p in home.glob('seen-*.json')]


def test_ufo_is_opt_in_and_requires_exact_existing_approval(tmp_path, monkeypatch):
    monkeypatch.delenv('JARVIS_UFO_ENABLED', raising=False)
    monkeypatch.delenv('JARVIS_UFO_HOME', raising=False)
    schemas = {s['function']['name']: s['function'] for s in ACTIONS}
    assert 'ufo_task' in schemas
    assert action_risk('ufo_task') is True
    assert 'ufo_status' not in schemas and 'ufo_cancel' not in schemas
    assert schemas['ufo_task']['parameters']['additionalProperties'] is False
    engine = ActionEngine(tmp_path)
    capability = engine.capabilities()['ufo']
    assert capability['enabled'] is False and capability['available'] is False
    assert capability['tested'] is False
    assert engine.execute('ufo_task', {'request': 'Open Notepad'})['error'] == 'ufo_unavailable'


def test_ufo_rejects_invalid_requests_before_launch(tmp_path):
    engine = ActionEngine(tmp_path)
    for args in ({}, {'request': ''}, {'request': ' '}, {'request': 4}, {'request': 'x' * 2001},
                 {'request': 'x\x00y'}, {'request': 'open', 'plan': '../../secret'},
                 {'request': 'open', 'timeout': 99999}, None):
        assert engine.execute('ufo_task', args)['error'] == 'invalid_arguments'


def test_capability_requires_enabled_home_and_interpreter(tmp_path, home):
    assert UFORunner(home=str(home), python=sys.executable, enabled=False).capabilities()['available'] is False
    assert UFORunner(home='relative', python=sys.executable, enabled=True).capabilities()['available'] is False
    assert UFORunner(home=str(tmp_path / 'missing'), python=sys.executable, enabled=True).capabilities()['available'] is False
    assert UFORunner(home=str(home), python=str(tmp_path / 'nope.exe'), enabled=True).capabilities()['available'] is False
    ok = runner(home).capabilities()
    assert ok['available'] is True and ok['configured'] is True and ok['tested'] is False
    assert str(home) not in json.dumps(ok)


def test_default_interpreter_is_checkout_venv(tmp_path):
    assert UFORunner(home=str(tmp_path), enabled=True).python == str(tmp_path / '.venv' / 'Scripts' / 'python.exe')


def test_success_comes_from_evaluation_not_exit_code(home):
    result = runner(home).run({'request': 'ok please'})
    assert result['ok'] is True and result['outcome'] == 'completed'
    assert 'ignore me' not in json.dumps(result) and str(home) not in json.dumps(result)


def test_exit_zero_without_evaluation_is_uncertain(home):
    result = runner(home).run({'request': 'silent'})
    assert result['ok'] is False and result['outcome'] == 'uncertain'


def test_negative_unsure_garbage_and_nonzero_are_never_success(home):
    for request, outcome in (('no', 'not_completed'), ('unsure', 'uncertain'), ('garbage', 'uncertain'), ('crash', 'failed')):
        result = runner(home).run({'request': request})
        assert result['ok'] is False and result['outcome'] == outcome, request


def test_argv_is_exact_single_values_no_shell_stdin_closed_and_env_scrubbed(home, monkeypatch):
    monkeypatch.setenv('JARVIS_SESSION_SECRET', 'sentinel')
    monkeypatch.setenv('ALLOWED_THING', '1')
    request = '--plan C:\\secret.json & calc "quoted" %PATH% ok'
    assert runner(home, env_allowlist=('ALLOWED_THING',)).run({'request': request})['ok'] is True
    (record,) = seen(home)
    argv = record['argv']
    assert f'--request={request}' in argv
    assert not any(a.startswith('--plan') for a in argv)
    assert sum(a.startswith('--mode') for a in argv) == 1 and '--mode=normal' in argv
    assert record['stdin'] == '' and record['cwd'].casefold() == str(home).casefold()
    assert 'JARVIS_SESSION_SECRET' not in record['env'] and 'ALLOWED_THING' in record['env']


def test_jarvis_approval_carries_into_ufo_sensitive_steps(home, monkeypatch):
    # The task already passed Jarvis's exact approval; UFO's own headless safeguard
    # must not silently deny the approved save/close steps.
    monkeypatch.delenv('UFO_AUTO_APPROVE_SENSITIVE', raising=False)
    assert runner(home).run({'request': 'ok'})['ok'] is True
    (record,) = seen(home)
    assert 'UFO_AUTO_APPROVE_SENSITIVE' in record['env']


def test_whitespace_in_checkout_path(tmp_path):
    root = make_home(tmp_path / 'path with spaces' / 'UFO')
    assert runner(root).run({'request': 'ok'})['outcome'] == 'completed'


def test_timeout_kills_whole_process_tree(home):
    started = time.monotonic()
    result = runner(home, timeout=3).run({'request': 'hang'})
    assert result['ok'] is False and result['outcome'] == 'timeout' and time.monotonic() - started < 15
    pid = int((home / 'child.pid').read_text())
    time.sleep(0.5)
    assert not alive(pid)


def test_cancel_check_kills_tree_and_reports_uncertain_side_effects(home):
    result = runner(home).run({'request': 'hang'}, cancel_check=lambda: (home / 'child.pid').exists())
    assert result['ok'] is False and result['outcome'] == 'cancelled' and 'may' in result['message']
    time.sleep(0.5)
    assert not alive(int((home / 'child.pid').read_text()))


def test_broken_cancel_callback_fails_closed(home):
    def broken():
        raise RuntimeError('boom')
    assert runner(home).run({'request': 'hang'}, cancel_check=broken)['outcome'] == 'cancelled'


def test_only_one_run_at_a_time(home):
    r = runner(home)
    results = []
    thread = threading.Thread(target=lambda: results.append(r.run({'request': 'hang'})))
    thread.start()
    wait_for(home / 'child.pid')
    assert r.run({'request': 'ok'})['error'] == 'ufo_busy'
    r.cancel()
    thread.join(15)
    assert results[0]['outcome'] == 'cancelled'


def test_engine_forwards_cancel_and_close_cancels_active_run(tmp_path, home):
    engine = ActionEngine(tmp_path)
    engine._ufo = runner(home)
    out = []
    thread = threading.Thread(target=lambda: out.append(engine.execute_cancellable('ufo_task', {'request': 'hang'}, lambda: False)))
    thread.start()
    wait_for(home / 'child.pid')
    engine.close()
    thread.join(15)
    assert out[0]['outcome'] == 'cancelled'
    assert not alive(int((home / 'child.pid').read_text()))
    assert engine.execute_cancellable('pause', 'bad', lambda: False)['ok'] is False


def test_engine_cancel_check_is_forwarded(tmp_path, home):
    engine = ActionEngine(tmp_path)
    engine._ufo = runner(home)
    result = engine.execute_cancellable('ufo_task', {'request': 'hang'}, lambda: (home / 'child.pid').exists())
    assert result['outcome'] == 'cancelled'


def test_old_run_logs_are_pruned_only_for_jarvis_task_dirs(home):
    logs = home / 'logs'
    (logs / 'user-task').mkdir(parents=True)
    r = runner(home, retain=2)
    for _ in range(4):
        assert r.run({'request': 'ok'})['ok']
    assert (logs / 'user-task').is_dir()
    assert len([p for p in logs.iterdir() if p.name.startswith('jarvis-')]) == 2
