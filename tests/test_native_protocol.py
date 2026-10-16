"""原生运行协议与运行审核：旧协议可读、摘要绑定、入口校验与能力声明。"""
import copy
import pytest
from fastapi import HTTPException
from backend.config import settings
from backend.db import Session
from backend.models import ScriptFeature, ScriptVersion
from backend.feature_reviews import require_native_approvable, runtime_digest_for
from backend.runtime_builds import dependency_digest, dependency_sources, environment_key, prepare
from packages.feature_protocol import CAPABILITY_REASONS, FeatureDefinition, Parameter, WalletInput
from packages.runtime_protocol import NativeRuntimeSpec, runtime_digest
from adapters.native import draft_spec
from tests.test_analysis_workflow import approve, draft, save

WALLET_BINDING = {'format': 'CSV', 'target_path': 'inputs/wallets.csv',
                  'fields': {'address': 'identity', 'private_key': 'credential'}, 'chain': 'EVM'}


def runtime_body(feature, **overrides):
    definition = feature['definition']
    body = {'entrypoint': definition['entrypoint'], 'workdir': '', 'callable': 'run_feature',
            'parameters': copy.deepcopy(definition['parameters']),
            'configuration_bindings': [{'parameter': parameter['name'], 'destination': 'environment',
                                        'target': 'AIRDROP_PARAM_' + parameter['name'].upper()}
                                       for parameter in definition['parameters']],
            'wallet_binding': copy.deepcopy(WALLET_BINDING),
            'capabilities': ['selected_wallet_private_keys'], 'result_mode': 'logs_only', 'unresolved': []}
    body.update(overrides)
    return body


def put_runtime(logged, feature_id, body):
    return logged.put('/api/script-features/' + feature_id + '/runtime', json={'runtime': body, 'confirmed': True})


def review_runtime(logged, feature_id, sha):
    return logged.post('/api/script-features/' + feature_id + '/runtime-review',
                       json={'reviewed_sha256': sha, 'confirmed': True})


async def test_features_without_runtime_definition_stay_readable(logged):
    _, _, data = await draft(logged)
    for feature in data['features']:
        view = logged.get('/api/script-features/' + feature['id'] + '/runtime').json()
        assert view['runtime_status'] == 'DRAFT' and view['approved_sha256'] is None
        assert isinstance(view['readiness'], list)
        if view['runtime_spec'] is None:
            assert view['readiness'] == ['尚未生成运行定义']
        else:
            # 规则草稿只复用静态分析能确认的结构，其余留给人工。
            assert view['runtime_spec']['protocol'] == 'airdrop-native-runtime-v1'
            assert view['runtime_spec']['entrypoint'] == feature['definition']['entrypoint']
        assert view['execution_enabled'] is False


async def test_unknown_entrypoint_and_escaping_paths_are_rejected(logged):
    _, _, data = await draft(logged)
    feature = next(item for item in data['features'] if item['definition']['entrypoint'])
    missing = put_runtime(logged, feature['id'], runtime_body(feature, entrypoint='airdrop_generated/absent.py'))
    assert missing.status_code == 422 and '入口文件不存在' in missing.json()['detail']
    escaping = put_runtime(logged, feature['id'], runtime_body(feature, entrypoint='../evil.py'))
    assert escaping.status_code == 422
    javascript = put_runtime(logged, feature['id'], runtime_body(feature, entrypoint='airdrop_generated/main.js'))
    assert javascript.status_code == 422 and '入口必须是' in javascript.json()['detail']


async def test_runtime_edit_invalidates_previous_approval(logged):
    _, _, data = await draft(logged)
    feature = next(item for item in data['features'] if item['definition']['entrypoint'])
    first = put_runtime(logged, feature['id'], runtime_body(feature))
    assert first.status_code == 200, first.text
    digest = first.json()['review_sha256']
    assert review_runtime(logged, feature['id'], digest).status_code == 200
    approved = logged.get('/api/script-features/' + feature['id'] + '/runtime').json()
    assert approved['runtime_status'] == 'APPROVED'
    # 依赖锁定摘要属于审核摘要的一部分；改变它必须让旧审核失效。
    changed = put_runtime(logged, feature['id'], runtime_body(feature, dependency_lock_sha256='a' * 64))
    assert changed.status_code == 200
    assert changed.json()['runtime_status'] == 'DRAFT' and changed.json()['approved_sha256'] is None
    assert changed.json()['review_sha256'] != digest
    stale = review_runtime(logged, feature['id'], digest)
    assert stale.status_code == 409


