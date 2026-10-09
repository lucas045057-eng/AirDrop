import pytest
from sqlalchemy import event, select
from backend import deletion_api
from backend.db import Session, engine
from backend.models import AuditLog, Wallet, WalletGroupMember, WalletSecret


@pytest.fixture
def wallets(logged):
    group = logged.post('/api/wallets/generate', json={'count': 4, 'group_name': 'bulk-delete-fixture'}).json()['group_id']
    records = logged.get('/api/wallets?group_id=' + group).json()
    return group, [w['id'] for w in records]


def batch(client, identities, confirmed=True):
    return client.post('/api/wallets/bulk-delete', json={'wallet_ids': identities, 'confirmed': confirmed})


def test_batch_partial_delete_preserves_referenced_wallet_and_key(logged, wallets):
    group, ids = wallets
    project = logged.post('/api/projects', json={'name': 'bulk-protection'}).json()['id']
    assert logged.put('/api/projects/' + project + '/wallets', json={'wallet_ids': [ids[1]]}).status_code == 200
    with Session() as db:protected_key = db.get(WalletSecret, ids[1]).ciphertext
    result = batch(logged, ids)
    assert result.status_code == 200
    body = result.json()
    assert body['requested_count'] == 4 and body['deleted_count'] == 3
    assert set(body['deleted_ids']) == set(ids) - {ids[1]}
    assert body['blocked'][0]['id'] == ids[1]
    assert body['blocked'][0]['references'] == [{'label': '项目绑定', 'count': 1}]
    assert 'private_key' not in result.text and protected_key not in result.text
    with Session() as db:
        assert db.get(Wallet, ids[1]) and db.get(WalletSecret, ids[1]).ciphertext == protected_key
        assert all(db.get(Wallet, id) is None and db.get(WalletSecret, id) is None for id in body['deleted_ids'])
        assert list(db.scalars(select(WalletGroupMember.wallet_id).where(WalletGroupMember.group_id == group))) == [ids[1]]
        audit = db.scalar(select(AuditLog).where(AuditLog.action == 'wallet.bulk_delete'))
        assert audit.detail['deleted_ids'] == body['deleted_ids'] and protected_key not in str(audit.detail)


def test_batch_deduplicates_and_reports_missing_without_touching_unselected(logged, wallets):
    group, ids = wallets
    missing = 'f' * 32
    result = batch(logged, [ids[0], ids[0], missing]).json()
    assert result['requested_count'] == 2 and result['deleted_count'] == 1
    assert result['missing_ids'] == [missing]
    retry = batch(logged, [ids[0], missing]).json()
    assert retry['deleted_count'] == 0 and set(retry['missing_ids']) == {ids[0], missing}
    with Session() as db:assert all(db.get(Wallet, id) and db.get(WalletSecret, id) for id in ids[1:])


@pytest.mark.parametrize('payload', [
    {'wallet_ids': [], 'confirmed': True},
    {'wallet_ids': ['a'], 'confirmed': False},
    {'wallet_ids': ['a']},
    {'wallet_ids': ['a'] * 1001, 'confirmed': True},
    {'wallet_ids': ['a' * 33], 'confirmed': True},
])
def test_invalid_batch_never_deletes(logged, wallets, payload):
    assert logged.post('/api/wallets/bulk-delete', json=payload).status_code == 422
    with Session() as db:assert all(db.get(Wallet, id) for id in wallets[1])


def test_batch_requires_session_and_csrf(logged, wallets):
    ids = wallets[1]
    csrf = logged.headers.pop('x-csrf-token')
    assert batch(logged, ids).status_code == 403
    logged.headers['x-csrf-token'] = csrf
    logged.cookies.clear()
    assert batch(logged, ids).status_code == 401
    with Session() as db:assert all(db.get(WalletSecret, id) for id in ids)


def test_batch_rolls_back_keys_and_members_if_audit_fails(logged, wallets, monkeypatch):
    def failure(**kwargs):raise RuntimeError('injected audit failure')
    monkeypatch.setattr(deletion_api, 'AuditLog', failure)
    with pytest.raises(RuntimeError, match='injected audit failure'):batch(logged, wallets[1])
    with Session() as db:
        for id in wallets[1]:
            assert db.get(Wallet, id) and db.get(WalletSecret, id)
            assert db.get(WalletGroupMember, (wallets[0], id))


def test_full_size_batch_has_bounded_queries_and_deletes_only_real_ids(logged, wallets):
    identities = wallets[1] + [f'{i:032x}' for i in range(996)]
    queries = []
    def record(conn, cursor, statement, parameters, context, executemany):
        if statement.lstrip().upper().startswith('SELECT'):queries.append(statement)
    event.listen(engine, 'before_cursor_execute', record)
    try:result = batch(logged, identities)
    finally:event.remove(engine, 'before_cursor_execute', record)
    assert result.status_code == 200
    assert result.json()['requested_count'] == 1000
    assert result.json()['deleted_count'] == 4 and len(result.json()['missing_ids']) == 996
    assert len(queries) <= 12


@pytest.mark.parametrize('reference', ['result', 'points', 'lock', 'transaction'])
def test_batch_protects_each_historical_or_control_reference(logged, wallets, reference):
    from backend.models import (Artifact, Execution, PointSnapshot, Project, ScriptPackage,
        ScriptVersion, Task, TransactionIntent, WalletLock, WalletTaskResult)
    protected = wallets[1][0]
    with Session() as db:
        wallet = db.get(Wallet, protected)
        project = Project(name='reference-fixture')
        package = ScriptPackage(name='reference-fixture')
        artifact = Artifact(path='/unused/fixture.enc', sha256='0'*64, kind='fixture')
        db.add_all([project, package, artifact]);db.flush()
        version = ScriptVersion(package_id=package.id, version=1, original_artifact_id=artifact.id,
                                source_artifact_id=artifact.id, analysis={})
        db.add(version);db.flush()
        task = Task(project_id=project.id, script_version_id=version.id, name='reference-fixture')
        db.add(task);db.flush()
        execution = Execution(task_id=task.id, project_id=project.id, script_version_id=version.id,
                              period='test:fixture', is_test=True, status='SUCCESS')
        db.add(execution);db.flush()
        row = {'result': lambda: WalletTaskResult(execution_id=execution.id, wallet_id=protected, address=wallet.address, chain=wallet.chain),
               'points': lambda: PointSnapshot(project_id=project.id, wallet_id=protected, execution_id=execution.id, points=1),
               'lock': lambda: WalletLock(wallet_id=protected, execution_id=execution.id),
               'transaction': lambda: TransactionIntent(idempotency_key='fixture', project_id=project.id, wallet_id=protected,
                   payload_hash='0'*64, chain='EVM', nonce=0, state='CONFIRMED', mock_hash='fixture')}
        db.add(row[reference]());db.commit()
    response = batch(logged, wallets[1])
    assert response.status_code == 200 and response.json()['deleted_count'] == 3
    assert response.json()['blocked'][0]['id'] == protected
    with Session() as db:assert db.get(Wallet, protected) and db.get(WalletSecret, protected)
