import os
import subprocess
import sys
import sqlite3


def test_migration_upgrade_downgrade_upgrade(tmp_path):
    env=dict(os.environ,AIRDROP_DATABASE_URL='sqlite:///'+(tmp_path/'migration.db').as_posix(),AIRDROP_DATA_DIR=str(tmp_path))
    for target in [('upgrade','head'),('downgrade','base'),('upgrade','head')]:
        completed=subprocess.run([sys.executable,'-m','alembic',*target],env=env,capture_output=True,text=True)
        assert completed.returncode==0,completed.stderr
    db=sqlite3.connect(tmp_path/'migration.db')
    tables={r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {'users','wallets','executions','wallet_task_results','schedules','transaction_intents'}<=tables
    assert db.execute('PRAGMA integrity_check').fetchone()[0]=='ok'
    db.close()


def test_receipts_backfill_only_formal_executions(tmp_path):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session
    from backend.models import Project, Artifact, ScriptPackage, ScriptVersion, Task, Execution
    path = tmp_path/'receipts.db'
    env = dict(os.environ, AIRDROP_DATABASE_URL='sqlite:///'+path.as_posix(), AIRDROP_DATA_DIR=str(tmp_path))
    old = subprocess.run([sys.executable, '-m', 'alembic', 'upgrade', '3c6a61ccad54'], env=env, capture_output=True, text=True)
    assert old.returncode == 0, old.stderr
    engine = create_engine('sqlite:///'+path.as_posix())
    with Session(engine) as db:
        project = Project(name='migration-test')
        package = ScriptPackage(name='migration-test')
        artifact = Artifact(path='/unused/test.enc', sha256='0'*64, kind='test')
        db.add_all([project, package, artifact]);db.flush()
        version = ScriptVersion(package_id=package.id, version=1, original_artifact_id=artifact.id,
                                source_artifact_id=artifact.id, analysis={})
        db.add(version);db.flush()
        task = Task(project_id=project.id, script_version_id=version.id, name='migration-test')
        db.add(task);db.flush()
        db.add_all([Execution(task_id=task.id, project_id=project.id, script_version_id=version.id,
                             period=period, is_test=is_test) for period, is_test in [('2026-10-09', False), ('test:fixture', True)]])
        db.commit()
    engine.dispose()
    upgraded = subprocess.run([sys.executable, '-m', 'alembic', 'upgrade', 'head'], env=env, capture_output=True, text=True)
    assert upgraded.returncode == 0, upgraded.stderr
    with sqlite3.connect(path) as db:
        assert db.execute('SELECT period FROM execution_receipts').fetchall() == [('2026-10-09',)]
        assert db.execute('PRAGMA foreign_key_check').fetchall() == []
