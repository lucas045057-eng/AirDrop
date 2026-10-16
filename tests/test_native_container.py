"""容器执行层：命令拼装、隔离限制、输出流、停止与状态收集（使用假 docker）。"""
import base64
import json
import os
import socket
import sys
import threading
import time
from pathlib import Path
import pytest
from backend.config import settings
from runner import controller_service, environment, log_stream, native
from tests.fakes import commands, container_record, make_image, reset_isolation, wire

ATTEMPT = 'a1' * 16
EXECUTION = 'b2' * 16
OTHER_ATTEMPT = 'c3' * 16


@pytest.fixture
def docker(tmp_path, monkeypatch):
    state = wire(monkeypatch, tmp_path)
    reset_isolation()
    return state


def start_payload(image_id, files=None, attempt=ATTEMPT, **overrides):
    body = {'attempt_id': attempt, 'execution_id': EXECUTION, 'image_id': image_id,
            'network': 'airdrop-native', 'subdir': '', 'arguments': ['main.py'],
            'files': files or {'main.py': base64.b64encode(b'print(1)\n').decode()}}
    body.update(overrides)
    return body


def start_container(state, image_id=None, files=None, **overrides):
    image = image_id or make_image(state, None)
    return controller_service.op_start(start_payload(image, files, **overrides), lambda event: None)


def test_build_locks_dependencies_with_a_fixed_dockerfile(docker, tmp_path):
    seen = []
    result = native.build_image('c' * 64, 'python:3.12-slim', '3.12', ['requirements.txt'],
                               {'requirements.txt': b'openpyxl==3.1.5\n'}, seen.append)
    assert result['image_id'].startswith('sha256:')
    lock = json.loads(result['lock'])
    assert lock['environment_key'] == 'c' * 64 and 'openpyxl==3.1.5' in lock['freeze']
    assert lock['format'] == 'airdrop-runtime-lock-v1'
    build = [item for item in commands(docker) if item[0] == 'build'][0]
    # BuildKit only receives the pinned dependency declarations and a configured index.
    assert '--network' in build and build[build.index('--network') + 1] == 'default'
    assert '--log-driver' not in build and '--pull=never' not in build
    assert '--progress' in build and build[build.index('--progress') + 1] == 'plain'
    assert any(value == 'AIRDROP_PYPI_INDEX_URL=https://pypi.org/simple' for value in build)
    assert build[build.index('--build-arg') + 1] == 'AIRDROP_BASE_IMAGE=python:3.12-slim'
    context = Path(build[-1])
    assert settings.native_run_root in context.parents
    assert not context.exists()
    assert seen and any('installing declared dependencies' in line for line in seen)


def test_build_dockerfile_only_copies_declared_dependencies():
    body = native.build_dockerfile(['requirements.txt'], '3.12').decode()
    assert 'COPY airdrop-dependencies/ /tmp/airdrop-dependencies/' in body
    assert 'pip download --only-binary=:all:' in body
    assert '--index-url="${AIRDROP_PYPI_INDEX_URL}"' in body
    assert 'pip install --no-cache-dir --no-index --find-links=/tmp/airdrop-wheelhouse' in body
    assert 'wallet' not in body.lower() and 'COPY .' not in body
    assert body.index('ARG AIRDROP_BASE_IMAGE') < body.index('FROM ${AIRDROP_BASE_IMAGE}')


def test_build_rejects_dependency_directives_and_urls(docker):
    with pytest.raises(native.NativeError, match='只允许标准 Python 包要求'):
        native.build_image('a' * 64, 'python:3.12-slim', '3.12', ['requirements.txt'],
                           {'requirements.txt': b'--extra-index-url https://packages.example/simple\n'})
    with pytest.raises(native.NativeError, match='只允许标准 Python 包要求'):
        native.build_image('b' * 64, 'python:3.12-slim', '3.12', ['requirements.txt'],
                           {'requirements.txt': b'custom @ https://packages.example/custom.whl\n'})


def test_build_failure_and_non_requirements_input_are_rejected(docker):
    with pytest.raises(native.NativeError) as blocked:
        native.build_image('d' * 64, 'python:3.12-slim', '3.12', ['setup.py'], {'setup.py': b''})
    assert 'requirements' in str(blocked.value)
    with pytest.raises(native.NativeError) as missing:
        native.build_image('e' * 64, 'python:3.12-slim', '3.12', ['requirements.txt'], {})
    assert '不在本版本工件中' in str(missing.value)
    with pytest.raises(native.NativeError) as failed:
        native.build_image('f' * 64, 'python:3.12-slim', '3.12', ['requirements.txt'],
                           {'requirements.txt': b'AIRDROP-FAKE-FAIL\n'})
    assert '依赖构建失败' in str(failed.value)


