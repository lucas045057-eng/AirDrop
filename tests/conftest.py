import os
import tempfile
from pathlib import Path
import pytest
from cryptography.fernet import Fernet

tmp = Path(tempfile.mkdtemp(prefix="airdrop-tests-"))
(tmp / "master.key").write_bytes(Fernet.generate_key())
os.environ.update(AIRDROP_DATABASE_URL=f"sqlite:///{(tmp/'test.db').as_posix()}", AIRDROP_DATA_DIR=str(tmp), AIRDROP_MASTER_KEY_FILE=str(tmp/'master.key'), AIRDROP_RUNNER_DIR=str(tmp/'runner'), AIRDROP_ADMIN_PASSWORD="test-password-1234")
from backend.db import Base, engine, Session
from backend.main import app
from backend.auth import attempts
from fastapi.testclient import TestClient


@pytest.fixture(autouse=True)
def clean_db(monkeypatch):
    # Queue regressions exercise the isolated engine explicitly; deployments default to management only.
    from backend.config import settings
    monkeypatch.setattr(settings, 'execution_enabled', True)
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    attempts.clear()
    yield


@pytest.fixture
def client():
    with TestClient(app) as client:
        yield client


@pytest.fixture
def logged(client):
    result = client.post('/api/auth/login', json={'username':'admin','password':'test-password-1234'})
    assert result.status_code == 200
    client.headers['x-csrf-token'] = result.json()['csrf']
    return client
