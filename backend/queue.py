"""Database queue; all claim/recovery/scheduling decisions share a transaction lock."""
import time
from contextlib import contextmanager
from datetime import datetime,timedelta
from zoneinfo import ZoneInfo
from sqlalchemy import select,text,delete,func
from backend.config import settings
from backend.db import Session,engine
from backend.models import *
from packages.protocol import Manifest,WalletResult
from backend.uploads import redact

GOOD={'SUCCESS','SKIPPED','ALREADY_DONE'}
RETRYABLE={'FAILED','TIMEOUT'}


@contextmanager
def locked_db():
    with Session() as db:
        if engine.dialect.name=='sqlite':db.execute(text('BEGIN IMMEDIATE'))
        else:db.execute(text('SELECT pg_advisory_xact_lock(73840213)'))
        try:yield db;db.commit()
        except Exception:db.rollback();raise


def emergency(db):
    obj=db.get(SystemSetting,'security')
    return bool(obj and obj.value.get('emergency_stop'))


def business_period(project,now=None):
    dt=datetime.fromtimestamp(now or time.time(),ZoneInfo(project.timezone))-timedelta(hours=project.reset_hour)
    return dt.date().isoformat()


def enqueue_db(db,task_id,period=None,is_test=False):
    if not settings.execution_enabled:
        raise ValueError('管理模式未开放脚本执行、自动调度或链上交易')
    task=db.get(Task,task_id)
    if not task or not task.enabled:raise ValueError('任务不存在或已停用')
    if task.script_feature_id or db.scalar(select(ScriptFeature.id).where(ScriptFeature.version_id==task.script_version_id).limit(1)):
        raise ValueError('功能级任务只允许资料管理，功能执行协议尚未验收；禁止运行或测试')
    if emergency(db):raise ValueError('Emergency Stop 已启用')
    version=db.get(ScriptVersion,task.script_version_id)
    if not version.manifest:raise ValueError('脚本尚未适配')
    manifest=Manifest.model_validate(version.manifest)
    if is_test and version.status not in {'APPROVED','TESTED','PUBLISHED'}:raise ValueError('测试前须审核 Diff')
    if not is_test and version.status!='PUBLISHED':raise ValueError('执行前须测试并发布版本')
    if version.analysis.get('secret_findings'):raise ValueError('源代码含硬编码凭证，禁止执行；请移除后重新上传')
    if version.analysis.get('web3') or manifest.allowed_operations!=['mock']:raise ValueError('当前 Runner 只接受模拟操作，真实签名和网络交互尚未开放')
    if manifest.dependencies and any(d not in {'openpyxl'} for d in manifest.dependencies):raise ValueError('未知依赖须人工构建审核后的 Runner 镜像；禁止自动安装')
    project=db.get(Project,task.project_id)
    period=period or (f'test:{uid()}' if is_test else business_period(project))
    existing=db.scalar(select(Execution).where(Execution.task_id==task_id,Execution.period==period))
    if existing:return existing,False
    if not is_test and db.get(ExecutionReceipt,(task_id,period)):
        raise ValueError('本周期已执行，历史记录已删除，禁止重复运行')
    wallets=list(db.scalars(select(Wallet).join(ProjectWalletBinding).where(ProjectWalletBinding.project_id==project.id,Wallet.enabled==True)))
    if not wallets:raise ValueError('项目没有启用的钱包')
    if any(w.chain!=manifest.wallet_chain for w in wallets):raise ValueError('项目钱包包含不同链，请分开绑定')
    if any(not w.is_test for w in wallets):raise ValueError('当前只允许专用测试钱包；导入真实钱包无法执行')
    execution=Execution(task_id=task.id,project_id=project.id,script_version_id=version.id,period=period,is_test=is_test)
    db.add(execution);db.flush()
    if not is_test:db.add(ExecutionReceipt(task_id=task.id,period=period,execution_id=execution.id))
    for w in wallets:db.add(WalletTaskResult(execution_id=execution.id,wallet_id=w.id,address=w.address,chain=w.chain,name=w.name))
    db.flush()
    return execution,True


def enqueue(task_id,period=None,is_test=False):
    with locked_db() as db:
        execution,created=enqueue_db(db,task_id,period,is_test)
        return execution.id,created


