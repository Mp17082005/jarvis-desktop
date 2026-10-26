"""Local command line only; exposes no HTTP or shell-execution endpoint."""
import argparse
import json
from pathlib import Path
import threading

from .actions import ActionEngine
from .queue import JobQueue


def main():
    parser = argparse.ArgumentParser(description='Local companion durable jobs')
    parser.add_argument('--workspace', type=Path, default=Path.cwd())
    parser.add_argument('--db', type=Path, default=None)
    commands = parser.add_subparsers(dest='command', required=True)
    submit = commands.add_parser('submit')
    submit.add_argument('action')
    submit.add_argument('--args', default='{}', help='JSON action arguments')
    submit.add_argument('--owner', required=True)
    submit.add_argument('--key', required=True, help='Stable idempotency key')
    submit.add_argument('--depends-on')
    for name in ('status', 'cancel'):
        cmd = commands.add_parser(name)
        cmd.add_argument('id')
        cmd.add_argument('--owner', required=True)
    worker = commands.add_parser('worker')
    worker.add_argument('--once', action='store_true')
    options = parser.parse_args()
    engine = ActionEngine(options.workspace)
    queue = JobQueue(options.db or options.workspace / 'data' / 'companion-jobs.sqlite3', engine)
    try:
        if options.command == 'submit':
            result = queue.submit(options.action, json.loads(options.args), options.owner, options.key, options.depends_on)
        elif options.command in ('status', 'cancel'):
            result = getattr(queue, 'get' if options.command == 'status' else 'cancel')(options.id, options.owner)
        elif options.once:
            result = queue.run_next()
        else:
            try:
                queue.worker(threading.Event())
            except KeyboardInterrupt:
                pass
            return
        print(json.dumps(result, ensure_ascii=True))
    except (ValueError, KeyError) as error:
        parser.error(str(error))
    finally:
        if hasattr(engine, '_youtube'):
            engine._youtube.close()


if __name__ == '__main__':
    main()
