"""Generate local secrets once; never overwrite an existing configuration."""
from pathlib import Path
import secrets
from cryptography.fernet import Fernet

root = Path(__file__).resolve().parents[1]
secret_dir = root / "secrets"
secret_dir.mkdir(mode=0o700,exist_ok=True)
key = secret_dir / "master.key"
if not key.exists():
    key.write_bytes(Fernet.generate_key())
    key.chmod(0o600)
env = root / ".env"
if not env.exists():
    password = secrets.token_urlsafe(20)
    env.write_text(f"AIRDROP_ADMIN_USERNAME=admin\nAIRDROP_ADMIN_PASSWORD={password}\nAIRDROP_EXECUTION_ENABLED=false\n", encoding="utf-8")
    env.chmod(0o600)
    login_file=secret_dir / "dev-login.txt"
    login_file.write_text(f"本地管理员：admin\n密码：{password}\n仅本地使用，请勿上传此文件。\n", encoding="utf-8")
    login_file.chmod(0o600)
print("本地配置已就绪。管理员密码保存在 secrets/dev-login.txt。")
