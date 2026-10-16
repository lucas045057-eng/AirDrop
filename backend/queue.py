"""Database queue; all claim/recovery/scheduling decisions share a transaction lock."""
import time
from contextlib import contextmanager
from datetime import datetime,timedelta
from zoneinfo import ZoneInfo
from fastapi import HTTPException
from sqlalchemy import select,text,delete,func
from backend.config import settings
from backend.db import Session,engine
from backend.models import *
from packages.protocol import Manifest,WalletResult
from backend.uploads import redact

GOOD={'SUCCESS','SKIPPED','ALREADY_DONE'}
RETRYABLE={'FAILED','TIMEOUT'}
# 原生路径的运行状态；UNKNOWN 与 MANUAL_REQUIRED 必须人工核查后才能再次运行。
ACTIVE_NATIVE={'QUEUED','PREPARING','RUNNING','STOPPING','RECONCILING'}
FINAL_NATIVE={'COMPLETED','FAILED','TIMEOUT','CANCELLED','UNKNOWN','MANUAL_REQUIRED'}
# 需要人工核查的状态：保留钱包锁与状态锁，禁止自动重放。
REVIEW_NATIVE={'UNKNOWN','MANUAL_REQUIRED'}
BLOCKING_NATIVE={'UNKNOWN','MANUAL_REQUIRED'}
NATIVE_NO_REPORT='NO_REPORT'


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


def release_state_lock(db,execution):
    """结束一次原生运行后释放状态空间锁；UNKNOWN 保留锁等待人工核查。"""
    state=db.scalar(select(ProjectScriptState).where(ProjectScriptState.locked_execution_id==execution.id))
    if state:
        state.locked_execution_id=None;state.updated_at=time.time()


def enqueue_native_db(db,task_id,request_key):
    """原生手动运行入队：请求键即幂等键，运行定义与本次输入在此固定。"""
    from backend import runtime_inputs
    from packages.runtime_protocol import NativeRuntimeSpec
    if not settings.native_execution_enabled:
        raise ValueError('原生执行开关未启用（AIRDROP_NATIVE_EXECUTION_ENABLED）')
    if not request_key:
        raise ValueError('原生运行必须携带请求键')
    task=db.get(Task,task_id)
    if not task or not task.enabled:raise ValueError('任务不存在或已停用')
    if not task.script_feature_id:raise ValueError('原生运行必须绑定已审核脚本功能')
    if emergency(db):raise ValueError('Emergency Stop 已启用')
    version=db.get(ScriptVersion,task.script_version_id)
    if not version or not version.manifest:raise ValueError('脚本尚未适配')
    if version.analysis.get('secret_findings'):raise ValueError('源代码含硬编码凭证，禁止执行；请移除后重新上传')
    feature=db.get(ScriptFeature,task.script_feature_id)
    if feature is None or feature.version_id!=version.id:raise ValueError('功能未审核或不属于所选版本')
    if feature.runtime_status!='APPROVED' or not feature.runtime_review_sha256:
        raise ValueError('运行定义尚未通过运行审核')
    try:spec=NativeRuntimeSpec.model_validate(feature.runtime_spec or {})
    except ValueError:raise ValueError('运行定义无效，请重新审核') from None
    notes=spec.readiness()
    if notes:raise ValueError('运行定义仍有未解决项：'+'；'.join(notes[:3]))
    build=db.get(RuntimeBuild,feature.runtime_build_id) if feature.runtime_build_id else None
    if build is None or build.status!='READY' or not build.image_id:
        raise ValueError('运行环境尚未构建完成，请先准备依赖环境')
    project=db.get(Project,task.project_id)
    if project is None:raise ValueError('项目不存在')
    period='manual:'+request_key
    existing=db.scalar(select(Execution).where(Execution.task_id==task_id,Execution.period==period))
    if existing:return existing,False
    active=db.scalar(select(Execution).where(Execution.task_id==task_id,Execution.execution_backend=='native',
                                             Execution.status.in_(ACTIVE_NATIVE)).order_by(Execution.created_at).limit(1))
    if active:return active,False
    blocked=db.scalar(select(Execution.id).where(Execution.task_id==task_id,Execution.execution_backend=='native',
                                                 Execution.status.in_(BLOCKING_NATIVE)).limit(1))
    if blocked:raise ValueError('存在结果未知或待核查的运行，请先完成核查再启动新运行')
    holder=db.scalar(select(ProjectScriptState).where(ProjectScriptState.project_id==project.id,
                                                      ProjectScriptState.package_id==version.package_id,
                                                      ProjectScriptState.locked_execution_id.is_not(None)))
    if holder is not None and holder.locked_execution_id:
        other=db.get(Execution,holder.locked_execution_id)
        if other is not None and (other.status in ACTIVE_NATIVE or other.status in BLOCKING_NATIVE
                                  or other.state_status == 'PENDING_REVIEW'):
            raise ValueError('该脚本状态正在被另一次运行写入，请等待其结束')
    wallet_ids=list((task.wallet_selection or {}).get('wallet_ids') or [])
    wallets=[db.get(Wallet,identity) for identity in wallet_ids]
    wallets=[wallet for wallet in wallets if wallet is not None and wallet.enabled]
    if not wallets:raise ValueError('任务没有可用的钱包选择，请在配置任务时选择钱包')
    declared_chain=spec.wallet_binding.chain if spec.wallet_binding else None
    if declared_chain and any(wallet.chain!=declared_chain for wallet in wallets):
        raise ValueError('本次钱包包含与运行定义不一致的链，请分开绑定')
    parameters=runtime_inputs.parameter_values(spec,task.feature_config_snapshot or {})
    execution=Execution(task_id=task.id,project_id=project.id,script_version_id=version.id,period=period,
                        execution_backend='native',request_key=request_key,state_status='',
                        runtime_snapshot={'runtime':spec.model_dump(mode='json'),'parameters':parameters,
                                          'feature_id':feature.id,'runtime_review_sha256':feature.runtime_review_sha256,
                                          'runtime_build_id':build.id,'image_id':build.image_id,
                                          'wallet_ids':[wallet.id for wallet in wallets],'frozen_at':time.time()})
    db.add(execution);db.flush()
    for wallet in wallets:
        db.add(WalletTaskResult(execution_id=execution.id,wallet_id=wallet.id,address=wallet.address,
                                chain=wallet.chain,name=wallet.name))
    db.flush()
    frozen=runtime_inputs.freeze(db,execution,spec,parameters)
    execution.runtime_snapshot={**execution.runtime_snapshot,'input_sha256':frozen['input_sha256']}
    db.add(AuditLog(action='execution.enqueue_native',target=execution.id,
                    detail={'request_key':request_key,'input_sha256':frozen['input_sha256'],
                            'wallets':len(wallets),'image_id':build.image_id}))
    return execution,True


