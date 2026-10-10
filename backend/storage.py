import base64
import hashlib
import json
import os
import threading
from contextlib import contextmanager
from pathlib import Path
from cryptography.fernet import Fernet
from backend.config import settings

_artifact_threads = threading.RLock()
_artifact_local = threading.local()


@contextmanager
def artifact_lock():
    """Serialize artifact writes/commits and garbage removal across API processes."""
    with _artifact_threads:
        depth = getattr(_artifact_local, 'depth', 0)
        if depth:
            _artifact_local.depth = depth + 1
            try:yield
            finally:_artifact_local.depth = depth
            return
        settings.data_dir.mkdir(parents=True, exist_ok=True)
        with (settings.data_dir / '.artifacts.lock').open('a+b') as handle:
            if os.name == 'nt':
                import msvcrt
                handle.seek(0, 2)
                if handle.tell() == 0:handle.write(b'0');handle.flush()
                handle.seek(0);msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
            else:
                import fcntl
                os.chmod(handle.name, 0o600)
                fcntl.flock(handle, fcntl.LOCK_EX)
            _artifact_local.depth = 1
            try:yield
            finally:
                _artifact_local.depth = 0
                if os.name == 'nt':
                    handle.seek(0);msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:fcntl.flock(handle, fcntl.LOCK_UN)


def cipher():
    if not settings.master_key_file.is_file():
        raise ValueError("主密钥不存在，请先初始化密钥")
    return Fernet(settings.master_key_file.read_bytes().strip())


def write_blob(data: bytes) -> tuple[str,str]:
    digest = hashlib.sha256(data).hexdigest()
    root = settings.data_dir / 'artifacts'
    root.mkdir(parents=True,exist_ok=True)
    path = root / f'{digest}.enc'
    if not path.exists():
        temp = path.with_suffix(f'.{os.urandom(6).hex()}.tmp')
        try:
            temp.write_bytes(cipher().encrypt(data))
            temp.chmod(0o600)
            os.replace(temp,path)
        finally:
            temp.unlink(missing_ok=True)
    return str(path),digest


def read_blob(path: str, digest: str) -> bytes:
    data = cipher().decrypt(Path(path).read_bytes())
    if hashlib.sha256(data).hexdigest()!=digest:
        raise ValueError("工件校验失败")
    return data


def encode_files(files):
    return json.dumps({k:base64.b64encode(v).decode() for k,v in files.items()},sort_keys=True).encode()


def decode_files(data):
    return {k:base64.b64decode(v,validate=True) for k,v in json.loads(data).items()}


def artifact_fingerprint(artifact) -> str:
    """Stream ciphertext integrity without control-plane bulk decryption."""
    path = Path(artifact.path)
    root = (settings.data_dir / 'artifacts').resolve()
    if path.is_symlink() or path.resolve().parent != root or path.name != artifact.sha256 + '.enc':
        raise ValueError('工件位置无效')
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        while block := stream.read(256_000):
            digest.update(block)
    return digest.hexdigest()
