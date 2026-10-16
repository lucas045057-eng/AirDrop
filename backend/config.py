from pathlib import Path
from urllib.parse import urlsplit
from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import Field, field_validator


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
    # Native execution (airdrop-native-runtime-v1) is a separate switch from the legacy mock path.
    native_execution_enabled: bool = False
    # 生产环境必须固定到镜像摘要；默认值仅用于本地受控验证。
    native_runtime_base_image: str = "python:3.12-slim"
    native_pypi_index_url: str = Field(default="https://pypi.org/simple", max_length=500)
    native_build_timeout_seconds: int = Field(default=1800, ge=60, le=7200)
    native_global_concurrency: int = Field(default=1, ge=1, le=8)
    native_build_concurrency: int = Field(default=1, ge=1, le=4)
    native_memory_mib: int = Field(default=1024, ge=128, le=32768)
    native_cpus: float = Field(default=1.0, gt=0, le=16)
    native_pids: int = Field(default=256, ge=16, le=4096)
    native_timeout_seconds: int = Field(default=3600, ge=1, le=86400)
    native_tmpfs_mib: int = Field(default=512, ge=32, le=8192)
    # 控制器地址：生产是本地 Unix socket 路径；开发机受控验证可用 tcp://127.0.0.1:<端口>。
    native_socket_path: str = "data/runner/native.sock"
    native_socket_gid: int = Field(default=10001, ge=0)
    native_network: str = "airdrop-native"
    native_docker_host: str = "unix:///var/run/docker.sock"
    native_run_root: Path = Path("/dev/shm/airdrop-runs")
    # 控制器执行 docker 的 argv 前缀；部署时可固定到绝对路径或包装脚本。
    native_docker_bin: list[str] = ["docker"]
    native_container_user: str = "65534:65534"
    native_network_subnet: str = "172.31.240.0/24"
    native_blocked_targets: list[str] = ["169.254.169.254:80", "169.254.170.2:80", "100.100.100.200:80"]
    native_blocked_cidrs: list[str] = []
    native_isolation_check_seconds: int = Field(default=900, ge=30, le=86400)
    native_probe_timeout_seconds: int = Field(default=60, ge=5, le=600)
    native_stop_grace_seconds: int = Field(default=30, ge=1, le=300)
    native_log_segment_bytes: int = Field(default=256 * 1024, ge=4096, le=1024 * 1024)
    native_log_page_bytes: int = Field(default=64 * 1024, ge=4096, le=256 * 1024)
    native_log_page_limit: int = Field(default=256 * 1024, ge=4096, le=256 * 1024)
    native_disk_floor_mib: int = Field(default=512, ge=64, le=102400)
    adapter_queue_limit: int = Field(default=8, ge=1, le=32)
    adapter_memory_mib: int = Field(default=256, ge=32, le=512)
    adapter_timeout_seconds: int = Field(default=90, ge=5, le=180)
    adapter_lease_seconds: int = Field(default=120, ge=10, le=300)

    @field_validator('native_pypi_index_url')
    @classmethod
    def secure_pypi_index(cls, value):
        parsed = urlsplit(value)
        if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError('原生构建包索引必须是无凭据的 HTTPS URL')
        return value.rstrip('/')


settings = Settings()
