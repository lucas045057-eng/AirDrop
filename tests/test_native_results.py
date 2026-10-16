"""原生运行的端到端结果：退出码、日志完整性、状态回写、停止与人工核查。

这里跑的是真实的 Worker 编排（worker.main.native_run）：控制器服务、假 docker、
日志分段、状态合并全部串起来，只有容器本身是假的。
"""
import asyncio
import io
import json
import socket
import subprocess
import sys
import threading
import time
import zipfile
from pathlib import Path
import pytest
from sqlalchemy import select
from backend.config import settings
from backend.db import Session
from backend.models import (Artifact, AuditLog, Execution, ExecutionAttempt, ProjectScriptState, RuntimeBuild,
                            Task, WalletLock, WalletTaskResult)
from backend.queue import claim_native, enqueue_native, finalize_native, locked_db
from backend.storage import decode_files, encode_files, read_blob, write_blob
from backend.vault import identity
from backend import runtime_logs
from runner import controller, controller_service
from tests.fakes import commands, container_record, make_image, reset_isolation, wire
from tests.test_native_queue import KEYS, NATIVE_NO_REPORT, native_setup  # noqa: F401（native_setup 以夹具形式导入）
from worker import main as worker_main


@pytest.fixture(autouse=True)
def native_enabled(monkeypatch):
    monkeypatch.setattr(settings, 'native_execution_enabled', True)


def free_port() -> int:
    probe = socket.socket()
    probe.bind(('127.0.0.1', 0))
    port = probe.getsockname()[1]
    probe.close()
    return port


@pytest.fixture
def runtime(native_setup, tmp_path, monkeypatch):
    """假 docker + 真控制器服务 + 一个可运行的镜像。"""
    state = wire(monkeypatch, tmp_path)
    reset_isolation()
    address = 'tcp://127.0.0.1:' + str(free_port())
    monkeypatch.setattr(settings, 'native_socket_path', address)
    threading.Thread(target=controller_service.serve, args=(address,), daemon=True).start()
    for _ in range(100):
        if controller.online(timeout=1):
            break
        time.sleep(0.05)
    else:
        pytest.fail('控制器服务没有在本地 socket 上就绪')
    image = make_image(state, monkeypatch)
    with locked_db() as db:
        for build in native_setup['builds'].values():
            db.get(RuntimeBuild, build).image_id = image
    return {**native_setup, 'state': state, 'image': image, 'tmp_path': tmp_path}


def patch_snapshot(execution_id: str, **values):
    """按用例需要调整本次运行的快照（超时、停止宽限）。"""
    with locked_db() as db:
        execution = db.get(Execution, execution_id)
        snapshot = dict(execution.runtime_snapshot or {})
        snapshot['runtime'] = {**(snapshot.get('runtime') or {}), **values}
        execution.runtime_snapshot = snapshot


def prepare(runtime, key='manual-key-1000', **snapshot):
    """入队并领取一次原生运行，返回 (execution_id, attempt_id)。"""
    execution, created = enqueue_native(runtime['tasks']['first'], key)
    assert created is True
    if snapshot:
        patch_snapshot(execution, **snapshot)
    claimed = claim_native(settings.worker_id)
    assert claimed and claimed[0] == execution
    return execution, claimed[1]


def work(execution_id: str, attempt_id: str):
    asyncio.run(worker_main.native_run(execution_id, attempt_id))


def read_log(execution_id: str, attempt_id: str, limit: int = 64 * 1024) -> str:
    """用游标把整份日志读完，模拟界面连续翻页。"""
    text, cursor = '', 0
    for _ in range(400):
        with Session() as db:
            page = runtime_logs.page(db, db.get(Execution, execution_id), db.get(ExecutionAttempt, attempt_id),
                                     cursor, limit)
        text += ''.join(chunk['text'] for chunk in page['chunks'])
        assert page['cursor'] >= cursor
        if not page['truncated'] or page['cursor'] == cursor:
            break
        cursor = page['cursor']
    return text


