from pathlib import Path
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="AIRDROP_", env_file=".env", extra="ignore")
    data_dir: Path = Path("data")
    database_url: str = "sqlite:///data/airdrop.db"
    admin_username: str = "admin"
    admin_password: str = ""
    master_key_file: Path = Path("secrets/master.key")
    secure_cookie: bool = False
    allowed_origins: str = "http://localhost:5173,http://127.0.0.1:5173,http://localhost:8080,http://127.0.0.1:8080,http://localhost:18080,http://127.0.0.1:18080"
    global_concurrency: int = 2
    lease_seconds: int = 30
    worker_id: str = "worker-local"
    runner_dir: Path = Path("data/runner")
    execution_enabled: bool = False


settings = Settings()
