"""Authenticated control plane; CPU-intensive work belongs to Adapter Worker."""
import json
import time
from typing import Literal
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session
from backend.analysis_jobs import QUEUED, ai_identity, cancel, create, digest, job_dict, require_source
from backend.auth import admin
from backend.config import settings
from backend.db import get_db
from backend.feature_reviews import feature_dict, review, save, verify_draft
from backend.models import Artifact, AuditLog, ScriptAnalysisJob, ScriptFeature, ScriptTransformationDraft, SystemSetting, User, Worker
from backend.queue import locked_db
from backend.storage import artifact_lock, read_blob
from cryptography.fernet import InvalidToken

router = APIRouter(tags=['AI 智能分析'], dependencies=[Depends(admin)])


class AnalysisRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    request_key: str = Field(min_length=1, max_length=100, pattern='^[A-Za-z0-9_-]+$')
    operation: Literal['DISCOVER', 'TRANSFORM'] = 'DISCOVER'
    selected_features: list[str] = Field(default_factory=list, max_length=20)
    use_ai: bool = False


@router.post('/scripts/{version_id}/analysis-jobs', status_code=202)
def start(version_id: str, body: AnalysisRequest, user: User = Depends(admin)):
    if body.operation == 'TRANSFORM' and not body.selected_features:
        raise HTTPException(422, '请选择需要重构的功能')
    if body.operation == 'DISCOVER' and (body.selected_features or body.use_ai):
        raise HTTPException(422, '结构发现无需模型；完成后选择功能再启动重构')
    return create(version_id, body.request_key, body.model_dump(exclude={'request_key'}), user.id)


@router.get('/analysis/capabilities')
def capabilities(db: Session = Depends(get_db)):
    worker = db.get(Worker, 'adapter-1')
    return {'worker_online': bool(worker and time.time() - worker.heartbeat_at < 30), 'concurrency': 1,
            'queue_limit': settings.adapter_queue_limit, 'memory_limit_mib': settings.adapter_memory_mib,
            'timeout_seconds': settings.adapter_timeout_seconds, 'ai_configured': db.get(SystemSetting, 'ai') is not None,
            'execution_enabled': False}


@router.get('/analysis-jobs')
def jobs(source_version_id: str | None = None, db: Session = Depends(get_db)):
    query = select(ScriptAnalysisJob).order_by(ScriptAnalysisJob.created_at.desc()).limit(50)
    if source_version_id:
        query = query.where(ScriptAnalysisJob.source_version_id == source_version_id)
    return [job_dict(job, full=False) for job in db.scalars(query)]


@router.get('/analysis-jobs/{job_id}')
def detail(job_id: str, db: Session = Depends(get_db)):
    job = db.get(ScriptAnalysisJob, job_id)
    if job is None:
        raise HTTPException(404, '分析任务不存在')
    return job_dict(job)


@router.post('/analysis-jobs/{job_id}/cancel')
def stop(job_id: str, user: User = Depends(admin)):
    return cancel(job_id, user.id)


class DeleteAnalysis(BaseModel):
    model_config = ConfigDict(extra='forbid')
    confirmed: Literal[True]


@router.delete('/analysis-jobs/{job_id}')
def cleanup(job_id: str, body: DeleteAnalysis, user: User = Depends(admin)):
    from backend.analysis_cleanup import remove_job
    return remove_job(job_id, user.id)


@router.get('/analysis-jobs/{job_id}/preview')
def preview(job_id: str, db: Session = Depends(get_db)):
    with artifact_lock():
        job = db.get(ScriptAnalysisJob, job_id)
        if job is None or job.status != 'WAITING_FOR_AI_APPROVAL' or not job.preview_artifact_id:
            raise HTTPException(409, '任务尚未等待 AI 发送审核')
        require_source(db, job)
        obj = db.get(Artifact, job.preview_artifact_id)
        try:
            payload = json.loads(read_blob(obj.path, obj.sha256))
            if digest(payload) != job.preview_sha256:
                raise ValueError('预览摘要改变')
        except (OSError, ValueError, InvalidToken):
            raise HTTPException(409, '脱敏预览已损坏或改变，请重新分析和审核') from None
        return {'payload': payload, 'sha256': job.preview_sha256,
                'source_sha256': job.source_sha256, 'provider': job.config.get('ai_provider'), 'requires_explicit_review': True}