def test_completed_run_keeps_the_whole_log_redacts_keys_and_commits_state(runtime, monkeypatch):
    private_key = identity('EVM', KEYS[0])[1]
    line = '第 1 行：本轮执行写入状态文件，并输出足够长的中文日志用于验证分段。\n'
    payload = line * 16000 + f'配置：private_key={private_key}\n'
    total = len(payload.encode())
    assert total > 1024 * 1024
    output = Path(runtime['tmp_path']) / 'native-output.txt'
    output.write_bytes(payload.encode())
    state_text = 'address,points\n' + ''.join(f'{address},3\n' for address in runtime['addresses'])
    monkeypatch.setenv('FAKE_DOCKER_OUTPUT_FILE', str(output))
    monkeypatch.setenv('FAKE_DOCKER_ATTACH_CHUNK', '65536')
    monkeypatch.setenv('FAKE_DOCKER_AUTO_EXIT', '1')
    monkeypatch.setenv('FAKE_DOCKER_WRITE_STATE', json.dumps([['state.csv', state_text]]))
    execution, attempt = prepare(runtime, 'manual-key-0010')

    started = time.time()
    work(execution, attempt)
    assert time.time() - started < 120

    with Session() as db:
        row = db.get(Execution, execution)
        attempt_row = db.get(ExecutionAttempt, attempt)
        assert row.status == 'COMPLETED' and row.error == ''
        assert row.log_status == 'COMPLETE' and row.state_status == 'COMMITTED' and row.state_generation == 1
        assert row.finished_at and row.lease_until is None
        assert attempt_row.process_exit_code == 0 and attempt_row.runner_handle
        assert attempt_row.status == 'COMPLETED'
        # 进程正常退出不等于每个钱包成功：没有结构化产物时只标记“未收到单钱包结果”。
        rows = list(db.scalars(select(WalletTaskResult).where(WalletTaskResult.execution_id == execution)))
        assert [entry.status for entry in rows] == [NATIVE_NO_REPORT] * 2
        assert not list(db.scalars(select(WalletLock).where(WalletLock.execution_id == execution)))
        state = db.scalar(select(ProjectScriptState).where(ProjectScriptState.project_id == runtime['projects']['first']))
        assert state.generation == 1 and state.locked_execution_id is None
        committed = decode_files(read_blob(db.get(Artifact, state.artifact_id).path, state.artifact_sha256))
        assert b'address,points' in committed['state.csv']
        for address in runtime['addresses']:
            assert address.encode() in committed['state.csv']
        assert db.scalar(select(AuditLog).where(AuditLog.action == 'runtime.state.commit'))
    stored = read_log(execution, attempt)
    # 平台事件与脚本输出都在同一份日志里，且一个字节都没有丢。
    assert '[平台] 容器已创建' in stored and '进程已退出（退出码 0）' in stored
    assert stored.count(line) == 16000
    assert private_key not in stored and '[REDACTED]' in stored
    with Session() as db:
        segments = runtime_logs.segments_of(db, attempt)
        assert len(segments) >= 4
        attempt_row = db.get(ExecutionAttempt, attempt)
        stored_bytes = sum(item.size_bytes for item in segments)
        assert attempt_row.log_bytes == stored_bytes
        assert attempt_row.raw_output_bytes == total
        page = runtime_logs.page(db, row, attempt_row, offset=stored_bytes)
        assert page['cursor'] == stored_bytes and page['finished'] is True and page['truncated'] is False
    download = ''.join(runtime_logs.download_chunks(execution, attempt))
    assert download == stored


def test_execution_detail_exposes_the_frozen_snapshot_and_attempt_log_size(logged, runtime, monkeypatch):
    """界面刷新后靠执行详情还原参数快照，并据 log_bytes 判断是否还有更早日志。"""
    output = Path(runtime['tmp_path']) / 'detail-output.txt'
    output.write_text('界面快照校验日志\n', encoding='utf-8')
    monkeypatch.setenv('FAKE_DOCKER_OUTPUT_FILE', str(output))
    execution, attempt = prepare(runtime, 'manual-key-1500')
    work(execution, attempt)

    detail = logged.get(f'/api/executions/{execution}')
    assert detail.status_code == 200
    frozen = detail.json()['runtime_snapshot']
    assert frozen['feature_id'] == runtime['features']['first']
    assert frozen['runtime_review_sha256'] == 'a' * 64
    assert frozen['image_id'] == runtime['image']
    assert frozen['runtime']['entrypoint'] == 'main.py'
    assert frozen['parameters'] == {}
    rows = detail.json()['attempts']
    assert len(rows) == 1 and rows[0]['log_bytes'] > 0


