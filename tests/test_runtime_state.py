"""状态空间投影/合并、账号白名单、代次保护与运行输入装配。"""
import csv
import io
import json
import pytest
from fastapi import HTTPException
from sqlalchemy import select
from backend.db import Session
from backend.models import Artifact, Execution, Project, ProjectScriptState, ScriptPackage, ScriptVersion, Task, Wallet, WalletSecret, WalletTaskResult, uid
from backend.storage import encode_files, write_blob
from backend.vault import encrypt_wallet, identity
from backend import runtime_inputs, runtime_state
from packages.runtime_protocol import NativeRuntimeSpec

KEYS = ['0x' + f'{index:02x}' * 32 for index in range(1, 4)]


def wallet_identity(index):
    """链上身份口径：地址是派生地址，私钥是钱包库里的原样字符串。"""
    return identity('EVM', KEYS[index])


def csv_rows(rows, columns=('address', 'points')):
    output = io.StringIO(newline='')
    writer = csv.DictWriter(output, fieldnames=list(columns))
    writer.writeheader()
    writer.writerows(rows)
    return output.getvalue().encode('utf-8-sig')


def make_wallet(db, index):
    address, private_key = wallet_identity(index)
    wallet = Wallet(chain='EVM', address=address, name=f'wallet-{index}', is_test=False)
    db.add(wallet)
    db.flush()
    db.add(WalletSecret(wallet_id=wallet.id, ciphertext=encrypt_wallet(wallet, private_key)))
    return wallet


def fixture(selected=(0, 1), state_rows=None):
    """建立一个项目/脚本/任务/执行与三个钱包，其中只有部分进入本次运行。

    state_rows 里用整数下标表示账号，写入状态文件前替换成对应钱包地址。
    """
    with Session.begin() as db:
        project = Project(name='state-project-' + uid()[:6])
        package = ScriptPackage(name='state-package-' + uid()[:6])
        db.add_all([project, package])
        db.flush()
        wallets = [make_wallet(db, index) for index in range(3)]
        addresses = [wallet.address for wallet in wallets]
        path, digest = write_blob(encode_files({'main.py': b'print(1)\n'}))
        artifact = Artifact(path=path, sha256=digest, kind='sources')
        db.add(artifact)
        db.flush()
        version = ScriptVersion(package_id=package.id, version=1, original_artifact_id=artifact.id,
                                source_artifact_id=artifact.id, analysis={}, status='APPROVED')
        db.add(version)
        db.flush()
        spec = {'protocol': 'airdrop-native-runtime-v1', 'entrypoint': 'main.py',
                'wallet_binding': {'format': 'CSV', 'target_path': 'inputs/wallets.csv', 'chain': 'EVM',
                                   'fields': {'address': 'identity', 'private_key': 'credential'}},
                'state_bindings': [{'path': 'state.csv', 'format': 'CSV', 'account_key': 'address'}],
                'capabilities': ['selected_wallet_private_keys'], 'parameters': []}
        task = Task(project_id=project.id, script_version_id=version.id, name='state-task',
                    execution_backend='native',
                    feature_config_snapshot={'runtime': spec, 'parameters': {}})
        db.add(task)
        db.flush()
        execution = Execution(task_id=task.id, project_id=project.id, script_version_id=version.id,
                              period='manual:' + uid()[:8], execution_backend='native', runtime_snapshot={'runtime': spec, 'parameters': {}})
        db.add(execution)
        db.flush()
        for index in selected:
            wallet = wallets[index]
            db.add(WalletTaskResult(execution_id=execution.id, wallet_id=wallet.id, address=wallet.address,
                                    chain=wallet.chain, name=wallet.name))
        if state_rows is not None:
            rows = [{name: addresses[value] if isinstance(value, int) else value for name, value in row.items()}
                    for row in state_rows]
            state = ProjectScriptState(project_id=project.id, package_id=package.id, state_schema_version=1, generation=4)
            db.add(state)
            db.flush()
            path, digest = write_blob(encode_files({'state.csv': csv_rows(rows)}))
            artifact = Artifact(path=path, sha256=digest, kind='runtime-state')
            db.add(artifact)
            db.flush()
            state.artifact_id = artifact.id
            state.artifact_sha256 = digest
        return {'project': project.id, 'package': package.id, 'version': version.id, 'task': task.id,
                'execution': execution.id, 'wallets': [wallet.id for wallet in wallets],
                'addresses': [wallet.address for wallet in wallets],
                'keys': [wallet_identity(index)[1] for index in range(3)], 'spec': spec}


def spec_of(fixture_data, **overrides):
    body = {**fixture_data['spec'], **overrides}
    return NativeRuntimeSpec.model_validate(body)


