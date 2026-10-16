"""原生运行队列：请求键幂等、状态锁、人工核查与禁止平台自动重放。"""
import time
import pytest
from sqlalchemy import func, select
from backend.config import settings
from backend.db import Session
from backend.models import (Artifact, AuditLog, Execution, ExecutionAttempt, Project, ProjectScriptState,
                            ProjectWalletBinding, RuntimeBuild, ScriptFeature, ScriptPackage, ScriptVersion,
                            Task, Wallet, WalletLock, WalletSecret, WalletTaskResult, uid)
from backend.queue import (NATIVE_NO_REPORT, claim, claim_native, enqueue_native, finalize_native, heartbeat,
                           locked_db, mark_running, recover_db, retry)
from backend.storage import encode_files, write_blob
from backend.vault import encrypt_wallet, identity

KEYS = ['0x' + f'{index:02x}' * 32 for index in range(1, 3)]
SPEC = {'protocol': 'airdrop-native-runtime-v1', 'entrypoint': 'main.py',
        'wallet_binding': {'format': 'CSV', 'target_path': 'inputs/wallets.csv', 'chain': 'EVM',
                           'fields': {'address': 'identity', 'private_key': 'credential'}},
        'state_bindings': [{'path': 'state.csv', 'format': 'CSV', 'account_key': 'address'}],
        'capabilities': ['selected_wallet_private_keys'], 'parameters': [], 'timeout_seconds': 3600}


def make_wallet(db, index):
    address, private_key = identity('EVM', KEYS[index])
    wallet = Wallet(chain='EVM', address=address, name=f'native-{index}', is_test=True)
    db.add(wallet)
    db.flush()
    db.add(WalletSecret(wallet_id=wallet.id, ciphertext=encrypt_wallet(wallet, private_key)))
    return wallet


def setup(spec_overrides=None):
    """两个互相独立的项目/脚本包，便于验证全局并发与状态空间互不干扰。"""
    spec = {**SPEC, **(spec_overrides or {})}
    with Session.begin() as db:
        wallets = [make_wallet(db, index) for index in range(len(KEYS))]
        result = {'spec': spec, 'wallets': [wallet.id for wallet in wallets],
                  'addresses': [wallet.address for wallet in wallets], 'tasks': {}, 'projects': {},
                  'packages': {}, 'versions': {}, 'features': {}, 'builds': {}}
        for number, name in enumerate(('first', 'second')):
            project = Project(name=f'native-project-{name}-' + uid()[:6])
            package = ScriptPackage(name=f'native-package-{name}-' + uid()[:6])
            db.add_all([project, package])
            db.flush()
            for wallet in wallets:
                db.add(ProjectWalletBinding(project_id=project.id, wallet_id=wallet.id))
            path, digest = write_blob(encode_files({'main.py': b'print(1)\n'}))
            artifact = Artifact(path=path, sha256=digest, kind='sources')
            db.add(artifact)
            db.flush()
            version = ScriptVersion(package_id=package.id, version=1, original_artifact_id=artifact.id,
                                    source_artifact_id=artifact.id, analysis={}, status='PUBLISHED',
                                    manifest={'package_id': package.id, 'package_version': 1,
                                              'entrypoint': 'main.py', 'retry_safe': True})
            db.add(version)
            db.flush()
            build = RuntimeBuild(package_id=package.id, environment_key=uid() + uid(),
                                 image_id='sha256:' + ('c' * 64 if number == 0 else 'd' * 64), status='READY')
            db.add(build)
            db.flush()
            feature = ScriptFeature(version_id=version.id, feature_key='run-' + name, definition={},
                                    code_sha256='b' * 64, status='APPROVED', runtime_spec=spec,
                                    runtime_status='APPROVED', runtime_review_sha256='a' * 64,
                                    runtime_build_id=build.id)
            db.add(feature)
            db.flush()
            task = Task(project_id=project.id, script_version_id=version.id, script_feature_id=feature.id,
                        name='native-' + name, execution_backend='native', max_retries=3,
                        wallet_selection={'wallet_ids': [wallet.id for wallet in wallets]},
                        feature_config_snapshot={'runtime': spec, 'parameters': {}})
            db.add(task)
            db.flush()
            if name == 'first':
                # 同一状态空间里的第二个任务：用来验证状态写入锁挡住的是脚本状态，而不是同一个任务。
                twin = Task(project_id=project.id, script_version_id=version.id, script_feature_id=feature.id,
                            name='native-twin', execution_backend='native',
                            wallet_selection={'wallet_ids': [wallet.id for wallet in wallets]},
                            feature_config_snapshot={'runtime': spec, 'parameters': {}})
                db.add(twin)
                db.flush()
                result['tasks']['twin'] = twin.id
            result['tasks'][name] = task.id
            result['projects'][name] = project.id
            result['packages'][name] = package.id
            result['versions'][name] = version.id
            result['features'][name] = feature.id
            result['builds'][name] = build.id
        return result