def test_container_is_created_with_isolation_limits_and_private_workdir(docker, monkeypatch):
    # The fake container needs no ownership changes, so host assertions can read the inputs.
    monkeypatch.setattr(native, 'grant_container_access', lambda root: True)
    image = make_image(docker, None)
    result = start_container(docker, image, files={'main.py': base64.b64encode(b'print(1)\n').decode(),
                                                   'inputs/wallets.csv': base64.b64encode(b'address\n').decode()},
                             arguments=['main.py', '{wallet_path}'])
    record = container_record(docker, result['container_id'])
    assert record['read_only'] is True and record['cap_drop'] == 'ALL'
    assert record['security_opt'] == 'no-new-privileges=true' and record['network'] == 'airdrop-native'
    assert record['pids_limit'] == str(settings.native_pids) and record['memory'] == f'{settings.native_memory_mib}m'
    assert record['memory_swap'] == f'{settings.native_memory_mib}m'
    assert record['log_driver'] == 'none' and record['user'] == settings.native_container_user
    assert record['workdir'] == '/work' and record['command'] == ['python', '-B', '-u', 'main.py', '{wallet_path}']
    assert record['labels']['airdrop.attempt'] == ATTEMPT and record['labels']['airdrop.execution'] == EXECUTION
    root = native.attempt_dir(ATTEMPT)
    assert record['mount'] == f'type=bind,source={root},target=/work'
    assert (root / 'main.py').read_bytes() == b'print(1)\n'
    assert (root / 'inputs' / 'wallets.csv').is_file()
    # 所有 docker 调用都是 argv 列表，没有经过任何 shell 解释器。
    shells = {'sh', 'bash', 'dash', 'cmd', 'cmd.exe', 'powershell', '/bin/sh'}
    assert not any(item in shells for command in commands(docker) for item in command)
    mount = record['mount']
    assert 'source=' + str(settings.native_run_root) in mount and ATTEMPT in mount


def test_repeated_start_returns_the_same_container(docker):
    image = make_image(docker, None)
    first = start_container(docker, image)
    second = start_container(docker, image)
    assert first['container_id'] == second['container_id']
    assert second['reused'] is True
    creates = [item for item in commands(docker) if item[0] == 'create']
    assert len(creates) == 1


def test_environment_variables_are_delivered_through_a_private_file(docker):
    image = make_image(docker, None)
    result = start_container(docker, image, environment={'CHECKIN_MODE': 'daily'})
    record = container_record(docker, result['container_id'])
    assert record['environment'] == {'CHECKIN_MODE': 'daily'}
    create = [item for item in commands(docker) if item[0] == 'create'][-1]
    assert all('CHECKIN_MODE=daily' not in value for value in create)
    # 临时环境文件在创建容器后立即删除，不留在运行目录里。
    assert not Path(record['env_file']).exists()
    with pytest.raises(controller_service.RequestError):
        start_container(docker, image, attempt=OTHER_ATTEMPT, environment={'lower': '1'})


def test_attach_streams_every_byte_and_masks_injected_secrets(docker, monkeypatch, tmp_path):
    image = make_image(docker, None)
    secret = '0x' + 'ab' * 32
    payload = ('开始\n' + '日志' * 400 + '\nAuthorization: Bearer abcdefghijklmnop\n'
               + 'key=' + secret + '\n结束\n').encode()
    source = tmp_path / 'container-output.txt'
    source.write_bytes(payload)
    monkeypatch.setenv('FAKE_DOCKER_OUTPUT_FILE', str(source))
    monkeypatch.setenv('FAKE_DOCKER_ATTACH_CHUNK', '7')
    container = start_container(docker, image)
    events = []
    result = controller_service.op_attach({'attempt_id': ATTEMPT, 'container_id': container['container_id'],
                                          'secrets': [secret], 'timeout_seconds': 60}, events.append)
    text = ''.join(base64.b64decode(event['data']).decode() for event in events if event.get('event') == 'log')
    assert secret not in text and 'abcdefghijklmnop' not in text
    assert log_stream.MASK in text
    assert text.startswith('开始\n日志') and text.endswith('结束\n')
    assert 'key=' + log_stream.MASK in text and 'Authorization: ' + log_stream.MASK in text
    # 加密前统计的字节数等于容器输出总量：跨块流没有丢内容。
    assert result['log_bytes'] == len(payload)
    assert result['exit_code'] == 0 and result['redacted'] >= 2
    assert any(event.get('event') == 'platform' for event in events)


