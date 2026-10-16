from pathlib import Path

import yaml


def test_smart_adapter_profile_keeps_native_execution_disabled():
    base = yaml.safe_load(Path('docker-compose.yml').read_text(encoding='utf-8'))
    assert base['services']['backend']['environment']['AIRDROP_NATIVE_EXECUTION_ENABLED'] == 'false'
    assert base['services']['worker']['environment']['AIRDROP_NATIVE_EXECUTION_ENABLED'] == 'false'

    compose = yaml.safe_load(Path('deploy/compose.adapter.yml').read_text(encoding='utf-8'))
    adapter = compose['services']['adapter']

    assert adapter['environment']['AIRDROP_NATIVE_EXECUTION_ENABLED'] == 'false'
    assert all('/sockets' not in str(volume) for volume in adapter.get('volumes', []))

    native = yaml.safe_load(Path('deploy/compose.native.yml').read_text(encoding='utf-8'))
    assert native['services']['backend']['environment']['AIRDROP_NATIVE_EXECUTION_ENABLED'] == 'true'
    assert native['services']['worker']['environment']['AIRDROP_NATIVE_EXECUTION_ENABLED'] == 'true'
