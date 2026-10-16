"""项目账号资料导入：遮蔽预览、冲突逐项确认、幂等与修订号保护。"""
import csv
import io
import time
import pytest
from sqlalchemy import select
from backend import account_imports
from backend.db import Session
from backend.models import AccountImportDraft, ProjectAccount, Wallet

KEY_ONE = '0x' + '01' * 32
KEY_TWO = '0x' + '02' * 32


def project(logged, name='account-project'):
    response = logged.post('/api/projects', json={'name': name})
    assert response.status_code == 200, response.text
    return response.json()


def package(logged, name='account-package'):
    from backend.db import Session
    from backend.models import ScriptPackage
    with Session.begin() as db:
        row = ScriptPackage(name=name)
        db.add(row)
        db.flush()
        return row.id


def csv_bytes(rows, columns):
    output = io.StringIO(newline='')
    writer = csv.DictWriter(output, fieldnames=columns)
    writer.writeheader()
    writer.writerows(rows)
    return output.getvalue().encode('utf-8-sig')


def preview(logged, project_id, package_id, rows, columns, mapping, chain='EVM', filename='accounts.csv'):
    return logged.post('/api/projects/' + project_id + '/account-imports',
                       data={'package_id': package_id, 'chain': chain, 'field_mapping': __import__('json').dumps(mapping)},
                       files={'file': (filename, csv_bytes(rows, columns))})


def commit(logged, draft, decisions=None, sha=None):
    return logged.post('/api/account-imports/' + draft['id'] + '/commit',
                       json={'preview_sha256': sha or draft['preview_sha256'], 'decisions': decisions or {}, 'confirmed': True})


def test_preview_masks_secrets_and_commit_is_idempotent(logged):
    owner = project(logged)
    pack = package(logged)
    rows = [{'address': '', 'private_key': KEY_ONE, 'invite_code': 'INV-A', 'proxy': 'http://user:pass@proxy-a:8080'},
            {'address': '', 'private_key': KEY_TWO, 'invite_code': 'INV-B', 'proxy': ''}]
    response = preview(logged, owner['id'], pack, rows, ['address', 'private_key', 'invite_code', 'proxy'],
                       {'address': 'address', 'private_key': 'private_key', 'invite_code': 'invite_code', 'proxy': 'proxy'})
    assert response.status_code == 200, response.text
    draft = response.json()
    assert draft['preview']['counts'] == {'new': 2, 'reuse': 0, 'conflict': 0, 'error': 0}
    entry = draft['preview']['entries'][0]
    assert 'private_key' not in entry['values'] and entry['has_private_key'] is True
    assert entry['values']['proxy'] != 'http://user:pass@proxy-a:8080' and '*' in entry['values']['proxy']
    first = commit(logged, draft)
    assert first.status_code == 200, first.text
    assert first.json()['created'] == 2 and first.json()['wallets_created'] == 2
    assert commit(logged, draft).json() == first.json()
    accounts = logged.get('/api/projects/' + owner['id'] + '/accounts?package_id=' + pack).json()
    assert len(accounts) == 2
    stored = next(row for row in accounts if row['public_config'].get('invite_code') == 'INV-A')
    assert 'proxy' in stored['secured_config'] and '*' in stored['secured_config']['proxy']
    assert 'pass' not in str(stored)
    with Session() as db:
        assert len(list(db.scalars(select(Wallet)))) == 2
        assert len(list(db.scalars(select(ProjectAccount)))) == 2


def test_credential_like_custom_fields_are_masked_and_encrypted(logged):
    owner = project(logged, 'credential-fields-project')
    pack = package(logged, 'credential-fields-package')
    values = {'address': '', 'private_key': KEY_ONE, 'cookie': 'cookie-value-123',
              'auth_token': 'auth-token-value-456', 'mnemonic': 'mnemonic-value-789',
              'accessKey': 'access-key-value-abc', 'author': 'public-author'}
    columns = list(values)
    draft = preview(logged, owner['id'], pack, [values], columns,
                    {name: name for name in columns}).json()

    entry = draft['preview']['entries'][0]
    for name in ('cookie', 'auth_token', 'mnemonic', 'accessKey'):
        assert entry['values'][name] != values[name] and '*' in entry['values'][name]
    assert entry['values']['author'] == 'public-author'

    assert commit(logged, draft).status_code == 200
    account = logged.get('/api/projects/' + owner['id'] + '/accounts?package_id=' + pack).json()[0]
    assert account['public_config'] == {'author': 'public-author'}
    assert set(account['secured_config']) == {'private_key', 'cookie', 'auth_token', 'mnemonic', 'accessKey'}
    leaked_fields = [name for name, value in values.items() if value and value != 'public-author' and value in str(account)]
    assert not leaked_fields, f'credential fields appeared unmasked: {leaked_fields}'