def test_materialized_callable_runs_from_nested_workdir_and_receives_parameters(runtime, tmp_path):
    from backend.models import ScriptFeature, ScriptVersion
    from worker import native_runner

    spec = {**runtime['spec'], 'entrypoint': 'src/main.py', 'workdir': 'src', 'callable': 'run_feature',
            'parameters': [{'name': 'label', 'type': 'string', 'required': True, 'source': 'function'}]}
    code = b"from pathlib import Path\ndef run_feature(label):\n    Path('business-called.txt').write_text(label, encoding='utf-8')\n"
    with locked_db() as db:
        version = db.get(ScriptVersion, runtime['versions']['first'])
        path, digest = write_blob(encode_files({'src/main.py': code}))
        artifact = Artifact(path=path, sha256=digest, kind='sources')
        db.add(artifact)
        db.flush()
        version.source_artifact_id = artifact.id
        feature = db.get(ScriptFeature, runtime['features']['first'])
        feature.runtime_spec = spec
        task = db.get(Task, runtime['tasks']['first'])
        task.feature_config_snapshot = {'runtime': spec, 'parameters': {'label': 'called-with-parameter'}}

    execution, _ = enqueue_native(runtime['tasks']['first'], 'manual-key-0063')
    with Session() as db:
        prepared = native_runner.materialize(db, db.get(Execution, execution))
    assert prepared['arguments'][0].endswith('/airdrop_runtime_launcher.py')
    assert prepared['arguments'][1].endswith('/src/main.py')

    for name, data in prepared['files'].items():
        target = tmp_path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    working = tmp_path / 'src'
    arguments = [Path(item).name for item in prepared['arguments']]
    run = subprocess.run([sys.executable, '-B', *arguments], cwd=working,
                         capture_output=True, text=True, check=False)
    assert run.returncode == 0, run.stderr
    assert (working / 'business-called.txt').read_text(encoding='utf-8') == 'called-with-parameter'


def test_dependency_build_does_not_hold_database_lock_while_waiting_for_controller(runtime, monkeypatch):
    from contextlib import contextmanager
    from backend import queue
    from backend.runtime_builds import run_build
    from runner import controller

    build_id = runtime['builds']['first']
    with locked_db() as db:
        db.get(RuntimeBuild, build_id).status = 'BUILDING'

    depth = 0
    original = queue.locked_db

    @contextmanager
    def guarded_locked_db():
        nonlocal depth
        if depth:
            raise AssertionError('database transaction remained open during controller call')
        with original() as db:
            depth += 1
            try:
                yield db
            finally:
                depth -= 1

    def build(*args, **kwargs):
        assert depth == 0
        return {'image_id': 'sha256:' + 'e' * 64, 'lock': '{"freeze":"python==3.12"}'}

    monkeypatch.setattr(queue, 'locked_db', guarded_locked_db)
    monkeypatch.setattr(controller, 'stream', build)
    assert run_build(build_id) is True
    with Session() as db:
        assert db.get(RuntimeBuild, build_id).status == 'READY'


def test_expired_dependency_build_is_released_for_manual_retry(runtime, monkeypatch):
    from backend.runtime_builds import claim_build

    monkeypatch.setattr(settings, 'native_build_timeout_seconds', 1)
    build_id = runtime['builds']['first']
    with locked_db() as db:
        build = db.get(RuntimeBuild, build_id)
        build.status = 'BUILDING'
        build.started_at = time.time() - 70

    assert claim_build() is None
    with Session() as db:
        build = db.get(RuntimeBuild, build_id)
        assert build.status == 'FAILED'
        assert '超时或失联' in build.error


