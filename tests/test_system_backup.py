import io
import sqlite3
import tarfile
from pathlib import Path
from cryptography.fernet import Fernet
from scripts.backup_system import backup


def test_system_backup_sqlite_recovery(tmp_path):
    (tmp_path/'data/artifacts').mkdir(parents=True)
    db=sqlite3.connect(tmp_path/'data/airdrop.db');db.execute('PRAGMA journal_mode=WAL');db.execute('CREATE TABLE verify(value TEXT)');db.execute("INSERT INTO verify VALUES ('recover-me')");db.commit()
    artifact=tmp_path/'data/artifacts/example.enc';artifact.write_bytes(b'encrypted-example')
    destination=tmp_path/'system.enc';key=backup(tmp_path,destination)
    decoded=Fernet(key.read_bytes()).decrypt(destination.read_bytes())
    with tarfile.open(fileobj=io.BytesIO(decoded),mode='r:gz') as archive:
        assert 'secrets/master.key' not in archive.getnames()
        restored=tmp_path/'restored.db';restored.write_bytes(archive.extractfile('data/airdrop.db').read())
    recovered=sqlite3.connect(restored)
    assert recovered.execute('PRAGMA integrity_check').fetchone()[0]=='ok'
    assert recovered.execute('SELECT value FROM verify').fetchone()[0]=='recover-me'
    recovered.close();db.close()
