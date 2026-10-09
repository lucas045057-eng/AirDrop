"""Trusted one-shot initializer; writes only this Compose project's dedicated volumes."""
import hmac
import os
from pathlib import Path

source=Path('/input/master.key').read_bytes().strip()
destination=Path('/service-secrets/master.key')
for root in (Path('/service-secrets'),Path('/app/data'),Path('/bus')):
    root.mkdir(exist_ok=True)
    os.chown(root,10001,10001)
    root.chmod(0o700)
if destination.exists():
    if not hmac.compare_digest(destination.read_bytes().strip(),source):
        raise SystemExit('主密钥与已有数据卷不一致；禁止自动覆盖或重新生成')
else:
    with destination.open('xb') as stream:stream.write(source)
os.chown(destination,10001,10001)
destination.chmod(0o600)
print('本项目数据卷初始化完成。')