def test_csv_row_limit_stops_reading_after_first_excess_row(monkeypatch):
    consumed = 0

    class Rows:
        def __iter__(self):
            return self

        def __next__(self):
            nonlocal consumed
            consumed += 1
            if consumed > account_imports.MAX_ROWS + 1:
                raise AssertionError('reader consumed rows after the configured limit')
            return {'address': 'fixture'}

    monkeypatch.setattr(account_imports.csv, 'DictReader', lambda _: Rows())
    with pytest.raises(ValueError, match='1000'):
        account_imports.read_table('accounts.csv', b'address\nfixture\n')
    assert consumed == account_imports.MAX_ROWS + 1


def test_conflicts_require_explicit_choice(logged):
    owner = project(logged, 'conflict-project')
    pack = package(logged, 'conflict-package')
    columns = ['address', 'private_key', 'proxy']
    mapping = {'address': 'address', 'private_key': 'private_key', 'proxy': 'proxy'}
    first = preview(logged, owner['id'], pack, [{'address': '', 'private_key': KEY_ONE, 'proxy': 'http://proxy-one:1'}], columns, mapping).json()
    assert commit(logged, first).status_code == 200
    second = preview(logged, owner['id'], pack, [{'address': '', 'private_key': KEY_ONE, 'proxy': 'http://proxy-two:2'}], columns, mapping).json()
    entry = second['preview']['entries'][0]
    assert entry['group'] == 'conflict' and entry['conflicts'][0]['field'] == 'proxy'
    assert commit(logged, second).status_code == 409
    address = entry['address']
    skipped = commit(logged, second, {address: 'skip'})
    assert skipped.status_code == 200 and skipped.json()['skipped'] == 1
    # 已消费草稿重复确认返回同一结果，不会再次写入。
    assert commit(logged, second, {address: 'overwrite'}).json() == skipped.json()
    third = preview(logged, owner['id'], pack, [{'address': '', 'private_key': KEY_ONE, 'proxy': 'http://proxy-three:3'}], columns, mapping).json()
    result = commit(logged, third, {address: 'overwrite'})
    assert result.status_code == 200 and result.json()['updated'] == 1
    accounts = logged.get('/api/projects/' + owner['id'] + '/accounts?package_id=' + pack).json()
    # 新建为修订号 1；跳过的一次不写入，覆盖后为 2。
    assert accounts[0]['revision'] == 2


def test_stale_preview_cannot_overwrite_newer_revision(logged):
    owner = project(logged, 'revision-project')
    pack = package(logged, 'revision-package')
    columns = ['address', 'private_key', 'invite_code']
    mapping = {'address': 'address', 'private_key': 'private_key', 'invite_code': 'invite_code'}
    draft = preview(logged, owner['id'], pack, [{'address': '', 'private_key': KEY_ONE, 'invite_code': 'A1'}], columns, mapping).json()
    assert commit(logged, draft).status_code == 200
    pending = preview(logged, owner['id'], pack, [{'address': '', 'private_key': KEY_ONE, 'invite_code': 'A2'}], columns, mapping).json()
    with Session.begin() as db:
        account = db.scalars(select(ProjectAccount)).one()
        account.revision += 1
    response = commit(logged, pending, {pending['preview']['entries'][0]['address']: 'overwrite'})
    assert response.status_code == 409 and '预览之后被修改' in response.json()['detail']
    with Session() as db:
        assert db.scalars(select(ProjectAccount)).one().revision == 2


