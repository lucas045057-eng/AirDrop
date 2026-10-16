import hashlib
import io
import random
import sqlite3
import tarfile
from pathlib import Path

import pytest
from cryptography.fernet import Fernet

from scripts import backup_system
from scripts.backup_system import backup, referenced_artifacts, restore_system


def make_database(root, artifacts=None, with_table=True):
    """Create a minimal backup database; artifacts maps ids to relative paths."""
    (root / 'data/artifacts').mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(root / 'data/airdrop.db')
    if with_table:
        db.execute('CREATE TABLE artifacts(id TEXT PRIMARY KEY, path TEXT, sha256 TEXT)')
        for identity, value in (artifacts or {}).items():
            db.execute('INSERT INTO artifacts VALUES (?,?,?)', (identity, str(value), Path(value).stem))
    else:
        db.execute('CREATE TABLE verify(value TEXT)')
    db.commit()
    db.close()


def test_backup_requires_every_referenced_artifact_before_writing(tmp_path):
    present = tmp_path / 'data/artifacts/present.enc'
    make_database(tmp_path, {'a': present})
    present.write_bytes(b'encrypted-input-snapshot')
    assert referenced_artifacts(tmp_path / 'data/airdrop.db', tmp_path) == [str(present)]
    key = backup(tmp_path, tmp_path / 'system.enc')
    assert key.is_file() and (tmp_path / 'system.enc').is_file()
    db = sqlite3.connect(tmp_path / 'data/airdrop.db')
    db.execute('INSERT INTO artifacts VALUES (?,?,?)',
               ('b', str(tmp_path / 'data/artifacts/gone.enc'), 'gone'))
    db.commit()
    db.close()
    with pytest.raises(ValueError, match='加密工件在磁盘上缺失'):
        backup(tmp_path, tmp_path / 'broken.enc')
    assert not (tmp_path / 'broken.enc').exists() and not (tmp_path / 'broken.enc.key').exists()


def test_artifact_check_handles_relative_paths_and_missing_table(tmp_path):
    make_database(tmp_path, {'a': 'data/artifacts/relative.enc'})
    (tmp_path / 'data/artifacts/relative.enc').write_bytes(b'encrypted')
    assert referenced_artifacts(tmp_path / 'data/airdrop.db', tmp_path) == ['data/artifacts/relative.enc']
    (tmp_path / 'data/artifacts/relative.enc').unlink()
    with pytest.raises(ValueError, match='加密工件在磁盘上缺失'):
        referenced_artifacts(tmp_path / 'data/airdrop.db', tmp_path)
    make_database(tmp_path / 'legacy', with_table=False)
    assert referenced_artifacts(tmp_path / 'legacy/data/airdrop.db', tmp_path / 'legacy') == []


def test_streaming_backup_restores_large_artifact_and_relocates_database_paths(tmp_path):
    project = tmp_path / 'source'
    (project / 'data/artifacts').mkdir(parents=True)
    db = sqlite3.connect(project / 'data/airdrop.db')
    db.execute('PRAGMA journal_mode=WAL')
    db.execute('CREATE TABLE artifacts(id TEXT PRIMARY KEY, path TEXT, sha256 TEXT)')
    db.execute('CREATE TABLE verify(value TEXT)')
    db.execute("INSERT INTO verify VALUES ('recover-me')")

    digest = hashlib.sha256()
    artifact = project / 'data/artifacts/pending.enc'
    randomizer = random.Random(90210)
    with artifact.open('wb') as stream:
        for _ in range(64):
            block = randomizer.randbytes(128 * 1024)
            digest.update(block)
            stream.write(block)
    sha = digest.hexdigest()
    final_artifact = project / 'data/artifacts' / (sha + '.enc')
    artifact.rename(final_artifact)
    db.execute('INSERT INTO artifacts VALUES (?,?,?)', ('a', str(final_artifact), sha))
    db.commit()
    db.close()

    destination = tmp_path / 'system.enc'
    key_path = backup(project, destination)
    assert destination.read_bytes().startswith(backup_system.MAGIC)
    restored = restore_system(destination, key_path, tmp_path / 'restored')

    recovered = sqlite3.connect(restored / 'data/airdrop.db')
    assert recovered.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
    assert recovered.execute('SELECT value FROM verify').fetchone()[0] == 'recover-me'
    path, = recovered.execute('SELECT path FROM artifacts WHERE id=?', ('a',)).fetchone()
    recovered.close()
    restored_artifact = Path(path)
    assert restored_artifact == restored / 'data/artifacts' / (sha + '.enc')
    check = hashlib.sha256()
    with restored_artifact.open('rb') as stream:
        while block := stream.read(256 * 1024):
            check.update(block)
    assert check.hexdigest() == sha
    with pytest.raises(ValueError, match='尚不存在'):
        restore_system(destination, key_path, restored)


def test_interrupted_backup_never_publishes_partial_archive_or_key(tmp_path, monkeypatch):
    project = tmp_path / 'source'
    make_database(project, with_table=False)

    def interrupt(*args, **kwargs):
        args[2].write_bytes(b'partial')
        raise OSError('simulated interruption')

    monkeypatch.setattr(backup_system, 'write_encrypted_archive', interrupt)
    destination = tmp_path / 'interrupted.enc'
    with pytest.raises(OSError, match='simulated interruption'):
        backup(project, destination)
    assert not destination.exists() and not destination.with_suffix('.enc.key').exists()


def test_restore_rejects_tampered_stream_and_cleans_staging_directory(tmp_path):
    project = tmp_path / 'source'
    make_database(project, with_table=False)
    destination = tmp_path / 'system.enc'
    key = backup(project, destination)
    damaged = tmp_path / 'damaged.enc'
    data = bytearray(destination.read_bytes())
    data[-5] ^= 1
    damaged.write_bytes(data)

    with pytest.raises(ValueError, match='校验失败|结束标记'):
        restore_system(damaged, key, tmp_path / 'restored')
    assert not (tmp_path / 'restored').exists()


def test_restore_rejects_windows_path_separators_in_archive_members(tmp_path):
    archive_bytes = io.BytesIO()
    name = 'data/artifacts/..\\..\\..\\escape.txt'
    with tarfile.open(fileobj=archive_bytes, mode='w') as archive:
        member = tarfile.TarInfo(name)
        member.size = len(b'outside')
        archive.addfile(member, io.BytesIO(b'outside'))

    staging = tmp_path / 'staging'
    staging.mkdir()
    with tarfile.open(fileobj=io.BytesIO(archive_bytes.getvalue()), mode='r:') as archive:
        with pytest.raises(ValueError, match='越界路径'):
            backup_system._extract_member(archive, archive.getmembers()[0], staging)

    assert not (tmp_path / 'escape.txt').exists()


def test_legacy_fernet_backup_can_still_be_decrypted(tmp_path):
    key = Fernet.generate_key()
    source = tmp_path / 'legacy.enc'
    source.write_bytes(Fernet(key).encrypt(b'legacy archive bytes'))
    decrypted = tmp_path / 'legacy.tar.gz'

    backup_system.decrypt_backup_to(source, key, decrypted)

    assert decrypted.read_bytes() == b'legacy archive bytes'
