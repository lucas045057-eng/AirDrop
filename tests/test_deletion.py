from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import time
import pytest
from sqlalchemy import select
from backend.config import settings
from backend.db import Session
from backend.models import (Artifact, AuditLog, Execution, ExecutionAttempt, ExecutionLogSegment, ExecutionReceipt,
    PointSnapshot, ProjectAccount, ProjectScriptState, ProjectWalletBinding, ScheduleOccurrence, ScriptVersion,
    Wallet, WalletLock, WalletSecret, WalletTaskResult, Workflow, WorkflowStep)
from backend.queue import claim, claim_native, enqueue, enqueue_native, finalize, finalize_native, locked_db
from backend import runtime_logs
from tests.test_native_queue import native_setup  # noqa: F401（native_setup 以夹具形式导入）


@pytest.fixture
def native_enabled(monkeypatch):
    monkeypatch.setattr(settings, 'native_execution_enabled', True)
    return True


def erase(client, path):
    return client.request('DELETE', '/api' + path, json={'confirmed': True})


@pytest.fixture
def setup(logged):
    group = logged.post('/api/wallets/generate', json={'count': 1, 'group_name': 'deletion-test'}).json()['group_id']
    wallet = logged.get('/api/wallets?group_id=' + group).json()[0]['id']
    project = logged.post('/api/projects', json={'name': 'deletion-test'}).json()['id']
    assert logged.put('/api/projects/' + project + '/wallets', json={'group_ids': [group]}).status_code == 200
    script = logged.post('/api/scripts/upload', files={'file': ('main.py', b"if __name__ == '__main__': pass\n")}).json()
    adapted = logged.post('/api/scripts/' + script['id'] + '/adapt', json={'manifest': {
        'package_id': script['package_id'], 'package_version': 1, 'entrypoint': 'main.py',
        'execution_mode': 'workflow', 'steps': ['main.py']}}).json()
    assert logged.post('/api/scripts/' + adapted['id'] + '/approve').status_code == 200
    task = logged.post('/api/tasks', json={'project_id': project, 'script_version_id': adapted['id'], 'name': 'deletion-task'}).json()['id']
    return {'group': group, 'wallet': wallet, 'project': project, 'script': script['id'], 'adapted': adapted['id'], 'task': task}


def test_confirmation_auth_csrf_and_not_found(logged):
    path = '/api/projects/missing'
    assert logged.request('DELETE', path, json={}).status_code == 422
    assert logged.request('DELETE', path, json={'confirmed': False}).status_code == 422
    assert logged.request('DELETE', path, json={'confirmed': True}).status_code == 404
    csrf = logged.headers.pop('x-csrf-token')
    assert logged.request('DELETE', path, json={'confirmed': True}).status_code == 403
    logged.cookies.clear()
    logged.headers['x-csrf-token'] = csrf
    assert logged.request('DELETE', path, json={'confirmed': True}).status_code == 401


def test_dependency_guards_and_ordered_permanent_removal(logged, setup):
    s = setup
    for kind, identity, expected in [('wallets', s['wallet'], '项目绑定'),
                                    ('wallets/groups', s['group'], '项目分组绑定'),
                                    ('projects', s['project'], '任务'),
                                    ('scripts', s['adapted'], '任务'),
                                    ('scripts', s['script'], '后续适配版本')]:
        response = erase(logged, f'/{kind}/{identity}')
        assert response.status_code == 409 and expected in response.json()['detail']
    assert erase(logged, '/tasks/' + s['task']).status_code == 200
    with Session() as db:
        assert not list(db.scalars(select(Workflow)))
        assert not list(db.scalars(select(WorkflowStep)))
    assert erase(logged, '/projects/' + s['project']).status_code == 200
    assert erase(logged, '/wallets/groups/' + s['group']).status_code == 200
    assert any(w['id'] == s['wallet'] for w in logged.get('/api/wallets').json())
    assert erase(logged, '/wallets/' + s['wallet']).status_code == 200
    with Session() as db:
        assert db.get(Wallet, s['wallet']) is None and db.get(WalletSecret, s['wallet']) is None
    assert erase(logged, '/scripts/' + s['adapted']).status_code == 200
    assert erase(logged, '/scripts/' + s['script']).status_code == 200
    with Session() as db:
        actions = {a.action for a in db.scalars(select(AuditLog))}
        assert {'task.delete', 'project.delete', 'group.delete', 'wallet.delete', 'script.delete'} <= actions