@pytest.fixture
def native_setup():
    return setup()


@pytest.fixture(autouse=True)
def native_enabled(monkeypatch):
    monkeypatch.setattr(settings, 'native_execution_enabled', True)


def attempt_row(execution_id):
    with Session() as db:
        return db.scalar(select(ExecutionAttempt).where(ExecutionAttempt.execution_id == execution_id)
                         .order_by(ExecutionAttempt.number.desc()).limit(1))


def test_request_key_is_idempotent_and_an_active_run_is_returned_instead(native_setup):
    task = native_setup['tasks']['first']
    identity, created = enqueue_native(task, 'manual-key-0001')
    assert created is True and identity
    with Session() as db:
        execution = db.get(Execution, identity)
        assert execution.period == 'manual:manual-key-0001' and execution.execution_backend == 'native'
        assert execution.status == 'QUEUED' and execution.input_snapshot_artifact_id
        assert execution.runtime_snapshot['input_sha256'] and execution.request_key == 'manual-key-0001'
        actions = {log.action for log in db.scalars(select(AuditLog))}
        assert 'execution.enqueue_native' in actions
    repeat, again = enqueue_native(task, 'manual-key-0001')
    assert repeat == identity and again is False
    claimed = claim_native(settings.worker_id)
    assert claimed == (identity, attempt_row(identity).id)
    # 运行尚未结束时，新的请求键不会创建第二个运行，而是返回正在进行的这一次。
    active, created_while_active = enqueue_native(task, 'manual-key-0002')
    assert active == identity and created_while_active is False


def test_native_and_legacy_claims_never_cross(native_setup):
    task = native_setup['tasks']['first']
    identity, _ = enqueue_native(task, 'manual-key-0010')
    # 旧模拟路径不领取原生运行，即使执行已经排队。
    assert claim('legacy-worker') is None
    assert claim_native(settings.worker_id)[0] == identity
    with Session.begin() as db:
        legacy = Execution(task_id=task, project_id=native_setup['projects']['first'],
                           script_version_id=native_setup['versions']['first'], period='2026-10-15',
                           execution_backend='legacy_mock')
        db.add(legacy)
        db.flush()
        legacy_id = legacy.id
    assert claim_native(settings.worker_id) is None
    assert claim('legacy-worker')[0] == legacy_id


def test_running_a_second_time_after_finish_needs_a_new_request_key(native_setup):
    task = native_setup['tasks']['first']
    first, _ = enqueue_native(task, 'manual-key-0020')
    attempt = claim_native(settings.worker_id)[1]
    assert finalize_native(first, attempt, {'status': '', 'exit_code': 0, 'log_status': 'COMPLETE'}) is True
    with Session() as db:
        assert db.get(Execution, first).status == 'COMPLETED'
    # 同一个请求键永远返回同一次运行，不会变成“再跑一次”。
    assert enqueue_native(task, 'manual-key-0020') == (first, False)
    second, created = enqueue_native(task, 'manual-key-0021')
    assert created is True and second != first


