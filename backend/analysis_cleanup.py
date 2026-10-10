"""Explicit cleanup preserves source, saved versions and still-referenced ciphertext."""
from fastapi import HTTPException
from sqlalchemy import delete, select
from backend.analysis_jobs import QUEUED
from backend.deletion_api import artifact_referenced, count, safe_artifact_path
from backend.models import Artifact, AuditLog, ScriptAnalysisJob, ScriptFeature, ScriptTransformationDraft, Task
from backend.queue import locked_db
from backend.storage import artifact_lock


def remove_job(job_id, user_id):
    paths, pending = [], 0
    with artifact_lock(), locked_db() as db:
        job = db.get(ScriptAnalysisJob, job_id)
        if job is None:
            raise HTTPException(404, '分析记录不存在')
        if job.status in QUEUED:
            raise HTTPException(409, '请先取消并等待分析停止，再清理记录')
        draft = db.scalar(select(ScriptTransformationDraft).where(ScriptTransformationDraft.job_id == job.id))
        artifacts = {job.preview_artifact_id} if job.preview_artifact_id else set()
        if draft:
            feature_ids = list(db.scalars(select(ScriptFeature.id).where(ScriptFeature.draft_id == draft.id)))
            if feature_ids and count(db, Task, Task.script_feature_id.in_(feature_ids)):
                raise HTTPException(409, '草稿功能被任务引用，不能删除')
            artifacts.update({draft.output_artifact_id, draft.diff_artifact_id})
            db.execute(delete(ScriptFeature).where(ScriptFeature.draft_id == draft.id))
            db.delete(draft); db.flush()
        db.delete(job); db.flush()
        for identity in artifacts:
            if artifact_referenced(db, identity):
                continue
            obj = db.get(Artifact, identity)
            path = safe_artifact_path(obj)
            db.delete(obj); db.flush()
            if not count(db, Artifact, Artifact.path == str(path)):
                paths.append(path)
        db.add(AuditLog(user_id=user_id, action='analysis.delete', target=job_id,
                        detail={'saved_versions_preserved': True, 'source_preserved': True}))
        db.commit()
        for path in paths:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pending += 1
    return {'ok': True, 'source_preserved': True, 'saved_versions_preserved': True, 'artifact_files_pending': pending}