class AIReview(BaseModel):
    model_config = ConfigDict(extra='forbid')
    reviewed_preview_sha256: str = Field(pattern='^[0-9a-f]{64}$')
    confirmed: Literal[True]


@router.post('/analysis-jobs/{job_id}/approve-ai')
def send(job_id: str, body: AIReview, user: User = Depends(admin)):
    with artifact_lock(), locked_db() as db:
        job = db.get(ScriptAnalysisJob, job_id)
        if not job or job.status != 'WAITING_FOR_AI_APPROVAL' or job.cancel_requested or job.ai_sent:
            raise HTTPException(409, '任务无法批准发送')
        require_source(db, job)
        if body.reviewed_preview_sha256 != job.preview_sha256:
            raise HTTPException(409, '审核摘要与当前脱敏预览不匹配')
        setting = db.get(SystemSetting, 'ai')
        if setting is None:
            raise HTTPException(409, '请先在系统设置配置现有兼容 AI Provider')
        if ai_identity(setting.value) != job.config.get('ai_provider'):
            raise HTTPException(409, 'AI Provider 来源或密钥已改变，旧审核失效，请新建任务')
        job.reviewed_preview_sha256 = body.reviewed_preview_sha256; job.status = 'PENDING'
        db.add(AuditLog(user_id=user.id, action='analysis.ai.approve', target=job.id, detail={'sha256': job.preview_sha256}))
        return job_dict(job)


@router.get('/transformation-drafts/{draft_id}')
def draft(draft_id: str, db: Session = Depends(get_db)):
    obj = db.get(ScriptTransformationDraft, draft_id)
    if not obj:
        raise HTTPException(404, '草稿不存在')
    return {'id': obj.id, 'job_id': obj.job_id, 'source_version_id': obj.source_version_id,
            'review_sha256': obj.review_sha256, 'output_sha256': obj.output_sha256,
            'status': obj.status, 'result_version_id': obj.result_version_id,
            'summary': {key: value for key, value in obj.summary.items() if key not in {'features', 'analysis'}},
            'features': [feature_dict(feature) for feature in db.scalars(select(ScriptFeature).where(ScriptFeature.draft_id == obj.id))]}


@router.get('/transformation-drafts/{draft_id}/diff')
def diff(draft_id: str, file: str | None = None, offset: int = Query(0, ge=0), limit: int = Query(200, ge=1, le=400), db: Session = Depends(get_db)):
    with artifact_lock():
        draft = db.get(ScriptTransformationDraft, draft_id)
        if draft is None:
            raise HTTPException(404, '草稿不存在')
        blob = db.get(Artifact, draft.diff_artifact_id)
        try:
            changes = json.loads(read_blob(blob.path, blob.sha256))
        except (OSError, ValueError, InvalidToken):
            raise HTTPException(409, 'Diff 工件已损坏，请重新分析') from None
        if file:
            changes = [change for change in changes if change['file'] == file]
        lines = [line for change in changes for line in ('文件：' + change['file'] + '\n' + change['diff']).splitlines()]
        return {'files': [change['file'] for change in changes], 'lines': lines[offset:offset + limit], 'offset': offset,
                'total_lines': len(lines), 'more': len(lines) > offset + limit, 'review_sha256': draft.review_sha256}


class FeatureReview(BaseModel):
    model_config = ConfigDict(extra='forbid')
    action: Literal['APPROVED', 'REJECTED']
    reviewed_sha256: str = Field(pattern='^[0-9a-f]{64}$')
    confirmed: Literal[True]


@router.post('/script-features/{feature_id}/review')
def approve_feature(feature_id: str, body: FeatureReview, user: User = Depends(admin)):
    return review(feature_id, body.action, body.reviewed_sha256, user.id)


class SaveDraft(BaseModel):
    model_config = ConfigDict(extra='forbid')
    feature_ids: list[str] = Field(min_length=1, max_length=20)
    reviewed_sha256: str = Field(pattern='^[0-9a-f]{64}$')
    confirmed: Literal[True]


@router.post('/transformation-drafts/{draft_id}/save')
def save_version(draft_id: str, body: SaveDraft, user: User = Depends(admin)):
    return save(draft_id, body.feature_ids, body.reviewed_sha256, user.id)


@router.get('/scripts/{version_id}/features')
def features(version_id: str, db: Session = Depends(get_db)):
    return [feature_dict(feature) for feature in db.scalars(select(ScriptFeature).where(ScriptFeature.version_id == version_id))]