def test_shared_blob_retained_until_last_reference(logged):
    content = b'# shared-deletion-fixture\npass\n'
    a = logged.post('/api/scripts/upload', files={'file': ('a.py', content)}).json()
    b = logged.post('/api/scripts/upload', files={'file': ('a.py', content)}).json()
    with Session() as db:
        paths = {Path(db.get(Artifact, id).path) for version in (a, b)
                 for id in (db.get(ScriptVersion, version['id']).source_artifact_id,
                            db.get(ScriptVersion, version['id']).original_artifact_id)}
    assert erase(logged, '/scripts/' + a['id']).status_code == 200
    assert all(path.exists() for path in paths)
    assert logged.get('/api/scripts/' + b['id'] + '/source').json()['a.py'] == content.decode()
    assert erase(logged, '/scripts/' + b['id']).status_code == 200
    assert all(not path.exists() for path in paths)


@pytest.mark.parametrize('state', ['QUEUED', 'RUNNING', 'UNKNOWN', 'MANUAL_REQUIRED'])
def test_active_or_uncertain_execution_cannot_be_deleted(logged, setup, state):
    identity, _ = enqueue(setup['task'], is_test=True)
    with locked_db() as db:db.get(Execution, identity).status = state
    assert erase(logged, '/executions/' + identity).status_code == 409
    with Session() as db:assert db.get(Execution, identity) is not None


def test_formal_history_removal_preserves_idempotency_and_blocks_wallet(logged, setup):
    s = setup
    with locked_db() as db:db.get(ScriptVersion, s['adapted']).status = 'PUBLISHED'
    identity, _ = enqueue(s['task'], period='2026-10-09')
    _, attempt = claim('deletion-worker')
    with Session() as db:address = db.get(Wallet, s['wallet']).address
    finalize(identity, attempt, {'status': 'DONE', 'exit_code': 0,
        'results': [{'address': address, 'status': 'SUCCESS', 'points': 7}]})
    assert logged.put('/api/projects/' + s['project'] + '/wallets', json={'group_ids': [], 'wallet_ids': []}).status_code == 200
    assert '执行结果' in erase(logged, '/wallets/' + s['wallet']).json()['detail']
    assert erase(logged, '/tasks/' + s['task']).status_code == 409
    assert erase(logged, '/executions/' + identity).status_code == 200
    with Session() as db:
        receipt = db.get(ExecutionReceipt, (s['task'], '2026-10-09'))
        assert receipt and receipt.execution_id is None
        assert db.get(Execution, identity) is None
        assert not list(db.scalars(select(PointSnapshot)))
        assert not list(db.scalars(select(WalletTaskResult)))
    with pytest.raises(ValueError, match='禁止重复运行'):enqueue(s['task'], period='2026-10-09')
    assert erase(logged, '/wallets/' + s['wallet']).status_code == 200
    assert erase(logged, '/tasks/' + s['task']).status_code == 200
    with Session() as db:assert not list(db.scalars(select(ExecutionReceipt)))


def test_schedule_history_and_wallet_lock_guards(logged, setup):
    s = setup
    schedule = logged.post('/api/schedules', json={'task_id': s['task'], 'hour': 23, 'minute': 59}).json()['id']
    identity, _ = enqueue(s['task'], is_test=True)
    with locked_db() as db:
        db.get(Execution, identity).status = 'CANCELLED'
        db.add(ScheduleOccurrence(schedule_id=schedule, due_at=time.time(), status='QUEUED', execution_id=identity))
    assert '定时调度历史' in erase(logged, '/executions/' + identity).json()['detail']
    assert erase(logged, '/tasks/' + s['task']).status_code == 409
    assert erase(logged, '/schedules/' + schedule).status_code == 200
    with Session() as db:assert db.get(Execution, identity) is not None
    with locked_db() as db:db.add(WalletLock(wallet_id=s['wallet'], execution_id=identity))
    assert '钱包执行锁' in erase(logged, '/executions/' + identity).json()['detail']
    with locked_db() as db:db.delete(db.get(WalletLock, s['wallet']))
    assert erase(logged, '/executions/' + identity).status_code == 200


def test_concurrent_upload_and_delete_share_blob_safely(logged):
    content = b'# concurrent-deletion-fixture\npass\n'
    a = logged.post('/api/scripts/upload', files={'file': ('main.py', content)}).json()
    with ThreadPoolExecutor(max_workers=2) as pool:
        removal = pool.submit(erase, logged, '/scripts/' + a['id'])
        upload = pool.submit(logged.post, '/api/scripts/upload', files={'file': ('main.py', content)})
        assert removal.result().status_code == 200
        new = upload.result()
        assert new.status_code == 200
    assert logged.get('/api/scripts/' + new.json()['id'] + '/source').json()['main.py'] == content.decode()


def test_clear_ai_config(logged, monkeypatch):
    # Clearing stored configuration must work in the offline test container.
    # Keep URL validation active while supplying a deterministic public DNS result.
    monkeypatch.setattr('backend.ai.socket.getaddrinfo', lambda *args, **kwargs: [(2, 1, 6, '', ('93.184.216.34', 443))])
    assert logged.put('/api/settings/ai', json={'url': 'https://example.com/v1', 'model': 'mock', 'api_key': 'test-key-deletion'}).status_code == 200
    assert erase(logged, '/settings/ai').status_code == 200
    assert not logged.get('/api/settings').json()['ai']['key_configured']