def recover_db(db,now):
    expired=list(db.scalars(select(Execution).where(Execution.status=='RUNNING',Execution.lease_until<now)))
    for e in expired:
        e.status='UNKNOWN';e.error='Worker 租约超时，禁止自动重放；请核对运行结果';e.finished_at=now
        if e.current_attempt_id:
            attempt=db.get(ExecutionAttempt,e.current_attempt_id)
            if attempt:attempt.status='UNKNOWN';attempt.finished_at=now
        for result in db.scalars(select(WalletTaskResult).where(WalletTaskResult.execution_id==e.id,WalletTaskResult.status.in_(['QUEUED','RUNNING']))):
            result.status='UNKNOWN';result.message=e.error
        # Retain wallet locks until explicit reconciliation, even in mock mode.
    return len(expired)


def claim(worker_id):
    now=time.time()
    with locked_db() as db:
        worker=db.get(Worker,worker_id)
        if not worker:worker=Worker(id=worker_id,heartbeat_at=now,status='ONLINE');db.add(worker)
        worker.heartbeat_at=now;worker.status='ONLINE'
        recover_db(db,now)
        if not settings.execution_enabled or emergency(db):return None
        concurrent=db.get(SystemSetting,'execution')
        limit=concurrent.value['global_concurrency'] if concurrent else settings.global_concurrency
        running=db.scalar(select(func.count()).select_from(Execution).where(Execution.status=='RUNNING'))
        if running>=limit:return None
        for e in db.scalars(select(Execution).where(Execution.status=='QUEUED',Execution.available_at<=now).order_by(Execution.created_at).limit(100)):
            if e.cancel_requested:e.status='CANCELLED';e.finished_at=now;continue
            project=db.get(Project,e.project_id)
            count=db.scalar(select(func.count()).select_from(Execution).where(Execution.project_id==e.project_id,Execution.status=='RUNNING'))
            if count>=project.concurrency:continue
            wallet_ids=list(db.scalars(select(WalletTaskResult.wallet_id).where(WalletTaskResult.execution_id==e.id,WalletTaskResult.status.in_(['QUEUED','RUNNING']))))
            if any(db.get(WalletLock,w) for w in wallet_ids):continue
            for w in wallet_ids:db.add(WalletLock(wallet_id=w,execution_id=e.id))
            e.status='RUNNING';e.worker_id=worker_id;e.lease_until=now+settings.lease_seconds;e.started_at=now
            attempt=ExecutionAttempt(execution_id=e.id,number=e.retry_count+1);db.add(attempt);db.flush()
            e.current_attempt_id=attempt.id
            for r in db.scalars(select(WalletTaskResult).where(WalletTaskResult.execution_id==e.id,WalletTaskResult.status=='QUEUED')):r.status='RUNNING'
            return e.id,attempt.id
    return None


def heartbeat(execution_id,attempt_id,worker_id):
    with locked_db() as db:
        e=db.get(Execution,execution_id)
        if not e or e.current_attempt_id!=attempt_id or e.status!='RUNNING' or e.worker_id!=worker_id:return False
        e.lease_until=time.time()+settings.lease_seconds
        worker=db.get(Worker,worker_id);worker.heartbeat_at=time.time()
        if emergency(db):e.cancel_requested=True
        return not e.cancel_requested


