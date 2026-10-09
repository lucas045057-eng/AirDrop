"""Consistent encrypted system archive; recover only into a new installation directory."""
import argparse
import io
import json
import sqlite3
import tarfile
import tempfile
from pathlib import Path
from cryptography.fernet import Fernet


def backup(project,destination):
    key_path=destination.with_suffix(destination.suffix+'.key')
    if destination.exists() or key_path.exists():raise ValueError('备份或备份密钥文件已存在，禁止覆盖')
    with tempfile.TemporaryDirectory(prefix='airdrop-backup-') as directory:
        snapshot=Path(directory)/'airdrop.db'
        # SQLite backup API includes committed WAL contents without copying an active file.
        source=sqlite3.connect(f'file:{(project/"data/airdrop.db").as_posix()}?mode=ro',uri=True)
        target=sqlite3.connect(snapshot)
        try:source.backup(target)
        finally:source.close();target.close()
        output=io.BytesIO()
        with tarfile.open(fileobj=output,mode='w:gz') as archive:
            archive.add(snapshot,arcname='data/airdrop.db')
            artifacts=project/'data/artifacts'
            if artifacts.exists():archive.add(artifacts,arcname='data/artifacts')
        key=Fernet.generate_key()
        with destination.open('xb') as stream:stream.write(Fernet(key).encrypt(output.getvalue()))
        destination.chmod(0o600)
        with key_path.open('xb') as stream:stream.write(key)
        key_path.chmod(0o600)
        return key_path


if __name__=='__main__':
    parser=argparse.ArgumentParser(description='加密备份本项目数据库与脚本工件，不包含主密钥')
    parser.add_argument('destination',type=Path)
    args=parser.parse_args()
    root=Path(__file__).resolve().parents[1]
    key=backup(root,args.destination.resolve())
    print(f'已生成加密系统备份：{args.destination}。请把 {key} 与主密钥分别保管。')
