import os
import sqlite3
import subprocess
import sys
from pathlib import Path
from cryptography.fernet import Fernet


def test_populated_v2_upgrade_keeps_login_wallets_tasks_cipher_and_review(tmp_path):
    path=tmp_path/'old.db'; key=tmp_path/'master.key'; key.write_bytes(Fernet.generate_key())
    env=dict(os.environ,AIRDROP_DATABASE_URL='sqlite:///'+path.as_posix(),AIRDROP_DATA_DIR=str(tmp_path),AIRDROP_MASTER_KEY_FILE=str(key),AIRDROP_ADMIN_USERNAME='migration-admin',AIRDROP_ADMIN_PASSWORD='migration-test-only-1234',AIRDROP_EXECUTION_ENABLED='false')
    def run(*args):
        result=subprocess.run([sys.executable,*args],env=env,capture_output=True,text=True)
        assert result.returncode==0,result.stderr
    run('-m','alembic','upgrade','71a960e315dc')
    run('-c', '''
from sqlalchemy import MetaData,Table,insert
from backend.db import Session,engine
from backend.models import User,Wallet,WalletSecret,Project,ScriptPackage,Artifact,ScriptVersion,ScriptAdaptation
from backend.auth import hash_password
from backend.storage import write_blob,encode_files,cipher
from packages.protocol import Manifest
with Session.begin() as db:
    user=User(id='admin',username='migration-admin',password_hash=hash_password('migration-test-only-1234'))
    wallet=Wallet(id='wallet',chain='EVM',address='test-only-identity',name='old-wallet',is_test=False)
    project=Project(id='project',name='old-project'); package=ScriptPackage(id='package',name='old-package')
    blob,sha=write_blob(encode_files({'main.py':b'def main(): return 1\\n'}))
    artifact=Artifact(id='artifact',path=blob,sha256=sha,kind='sources')
    db.add_all([user,wallet,project,package,artifact]); db.flush()
    db.add(WalletSecret(wallet_id='wallet',ciphertext=cipher().encrypt(b'fixture-only-credential').decode()))
    manifest=Manifest(package_id='package',package_version=1,entrypoint='main.py').model_dump()
    version=ScriptVersion(id='version',package_id='package',version=1,original_artifact_id='artifact',source_artifact_id='artifact',analysis={},manifest=manifest,status='APPROVED')
    db.add(version); db.flush()
    legacy=Table('tasks',MetaData(),autoload_with=engine)
    db.execute(insert(legacy).values(id='task',project_id='project',script_version_id='version',name='old-task',batch_size=100,max_retries=0,enabled=True))
''')
    with sqlite3.connect(path) as db:
        snapshots={table:db.execute('SELECT * FROM '+table).fetchall() for table in ['users','wallets','wallet_secrets','script_versions','artifacts']}
        artifact_path=db.execute('SELECT path FROM artifacts').fetchone()[0]
    before=Path(artifact_path).read_bytes()
    run('-m','alembic','upgrade','head')
    with sqlite3.connect(path) as db:
        for table,rows in snapshots.items(): assert db.execute('SELECT * FROM '+table).fetchall()==rows
        assert db.execute('SELECT script_feature_id,feature_config_snapshot FROM tasks').fetchone()==(None,None)
        assert db.execute('SELECT COUNT(*) FROM script_features').fetchone()[0]==0
        assert db.execute('PRAGMA foreign_key_check').fetchall()==[]
    assert Path(artifact_path).read_bytes()==before
    run('-c', '''
from fastapi.testclient import TestClient
from backend.main import app
with TestClient(app) as client:
    response=client.post('/api/auth/login',json={'username':'migration-admin','password':'migration-test-only-1234'})
    assert response.status_code==200
    assert len(client.get('/api/wallets').json())==1
    assert client.get('/api/tasks').json()[0]['script_feature_id'] is None
    assert client.get('/api/scripts').json()[0]['status']=='APPROVED'
''')
