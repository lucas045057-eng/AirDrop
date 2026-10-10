import asyncio
import copy
import json
import time
import pytest
from sqlalchemy import func, select
from backend.config import settings
from backend.db import Session
from backend.models import Artifact, ScriptAnalysisJob, ScriptFeature, ScriptTransformationDraft, ScriptVersion, SystemSetting
from backend.storage import cipher
from worker.adapter_worker import AdapterWorker
from adapters.discovery import scan
from adapters.transformation import rules_plan
from tests.test_feature_discovery import MENU
from tests.test_feature_transformation import FakeProvider


def upload(logged, source=MENU):
    response = logged.post('/api/scripts/upload', files={'file': ('menu.py', source.encode())})
    assert response.status_code == 200, response.text
    return response.json()


def start(logged, version, key, operation='DISCOVER', selected=None, ai=False):
    response = logged.post('/api/scripts/' + version['id'] + '/analysis-jobs', json={
        'request_key': key, 'operation': operation, 'selected_features': selected or [], 'use_ai': ai})
    assert response.status_code == 202, response.text
    return response.json()


def detail(logged, job):
    response = logged.get('/api/analysis-jobs/' + job['id'])
    assert response.status_code == 200, response.text
    return response.json()


async def discovery(logged, source=MENU):
    version = upload(logged, source)
    job = start(logged, version, 'discover')
    assert await AdapterWorker().run_once()
    result = detail(logged, job)
    assert result['status'] == 'REVIEW_REQUIRED', result
    return version, [feature['feature_id'] for feature in result['result']['features']]


def ai_config():
    with Session.begin() as db:
        db.add(SystemSetting(key='ai', value={'url': 'https://provider.example.invalid/v1', 'model': 'test-only-model',
            'encrypted_key': cipher().encrypt(b'fake-key-test-only').decode()}))


async def draft(logged):
    version, ids = await discovery(logged)
    job = start(logged, version, 'transform', 'TRANSFORM', ids)
    await AdapterWorker().run_once()
    result = detail(logged, job)
    assert result['status'] == 'REVIEW_REQUIRED', result
    data = logged.get('/api/transformation-drafts/' + result['result']['draft_id']).json()
    return version, job, data


def approve(logged, feature):
    return logged.post('/api/script-features/' + feature['id'] + '/review', json={
        'action': 'APPROVED', 'reviewed_sha256': feature['review_sha256'], 'confirmed': True})


def save(logged, data):
    return logged.post('/api/transformation-drafts/' + data['id'] + '/save', json={
        'feature_ids': [feature['id'] for feature in data['features']], 'reviewed_sha256': data['review_sha256'], 'confirmed': True})


async def test_complete_management_flow_preserves_original_and_saves_feature_tasks(logged, monkeypatch):
    monkeypatch.setattr(settings, 'execution_enabled', False)
    original, job, data = await draft(logged)
    assert len(data['features']) == 3
    diff = logged.get('/api/transformation-drafts/' + data['id'] + '/diff?limit=5').json()
    assert len(diff['lines']) == 5 and diff['more']
    assert save(logged, data).status_code == 409
    for feature in data['features']:
        response = approve(logged, feature)
        assert response.status_code == 200, response.text
    saved = save(logged, data)
    assert saved.status_code == 200, saved.text
    result = saved.json()
    assert result['version'] == 2 and result['status'] == 'APPROVED'
    assert save(logged, data).json()['id'] == result['id']
    features = logged.get('/api/scripts/' + result['id'] + '/features').json()
    assert len(features) == 3 and all(f['version_id'] == result['id'] for f in features)
    assert logged.get('/api/scripts/' + original['id'] + '/source').json()['menu.py'] == MENU
    project = logged.post('/api/projects', json={'name': 'owned-feature-project'}).json()
    assert logged.post('/api/tasks',json={'project_id':project['id'],'script_version_id':result['id'],'name':'must-choose-feature'}).status_code==422
    chosen = next(feature for feature in features if feature['definition']['parameters'])
    body = {'project_id': project['id'], 'script_version_id': result['id'], 'script_feature_id': chosen['id'], 'name': 'single-feature', 'feature_parameters': {}}
    task = logged.post('/api/tasks', json=body)
    assert task.status_code == 200, task.text
    rows = logged.get('/api/tasks').json()
    assert rows[0]['feature_config_snapshot']['entrypoint'] == chosen['definition']['entrypoint']
    assert rows[0]['feature_config_snapshot']['parameters'] == {'count': 1}
    assert logged.post('/api/tasks/' + task.json()['id'] + '/run').status_code == 409
    assert logged.post('/api/tasks/' + task.json()['id'] + '/test').status_code == 409
    assert logged.get('/api/executions').json() == []
    assert logged.post('/api/scripts/' + result['id'] + '/approve').status_code == 409
    assert logged.get('/api/health').json()['execution_enabled'] is False