def test_artifact_path_guard_rolls_back_database_delete(logged, tmp_path):
    script = logged.post('/api/scripts/upload', files={'file': ('main.py', b'# path-guard-fixture\npass\n')}).json()
    external = tmp_path/'keep.enc'
    external.write_bytes(b'keep')
    with locked_db() as db:
        version = db.get(ScriptVersion, script['id'])
        db.get(Artifact, version.source_artifact_id).path = str(external)
    assert erase(logged, '/scripts/' + script['id']).status_code == 409
    with Session() as db:assert db.get(ScriptVersion, script['id']) is not None
    assert external.read_bytes() == b'keep'


def test_enqueue_and_task_delete_are_atomic(logged, setup):
    def attempt_enqueue():
        try:return enqueue(setup['task'], is_test=True)
        except ValueError:return None
    with ThreadPoolExecutor(max_workers=2) as pool:
        enqueued = pool.submit(attempt_enqueue)
        deleted = pool.submit(erase, logged, '/tasks/' + setup['task'])
        execution, response = enqueued.result(), deleted.result()
    if execution:
        assert response.status_code == 409
        with Session() as db:assert db.get(Execution, execution[0]) is not None
    else:assert response.status_code == 200


def test_native_execution_deletion_removes_logs_snapshot_and_state_lock(logged, native_setup, native_enabled):
    """原生运行的加密工件（输入快照、日志分段）随执行记录一起消失。"""
    identity, _ = enqueue_native(native_setup['tasks']['first'], 'manual-key-0001')
    attempt = claim_native(settings.worker_id)[1]
    with Session.begin() as db:
        runtime_logs.append_text(db, attempt, 0, '原生运行日志：开始\n')
        runtime_logs.append_text(db, attempt, 1, '原生运行日志：结束\n')
    assert finalize_native(identity, attempt, {'status': '', 'exit_code': 0, 'log_bytes': 40,
                                               'log_status': 'COMPLETE'}) is True
    with locked_db() as db:
        execution = db.get(Execution, identity)
        assert execution.status == 'COMPLETED'
        snapshot_id = execution.input_snapshot_artifact_id
        snapshot_path = Path(db.get(Artifact, snapshot_id).path)
        segment_paths = [Path(db.get(Artifact, segment.artifact_id).path)
                         for segment in runtime_logs.segments_of(db, attempt)]
        # 人工核查前的残留锁必须在删除时一并清除，不能留下悬空引用。
        state = db.scalar(select(ProjectScriptState).where(ProjectScriptState.project_id == native_setup['projects']['first']))
        state.locked_execution_id = identity
    result = erase(logged, '/executions/' + identity)
    assert result.status_code == 200
    assert result.json()['removed']['artifacts'] >= 3 and result.json()['artifact_files_pending'] == 0
    with Session() as db:
        assert db.get(Execution, identity) is None
        assert not list(db.scalars(select(ExecutionAttempt).where(ExecutionAttempt.execution_id == identity)))
        assert not list(db.scalars(select(ExecutionLogSegment)))
        assert db.get(Artifact, snapshot_id) is None
        for path in segment_paths:
            assert db.scalar(select(Artifact).where(Artifact.path == str(path))) is None
        state = db.scalar(select(ProjectScriptState).where(ProjectScriptState.project_id == native_setup['projects']['first']))
        assert state.locked_execution_id is None
    assert not snapshot_path.exists() and all(not path.exists() for path in segment_paths)


def test_wallet_with_project_account_data_cannot_be_deleted(logged, native_setup):
    """项目资料（ProjectAccount）也是引用：删钱包前必须先清掉它。"""
    wallet_id = native_setup['wallets'][0]
    with locked_db() as db:
        for binding in db.scalars(select(ProjectWalletBinding).where(ProjectWalletBinding.wallet_id == wallet_id)):
            db.delete(binding)
        db.add(ProjectAccount(project_id=native_setup['projects']['first'], package_id=native_setup['packages']['first'],
                              wallet_id=wallet_id, public_config={'note': '项目资料'}))
    blocked = erase(logged, '/wallets/' + wallet_id)
    assert blocked.status_code == 409 and '项目资料' in blocked.json()['detail']
    with Session() as db:
        assert db.get(Wallet, wallet_id) is not None
    with locked_db() as db:
        db.delete(db.scalar(select(ProjectAccount).where(ProjectAccount.wallet_id == wallet_id)))
    assert erase(logged, '/wallets/' + wallet_id).status_code == 200
