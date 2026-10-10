"""Durable bounded analysis queue; no script execution queue is used here."""
import hashlib
import json
import time
from fastapi import HTTPException
from sqlalchemy import func, select
from backend.config import settings
from backend.models import Artifact, AuditLog, ScriptAnalysisJob, ScriptVersion, Worker, uid
from backend.queue import locked_db
from backend.storage import artifact_fingerprint, artifact_lock

ACTIVE = {'SCANNING', 'DISCOVERING', 'AI_ANALYZING', 'GENERATING', 'VALIDATING'}
QUEUED = ACTIVE | {'PENDING', 'WAITING_FOR_AI_APPROVAL'}
TERMINAL = {'REVIEW_REQUIRED', 'APPROVED', 'COMPLETED', 'FAILED', 'CANCELLED', 'MANUAL_REQUIRED'}


def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':')).encode()


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def ai_identity(value):
    return {'url': value['url'], 'model': value['model'],
            'key_fingerprint': hashlib.sha256(value['encrypted_key'].encode()).hexdigest()}


def source_unchanged(db, job):
    version = db.get(ScriptVersion, job.source_version_id)
    obj = db.get(Artifact, job.source_artifact_id)
    if not (version and obj and version.source_artifact_id == job.source_artifact_id and obj.sha256 == job.source_sha256):
        return False
    try:
        return digest(version.manifest) == job.config.get('source_manifest_sha256') and artifact_fingerprint(obj) == job.config.get('source_cipher_sha256')
    except (OSError, ValueError):
        return False


def require_source(db, job):
    if not source_unchanged(db, job):
        raise HTTPException(409, '源版本或工件摘要已改变，审核失效，请重新分析')


def create(source_version_id, request_key, config, user_id):
    with artifact_lock(), locked_db() as db:
        existing = db.scalar(select(ScriptAnalysisJob).where(ScriptAnalysisJob.source_version_id == source_version_id,
                                                           ScriptAnalysisJob.request_key == request_key))
        if existing:
            if {key: existing.config.get(key) for key in config} != config:
                raise HTTPException(409, '同一请求键不能提交不同分析配置')
            return job_dict(existing)
        source = db.get(ScriptVersion, source_version_id)
        if not source:
            raise HTTPException(404, '源版本不存在')
        pending = db.scalar(select(func.count()).select_from(ScriptAnalysisJob).where(ScriptAnalysisJob.status.in_(QUEUED)))
        if pending >= settings.adapter_queue_limit:
            raise HTTPException(429, '分析队列已满，请先完成或取消已有任务')
        obj = db.get(Artifact, source.source_artifact_id)
        config = {**config, 'source_manifest_sha256': digest(source.manifest), 'source_cipher_sha256': artifact_fingerprint(obj)}
        job = ScriptAnalysisJob(source_version_id=source.id, source_artifact_id=obj.id, source_sha256=obj.sha256,
                                request_key=request_key, operation=config['operation'], config=config, created_by=user_id)
        db.add(job); db.flush()
        db.add(AuditLog(user_id=user_id, action='analysis.create', target=job.id, detail={'source_sha256': obj.sha256, 'operation': job.operation}))
        return job_dict(job)


def job_dict(job, full=True):
    fields = ('id', 'source_version_id', 'source_sha256', 'operation', 'status', 'config', 'error', 'cancel_requested',
              'attempts', 'created_at', 'started_at', 'finished_at', 'preview_sha256', 'reviewed_preview_sha256')
    result = {key: getattr(job, key) for key in fields}
    result['result'] = job.result if full else {'feature_count': len(job.result.get('features', []))}
    result['execution_enabled'] = False
    result['stage_progress'] = {'stage': job.status, 'mode': 'actual_stage', 'percent': None}
    return result


def recover(db, now):
    for job in db.scalars(select(ScriptAnalysisJob).where(ScriptAnalysisJob.status.in_(ACTIVE), ScriptAnalysisJob.lease_until < now)):
        job.lease_token = None; job.lease_until = None
        if job.cancel_requested:
            job.status = 'CANCELLED'; job.finished_at = now
        elif job.ai_sent or job.attempts >= 2:
            job.status = 'MANUAL_REQUIRED'; job.finished_at = now
            job.error = '分析进程中断；模型请求可能已发送或重试预算耗尽，禁止自动重放。请重新审核后新建任务。'
        else:
            job.status = 'PENDING'; job.error = '静态分析进程中断，正在执行一次有界恢复'


def claim(worker_id='adapter-1'):
    now = time.time()
    with locked_db() as db:
        recover(db, now)
        worker = db.get(Worker, worker_id)
        if worker is None:
            db.add(Worker(id=worker_id, status='ONLINE', heartbeat_at=now))
        else:
            worker.heartbeat_at = now; worker.status = 'ONLINE'
        # Enforce one active analysis globally, even with two worker processes.
        if db.scalar(select(func.count()).select_from(ScriptAnalysisJob).where(ScriptAnalysisJob.status.in_(ACTIVE))):
            return None
        job = db.scalar(select(ScriptAnalysisJob).where(ScriptAnalysisJob.status == 'PENDING').order_by(ScriptAnalysisJob.created_at).limit(1))
        if job is None:
            return None
        if job.cancel_requested:
            job.status = 'CANCELLED'; job.finished_at = now
            return None
        job.status = 'SCANNING'; job.attempts += 1; job.started_at = job.started_at or now
        job.lease_token = uid(); job.lease_until = now + settings.adapter_lease_seconds
        return job.id, job.lease_token


def update(job_id, token, status, *, result=None, error=''):
    with locked_db() as db:
        job = db.get(ScriptAnalysisJob, job_id)
        if not job or job.lease_token != token or job.cancel_requested:
            return False
        job.status = status; job.error = error
        job.lease_until = time.time() + settings.adapter_lease_seconds
        if result is not None:
            job.result = result
        if status in TERMINAL or status == 'WAITING_FOR_AI_APPROVAL':
            job.finished_at = time.time() if status in TERMINAL else None
            job.lease_token = None; job.lease_until = None
        return True


def cancel(job_id, user_id):
    with locked_db() as db:
        job = db.get(ScriptAnalysisJob, job_id)
        if not job:
            raise HTTPException(404, '分析任务不存在')
        if job.status == 'COMPLETED':
            raise HTTPException(409, '版本已经保存；取消不会删除已保存版本')
        job.cancel_requested = True
        if job.status not in ACTIVE:
            job.status = 'CANCELLED'; job.finished_at = time.time(); job.lease_token = None; job.lease_until = None
        db.add(AuditLog(user_id=user_id, action='analysis.cancel', target=job.id))
        return job_dict(job)
