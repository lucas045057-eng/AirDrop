"""Stream encrypted system backups; restore only into a new installation directory."""
import argparse
import os
import shutil
import sqlite3
import struct
import tarfile
import tempfile
from pathlib import Path, PurePosixPath

from cryptography.fernet import Fernet, InvalidToken

MAGIC = b'AIRDROP-SYSTEM-BACKUP-V2\n'
CHUNK_BYTES = 1024 * 1024
FRAME = struct.Struct('>I')
MAX_FRAME_BYTES = CHUNK_BYTES * 2 + 4096


class EncryptedChunkWriter:
    """Encrypt bounded tar stream chunks as independent authenticated Fernet tokens."""

    def __init__(self, output, cipher, chunk_bytes=CHUNK_BYTES):
        self.output = output
        self.cipher = cipher
        self.chunk_bytes = chunk_bytes
        self.buffer = bytearray()
        self.finished = False

    def write(self, data):
        if self.finished:
            raise ValueError('备份加密流已结束')
        view = memoryview(data)
        written = len(view)
        while view:
            take = min(self.chunk_bytes - len(self.buffer), len(view))
            self.buffer.extend(view[:take])
            view = view[take:]
            if len(self.buffer) == self.chunk_bytes:
                self._write_frame()
        return written

    def _write_frame(self):
        if not self.buffer:
            return
        token = self.cipher.encrypt(bytes(self.buffer))
        self.output.write(FRAME.pack(len(token)))
        self.output.write(token)
        self.buffer.clear()

    def flush(self):
        self._write_frame()
        self.output.flush()

    def finish(self):
        if self.finished:
            return
        self._write_frame()
        self.output.write(FRAME.pack(0))
        self.output.flush()
        self.finished = True


def referenced_artifacts(snapshot, project):
    """Reject missing or out-of-tree blobs so every database reference is restorable."""
    connection = sqlite3.connect(f'file:{snapshot.as_posix()}?mode=ro', uri=True)
    try:
        try:
            rows = [row[0] for row in connection.execute('SELECT path FROM artifacts')]
        except sqlite3.OperationalError:
            return []
    finally:
        connection.close()
    root = (project / 'data/artifacts').resolve()
    missing, unsupported = [], []
    for value in rows:
        path = Path(str(value))
        target = path if path.is_absolute() else project / path
        try:
            resolved = target.resolve(strict=True)
        except OSError:
            missing.append(str(value))
            continue
        if target.is_symlink() or resolved.parent != root or not resolved.is_file():
            unsupported.append(str(value))
    if missing:
        raise ValueError('以下加密工件在磁盘上缺失，请先核对存储再备份：' + ', '.join(sorted(missing)[:10]))
    if unsupported:
        raise ValueError('以下加密工件不在 data/artifacts 根目录，无法安全恢复：' + ', '.join(sorted(unsupported)[:10]))
    return rows


def write_encrypted_archive(snapshot, artifacts, destination, key):
    cipher = Fernet(key)
    with destination.open('wb') as output:
        output.write(MAGIC)
        encrypted = EncryptedChunkWriter(output, cipher)
        with tarfile.open(fileobj=encrypted, mode='w|gz') as archive:
            archive.add(snapshot, arcname='data/airdrop.db')
            if artifacts.exists():
                archive.add(artifacts, arcname='data/artifacts')
        encrypted.finish()
        output.flush()
        os.fsync(output.fileno())


def backup(project, destination):
    project = Path(project).resolve()
    destination = Path(destination).resolve()
    key_path = destination.with_suffix(destination.suffix + '.key')
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists() or key_path.exists():
        raise ValueError('备份或备份密钥文件已存在，禁止覆盖')

    archive_fd, archive_temp_name = tempfile.mkstemp(prefix='.airdrop-backup-', suffix='.tmp',
                                                      dir=destination.parent)
    os.close(archive_fd)
    key_fd, key_temp_name = tempfile.mkstemp(prefix='.airdrop-backup-key-', suffix='.tmp',
                                             dir=destination.parent)
    os.close(key_fd)
    archive_temp, key_temp = Path(archive_temp_name), Path(key_temp_name)
    published_archive = published_key = False
    try:
        with tempfile.TemporaryDirectory(prefix='airdrop-backup-') as directory:
            snapshot = Path(directory) / 'airdrop.db'
            source = sqlite3.connect(f'file:{(project / "data/airdrop.db").as_posix()}?mode=ro', uri=True)
            target = sqlite3.connect(snapshot)
            try:
                source.backup(target)
            finally:
                source.close()
                target.close()
            referenced_artifacts(snapshot, project)
            key = Fernet.generate_key()
            write_encrypted_archive(snapshot, project / 'data/artifacts', archive_temp, key)
        archive_temp.chmod(0o600)
        key_temp.write_bytes(key)
        key_temp.chmod(0o600)
        # Hard links publish complete files atomically and refuse to overwrite a concurrent backup.
        os.link(archive_temp, destination)
        published_archive = True
        os.link(key_temp, key_path)
        published_key = True
        return key_path
    except Exception:
        if published_archive:
            destination.unlink(missing_ok=True)
        if published_key:
            key_path.unlink(missing_ok=True)
        raise
    finally:
        archive_temp.unlink(missing_ok=True)
        key_temp.unlink(missing_ok=True)