def test_non_zero_exit_is_failed_and_never_auto_retried(runtime, monkeypatch):
    monkeypatch.setenv('FAKE_DOCKER_EXIT_CODE', '3')
    execution, attempt = prepare(runtime, 'manual-key-0020')
    work(execution, attempt)
    with Session() as db:
        row = db.get(Execution, execution)
        assert row.status == 'FAILED' and row.retry_count == 0 and row.lease_until is None
        assert db.get(ExecutionAttempt, attempt).process_exit_code == 3
        rows = list(db.scalars(select(WalletTaskResult).where(WalletTaskResult.execution_id == execution)))
        assert all(entry.status == NATIVE_NO_REPORT and '未正常结束' in entry.message for entry in rows)
        assert db.scalar(select(AuditLog).where(AuditLog.action == 'execution.alert'))
    # 已经结束的运行不会自己回到队列，也不会被再次领取。
    assert claim_native(settings.worker_id) is None


def test_declared_timeout_stops_the_container_and_is_reported_as_timeout(runtime, monkeypatch):
    monkeypatch.setenv('FAKE_DOCKER_AUTO_EXIT', '0')
    monkeypatch.setenv('FAKE_DOCKER_HOLD_SECONDS', '120')
    execution, attempt = prepare(runtime, 'manual-key-0030', timeout_seconds=1, stop_grace_seconds=2)
    started = time.time()
    work(execution, attempt)
    elapsed = time.time() - started
    assert elapsed < 30, elapsed
    with Session() as db:
        row = db.get(Execution, execution)
        assert row.status == 'TIMEOUT' and row.finished_at
        assert db.get(ExecutionAttempt, attempt).status == 'TIMEOUT'
    signals = [item for item in commands(runtime['state']) if item[0] == 'kill']
    assert signals and signals[0][1:3] == ['--signal', 'SIGTERM']
    assert '运行超过声明时限' in read_log(execution, attempt)


def test_manual_stop_ends_as_cancelled_with_the_declared_signal(runtime, monkeypatch):
    monkeypatch.setenv('FAKE_DOCKER_AUTO_EXIT', '0')
    monkeypatch.setenv('FAKE_DOCKER_HOLD_SECONDS', '120')
    execution, attempt = prepare(runtime, 'manual-key-0040', stop_grace_seconds=2)
    worker = threading.Thread(target=work, args=(execution, attempt), daemon=True)
    worker.start()
    try:
        deadline = time.time() + 60
        while time.time() < deadline:
            with Session() as db:
                row = db.get(Execution, execution)
                if row.status == 'RUNNING' and db.get(ExecutionAttempt, attempt).runner_handle:
                    break
            time.sleep(0.1)
        else:
            pytest.fail('运行没有进入 RUNNING')
        # 人工停止：先置取消标记，由 Worker 按脚本声明的信号停止进程。
        with locked_db() as db:
            row = db.get(Execution, execution)
            row.cancel_requested = True
            row.status = 'STOPPING'
    finally:
        worker.join(timeout=90)
    assert not worker.is_alive()
    with Session() as db:
        row = db.get(Execution, execution)
        assert row.status == 'CANCELLED' and row.finished_at
        assert db.get(ExecutionAttempt, attempt).process_exit_code == 130
        rows = list(db.scalars(select(WalletTaskResult).where(WalletTaskResult.execution_id == execution)))
        assert all(entry.status == NATIVE_NO_REPORT for entry in rows)
    signals = [item for item in commands(runtime['state']) if item[0] == 'kill']
    assert signals and signals[0][1:3] == ['--signal', 'SIGINT']


