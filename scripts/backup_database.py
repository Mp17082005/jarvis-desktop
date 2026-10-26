"""SQLite online backup. Explicit local path; never uploads or copies secrets."""
import argparse
import sqlite3
from pathlib import Path


def backup_database(source, destination):
    source, destination = Path(source).resolve(), Path(destination).resolve()
    if not source.is_file():
        raise ValueError('Source database does not exist')
    if source == destination or destination.exists():
        raise ValueError('Destination must be a new distinct file')
    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination.open('xb'):
        pass
    with sqlite3.connect(source.as_uri()+'?mode=ro',uri=True) as src:
        with sqlite3.connect(destination) as dst:
            src.backup(dst)
            if dst.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                raise ValueError('Backup integrity check failed')
    return destination


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('source');parser.add_argument('destination')
    args=parser.parse_args()
    result=backup_database(args.source,args.destination)
    print('Verified local SQLite backup:',result)