def decrypt_backup_to(source_path, key, destination):
    """Decrypt v2 frames incrementally; accept legacy Fernet tokens for recovery."""
    cipher = Fernet(key)
    with Path(source_path).open('rb') as source, Path(destination).open('wb') as output:
        header = source.read(len(MAGIC))
        if header != MAGIC:
            try:
                output.write(cipher.decrypt(header + source.read()))
            except InvalidToken:
                raise ValueError('备份格式或密钥无效') from None
            return
        while True:
            size_bytes = source.read(FRAME.size)
            if len(size_bytes) != FRAME.size:
                raise ValueError('加密备份被截断，缺少结束标记')
            size = FRAME.unpack(size_bytes)[0]
            if size == 0:
                if source.read(1):
                    raise ValueError('加密备份结束标记后存在多余数据')
                break
            if size > MAX_FRAME_BYTES:
                raise ValueError('加密备份分段超过允许大小')
            token = source.read(size)
            if len(token) != size:
                raise ValueError('加密备份分段不完整')
            try:
                chunk = cipher.decrypt(token)
            except InvalidToken:
                raise ValueError('备份分段校验失败或密钥无效') from None
            if not chunk or len(chunk) > CHUNK_BYTES:
                raise ValueError('加密备份分段长度无效')
            output.write(chunk)


def _extract_member(archive, member, staging):
    if not member.name or any(character in member.name for character in ('\\', '\x00', ':')):
        raise ValueError('备份包含越界路径')
    path = PurePosixPath(member.name)
    if path.is_absolute() or '..' in path.parts or not path.parts:
        raise ValueError('备份包含越界路径')
    allowed = path == PurePosixPath('data') or path == PurePosixPath('data/airdrop.db') \
        or path == PurePosixPath('data/artifacts') or path.parts[:2] == ('data', 'artifacts')
    if not allowed:
        raise ValueError('备份包含不支持的路径：' + member.name[:160])
    target = staging.joinpath(*path.parts)
    if member.isdir():
        target.mkdir(parents=True, exist_ok=True)
        target.chmod(0o700)
        return
    if member.type not in {tarfile.REGTYPE, tarfile.AREGTYPE} or target.exists():
        raise ValueError('备份包含链接、特殊文件或重复路径：' + member.name[:160])
    target.parent.mkdir(parents=True, exist_ok=True)
    source = archive.extractfile(member)
    if source is None:
        raise ValueError('备份文件内容无法读取：' + member.name[:160])
    copied = 0
    with source, target.open('xb') as output:
        while block := source.read(256 * 1024):
            output.write(block)
            copied += len(block)
    if copied != member.size:
        target.unlink(missing_ok=True)
        raise ValueError('备份文件长度与归档记录不符：' + member.name[:160])
    target.chmod(0o600)


def _validate_and_relocate_database(database, staging, destination):
    connection = sqlite3.connect(database)
    try:
        if connection.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
            raise ValueError('恢复后的数据库完整性检查失败')
        try:
            columns = {row[1] for row in connection.execute('PRAGMA table_info(artifacts)')}
            if not columns:
                return
            if not {'id', 'path', 'sha256'} <= columns:
                raise ValueError('恢复数据库的工件表结构不受支持')
            rows = connection.execute('SELECT id,sha256 FROM artifacts').fetchall()
        except sqlite3.OperationalError:
            return
        for identity, digest in rows:
            name = str(digest) + '.enc'
            source = staging / 'data/artifacts' / name
            if not source.is_file() or source.is_symlink():
                raise ValueError('恢复归档缺少数据库引用的加密工件：' + name)
            restored_path = destination / 'data/artifacts' / name
            connection.execute('UPDATE artifacts SET path=? WHERE id=?', (str(restored_path), identity))
        if connection.execute('PRAGMA foreign_key_check').fetchone() is not None:
            raise ValueError('恢复后的数据库包含外键错误')
        connection.commit()
    finally:
        connection.close()


def restore_system(backup_path, key_path, destination):
    """Restore and verify a backup atomically into a new directory."""
    backup_path, key_path = Path(backup_path).resolve(), Path(key_path).resolve()
    destination = Path(destination).resolve()
    if destination.exists():
        raise ValueError('恢复目标必须是尚不存在的新目录')
    if not backup_path.is_file() or not key_path.is_file():
        raise ValueError('备份文件或独立密钥文件不存在')
    destination.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix='.airdrop-restore-', dir=destination.parent))
    try:
        archive_path = staging / '.restore.tar.gz'
        decrypt_backup_to(backup_path, key_path.read_bytes().strip(), archive_path)
        with tarfile.open(archive_path, mode='r|gz') as archive:
            for member in archive:
                _extract_member(archive, member, staging)
        archive_path.unlink(missing_ok=True)
        database = staging / 'data/airdrop.db'
        if not database.is_file():
            raise ValueError('备份中缺少 data/airdrop.db')
        _validate_and_relocate_database(database, staging, destination)
        database.chmod(0o600)
        staging.rename(destination)
        return destination
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='流式加密备份系统数据，或恢复到新的安装目录')
    parser.add_argument('destination', type=Path, nargs='?', help='新备份文件路径')
    parser.add_argument('--restore-archive', type=Path, help='待恢复的加密备份文件')
    parser.add_argument('--key', type=Path, help='独立保存的备份密钥')
    parser.add_argument('--restore-to', type=Path, help='尚不存在的新恢复目录')
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    if args.restore_archive:
        if not args.key or not args.restore_to:
            parser.error('--restore-archive 需要同时提供 --key 和 --restore-to')
        result = restore_system(args.restore_archive, args.key, args.restore_to)
        print(f'已恢复并验证数据库与加密工件：{result}')
    else:
        if not args.destination:
            parser.error('备份时必须提供新备份文件路径')
        key = backup(root, args.destination.resolve())
        print(f'已生成加密系统备份：{args.destination}。请把 {key} 与主密钥分别保管。')