def test_log_storage_failure_stops_the_container_and_keeps_every_lock(runtime, monkeypatch):
    monkeypatch.setenv('FAKE_DOCKER_AUTO_EXIT', '0')
    monkeypatch.setenv('FAKE_DOCKER_HOLD_SECONDS', '120')
    monkeypatch.setenv('FAKE_DOCKER_ATTACH_CHUNK', '65536')
    output = Path(runtime['tmp_path']) / 'native-output.txt'
    output.write_bytes(('日志保存必须成功，否则这条记录不算完整。\n' * 20000).encode())
    monkeypatch.setenv('FAKE_DOCKER_OUTPUT_FILE', str(output))

    def explode(*args, **kwargs):
        raise RuntimeError('磁盘写入失败')

    monkeypatch.setattr(runtime_logs, 'append_text', explode)
    execution, attempt = prepare(runtime, 'manual-key-0050', stop_grace_seconds=2)
    work(execution, attempt)
    with Session() as db:
        row = db.get(Execution, execution)
        assert row.status == 'MANUAL_REQUIRED' and row.log_status == 'STORAGE_FAILED'
        assert '日志保存失败' in row.error
        # 日志不完整时既不自动重放，也不释放钱包锁与状态锁。
        assert db.scalar(select(WalletLock).where(WalletLock.execution_id == execution))
        assert db.scalar(select(ProjectScriptState).where(ProjectScriptState.locked_execution_id == execution))
        attempt_row = db.get(ExecutionAttempt, attempt)
        assert not runtime_logs.segments_of(db, attempt)
        assert attempt_row.status == 'MANUAL_REQUIRED'
    record = container_record(runtime['state'], attempt_handle(execution, attempt))
    assert record['signal'] == 'SIGINT' and record['removed'] is True


def test_unknown_run_preserves_the_container_and_private_workdir(runtime, monkeypatch):
    from runner import native

    execution, attempt = prepare(runtime, 'manual-key-0060')

    def disconnected(*args, **kwargs):
        raise controller.ControllerError('controller disconnected')

    monkeypatch.setattr(worker_main.native_runner, 'attach', disconnected)
    work(execution, attempt)

    with Session() as db:
        row = db.get(Execution, execution)
        assert row.status == 'UNKNOWN'
        handle = db.get(ExecutionAttempt, attempt).runner_handle
    assert not container_record(runtime['state'], handle)['removed']
    assert native.attempt_dir(attempt).is_dir()


def test_cancel_before_output_attach_removes_created_container_without_starting_it(runtime, monkeypatch):
    execution, attempt = prepare(runtime, 'manual-key-cancel-before-start')
    mark_running = worker_main.mark_running

    def deny_start(*args):
        mark_running(*args)
        return False

    monkeypatch.setattr(worker_main, 'mark_running', deny_start)

    work(execution, attempt)

    with Session() as db:
        row = db.get(Execution, execution)
        handle = db.get(ExecutionAttempt, attempt).runner_handle
        assert row.status == 'CANCELLED', f'{row.status}: {row.error}'
    assert container_record(runtime['state'], handle)['removed'] is True
    assert not any(command[0] == 'start' and '--attach' in command for command in commands(runtime['state']))
    assert not any(command[0] == 'attach' for command in commands(runtime['state']))


def test_state_generation_conflict_preserves_encrypted_candidate(runtime):
    import base64
    from packages.runtime_protocol import NativeRuntimeSpec

    execution, attempt = prepare(runtime, 'manual-key-0061')
    produced = f"address,points\n{runtime['addresses'][0]},42\n".encode()
    with locked_db() as db:
        state = db.scalar(select(ProjectScriptState).where(
            ProjectScriptState.project_id == runtime['projects']['first']))
        state.generation = 4

    worker_main.save_state(execution, attempt,
                           {'state.csv': {'data': base64.b64encode(produced).decode()}},
                           {'spec': NativeRuntimeSpec.model_validate(runtime['spec'])})

    with Session() as db:
        row = db.get(Execution, execution)
        assert row.state_status == 'PENDING_REVIEW'
        assert row.state_review_artifact_id
        candidate = db.get(Artifact, row.state_review_artifact_id)
        assert candidate.kind == 'runtime-state-pending'
        assert decode_files(read_blob(candidate.path, candidate.sha256)) == {'state.csv': produced}
        assert produced not in Path(candidate.path).read_bytes()
        audit = db.scalar(select(AuditLog).where(
            AuditLog.action == 'runtime.state.review', AuditLog.target == execution))
        assert audit.detail['expected_generation'] == 0 and audit.detail['current_generation'] == 4
    assert finalize_native(execution, attempt, {'exit_code': 0, 'log_status': 'COMPLETE'})
    with Session() as db:
        row = db.get(Execution, execution)
        state = db.scalar(select(ProjectScriptState).where(
            ProjectScriptState.project_id == runtime['projects']['first']))
        assert row.status == 'COMPLETED' and row.state_status == 'PENDING_REVIEW'
        assert '状态回写待处理' in row.error
        assert state.locked_execution_id == execution
        assert db.scalar(select(WalletLock).where(WalletLock.execution_id == execution))


