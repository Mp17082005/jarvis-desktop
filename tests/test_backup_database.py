import importlib.util
import sqlite3
from pathlib import Path
import pytest


def test_online_backup_includes_committed_wal_and_refuses_overwrite(tmp_path):
    path=Path(__file__).resolve().parents[1]/'scripts/backup_database.py'
    assert path.exists(), 'Backup implementation not available'
    spec=importlib.util.spec_from_file_location('backup_database',path)
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    source=tmp_path/'source.sqlite';dest=tmp_path/'backup.sqlite'
    with sqlite3.connect(source) as db:
        db.execute('PRAGMA journal_mode=WAL');db.execute('CREATE TABLE data(value TEXT)');db.execute('INSERT INTO data VALUES (?)',('verified',));db.commit()
        module.backup_database(source,dest)
    with sqlite3.connect(dest) as db:
        assert db.execute('SELECT value FROM data').fetchone()[0]=='verified'
        assert db.execute('PRAGMA integrity_check').fetchone()[0]=='ok'
    with pytest.raises(ValueError):module.backup_database(source,dest)
    with pytest.raises(ValueError):module.backup_database(source,source)