def test_completed_run_releases_locks_and_never_claims_wallet_success(native_setup):
    task = native_setup['tasks']['first']
    identity, _ = enqueue_native(task, 'manual-key-0030')
    attempt = claim_native(settings.worker_id)[1]
    assert finalize_native(identity, attempt, {'status': '', 'exit_code': 0, 'log_bytes': 2048,
                                               'log_status': 'COMPLETE'}) is True
    with Session() as db:
        execution = db.get(Execution, identity)
        assert execution.status == 'COMPLETED' and execution.state_generation == 0
        assert execution.log_status == 'COMPLETE' and execution.state_status == 'UNCOLLECTED'
        assert db.get(ExecutionAttempt, attempt).log_bytes == 0
        assert db.get(ExecutionAttempt, attempt).raw_output_bytes == 2048
        assert not list(db.scalars(select(WalletLock).where(WalletLock.execution_id == identity)))
        assert db.scalar(select(ProjectScriptState).where(ProjectScriptState.locked_execution_id == identity)) is None
        rows = list(db.scalars(select(WalletTaskResult).where(WalletTaskResult.execution_id == identity)))
        assert [row.status for row in rows] == [NATIVE_NO_REPORT] * 2
        # 没有结构化产物时不编造逐个钱包的成功率，只说明结果在日志里。
        assert all('运行日志' in row.message for row in rows)


def test_failed_native_run_is_not_auto_retried(native_setup):
    task = native_setup['tasks']['first']
    identity, _ = enqueue_native(task, 'manual-key-0040')
    attempt = claim_native(settings.worker_id)[1]
    assert finalize_native(identity, attempt, {'status': '', 'exit_code': 3, 'error': '脚本退出码 3'}) is True
    with Session() as db:
        execution = db.get(Execution, identity)
        assert execution.status == 'FAILED' and execution.retry_count == 0
        assert execution.finished_at and execution.lease_until is None
        assert not list(db.scalars(select(WalletLock).where(WalletLock.execution_id == identity)))
        rows = list(db.scalars(select(WalletTaskResult).where(WalletTaskResult.execution_id == identity)))
        assert all(row.status == NATIVE_NO_REPORT and '未正常结束' in row.message for row in rows)
    with pytest.raises(ValueError, match='不使用逐钱包补跑'):
        retry(identity)
    with Session() as db:
        assert db.get(Execution, identity).status == 'FAILED'
        assert db.scalar(select(func.count()).select_from(ExecutionAttempt)) == 1


def test_unknown_run_keeps_locks_and_blocks_a_new_run(logged, native_setup, monkeypatch):
    task = native_setup['tasks']['first']
    identity, _ = enqueue_native(task, 'manual-key-0050')
    attempt = claim_native(settings.worker_id)[1]
    assert finalize_native(identity, attempt, {'status': 'UNKNOWN', 'exit_code': None,
                                               'error': '运行失联'}) is True
    with Session() as db:
        assert db.get(Execution, identity).state_status == 'LOCKED'
        assert db.scalar(select(ProjectScriptState).where(ProjectScriptState.locked_execution_id == identity))
        assert db.scalar(select(WalletLock).where(WalletLock.execution_id == identity)) is not None
    with pytest.raises(ValueError, match='结果未知'):
        enqueue_native(task, 'manual-key-0051')
    from worker import native_runner
    monkeypatch.setattr(native_runner, 'status', lambda attempt_id, container_id=None: {'running': False})
    reconciled = logged.post(f'/api/executions/{identity}/reconcile',
                             json={'confirm_container_stopped': True, 'note': '人工核对容器已停止'})
    assert reconciled.status_code == 200
    with Session() as db:
        assert db.get(Execution, identity).status == 'CANCELLED'
        assert db.scalar(select(ProjectScriptState).where(ProjectScriptState.locked_execution_id == identity)) is None
        assert not list(db.scalars(select(WalletLock).where(WalletLock.execution_id == identity)))
    assert enqueue_native(task, 'manual-key-0051')[1] is True


