import time

import pytest
from sqlalchemy import func, select

from backend.config import settings
from backend.db import Session
from backend.models import Execution, ExecutionAttempt, Schedule, ScheduleOccurrence
from backend.queue import claim, enqueue
from backend.scheduler import tick
from tests.test_queue import task_setup


def test_management_health_and_removed_endpoints(logged, monkeypatch):
    monkeypatch.setattr(settings, 'execution_enabled', False)
    health = logged.get('/api/health').json()
    assert health['mode'] == 'management' and not health['execution_enabled'] and not health['real_signing_enabled']
    assert not logged.get('/api/settings').json()['capabilities']['execution_enabled']
    assert logged.post('/api/demo/setup').status_code == 404
    assert all('/demo' not in path and '/mock' not in path for path in logged.get('/openapi.json').json()['paths'])


@pytest.mark.parametrize('operation', ['run', 'test'])
def test_management_rejects_execution_before_queue_write(task_setup, monkeypatch, operation):
    logged, task_id, _ = task_setup
    monkeypatch.setattr(settings, 'execution_enabled', False)
    response = logged.post(f'/api/tasks/{task_id}/{operation}')
    assert response.status_code == 409 and '管理模式' in response.text
    with Session() as db:
        assert db.scalar(select(func.count()).select_from(Execution)) == 0
        assert db.scalar(select(func.count()).select_from(ExecutionAttempt)) == 0


def test_management_worker_does_not_claim_existing_queue(task_setup, monkeypatch):
    _, task_id, _ = task_setup
    identity, _ = enqueue(task_id, is_test=True)
    monkeypatch.setattr(settings, 'execution_enabled', False)
    assert claim('management-worker') is None
    with Session() as db:
        assert db.get(Execution, identity).status == 'QUEUED'
        assert db.scalar(select(func.count()).select_from(ExecutionAttempt)) == 0


def test_management_scheduler_does_not_trigger_legacy_enabled_plan(task_setup, monkeypatch):
    logged, task_id, _ = task_setup
    body = {'task_id': task_id, 'hour': 0, 'minute': 0, 'enabled': True}
    identity = logged.post('/api/schedules', json=body).json()['id']
    with Session.begin() as db:
        db.get(Schedule, identity).next_run_at = time.time() - 10
    monkeypatch.setattr(settings, 'execution_enabled', False)
    tick()
    with Session() as db:
        assert db.scalar(select(func.count()).select_from(ScheduleOccurrence)) == 0
        assert db.scalar(select(func.count()).select_from(Execution)) == 0


def test_management_allows_disabled_plan_but_rejects_activation(task_setup, monkeypatch):
    logged, task_id, _ = task_setup
    monkeypatch.setattr(settings, 'execution_enabled', False)
    body = {'task_id': task_id, 'hour': 0, 'minute': 0, 'enabled': False}
    created = logged.post('/api/schedules', json=body)
    assert created.status_code == 200
    identity = created.json()['id']
    assert logged.put('/api/schedules/' + identity, json={**body, 'enabled': True}).status_code == 409
    assert logged.post('/api/schedules', json={**body, 'enabled': True}).status_code == 409
    with Session() as db:
        assert not db.get(Schedule, identity).enabled
        assert db.scalar(select(func.count()).select_from(Schedule)) == 1


def test_management_rejects_retry_and_removed_ai_provider(task_setup, monkeypatch):
    logged, _, version_id = task_setup
    monkeypatch.setattr(settings, 'execution_enabled', False)
    assert logged.post('/api/executions/nonexistent/retry').status_code == 409
    with Session() as db:
        from backend.models import ScriptVersion
        manifest = db.get(ScriptVersion, version_id).manifest
    response = logged.post('/api/scripts/' + version_id + '/adapt', json={'manifest': manifest, 'provider': 'mock'})
    assert response.status_code == 422