async def test_unresolved_and_missing_delivery_block_runtime_approval(logged):
    _, _, data = await draft(logged)
    feature = next(item for item in data['features'] if item['definition']['entrypoint'])
    pending = put_runtime(logged, feature['id'], runtime_body(feature, unresolved=['需要人工确认代理来源']))
    assert pending.status_code == 200
    blocked = review_runtime(logged, feature['id'], pending.json()['review_sha256'])
    assert blocked.status_code == 409 and '未解决项' in blocked.json()['detail']
    feature_parameters = feature['definition']['parameters']
    if feature_parameters:
        delivery = put_runtime(logged, feature['id'], runtime_body(feature, configuration_bindings=[], callable=None))
        assert delivery.status_code == 200
        assert any('没有交付位置' in note for note in delivery.json()['readiness'])
        assert review_runtime(logged, feature['id'], delivery.json()['review_sha256']).status_code == 409


async def test_saved_version_keeps_runtime_definition_and_approval(logged):
    _, _, data = await draft(logged)
    feature = next(item for item in data['features'] if item['definition']['entrypoint'])
    created = put_runtime(logged, feature['id'], runtime_body(feature))
    assert created.status_code == 200, created.text
    digest = created.json()['review_sha256']
    assert review_runtime(logged, feature['id'], digest).status_code == 200
    for item in data['features']:
        assert approve(logged, item).status_code == 200
    saved = save(logged, data)
    assert saved.status_code == 200, saved.text
    with Session() as db:
        version = db.get(ScriptVersion, saved.json()['id'])
        stored = next(row for row in db.query(ScriptFeature).filter_by(version_id=version.id)
                      if row.feature_key == feature['definition']['feature_id'])
        assert stored.runtime_spec['entrypoint'] == feature['definition']['entrypoint']
        assert stored.runtime_status == 'APPROVED' and stored.runtime_review_sha256
        spec = NativeRuntimeSpec.model_validate(stored.runtime_spec)
        from backend.feature_reviews import feature_files
        assert stored.runtime_review_sha256 == runtime_digest_for(stored, spec, feature_files(db, stored))


def test_high_risk_feature_needs_declared_capability():
    definition = FeatureDefinition.model_validate({
        'feature_id': 'checkin', 'name': '签到', 'description': '', 'entrypoint_candidate': 'main.py',
        'source_functions': ['main.py:checkin'], 'entrypoint': 'main.py', 'risk_level': 'high', 'confidence': 0.6,
        'automation_status': 'REVIEW_REQUIRED',
        'evidence': [{'file': 'main.py', 'line': 1, 'end_line': 1, 'type': 'FUNCTION', 'confidence': 0.6,
                      'feasibility': 'REVIEW_REQUIRED', 'manual_required': False}]})

    class Row:
        runtime_spec = None
        id = 'feature'

    with pytest.raises(HTTPException) as blocked:
        require_native_approvable(Row(), definition)
    assert '能力' in blocked.value.detail
    Row.runtime_spec = NativeRuntimeSpec(entrypoint='main.py', capabilities=['external_network']).model_dump(mode='json')
    require_native_approvable(Row(), definition)
    structural = definition.model_copy(update={
        'automation_status': 'MANUAL_REQUIRED',
        'blocking_reasons': [CAPABILITY_REASONS[0], '调用链仍包含交互'],
    })
    with pytest.raises(HTTPException) as unresolved:
        require_native_approvable(Row(), structural)
    assert '结构问题' in unresolved.value.detail


def test_capability_only_blocker_can_continue_after_runtime_declaration():
    definition = FeatureDefinition.model_validate({
        'feature_id': 'checkin', 'name': '签到', 'description': '', 'entrypoint_candidate': 'main.py',
        'source_functions': ['main.py:checkin'], 'entrypoint': 'main.py', 'risk_level': 'high', 'confidence': 0.6,
        'automation_status': 'MANUAL_REQUIRED', 'blocking_reasons': [CAPABILITY_REASONS[0]],
        'evidence': [{'file': 'main.py', 'line': 1, 'end_line': 1, 'type': 'FUNCTION', 'confidence': 0.6,
                      'feasibility': 'REVIEW_REQUIRED', 'manual_required': False}]})

    class Row:
        runtime_spec = None

    with pytest.raises(HTTPException) as missing:
        require_native_approvable(Row(), definition)
    assert '能力' in missing.value.detail
    Row.runtime_spec = NativeRuntimeSpec(entrypoint='main.py', capabilities=['selected_wallet_private_keys']).model_dump(mode='json')
    require_native_approvable(Row(), definition)