def test_expired_lease_becomes_unknown_and_keeps_every_lock(native_setup):
    task = native_setup['tasks']['first']
    identity, _ = enqueue_native(task, 'manual-key-0060')
    attempt = claim_native(settings.worker_id)[1]
    with locked_db() as db:
        db.get(Execution, identity).lease_until = time.time() - 1
        assert recover_db(db, time.time()) == 1
    assert not mark_running(identity, attempt, 'container-x')
    with Session() as db:
        execution = db.get(Execution, identity)
        assert execution.status == 'UNKNOWN' and execution.current_attempt_id == attempt
        assert db.get(ExecutionAttempt, attempt).status == 'UNKNOWN'
        rows = list(db.scalars(select(WalletTaskResult).where(WalletTaskResult.execution_id == identity)))
        assert all(row.status == 'UNKNOWN' for row in rows)
        assert db.scalar(select(WalletLock).where(WalletLock.execution_id == identity)) is not None
        assert db.get(Execution, identity).lease_until < time.time() or execution.lease_until is None


def test_startup_recovery_expires_preparing_runs_and_stopping_keeps_the_lease(native_setup):
    task = native_setup['tasks']['first']
    identity, _ = enqueue_native(task, 'manual-key-0070')
    attempt = claim_native(settings.worker_id)[1]
    with locked_db() as db:
        execution = db.get(Execution, identity)
        execution.status = 'STOPPING'
        execution.cancel_requested = True
    assert heartbeat(identity, attempt, settings.worker_id) is False
    with Session() as db:
        # 停止过程中仍然续租，不会被误判为 UNKNOWN。
        assert db.get(Execution, identity).lease_until > time.time()
    assert finalize_native(identity, attempt, {'status': '', 'exit_code': 130}) is True
    with Session() as db:
        assert db.get(Execution, identity).status == 'CANCELLED'
    # 已经结束的运行拒绝迟到的第二次收尾，避免重复写入。
    assert finalize_native(identity, attempt, {'status': '', 'exit_code': 0}) is False


def test_native_global_concurrency_is_enforced_across_tasks(native_setup):
    first, _ = enqueue_native(native_setup['tasks']['first'], 'manual-key-0080')
    second, _ = enqueue_native(native_setup['tasks']['second'], 'manual-key-0081')
    assert claim_native(settings.worker_id)[0] == first
    # 全局并发为 1：第二次领取必须等待，即使它属于另一个项目。
    assert claim_native('native-worker-2') is None
    attempt = attempt_row(first).id
    finalize_native(first, attempt, {'status': '', 'exit_code': 0})
    assert claim_native('native-worker-2')[0] == second


def test_wallet_lock_blocks_claiming_a_native_run(native_setup):
    with locked_db() as db:
        other = Execution(task_id=native_setup['tasks']['second'], project_id=native_setup['projects']['second'],
                          script_version_id=native_setup['versions']['second'], period='manual:other-0001',
                          execution_backend='native', status='RUNNING')
        db.add(other)
        db.flush()
        # 另一个运行占住本次选择中的一个钱包：整轮都不能领取，避免并发读写同一账号。
        db.add(WalletLock(wallet_id=native_setup['wallets'][0], execution_id=other.id))
    identity, created = enqueue_native(native_setup['tasks']['first'], 'manual-key-0090')
    assert created is True and claim_native(settings.worker_id) is None
    with Session() as db:
        assert db.get(Execution, identity).status == 'QUEUED'
        assert len(list(db.scalars(select(WalletLock).where(WalletLock.execution_id == identity)))) == 0