async def test_ai_requires_exact_preview_then_fake_provider_can_generate(logged):
    version, ids = await discovery(logged)
    ai_config()
    proposal = rules_plan({'menu.py': MENU.encode()}, scan({'menu.py': MENU.encode()}), ids).model_dump()
    provider = FakeProvider(proposal)
    worker = AdapterWorker(provider_factory=lambda config: provider)
    job = start(logged, version, 'ai-transform', 'TRANSFORM', ids, True)
    await worker.run_once()
    assert detail(logged, job)['status'] == 'WAITING_FOR_AI_APPROVAL'
    assert provider.calls == 0
    preview = logged.get('/api/analysis-jobs/' + job['id'] + '/preview').json()
    rejected = logged.post('/api/analysis-jobs/' + job['id'] + '/approve-ai', json={'reviewed_preview_sha256': '0' * 64, 'confirmed': True})
    assert rejected.status_code == 409
    approved = logged.post('/api/analysis-jobs/' + job['id'] + '/approve-ai', json={'reviewed_preview_sha256': preview['sha256'], 'confirmed': True})
    assert approved.status_code == 200
    await worker.run_once()
    result = detail(logged, job)
    assert result['status'] == 'REVIEW_REQUIRED', result
    assert provider.calls == 1
    assert result['result']['draft_id']


async def test_user_rejects_function_and_references_prevent_deletion(logged):
    original, job, data = await draft(logged)
    feature = data['features'][0]
    response = logged.post('/api/script-features/' + feature['id'] + '/review', json={
        'action': 'REJECTED', 'reviewed_sha256': feature['review_sha256'], 'confirmed': True})
    assert response.status_code == 200
    assert save(logged, data).status_code == 409
    assert logged.request('DELETE', '/api/scripts/' + original['id'], json={'confirmed': True}).status_code == 409
    assert logged.request('DELETE', '/api/analysis-jobs/' + job['id'], json={'confirmed': True}).status_code == 200
    assert logged.get('/api/scripts/' + original['id'] + '/source').status_code == 200


async def test_cancel_pending_and_bounded_idempotent_queue(logged, monkeypatch):
    version = upload(logged)
    monkeypatch.setattr(settings, 'adapter_queue_limit', 1)
    job = start(logged, version, 'same')
    assert start(logged, version, 'same')['id'] == job['id']
    response = logged.post('/api/scripts/' + version['id'] + '/analysis-jobs', json={'request_key': 'other'})
    assert response.status_code == 429
    assert logged.request('DELETE', '/api/analysis-jobs/' + job['id'], json={'confirmed': True}).status_code == 409
    assert logged.post('/api/analysis-jobs/' + job['id'] + '/cancel').status_code == 200
    assert detail(logged, job)['status'] == 'CANCELLED'
    assert not await AdapterWorker().run_once()


def test_analysis_auth_and_csrf(client, logged):
    from fastapi.testclient import TestClient
    from backend.main import app
    with TestClient(app) as anonymous:
        assert anonymous.get('/api/analysis-jobs').status_code == 401
    version = upload(logged)
    token = logged.headers.pop('x-csrf-token')
    assert logged.post('/api/scripts/' + version['id'] + '/analysis-jobs', json={'request_key': 'blocked'}).status_code == 403
    logged.headers['x-csrf-token'] = token


async def test_review_invalidated_after_source_digest_change(logged):
    version, ids = await discovery(logged)
    ai_config()
    job = start(logged, version, 'ai', 'TRANSFORM', ids, True)
    await AdapterWorker().run_once()
    preview = logged.get('/api/analysis-jobs/' + job['id'] + '/preview').json()
    with Session.begin() as db:
        source = db.get(ScriptVersion, version['id'])
        db.get(Artifact, source.source_artifact_id).sha256 = 'f' * 64
    response = logged.post('/api/analysis-jobs/' + job['id'] + '/approve-ai', json={'reviewed_preview_sha256': preview['sha256'], 'confirmed': True})
    assert response.status_code == 409