def test_short_lived_container_output_is_attached_before_process_start(docker, monkeypatch, tmp_path):
    payload = b'early output before the log reader can connect\n'
    source = tmp_path / 'short-output.txt'
    source.write_bytes(payload)
    monkeypatch.setenv('FAKE_DOCKER_OUTPUT_FILE', str(source))
    monkeypatch.setenv('FAKE_DOCKER_EXIT_ON_START', '1')
    image = make_image(docker, None)
    handle = start_container(docker, image)
    events = []

    result = controller_service.op_attach({'attempt_id': ATTEMPT, 'container_id': handle['container_id']}, events.append)
    output = b''.join(base64.b64decode(event['data']) for event in events
                      if event.get('event') == 'log' and event.get('encoding') == 'base64')
    start_commands = [item for item in commands(docker) if item[0] == 'start']

    assert output == payload
    assert start_commands and '--attach' in start_commands[-1]
    assert result['exit_code'] == 0 and result['state'] == 'EXITED'


def test_attach_delivers_small_chunks_without_waiting_for_read_buffer(docker, monkeypatch, tmp_path):
    payload = b'x' * 40
    source = tmp_path / 'slow-output.txt'
    source.write_bytes(payload)
    monkeypatch.setenv('FAKE_DOCKER_OUTPUT_FILE', str(source))
    monkeypatch.setenv('FAKE_DOCKER_ATTACH_CHUNK', '1')
    monkeypatch.setenv('FAKE_DOCKER_ATTACH_DELAY', '0.01')
    image = make_image(docker, None)
    handle = start_container(docker, image)
    first_chunk = threading.Event()
    started = time.monotonic()
    chunks = []

    def on_chunk(chunk):
        chunks.append(chunk)
        first_chunk.set()

    reader = threading.Thread(target=lambda: native.attach(handle['container_id'], on_chunk, idle_timeout=60),
                              daemon=True)
    reader.start()
    assert first_chunk.wait(0.5)
    assert time.monotonic() - started < 0.5
    reader.join(timeout=5)
    assert not reader.is_alive() and b''.join(chunks) == payload


def test_attach_reports_failure_exit_code_and_oom(docker, monkeypatch):
    image = make_image(docker, None)
    monkeypatch.setenv('FAKE_DOCKER_EXIT_CODE', '3')
    container = start_container(docker, image)
    result = controller_service.op_attach({'attempt_id': ATTEMPT, 'container_id': container['container_id']},
                                          lambda event: None)
    assert result['exit_code'] == 3 and result['state'] == 'EXITED' and result['timed_out'] is False


def test_attach_timeout_stops_the_container(docker, monkeypatch):
    image = make_image(docker, None)
    monkeypatch.setenv('FAKE_DOCKER_AUTO_EXIT', '0')
    container = start_container(docker, image)
    chunks = []
    result = native.attach(container['container_id'], chunks.append, idle_timeout=60,
                           deadline=time.monotonic() + 0.5)
    assert result['timed_out'] is True
    assert native.inspect_container(container['container_id'])['running'] is False


def test_stop_sends_the_declared_signal_then_forces_termination(docker, monkeypatch):
    image = make_image(docker, None)
    monkeypatch.setenv('FAKE_DOCKER_AUTO_EXIT', '0')
    container = start_container(docker, image)
    outcome = {}

    def attachment():
        outcome.update(native.attach(container['container_id'], lambda block: None, idle_timeout=60))

    thread = threading.Thread(target=attachment, daemon=True)
    thread.start()
    time.sleep(0.4)
    stopped = controller_service.op_stop({'attempt_id': ATTEMPT, 'signal': 'SIGINT', 'grace_seconds': 5})
    thread.join(timeout=10)
    assert stopped['stopped'] is True and stopped['forced'] is False and stopped['exit_code'] == 130
    signals = [item for item in commands(docker) if item[0] == 'kill']
    assert signals and signals[0][1:3] == ['--signal', 'SIGINT']
    assert outcome.get('exit_code') == 130