def test_draft_spec_marks_unresolved_and_rejects_unusable_skeleton():
    definition = FeatureDefinition.model_validate({
        'feature_id': 'register', 'name': '注册', 'description': '', 'entrypoint_candidate': 'main.py',
        'source_functions': ['main.py:register'], 'entrypoint': 'main.py', 'confidence': 0.5, 'risk_level': 'medium',
        'wallet_input': WalletInput(mode='CSV', source='wallets.csv',
                                    fields={'address': 'identity', 'private_key': 'credential'}).model_dump(),
        'parameters': [Parameter(name='count', type='integer', source='function').model_dump()],
        'evidence': [{'file': 'main.py', 'line': 1, 'end_line': 1, 'type': 'FUNCTION', 'confidence': 0.5,
                      'feasibility': 'REVIEW_REQUIRED', 'manual_required': False}]})
    report = {'file_tree': ['main.py'], 'findings': [{'type': 'NETWORK'}], 'excluded_data_files': [{'file': 'state.csv'}]}
    spec = draft_spec(definition.model_dump(), report, None, {'wallet_chain': 'EVM'})
    assert spec['capabilities'] == ['external_network', 'selected_wallet_private_keys']
    assert spec['state_bindings'][0]['path'] == 'state.csv'
    assert any('参数如何传给脚本' in note for note in spec['unresolved'])
    hint = NativeRuntimeSpec(entrypoint='main.py').model_dump(mode='json')
    assert draft_spec(definition.model_dump(), report, hint, {}) == hint
    # 非 Python 入口无法形成可运行定义，只能走人工适配。
    broken = definition.model_copy(update={'entrypoint': 'main.js'})
    assert draft_spec(broken.model_dump(), report, None, {}) is None


def test_environment_key_depends_on_resolved_declaration_and_arch(monkeypatch):
    monkeypatch.setattr(settings, 'native_execution_enabled', False)
    spec = NativeRuntimeSpec(entrypoint='main.py')
    files = {'main.py': b'pass', 'requirements.txt': b'requests==2.32.0\n'}
    sources = dependency_sources(spec, files)
    assert sources == ['requirements.txt']
    digest = dependency_digest(sources, files)
    key = environment_key('python:3.12-slim', '3.12', 'x86_64', digest)
    assert key == environment_key('python:3.12-slim', '3.12', 'x86_64', digest)
    assert key != environment_key('python:3.12-slim', '3.12', 'aarch64', digest)
    changed = dependency_digest(sources, {'main.py': b'pass', 'requirements.txt': b'requests==2.33.0\n'})
    assert changed != digest
    # 未声明依赖文件时按惯例识别，且不猜测不存在的内容。
    assert dependency_sources(spec, {'main.py': b'pass'}) == []


def test_prepare_refuses_when_native_execution_is_disabled(logged, monkeypatch):
    monkeypatch.setattr(settings, 'native_execution_enabled', False)
    # 通过接口确认能力开关与构建入口的失败信息一致。
    capabilities = logged.get('/api/runtime/capabilities').json()
    assert capabilities['native_execution_enabled'] is False and capabilities['execution_available'] is False
    assert capabilities['controller']['reason']
    with Session() as db:
        feature = ScriptFeature(runtime_spec=None, feature_key='fixture')
        with pytest.raises(HTTPException) as blocked:
            prepare(db, feature, NativeRuntimeSpec(entrypoint='main.py'), {'main.py': b'pass'}, 'admin')
    assert blocked.value.status_code == 409 and '不可用' in blocked.value.detail


def test_protocol_digest_covers_code_definition_and_runtime():
    spec = NativeRuntimeSpec(entrypoint='main.py')
    base = runtime_digest(spec.model_dump(mode='json'), code_sha256='a' * 64, definition_digest='b' * 64)
    assert base != runtime_digest(spec.model_dump(mode='json'), code_sha256='c' * 64, definition_digest='b' * 64)
    assert base != runtime_digest(spec.model_dump(mode='json'), code_sha256='a' * 64, definition_digest='d' * 64)
    with_lock = spec.model_copy(update={'dependency_lock_sha256': 'e' * 64})
    assert base != runtime_digest(with_lock.model_dump(mode='json'), code_sha256='a' * 64, definition_digest='b' * 64)