def test_state_candidate_can_be_downloaded_and_explicitly_merged_without_losing_newer_rows(runtime, logged):
    import base64

    from backend import runtime_state
    from packages.runtime_protocol import NativeRuntimeSpec

    with locked_db() as db:
        db.get(Task, runtime['tasks']['first']).wallet_selection = {'wallet_ids': [runtime['wallets'][0]]}
    execution, attempt = prepare(runtime, 'manual-key-0062')
    current = f"address,points\n{runtime['addresses'][1]},99\n".encode()
    produced = f"address,points\n{runtime['addresses'][0]},42\n".encode()
    with locked_db() as db:
        state = db.scalar(select(ProjectScriptState).where(
            ProjectScriptState.project_id == runtime['projects']['first']))
        path, digest = write_blob(encode_files({'state.csv': current}))
        artifact = Artifact(path=path, sha256=digest, kind='runtime-state')
        db.add(artifact)
        db.flush()
        state.generation = 4
        state.artifact_id = artifact.id
        state.artifact_sha256 = digest

    worker_main.save_state(execution, attempt,
                           {'state.csv': {'data': base64.b64encode(produced).decode()}},
                           {'spec': NativeRuntimeSpec.model_validate(runtime['spec'])})
    assert finalize_native(execution, attempt, {'exit_code': 0, 'log_status': 'COMPLETE'})

    detail = logged.get('/api/executions/' + execution)
    assert detail.status_code == 200, detail.text
    candidate_sha = detail.json()['state_review_candidate_sha256']
    candidate = logged.get('/api/executions/' + execution + '/state-candidate')
    assert candidate.status_code == 200, candidate.text
    with zipfile.ZipFile(io.BytesIO(candidate.content)) as archive:
        assert archive.read('state.csv') == produced

    rejected = logged.post('/api/executions/' + execution + '/state-review/commit', json={
        'candidate_sha256': '0' * 64, 'confirmed': True, 'note': 'candidate checked against current state'
    })
    assert rejected.status_code == 409
    recovered = logged.post('/api/executions/' + execution + '/state-review/commit', json={
        'candidate_sha256': candidate_sha, 'confirmed': True, 'note': 'candidate checked against current state'
    })
    assert recovered.status_code == 200, recovered.text
    with Session() as db:
        row = db.get(Execution, execution)
        state = db.scalar(select(ProjectScriptState).where(
            ProjectScriptState.project_id == runtime['projects']['first']))
        artifact = db.get(Artifact, state.artifact_id)
        names, rows = runtime_state.read_rows('state.csv', decode_files(read_blob(artifact.path, artifact.sha256))['state.csv'],
                                              NativeRuntimeSpec.model_validate(runtime['spec']).state_bindings[0])
        assert names == ['address', 'points']
        assert {item['address']: item['points'] for item in rows} == {
            runtime['addresses'][0]: '42', runtime['addresses'][1]: '99'}
        assert state.generation == 5 and state.locked_execution_id is None
        assert row.status == 'COMPLETED' and row.state_status == 'COMMITTED'
        assert row.state_review_artifact_id is None


def attempt_handle(execution_id: str, attempt_id: str) -> str:
    with Session() as db:
        return db.get(ExecutionAttempt, attempt_id).runner_handle