def test_collect_returns_declared_state_files_and_cleanup_removes_everything(docker):
    image = make_image(docker, None)
    container = start_container(docker, image)
    root = native.attempt_dir(ATTEMPT)
    (root / 'state.csv').write_bytes(b'address,points\n0xabc,7\n')
    collected = controller_service.op_collect({'attempt_id': ATTEMPT, 'paths': ['state.csv', 'missing.csv']})
    entry = collected['files']['state.csv']
    assert base64.b64decode(entry['data']) == b'address,points\n0xabc,7\n' and entry['size'] == 23
    assert collected['missing'] == ['missing.csv']
    with pytest.raises(native.NativeError):
        native.read_workdir_file(ATTEMPT, '../outside.csv')
    with pytest.raises(native.NativeError):
        native.read_workdir_file(ATTEMPT, '/tmp/outside.csv')
    controller_service.op_cleanup({'attempt_id': ATTEMPT})
    assert not root.exists() and native.find_container(ATTEMPT) is None


def test_reachable_metadata_target_blocks_container_start(docker):
    image = make_image(docker, None)
    server = socket.socket()
    server.bind(('127.0.0.1', 0))
    server.listen(1)
    server.settimeout(0.2)
    port = server.getsockname()[1]
    stop = threading.Event()

    def accept_forever():
        # 探针会连接多次；这里持续接受，模拟一个仍然可达的管理端口。
        while not stop.is_set():
            try:
                connection, _ = server.accept()
            except (OSError, TimeoutError):
                continue
            connection.close()

    listener = threading.Thread(target=accept_forever, daemon=True)
    listener.start()
    try:
        settings.native_blocked_targets[:] = ['127.0.0.1:' + str(port)]
        environment._verification = {'at': 0.0, 'ok': False, 'reason': '尚未验证', 'image': None}
        verification = environment.verify_isolation(image, force=True)
        assert verification['ok'] is False and '127.0.0.1' in verification['reason']
        with pytest.raises(controller_service.RequestError) as blocked:
            start_container(docker, image)
        assert '网络隔离未通过验证' in str(blocked.value)
    finally:
        stop.set()
        listener.join(timeout=5)
        server.close()


def test_firewall_rules_cover_metadata_and_the_dedicated_bridge(docker):
    environment.ensure_network()
    settings.native_blocked_targets[:] = ['169.254.169.254:80']
    settings.native_blocked_cidrs[:] = ['10.0.0.0/8']
    rules = environment.firewall_rules()
    assert any('169.254.169.254' in rule for rule in rules) and any('10.0.0.0/8' in rule for rule in rules)
    assert any('DOCKER-USER' in rule and rule.startswith('iptables -C DOCKER-USER -i br-') for rule in rules)
    assert any('INPUT' in rule and 'AIRDROP-NATIVE-INPUT' in rule for rule in rules)
    assert rules[0].endswith('|| true')


def test_refused_target_is_not_reported_as_firewall_blocked(docker):
    image = make_image(docker, None)
    settings.native_blocked_targets[:] = ['127.0.0.1:1']

    result = environment.verify_isolation(image, force=True)

    assert result['ok'] is False, result
    assert result['targets']['127.0.0.1:1'] == 'CONNECTION_REFUSED'


def test_prepare_runtime_reports_capabilities(docker):
    image = make_image(docker, None)
    status = environment.prepare_runtime(image)
    assert status['usable'] is True and status['docker']['server'] == '27.5.1'
    assert status['network']['name'] == 'airdrop-native' and status['disk_ok'] is True
    ping = controller_service.op_ping({})
    assert ping['docker'] is True and ping['network_ready'] is True


def test_requests_outside_the_platform_contract_are_rejected(docker):
    image = make_image(docker, None)
    with pytest.raises(controller_service.RequestError):
        controller_service.dispatch({'protocol': 'airdrop-controller-v1', 'op': 'shell', 'payload': {}})
    with pytest.raises(controller_service.RequestError):
        controller_service.dispatch({'protocol': 'other', 'op': 'ping', 'payload': {}})
    with pytest.raises(native.NativeError):
        native.require_id('../etc/passwd')
    with pytest.raises(controller_service.RequestError) as network:
        start_container(docker, image, network='bridge')
    assert '专用容器网络' in str(network.value)
    with pytest.raises(native.NativeError):
        native.write_inputs(ATTEMPT, {'../escape.py': b'x'})
    with pytest.raises(controller_service.RequestError) as image_error:
        start_container(docker, 'sha256:missing')
    assert '镜像不存在' in str(image_error.value)