def test_state_lock_blocks_enqueueing_another_run_of_the_same_script(native_setup):
    with locked_db() as db:
        other = Execution(task_id=native_setup['tasks']['twin'], project_id=native_setup['projects']['first'],
                          script_version_id=native_setup['versions']['first'], period='manual:other-0002',
                          execution_backend='native', status='RUNNING')
        db.add(other)
        db.flush()
        db.add(ProjectScriptState(project_id=native_setup['projects']['first'],
                                  package_id=native_setup['packages']['first'], state_schema_version=1,
                                  locked_execution_id=other.id))
    with pytest.raises(ValueError, match='状态正在被另一次运行写入'):
        enqueue_native(native_setup['tasks']['first'], 'manual-key-0091')


def test_state_lock_race_does_not_leave_wallet_lock_on_queued_run(native_setup, monkeypatch):
    monkeypatch.setattr(settings, 'native_global_concurrency', 2)
    first_task, second_task = native_setup['tasks']['first'], native_setup['tasks']['twin']
    with locked_db() as db:
        db.get(Task, first_task).wallet_selection = {'wallet_ids': [native_setup['wallets'][0]]}
        db.get(Task, second_task).wallet_selection = {'wallet_ids': [native_setup['wallets'][1]]}
    first, _ = enqueue_native(first_task, 'manual-key-0092')
    second, _ = enqueue_native(second_task, 'manual-key-0093')

    claimed_first = claim_native(settings.worker_id)
    assert claimed_first and claimed_first[0] == first
    assert claim_native('native-worker-2') is None
    with Session() as db:
        assert not db.scalar(select(WalletLock).where(WalletLock.execution_id == second))

    assert finalize_native(first, claimed_first[1], {'exit_code': 0})
    claimed_second = claim_native('native-worker-2')
    assert claimed_second and claimed_second[0] == second


def test_unknown_state_holder_cannot_be_replaced_by_another_task(native_setup):
    first, _ = enqueue_native(native_setup['tasks']['first'], 'manual-key-0094')
    claimed = claim_native(settings.worker_id)
    assert claimed and claimed[0] == first
    assert finalize_native(first, claimed[1], {'status': 'UNKNOWN', 'exit_code': None})

    with pytest.raises(ValueError):
        enqueue_native(native_setup['tasks']['twin'], 'manual-key-0095')
    with Session() as db:
        state = db.scalar(select(ProjectScriptState).where(
            ProjectScriptState.project_id == native_setup['projects']['first']))
        assert state.locked_execution_id == first


def test_enqueue_native_refuses_unapproved_or_incomplete_definitions(native_setup, monkeypatch):
    task = native_setup['tasks']['first']
    feature = native_setup['features']['first']
    build = native_setup['builds']['first']
    with locked_db() as db:
        db.get(ScriptFeature, feature).runtime_status = 'DRAFT'
    with pytest.raises(ValueError, match='运行审核'):
        enqueue_native(task, 'manual-key-0100')
    with locked_db() as db:
        db.get(ScriptFeature, feature).runtime_status = 'APPROVED'
        db.get(RuntimeBuild, build).status = 'PENDING'
    with pytest.raises(ValueError, match='依赖环境'):
        enqueue_native(task, 'manual-key-0100')
    with locked_db() as db:
        db.get(RuntimeBuild, build).status = 'READY'
        db.get(ScriptFeature, feature).runtime_spec = {**SPEC, 'unresolved': ['下载入口需要人工确认']}
    with pytest.raises(ValueError, match='未解决项'):
        enqueue_native(task, 'manual-key-0100')
    monkeypatch.setattr(settings, 'native_execution_enabled', False)
    with pytest.raises(ValueError, match='原生执行开关未启用'):
        enqueue_native(task, 'manual-key-0100')
    monkeypatch.setattr(settings, 'native_execution_enabled', True)
    with pytest.raises(ValueError, match='请求键'):
        enqueue_native(task, '')
    with Session() as db:
        assert db.scalar(select(func.count()).select_from(Execution)) == 0
