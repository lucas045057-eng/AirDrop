"""控制器服务端与客户端：本地 socket 协议、幂等启动、停止与错误返回。"""
import base64
import json
import socket
import threading
import time
import pytest
from backend.config import settings
from runner import controller, controller_service
from tests.fakes import commands, make_image, reset_isolation, wire

ATTEMPT = 'd4' * 16
EXECUTION = 'e5' * 16


def free_port():
    probe = socket.socket()
    probe.bind(('127.0.0.1', 0))
    port = probe.getsockname()[1]
    probe.close()
    return port


@pytest.fixture
def service(tmp_path, monkeypatch):
    state = wire(monkeypatch, tmp_path)
    reset_isolation()
    address = 'tcp://127.0.0.1:' + str(free_port())
    monkeypatch.setattr(settings, 'native_socket_path', address)
    thread = threading.Thread(target=controller_service.serve, args=(address,), daemon=True)
    thread.start()
    for _ in range(100):
        if controller.online(timeout=1):
            break
        time.sleep(0.05)
    else:
        pytest.fail('控制器服务没有在本地 socket 上就绪')
    return state


def test_client_and_service_share_the_same_protocol(service):
    status = controller.online()
    assert status['protocol'] == 'airdrop-controller-v1' and status['docker'] is True
    assert status['network_ready'] in {True, False}
    image = make_image(service, None)
    assert controller.call('image', {'image_id': image})['exists'] is True
    events = []
    built = controller.stream('build', {'environment_key': '9' * 64, 'base_image': 'python:3.12-slim',
                                        'python_version': '3.12', 'dependencies': ['requirements.txt'],
                                        'files': {'requirements.txt': base64.b64encode(b'openpyxl==3.1.5\n').decode()}},
                              events.append, idle_timeout=60)
    assert built['image_id'].startswith('sha256:')
    assert any(event.get('event') == 'log' and 'installing' in str(event.get('data')) for event in events)
    assert json.loads(built['lock'])['format'] == 'airdrop-runtime-lock-v1'


def test_service_runs_attaches_collects_and_cleans_up(service, monkeypatch):
    image = make_image(service, None)
    monkeypatch.setenv('FAKE_DOCKER_OUTPUT_FILE', str(_output_file(service, b'\xe5\xbc\x80\xe5\xa7\x8b\n')))
    monkeypatch.setenv('FAKE_DOCKER_WRITE_STATE', json.dumps([['state.csv', 'address,points\n0xabc,3\n']]))
    handle = controller.call('start', {'attempt_id': ATTEMPT, 'execution_id': EXECUTION, 'image_id': image,
                                       'network': 'airdrop-native', 'subdir': '', 'arguments': ['main.py'],
                                       'files': {'main.py': base64.b64encode(b'print(1)\n').decode()}})
    assert handle['reused'] is False
    again = controller.call('start', {'attempt_id': ATTEMPT, 'execution_id': EXECUTION, 'image_id': image,
                                      'network': 'airdrop-native', 'subdir': '', 'arguments': ['main.py'],
                                      'files': {}})
    assert again['container_id'] == handle['container_id'] and again['reused'] is True
    events = []
    outcome = controller.stream('attach', {'attempt_id': ATTEMPT, 'container_id': handle['container_id'],
                                           'timeout_seconds': 60, 'secrets': []}, events.append, idle_timeout=60)
    assert outcome['exit_code'] == 0 and outcome['state'] == 'EXITED'
    log = b''.join(base64.b64decode(event['data']) for event in events if event.get('encoding') == 'base64')
    assert log == '开始\n'.encode()
    collected = controller.call('collect', {'attempt_id': ATTEMPT, 'paths': ['state.csv']})
    assert base64.b64decode(collected['files']['state.csv']['data']) == b'address,points\n0xabc,3\n'
    assert controller.call('status', {'attempt_id': ATTEMPT})['running'] is False
    assert controller.call('cleanup', {'attempt_id': ATTEMPT})['cleaned'] is True
    assert controller.call('status', {'attempt_id': ATTEMPT})['found'] is False


def test_service_stops_a_running_container_and_reports_errors(service, monkeypatch):
    monkeypatch.setenv('FAKE_DOCKER_AUTO_EXIT', '0')
    image = make_image(service, None)
    handle = controller.call('start', {'attempt_id': ATTEMPT, 'execution_id': EXECUTION, 'image_id': image,
                                       'network': 'airdrop-native', 'subdir': '', 'arguments': ['main.py'],
                                       'files': {'main.py': base64.b64encode(b'print(1)\n').decode()}})
    with pytest.raises(controller.ControllerError) as unknown:
        controller.call('op_not_supported', {})
    assert '不支持的操作' in str(unknown.value)
    with pytest.raises(controller.ControllerError) as invalid:
        controller.call('start', {'attempt_id': '../etc', 'execution_id': EXECUTION, 'image_id': image,
                                  'files': {}})
    assert '32 位标识' in str(invalid.value)
    events = []
    outcome = []
    attaching = threading.Thread(target=lambda: outcome.append(controller.stream(
        'attach', {'attempt_id': ATTEMPT, 'container_id': handle['container_id'],
                   'timeout_seconds': 60, 'secrets': []}, events.append, idle_timeout=60)))
    attaching.start()
    for _ in range(100):
        if controller.call('status', {'attempt_id': ATTEMPT})['running']:
            break
        time.sleep(0.05)
    else:
        pytest.fail('attach 没有先启动容器')
    stopped = controller.call('stop', {'attempt_id': ATTEMPT, 'container_id': handle['container_id'],
                                       'signal': 'SIGINT', 'grace_seconds': 2}, timeout=30)
    assert stopped['found'] is True
    attaching.join(timeout=10)
    assert not attaching.is_alive() and outcome and outcome[0]['state'] == 'EXITED'
    signals = [item for item in commands(service) if item[0] == 'kill']
    assert signals and signals[0][1:3] == ['--signal', 'SIGINT']


def test_service_rejects_malformed_requests(service):
    handle = socket.create_connection(('127.0.0.1', int(str(settings.native_socket_path).rsplit(':', 1)[1])), timeout=5)
    reader = handle.makefile('rb')
    try:
        handle.sendall(b'not json\n')
        assert json.loads(reader.readline())['ok'] is False
        handle.sendall(json.dumps({'protocol': 'airdrop-controller-v1', 'op': 'start',
                                   'payload': {'attempt_id': ATTEMPT, 'execution_id': EXECUTION,
                                               'image_id': 'sha256:x', 'network': 'bridge'}}).encode() + b'\n')
        reply = json.loads(reader.readline())
        assert reply['ok'] is False and '专用容器网络' in reply['error']
    finally:
        reader.close()
        handle.close()


def _output_file(state, payload):
    path = state.parent / 'controller-output.txt'
    path.write_bytes(payload)
    return path