def test_projection_and_merge_keep_unselected_accounts():
    data = fixture(state_rows=[{'address': 0, 'points': '1'}, {'address': 1, 'points': '2'},
                               {'address': 2, 'points': '3'}])
    addresses = data['addresses']
    with Session.begin() as db:
        execution = db.get(Execution, data['execution'])
        spec = spec_of(data)
        keys = runtime_inputs.state_keys(db, execution, spec)
        projected = runtime_state.project(db, execution, spec, keys)
        rows = list(csv.DictReader(io.StringIO(projected['state.csv'].decode('utf-8-sig'))))
        # 只投影本次选中的两个账号，第三个账号不交给脚本。
        assert [row['address'] for row in rows] == addresses[:2]
        produced = csv_rows([{'address': addresses[0], 'points': '10'}, {'address': addresses[1], 'points': '20'}])
        merged, stats = runtime_state.merge(db, execution, spec, {'state.csv': produced}, keys)
        result = list(csv.DictReader(io.StringIO(merged['state.csv'].decode('utf-8-sig'))))
        assert {row['address']: row['points'] for row in result} == {addresses[0]: '10', addresses[1]: '20',
                                                                    addresses[2]: '3'}
        assert stats['state.csv']['kept'] == 1
        outcome = runtime_state.commit(db, execution, spec, merged, expected_generation=4)
        assert outcome == {'committed': True, 'generation': 5}
        assert execution.state_generation == 5 and execution.state_status == 'COMMITTED'


def test_unknown_accounts_duplicates_and_undeclared_files_are_rejected():
    data = fixture(state_rows=[{'address': 0, 'points': '1'}])
    with Session.begin() as db:
        execution = db.get(Execution, data['execution'])
        spec = spec_of(data)
        keys = runtime_inputs.state_keys(db, execution, spec)
        with pytest.raises(ValueError) as unknown:
            runtime_state.merge(db, execution, spec,
                                {'state.csv': csv_rows([{'address': data['addresses'][2], 'points': '9'}])}, keys)
        assert '不在本次选择' in str(unknown.value)
        with pytest.raises(ValueError) as duplicate:
            runtime_state.merge(db, execution, spec,
                                {'state.csv': csv_rows([{'address': data['addresses'][0], 'points': '1'},
                                                        {'address': data['addresses'][0], 'points': '2'}])}, keys)
        assert '重复账号' in str(duplicate.value)
        with pytest.raises(ValueError) as undeclared:
            runtime_state.merge(db, execution, spec, {'other.csv': csv_rows([])}, keys)
        assert '未声明' in str(undeclared.value)
        with pytest.raises(ValueError) as outside:
            runtime_state.merge(db, execution, spec, {'state.csv': csv_rows([{'address': '', 'points': '1'}])}, keys)
        assert '缺少账号标识' in str(outside.value)


def test_generation_conflict_preserves_newer_state():
    data = fixture(state_rows=[{'address': 0, 'points': '1'}])
    with Session.begin() as db:
        execution = db.get(Execution, data['execution'])
        spec = spec_of(data)
        outcome = runtime_state.commit(db, execution, spec,
                                       {'state.csv': csv_rows([{'address': data['addresses'][0], 'points': '5'}])},
                                       expected_generation=3)
        assert outcome['committed'] is False and outcome['generation'] == 4
        assert execution.state_status == 'PENDING_REVIEW'
        state = db.scalars(select(ProjectScriptState)).one()
        assert state.generation == 4


def test_state_lock_blocks_a_second_writer():
    data = fixture()
    with Session.begin() as db:
        execution = db.get(Execution, data['execution'])
        spec = spec_of(data)
        assert runtime_state.lock(db, execution, spec) == 0
        other = Execution(task_id=data['task'], project_id=data['project'], script_version_id=data['version'],
                          period='manual:' + uid()[:8], execution_backend='native', status='RUNNING')
        db.add(other)
        db.flush()
        with pytest.raises(HTTPException) as blocked:
            runtime_state.lock(db, other, spec)
        assert '正在被另一次运行写入' in blocked.value.detail


def test_build_stream_delivers_only_selected_wallet_secrets():
    data = fixture()
    with Session.begin() as db:
        execution = db.get(Execution, data['execution'])
        stream = runtime_inputs.build_stream(db, execution)
        wallet_rows = list(csv.DictReader(io.StringIO(stream['files']['inputs/wallets.csv'].decode('utf-8-sig'))))
        # 顺序按地址稳定排序，只包含本次选中的两个钱包。
        expected = sorted(zip(data['addresses'][:2], data['keys'][:2], data['wallets'][:2]))
        assert [row['address'] for row in wallet_rows] == [item[0] for item in expected]
        assert [row['private_key'] for row in wallet_rows] == [item[1] for item in expected]
        assert stream['wallet_ids'] == [item[2] for item in expected]
        assert stream['wallet_addresses'] == [item[0] for item in expected]
        assert data['keys'][2] not in json.dumps(stream['files'], default=str)
        assert data['addresses'][2] not in json.dumps(stream['files'], default=str)
        assert len(stream['input_sha256']) == 64


def test_stream_rejects_undeclared_parameters():
    data = fixture()
    with Session.begin() as db:
        execution = db.get(Execution, data['execution'])
        execution.runtime_snapshot = {**execution.runtime_snapshot, 'parameters': {'unknown': 1}}
        with pytest.raises(HTTPException) as blocked:
            runtime_inputs.build_stream(db, execution)
        assert '不在已审核定义中' in blocked.value.detail