def test_two_new_account_previews_cannot_race_into_duplicate_wallet(logged):
    owner = project(logged, 'new-account-race-project')
    pack = package(logged, 'new-account-race-package')
    columns = ['address', 'private_key', 'invite_code']
    mapping = {'address': 'address', 'private_key': 'private_key', 'invite_code': 'invite_code'}
    rows_a = [{'address': '', 'private_key': KEY_ONE, 'invite_code': 'A'}]
    rows_b = [{'address': '', 'private_key': KEY_ONE, 'invite_code': 'B'}]
    first = preview(logged, owner['id'], pack, rows_a, columns, mapping).json()
    stale = preview(logged, owner['id'], pack, rows_b, columns, mapping).json()
    assert first['preview']['entries'][0]['revision'] is None
    assert stale['preview']['entries'][0]['revision'] is None

    assert commit(logged, first).status_code == 200
    response = commit(logged, stale, {stale['preview']['entries'][0]['address']: 'overwrite'})

    assert response.status_code == 409
    assert '预览之后被修改' in response.json()['detail']
    accounts = logged.get('/api/projects/' + owner['id'] + '/accounts?package_id=' + pack).json()
    assert len(accounts) == 1
    assert accounts[0]['public_config']['invite_code'] == 'A'
    with Session() as db:
        assert len(list(db.scalars(select(Wallet)))) == 1


def test_bad_rows_and_tampered_confirmations_are_rejected(logged):
    owner = project(logged, 'invalid-project')
    pack = package(logged, 'invalid-package')
    columns = ['address', 'private_key']
    mapping = {'address': 'address', 'private_key': 'private_key'}
    mismatched = preview(logged, owner['id'], pack, [{'address': '0x0000000000000000000000000000000000000000', 'private_key': KEY_ONE}], columns, mapping).json()
    assert mismatched['preview']['counts']['error'] == 1 and '不一致' in mismatched['preview']['errors'][0]['reason']
    duplicate = preview(logged, owner['id'], pack, [{'address': '', 'private_key': KEY_ONE}, {'address': '', 'private_key': KEY_ONE}], columns, mapping).json()
    assert duplicate['preview']['counts']['error'] == 1 and '重复' in duplicate['preview']['errors'][0]['reason']
    assert preview(logged, owner['id'], pack, [{'address': '', 'private_key': KEY_ONE}], columns, {'address': 'address'}).json()['preview']['counts']['error'] == 1
    assert preview(logged, owner['id'], pack, [{'address': '', 'private_key': KEY_ONE}], columns, {'address': 'absent'}).status_code == 422
    assert preview(logged, owner['id'], pack, [{'address': '', 'private_key': KEY_ONE}], columns, mapping, chain='DOGE').status_code == 422
    valid = preview(logged, owner['id'], pack, [{'address': '', 'private_key': KEY_ONE}], columns, mapping).json()
    assert commit(logged, valid, sha='0' * 64).status_code == 409
    with Session.begin() as db:
        draft = db.get(AccountImportDraft, valid['id'])
        draft.expires_at = time.time() - 1
    assert commit(logged, valid).status_code == 409


def test_manual_account_edit_checks_revision_and_hides_secrets(logged):
    owner = project(logged, 'edit-project')
    pack = package(logged, 'edit-package')
    columns = ['address', 'private_key', 'invite_code', 'proxy']
    mapping = {'address': 'address', 'private_key': 'private_key', 'invite_code': 'invite_code', 'proxy': 'proxy'}
    draft = preview(logged, owner['id'], pack, [{'address': '', 'private_key': KEY_ONE, 'invite_code': 'A1', 'proxy': 'http://proxy:1'}], columns, mapping).json()
    commit(logged, draft)
    account = logged.get('/api/projects/' + owner['id'] + '/accounts?package_id=' + pack).json()[0]
    updated = logged.put('/api/projects/' + owner['id'] + '/accounts/' + account['id'],
                         json={'revision': account['revision'], 'public_config': {'invite_code': 'A2'}, 'clear': [], 'confirmed': True})
    assert updated.status_code == 200 and updated.json()['public_config']['invite_code'] == 'A2'
    stale = logged.put('/api/projects/' + owner['id'] + '/accounts/' + account['id'],
                       json={'revision': account['revision'], 'public_config': {'invite_code': 'A3'}, 'clear': [], 'confirmed': True})
    assert stale.status_code == 409
    secret = logged.put('/api/projects/' + owner['id'] + '/accounts/' + account['id'],
                        json={'revision': updated.json()['revision'], 'public_config': {'proxy': 'http://other:9'}, 'clear': [], 'confirmed': True})
    assert secret.status_code == 422 and '敏感字段' in secret.json()['detail']
