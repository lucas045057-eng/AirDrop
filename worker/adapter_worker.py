"""Single-concurrency Adapter Worker. No uploaded module is ever imported."""
import asyncio
import hashlib
import json
import time
from sqlalchemy import select
from backend.ai import CompatibleProvider
from backend.analysis_jobs import ACTIVE, ai_identity, canonical, claim, digest, require_source, update
from backend.config import settings
from backend.db import Session
from backend.models import Artifact, AuditLog, ScriptAnalysisJob, ScriptFeature, ScriptTransformationDraft, ScriptVersion, SystemSetting, Worker
from backend.queue import locked_db
from backend.storage import artifact_lock, cipher, read_blob
from worker.process_limits import run_child


def existing_provider(config):
    return CompatibleProvider(config['url'], config['model'], cipher().decrypt(config['encrypted_key'].encode()).decode())


class AdapterWorker:
    def __init__(self, provider_factory=existing_provider, worker_id='adapter-1'):
        self.provider_factory, self.worker_id = provider_factory, worker_id

    async def run_once(self):
        claimed = await asyncio.to_thread(claim, self.worker_id)
        if not claimed:
            return False
        job_id, token = claimed
        started = time.monotonic()

        async def control():
            def check():
                with locked_db() as db:
                    job = db.get(ScriptAnalysisJob, job_id)
                    if not job or job.lease_token != token or job.cancel_requested:
                        return True
                    if time.monotonic() - started > settings.adapter_timeout_seconds:
                        job.error = '任务超过总处理时限，原版本保留'; job.status = 'FAILED'
                        job.finished_at = time.time(); job.lease_token = None; job.lease_until = None
                        return True
                    job.lease_until = time.time() + settings.adapter_lease_seconds
                    worker = db.get(Worker, self.worker_id)
                    if worker:
                        worker.heartbeat_at = time.time()
                    return False
            return await asyncio.to_thread(check)

        async def child(operation, args, body=b''):
            async def stage(status):
                await asyncio.to_thread(update, job_id, token, status)
            return await run_child(operation, args, body, memory_mib=settings.adapter_memory_mib,
                                   timeout=min(60, settings.adapter_timeout_seconds), cancelled=control, stage_callback=stage)

        try:
            with Session() as db:
                job = db.get(ScriptAnalysisJob, job_id); require_source(db, job)
                obj = db.get(Artifact, job.source_artifact_id)
                args = {'source_path': obj.path, 'source_sha256': obj.sha256}
                config = job.config
            report, metrics = await child('scan', args)
            report['resources'] = metrics
            if not await asyncio.to_thread(update, job_id, token, 'DISCOVERING', result=report):
                raise asyncio.CancelledError()
            if config['operation'] == 'DISCOVER':
                await asyncio.to_thread(update, job_id, token, report['status'], result=report)
                return True
            selected = config['selected_features']; args['selected_features'] = selected
            if not report['features'] or report['errors']:
                await asyncio.to_thread(update, job_id, token, 'MANUAL_REQUIRED', result=report,
                                        error='静态分析未发现可靠功能或超出预算，需先处理结构问题')
                return True
            proposal = b''
            if config.get('use_ai'):
                with Session() as db:
                    job = db.get(ScriptAnalysisJob, job_id)
                    reviewed = job.reviewed_preview_sha256
                if not reviewed:
                    preview, _ = await child('context', args)
                    with artifact_lock(), locked_db() as db:
                        job = db.get(ScriptAnalysisJob, job_id); require_source(db, job)
                        if job.lease_token != token or job.cancel_requested:
                            raise asyncio.CancelledError()
                        setting = db.get(SystemSetting, 'ai')
                        if not setting:
                            raise ValueError('请先在现有系统设置中配置兼容 AI Provider，再创建 AI 重构任务')
                        job.config = {**job.config, 'ai_provider': ai_identity(setting.value)}
                        artifact = Artifact(path=preview['path'], sha256=preview['sha256'], kind='analysis-preview')
                        db.add(artifact); db.flush()
                        job.preview_artifact_id = artifact.id; job.preview_sha256 = preview['preview_sha256']
                    await asyncio.to_thread(update, job_id, token, 'WAITING_FOR_AI_APPROVAL', result=report)
                    return True
                with artifact_lock(), locked_db() as db:
                    job = db.get(ScriptAnalysisJob, job_id); require_source(db, job)
                    if job.lease_token != token or job.cancel_requested or job.ai_sent:
                        raise ValueError('审核失效或模型请求已经发送，禁止重复请求')
                    preview = db.get(Artifact, job.preview_artifact_id)
                    payload = json.loads(read_blob(preview.path, preview.sha256))
                    if hashlib.sha256(canonical(payload)).hexdigest() != reviewed or reviewed != job.preview_sha256:
                        raise ValueError('脱敏预览摘要已改变，须重新审核')
                    setting = db.get(SystemSetting, 'ai')
                    if not setting:
                        raise ValueError('请先在现有系统设置中配置兼容 AI Provider')
                    provider_config = setting.value
                    if ai_identity(provider_config) != job.config.get('ai_provider'):
                        raise ValueError('AI Provider 来源或密钥配置改变，发送审核失效，请重新创建任务')
                    job.ai_sent = True; job.status = 'AI_ANALYZING'
                    db.add(AuditLog(user_id=job.created_by, action='analysis.ai.send', target=job.id,
                                    detail={'preview_sha256': reviewed, 'source_sha256': job.source_sha256, 'model': provider_config['model']}))
                call = asyncio.create_task(self.provider_factory(provider_config).plan(payload))
                try:
                    while not call.done():
                        if await control():
                            raise asyncio.CancelledError()
                        await asyncio.sleep(.1)
                    proposal = canonical(await call)
                finally:
                    if not call.done():
                        call.cancel(); await asyncio.gather(call, return_exceptions=True)
            await asyncio.to_thread(update, job_id, token, 'GENERATING')
            output, validation_metrics = await child('transform', args, proposal)
            with artifact_lock(), locked_db() as db:
                job = db.get(ScriptAnalysisJob, job_id); require_source(db, job)
                if job.lease_token != token or job.cancel_requested:
                    raise asyncio.CancelledError()
                existing = db.scalar(select(ScriptTransformationDraft).where(ScriptTransformationDraft.job_id == job_id))
                if existing:
                    raise ValueError('该任务已经生成草稿，不能部分覆盖')
                artifacts = {}
                for key in ['output', 'diff']:
                    blob = output[key]; obj = Artifact(path=blob['path'], sha256=blob['sha256'], kind='feature-' + key)
                    db.add(obj); db.flush(); artifacts[key] = obj
                from backend.storage import artifact_fingerprint
                output['summary']['output_cipher_sha256'] = artifact_fingerprint(artifacts['output'])
                review_sha = digest({'source_sha256': job.source_sha256, 'output_sha256': artifacts['output'].sha256,
                                     'summary': output['summary']})
                draft = ScriptTransformationDraft(job_id=job.id, source_version_id=job.source_version_id,
                    source_artifact_id=job.source_artifact_id, source_sha256=job.source_sha256,
                    output_artifact_id=artifacts['output'].id, diff_artifact_id=artifacts['diff'].id,
                    output_sha256=artifacts['output'].sha256, review_sha256=review_sha, summary={**output['summary'], 'analysis': output['analysis']})
                db.add(draft); db.flush()
                for definition in output['summary']['features']:
                    db.add(ScriptFeature(draft_id=draft.id, feature_key=definition['feature_id'], definition=definition,
                                         code_sha256=draft.output_sha256, status=definition['automation_status']))
                job.result = {**report, 'draft_id': draft.id, 'validation_resources': validation_metrics}
                job.status = 'REVIEW_REQUIRED'; job.finished_at = time.time(); job.lease_token = None; job.lease_until = None
                db.add(AuditLog(user_id=job.created_by, action='analysis.draft.create', target=draft.id, detail={'output_sha256': draft.output_sha256}))
        except asyncio.CancelledError:
            with locked_db() as db:
                job = db.get(ScriptAnalysisJob, job_id)
                if job and job.lease_token == token:
                    job.status = 'CANCELLED'; job.finished_at = time.time(); job.lease_token = None; job.lease_until = None
                    job.error = '分析已取消，原版本保留'
        except Exception as exc:
            # Network/provider bodies, credentials and validation inputs never enter errors.
            from pydantic import ValidationError
            from fastapi import HTTPException
            message = ('模型输出不符合严格协议' if isinstance(exc, ValidationError) else
                       str(exc) if isinstance(exc, ValueError) else
                       exc.detail if isinstance(exc, HTTPException) else
                       '模型请求或分析失败（' + type(exc).__name__ + '），原版本保留')
            await asyncio.to_thread(update, job_id, token, 'FAILED', error=message[:1000])
        return True


async def main():
    worker = AdapterWorker()
    while True:
        await worker.run_once()
        await asyncio.sleep(1)


if __name__ == '__main__':
    asyncio.run(main())
