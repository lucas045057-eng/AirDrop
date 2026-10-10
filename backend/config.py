from pathlib import Path
from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import Field


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
    adapter_queue_limit: int = Field(default=8, ge=1, le=32)
    adapter_memory_mib: int = Field(default=256, ge=32, le=512)
    adapter_timeout_seconds: int = Field(default=90, ge=5, le=180)
    adapter_lease_seconds: int = Field(default=120, ge=10, le=300)


settings = Settings()
