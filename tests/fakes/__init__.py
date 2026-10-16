"""测试夹具：假 docker 客户端与接线工具。"""
import sys
from pathlib import Path

FAKE_DOCKER = Path(__file__).with_name('fake_docker.py')


def wire(monkeypatch, tmp_path, name='docker-state'):
    """把 runner.native 指向假 docker，并把运行目录放进临时目录。"""
    from backend.config import settings
    state = tmp_path / name
    monkeypatch.setenv('FAKE_DOCKER_STATE', str(state))
    monkeypatch.setattr(settings, 'native_docker_bin', [sys.executable, str(FAKE_DOCKER)])
    monkeypatch.setattr(settings, 'native_docker_host', '')
    monkeypatch.setattr(settings, 'native_run_root', tmp_path / 'runs')
    monkeypatch.setattr(settings, 'native_network_subnet', '172.31.240.0/24')
    monkeypatch.setattr(settings, 'native_stop_grace_seconds', 1)
    monkeypatch.setattr(settings, 'native_blocked_targets', ['127.0.0.1:9'])
    monkeypatch.setattr(settings, 'native_isolation_check_seconds', 30)
    return state


def reset_isolation():
    from runner import environment
    environment._verification = {'at': 0.0, 'ok': False, 'reason': '尚未验证', 'image': None}


def commands(state):
    """假 docker 收到的全部 argv，按顺序返回。"""
    import json
    path = Path(state) / 'commands.log'
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines() if line.strip()]


def container_record(state, container_id):
    import json
    return json.loads((Path(state) / 'containers' / f'{container_id}.json').read_text(encoding='utf-8'))


def make_image(state, monkeypatch, key='a' * 64):
    """用假 docker 构一次依赖环境，得到一个可运行的镜像标识。"""
    from runner import native
    reset_isolation()
    return native.build_image(key, 'python:3.12-slim', '3.12', ['requirements.txt'],
                              {'requirements.txt': b'openpyxl==3.1.5\n'}, lambda line: None)['image_id']