def finalize(execution_id,attempt_id,response):
    now=time.time()
    with locked_db() as db:
        e=db.get(Execution,execution_id)
        if not e or e.current_attempt_id!=attempt_id or e.status!='RUNNING':return False
        attempt=db.get(ExecutionAttempt,attempt_id)
        attempt.process_exit_code=response.get('exit_code');attempt.log=redact(str(response.get('log',''))[:64000])[0];attempt.finished_at=now
        pending={r.address:r for r in db.scalars(select(WalletTaskResult).where(WalletTaskResult.execution_id==e.id,WalletTaskResult.status=='RUNNING'))}
        seen=set();invalid=False
        for raw in response.get('results',[]):
            try:result=WalletResult.model_validate(raw)
            except ValueError:invalid=True;continue
            if result.address not in pending or result.address in seen:invalid=True;continue
            seen.add(result.address);r=pending[result.address]
            r.status=result.status.value;r.message=redact(result.message)[0];r.points=result.points;r.transaction_hash=result.transaction_hash
            if result.transaction_hash:r.status='MANUAL_REQUIRED';r.message='模拟 Runner 不接受真实交易结果'
            if result.points is not None:db.add(PointSnapshot(project_id=e.project_id,wallet_id=r.wallet_id,execution_id=e.id,points=result.points))
        reason=response.get('status')
        fallback=reason if reason in {'TIMEOUT','CANCELLED','FAILED','MANUAL_REQUIRED'} else 'UNKNOWN'
        if e.cancel_requested:fallback='CANCELLED'
        for address,r in pending.items():
            if address not in seen:r.status=fallback;r.message=redact(str(response.get('error','没有收到该钱包的结构化结果')))[0][:2000]
        states=[r.status for r in db.scalars(select(WalletTaskResult).where(WalletTaskResult.execution_id==e.id))]
        if e.cancel_requested:e.status='CANCELLED'
        elif reason in {'TIMEOUT','CANCELLED','MANUAL_REQUIRED'}:e.status=reason
        elif 'UNKNOWN' in states or invalid:e.status='UNKNOWN'
        elif 'MANUAL_REQUIRED' in states:e.status='MANUAL_REQUIRED'
        elif all(s in GOOD for s in states):e.status='SUCCESS'
        elif any(s in GOOD or s=='PARTIAL_SUCCESS' for s in states):e.status='PARTIAL_SUCCESS'
        else:e.status='FAILED'
        # Process success and business success are distinct; crash after results stays inspectable.
        if (response.get('exit_code') not in {0,None} or reason=='FAILED') and e.status=='SUCCESS':e.status='PARTIAL_SUCCESS'
        attempt.status=e.status;e.finished_at=now;e.lease_until=None;e.error=redact(str(response.get('error','')))[0][:2000]
        if e.status not in {'UNKNOWN','MANUAL_REQUIRED'}:
            db.execute(delete(WalletLock).where(WalletLock.execution_id==e.id))
        version=db.get(ScriptVersion,e.script_version_id)
        if e.is_test and e.status in {'SUCCESS','PARTIAL_SUCCESS'} and attempt.process_exit_code==0 and reason=='DONE' and not invalid:
            version.tested_sha256=db.get(Artifact,version.source_artifact_id).sha256
            if version.status!='PUBLISHED':version.status='TESTED'
        if e.status in {'FAILED','PARTIAL_SUCCESS','TIMEOUT'}:
            task=db.get(Task,e.task_id);manifest=Manifest.model_validate(version.manifest)
            retry_rows=[r for r in db.scalars(select(WalletTaskResult).where(WalletTaskResult.execution_id==e.id)) if r.status in RETRYABLE]
            if manifest.retry_safe and retry_rows and e.retry_count<task.max_retries and not e.cancel_requested:
                e.retry_count+=1;e.status='QUEUED';e.available_at=now+min(60,2**e.retry_count);e.finished_at=None
                for r in retry_rows:r.status='QUEUED'
        if e.status not in {'SUCCESS','QUEUED'}:db.add(AuditLog(action='execution.alert',target=e.id,detail={'status':e.status,'message':'执行异常，请在后台核查'}))
        return True


def retry(execution_id):
    if not settings.execution_enabled:
        raise ValueError('管理模式未开放执行重试')
    with locked_db() as db:
        e=db.get(Execution,execution_id)
        if not e or e.status not in {'FAILED','PARTIAL_SUCCESS','TIMEOUT'}:raise ValueError('该状态不可安全重试；UNKNOWN 必须先核查')
        manifest=Manifest.model_validate(db.get(ScriptVersion,e.script_version_id).manifest)
        if not manifest.retry_safe:raise ValueError('Manifest 未声明安全重试')
        pending=list(db.scalars(select(WalletTaskResult).where(WalletTaskResult.execution_id==e.id,WalletTaskResult.status.in_(list(RETRYABLE)))))
        if not pending:raise ValueError('没有可重试的钱包')
        for r in pending:r.status='QUEUED'
        e.retry_count+=1;e.status='QUEUED';e.cancel_requested=False;e.available_at=time.time();e.finished_at=None
        return len(pending)