def enqueue_native(task_id,request_key):
    """原生运行入队；摘要计算与快照写入需要工件锁，先锁工件再锁数据库。"""
    from backend.storage import artifact_lock
    with artifact_lock(),locked_db() as db:
        execution,created=enqueue_native_db(db,task_id,request_key)
        return execution.id,created


def recover_db(db,now):
    expired=list(db.scalars(select(Execution).where(Execution.status.in_(['RUNNING','PREPARING','STOPPING']),
                                                   Execution.lease_until<now)))
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
        # 原生运行由 claim_native 领取；这里只处理旧模拟路径。
        for e in db.scalars(select(Execution).where(Execution.status=='QUEUED',Execution.available_at<=now,
                                                   Execution.execution_backend!='native')
                            .order_by(Execution.created_at).limit(100)):
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


def claim_native(worker_id):
    """领取一次原生运行：固定状态代次、取得钱包锁并建立运行尝试。"""
    from packages.runtime_protocol import NativeRuntimeSpec
    from backend import runtime_state
    now=time.time()
    with locked_db() as db:
        worker=db.get(Worker,worker_id)
        if not worker:worker=Worker(id=worker_id,heartbeat_at=now,status='ONLINE');db.add(worker)
        worker.heartbeat_at=now;worker.status='ONLINE'
        recover_db(db,now)
        if not settings.native_execution_enabled or emergency(db):return None
        active=db.scalar(select(func.count()).select_from(Execution).where(
            Execution.execution_backend=='native',Execution.status.in_(['PREPARING','RUNNING','STOPPING'])))
        if active>=settings.native_global_concurrency:return None
        queued=select(Execution).where(Execution.status=='QUEUED',Execution.execution_backend=='native',
                                       Execution.available_at<=now).order_by(Execution.created_at).limit(50)
        for e in db.scalars(queued):
            if e.cancel_requested:
                e.status='CANCELLED';e.finished_at=now
                e.state_status='UNCOLLECTED' if e.state_status=='LOCKED' else e.state_status
                for r in db.scalars(select(WalletTaskResult).where(WalletTaskResult.execution_id==e.id)):
                    r.status='CANCELLED';r.message='启动前已取消'
                release_state_lock(db,e)
                continue
            try:spec=NativeRuntimeSpec.model_validate((e.runtime_snapshot or {}).get('runtime') or {})
            except ValueError:
                e.status='FAILED';e.finished_at=now;e.error='运行定义快照无效，请重新配置任务并再次运行'
                continue
            wallet_ids=list(db.scalars(select(WalletTaskResult.wallet_id).where(WalletTaskResult.execution_id==e.id)))
            if any(db.get(WalletLock,w) for w in wallet_ids):continue
            try:runtime_state.lock(db,e,spec)
            except HTTPException:continue
            for w in wallet_ids:db.add(WalletLock(wallet_id=w,execution_id=e.id))
            e.status='PREPARING';e.worker_id=worker_id;e.lease_until=now+settings.lease_seconds;e.started_at=now
            attempt=ExecutionAttempt(execution_id=e.id,number=e.retry_count+1,lease_generation=e.retry_count)
            db.add(attempt);db.flush()
            e.current_attempt_id=attempt.id
            for r in db.scalars(select(WalletTaskResult).where(WalletTaskResult.execution_id==e.id,
                                                               WalletTaskResult.status=='QUEUED')):
                r.status='RUNNING'
            return e.id,attempt.id
    return None


