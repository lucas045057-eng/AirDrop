import json

import pytest

from backend import vault
from backend.db import Session
from backend.models import WalletGroupMember, WalletSecret


@pytest.mark.parametrize('chain', ['EVM', 'SOLANA'])
def test_generate_into_existing_group_by_id(logged, chain):
    target = logged.post('/api/wallets/groups', json={'name': '已有分组'}).json()['id']
    other = logged.post('/api/wallets/groups', json={'name': '其他分组'}).json()['id']
    response = logged.post('/api/wallets/generate', json={'chain': chain, 'count': 2, 'group_id': target})
    assert response.status_code == 200, response.text
    assert response.json()['group_id'] == target
    wallets = logged.get('/api/wallets?group_id=' + target).json()
    assert len(wallets) == 2 and all(w['chain'] == chain and w['name'].startswith('已有分组 #') for w in wallets)
    assert logged.get('/api/wallets?group_id=' + other).json() == []
    assert {g['id']: g['count'] for g in logged.get('/api/wallets/groups').json()} == {target: 2, other: 0}
    with Session() as db:
        for wallet in wallets:
            assert db.get(WalletGroupMember, (target, wallet['id']))
            assert db.get(WalletSecret, wallet['id'])


@pytest.mark.parametrize('destination,status', [
    ({'group_id': 'deleted-group'}, 404),
    ({'group_id': 'deleted-group', 'group_name': '意外新组'}, 422),
    ({'group_name': '   '}, 422),
])
def test_invalid_group_destination_does_not_generate_or_create_group(logged, monkeypatch, destination, status):
    def forbidden_generate(chain):
        pytest.fail('Invalid group must be rejected before any private key is generated')
    monkeypatch.setattr(vault, 'generate', forbidden_generate)
    response = logged.post('/api/wallets/generate', json={'count': 1, **destination})
    assert response.status_code == status
    assert logged.get('/api/wallets').json() == []
    assert logged.get('/api/wallets/groups').json() == []


def test_generate_without_destination_keeps_legacy_default(logged):
    response = logged.post('/api/wallets/generate', json={'count': 1})
    assert response.status_code == 200
    assert logged.get('/api/wallets/groups').json()[0]['name'] == '默认分组'


def test_new_group_name_trimmed_and_legacy_name_reuses_group(logged):
    first = logged.post('/api/wallets/generate', json={'count': 1, 'group_name': '  活动分组  '}).json()
    second = logged.post('/api/wallets/generate', json={'count': 1, 'group_name': '活动分组'}).json()
    assert first['group_id'] == second['group_id']
    groups = logged.get('/api/wallets/groups').json()
    assert len(groups) == 1 and groups[0]['name'] == '活动分组' and groups[0]['count'] == 2


@pytest.mark.parametrize('file_import', [False, True])
def test_import_supports_existing_group_selection(logged, file_import):
    target = logged.post('/api/wallets/groups', json={'name': '导入目标'}).json()['id']
    _, key = vault.generate('EVM')
    if file_import:
        response = logged.post('/api/wallets/import-file',
            files={'file': ('wallets.json', json.dumps([{'private_key': key}]).encode())},
            data={'chain': 'EVM', 'group_id': target, 'mapping_json': '{"private_key":"private_key"}'})
    else:
        response = logged.post('/api/wallets/import', json={'chain': 'EVM', 'private_keys': [key], 'group_id': target})
    assert response.status_code == 200, response.text
    assert response.json()['created'] == 1 and response.json()['group_id'] == target
    assert len(logged.get('/api/wallets?group_id=' + target).json()) == 1
    assert len(logged.get('/api/wallets/groups').json()) == 1
    assert key not in response.text


@pytest.mark.parametrize('file_import', [False, True])
def test_import_requires_explicit_destination(logged, file_import):
    _, key = vault.generate('EVM')
    if file_import:
        response = logged.post('/api/wallets/import-file',
            files={'file': ('wallets.json', json.dumps([{'private_key': key}]).encode())},
            data={'chain': 'EVM', 'mapping_json': '{"private_key":"private_key"}'})
    else:
        response = logged.post('/api/wallets/import', json={'chain': 'EVM', 'private_keys': [key]})
    assert response.status_code == 422 and key not in response.text
    assert logged.get('/api/wallets/groups').json() == []
    assert logged.get('/api/wallets').json() == []
