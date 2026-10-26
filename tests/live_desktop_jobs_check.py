"""Live check: a real ufo_task through jarvis_desktop.jobs (UFO uses the running 8780 proxy)."""
import os
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
UFO_HOME = Path(os.environ['JARVIS_UFO_HOME'])


def main():
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    from jarvis_desktop.jobs import JobRunner
    marker = f'Jarvis desktop job check {int(time.time())}'
    out = ROOT / 'workspace' / 'desktop' / f'jobs-note-{int(time.time())}.txt'
    out.parent.mkdir(parents=True, exist_ok=True)
    said = []
    jobs = JobRunner(Path(tempfile.mkdtemp(prefix='jarvis-jobs-')), UFO_HOME, said.append).start()
    request = f'Open Notepad, type exactly: {marker} and save the file as {out} then close Notepad.'
    started = time.monotonic()
    job = jobs.submit(request)
    print('SUBMITTED', job['id'], job['state'])
    while time.monotonic() - started < 900 and not said:
        time.sleep(2)
    print('ANNOUNCED:', said, f'after {time.monotonic() - started:.0f}s')
    print('STATUS:', jobs.status(job['id']))
    content = out.read_text(encoding='utf-8', errors='replace').strip() if out.exists() else None
    print('FILE EXISTS:', out.exists(), 'CONTENT MATCH:', content == marker, repr(content))
    jobs.close()


if __name__ == '__main__':
    main()