def mark_running(execution_id,attempt_id,container_id=None):
    """容器启动成功后才进入 RUNNING；同时记录容器句柄供停止与核查使用。"""
    with locked_db() as db:
        e=db.get(Execution,execution_id)
        if not e or e.current_attempt_id!=attempt_id or e.status not in {'PREPARING','RUNNING'}:return False
        e.status='RUNNING';e.lease_until=time.time()+settings.lease_seconds
        if container_id and attempt_id:
            attempt=db.get(ExecutionAttempt,attempt_id)
            if attempt:attempt.runner_handle=str(container_id)[:200]
        return not e.cancel_requested


def finalize_native(execution_id,attempt_id,outcome):
    """结束一次原生运行：进程结果、日志完整性与状态回写分开记录。"""
    now=time.time()
    with locked_db() as db:
        e=db.get(Execution,execution_id)
        if not e or e.current_attempt_id!=attempt_id or e.status not in {'PREPARING','RUNNING','STOPPING'}:return False
        attempt=db.get(ExecutionAttempt,attempt_id)
        exit_code=outcome.get('exit_code')
        attempt.process_exit_code=exit_code
        raw_output_bytes=outcome.get('raw_output_bytes')
        if raw_output_bytes is None:raw_output_bytes=outcome.get('log_bytes')
        if raw_output_bytes is not None:
            attempt.raw_output_bytes=max(int(raw_output_bytes),int(attempt.raw_output_bytes or 0))
        attempt.finished_at=now
        if outcome.get('log_status'):e.log_status=outcome['log_status']
        elif not e.log_status:e.log_status='COMPLETE'
        reported_error=redact(str(outcome.get('error') or ''))[0][:2000]
        if reported_error:
            e.error=(e.error + ('；' if e.error else '') + reported_error)[:2000]
        reason=str(outcome.get('status') or '')
        if e.cancel_requested:status='CANCELLED'
        elif reason in FINAL_NATIVE:status=reason
        elif outcome.get('timed_out'):status='TIMEOUT'
        elif exit_code is None:status='UNKNOWN'
        elif exit_code==0:status='COMPLETED'
        else:status='FAILED'
        e.status=status
        attempt.status=status
        e.finished_at=now;e.lease_until=None
        if e.state_status=='LOCKED' and status not in REVIEW_NATIVE:
            # 声明了状态文件但这次没有提交成功，明确标为待收集。
            e.state_status='UNCOLLECTED'
        message=('运行结束，业务结果见运行日志。' if status=='COMPLETED' else
                 '进程未正常结束，业务结果见运行日志。')
        for r in db.scalars(select(WalletTaskResult).where(WalletTaskResult.execution_id==e.id,
                                                           WalletTaskResult.status.in_(['QUEUED','RUNNING']))):
            # 没有结构化摘要时不生成钱包成功率，只记录“本平台未收到单钱包结果”。
            r.status=NATIVE_NO_REPORT;r.message=message
        needs_review=status in REVIEW_NATIVE or e.state_status=='PENDING_REVIEW'
        if not needs_review:
            db.execute(delete(WalletLock).where(WalletLock.execution_id==e.id))
            release_state_lock(db,e)
        if status not in {'COMPLETED','CANCELLED'}:
            db.add(AuditLog(action='execution.alert',target=e.id,
                            detail={'status':status,'backend':'native','message':'原生运行异常，请核对日志与状态'}))
        # 原生路径不做平台自动重放，也不调用逐钱包补跑。
        return True


def heartbeat(execution_id,attempt_id,worker_id):
    with locked_db() as db:
        e=db.get(Execution,execution_id)
        # STOPPING 期间继续续租，避免停止流程被租约超时误判为 UNKNOWN。
        if not e or e.current_attempt_id!=attempt_id or e.status not in {'RUNNING','STOPPING'} or e.worker_id!=worker_id:return False
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
            if e.execution_backend!='native' and manifest.retry_safe and retry_rows and e.retry_count<task.max_retries and not e.cancel_requested:
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
        if e.execution_backend=='native':
            raise ValueError('原生运行不使用逐钱包补跑；请核对结果后按新请求键再次发起运行')
        manifest=Manifest.model_validate(db.get(ScriptVersion,e.script_version_id).manifest)
        if not manifest.retry_safe:raise ValueError('Manifest 未声明安全重试')
        pending=list(db.scalars(select(WalletTaskResult).where(WalletTaskResult.execution_id==e.id,WalletTaskResult.status.in_(list(RETRYABLE)))))
        if not pending:raise ValueError('没有可重试的钱包')
        for r in pending:r.status='QUEUED'
        e.retry_count+=1;e.status='QUEUED';e.cancel_requested=False;e.available_at=time.time();e.finished_at=None
        return len(pending)
