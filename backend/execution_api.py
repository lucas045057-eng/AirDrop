import io
import time
import zipfile
from typing import Literal
from fastapi import APIRouter,Depends,HTTPException
from pydantic import BaseModel,Field
from sqlalchemy import select,func,delete
from sqlalchemy.orm import Session
from starlette.responses import StreamingResponse
from backend.auth import admin
from backend.db import get_db
from backend.models import *
from backend.queue import enqueue,enqueue_native,retry,locked_db
from packages.protocol import Manifest
from packages.runtime_protocol import NativeRuntimeSpec
from backend import runtime_inputs,runtime_state
from backend.storage import artifact_lock,decode_files,read_blob
from backend.uploads import redact,safe_path
from backend import runtime_logs

router=APIRouter(tags=['执行中心'],dependencies=[Depends(admin)])


class TaskBody(BaseModel):
    project_id:str
    script_version_id:str
    name:str=Field(min_length=1,max_length=200)
    batch_size:int=Field(default=100,ge=1,le=1000)
    max_retries:int=Field(default=0,ge=0,le=3)
    script_feature_id:str|None=Field(default=None,max_length=32)
    feature_parameters:dict=Field(default_factory=dict)
    wallet_input:dict|None=None
    execution_backend:str=Field(default='legacy_mock',pattern='^(legacy_mock|native)$')
    wallet_ids:list[str]|None=Field(default=None,max_length=1000)


@router.post('/tasks')
def create_task(body:TaskBody,db:Session=Depends(get_db),user:User=Depends(admin)):
    with artifact_lock():
        return _create_task(body,db,user)


def _create_task(body,db,user):
    p=db.get(Project,body.project_id);v=db.get(ScriptVersion,body.script_version_id)
    if not p or not v:raise HTTPException(404,'项目或脚本版本不存在')
    if not v.manifest:raise HTTPException(422,'须先适配脚本')
    m=Manifest.model_validate(v.manifest)
    snapshot=None
    if not body.script_feature_id and db.scalar(select(ScriptFeature.id).where(ScriptFeature.version_id==v.id).limit(1)):
        raise HTTPException(422,'多功能版本须在智能分析页选择具体已审核功能后创建任务')
    if body.script_feature_id:
        from backend.feature_reviews import task_snapshot
        feature=db.get(ScriptFeature,body.script_feature_id)
        if not feature:raise HTTPException(404,'功能不存在')
        if body.wallet_input is not None:
            from packages.feature_protocol import WalletInput
            try:wallet_input=WalletInput.model_validate(body.wallet_input).model_dump()
            except ValueError:raise HTTPException(422,'钱包输入映射无效') from None
        else:wallet_input=None
        snapshot=task_snapshot(db,feature,v,body.feature_parameters,wallet_input)
    elif body.feature_parameters or body.wallet_input:
        raise HTTPException(422,'功能参数必须绑定已审核功能')
    wallet_selection=None
    if body.execution_backend=='native':
        if not body.script_feature_id:raise HTTPException(422,'原生运行必须绑定已审核脚本功能')
        from backend.feature_reviews import parse_runtime
        feature=db.get(ScriptFeature,body.script_feature_id)
        if not feature.runtime_spec:raise HTTPException(409,'该功能尚未生成运行定义，请先提交并审核运行定义')
        parse_runtime(feature.runtime_spec)
        if not body.wallet_ids:
            raise HTTPException(422,'原生运行必须在配置任务时明确选择本次钱包')
        wallets=db.scalars(select(Wallet).join(ProjectWalletBinding).where(
            ProjectWalletBinding.project_id==p.id,Wallet.enabled==True)).all()
        available={wallet.id for wallet in wallets}
        chosen=list(dict.fromkeys(body.wallet_ids))
        unknown=[identity for identity in chosen if identity not in available]
        if unknown:raise HTTPException(422,'所选钱包不在该项目绑定中：'+', '.join(unknown[:5]))
        wallet_selection={'wallet_ids':chosen}
    workflow=None
    if m.execution_mode=='workflow':
        workflow=Workflow(project_id=p.id,name=body.name);db.add(workflow);db.flush()
        for i,entry in enumerate(m.steps):db.add(WorkflowStep(workflow_id=workflow.id,position=i,entrypoint=entry))
    task=Task(**body.model_dump(exclude={'feature_parameters','wallet_input','wallet_ids'}),
              feature_config_snapshot=snapshot,wallet_selection=wallet_selection,
              workflow_id=workflow.id if workflow else None);db.add(task);db.flush()
    db.add(AuditLog(user_id=user.id,action='task.create',target=task.id));db.commit()
    return {'id':task.id}


@router.get('/tasks')
def tasks(db:Session=Depends(get_db)):
    return [{k:getattr(t,k) for k in ('id','name','project_id','script_version_id','script_feature_id','feature_config_snapshot','execution_backend','wallet_selection','batch_size','max_retries','enabled')} for t in db.scalars(select(Task))]


class RunBody(BaseModel):
    request_key:str|None=Field(default=None,pattern=r'^[A-Za-z0-9_-]{8,64}$')


@router.post('/tasks/{task_id}/run')
def run_task(task_id:str,body:RunBody|None=None,db:Session=Depends(get_db),user:User=Depends(admin)):
    """手动启动；原生路径以请求键做幂等键，重复点击不会创建第二个运行。"""
    task=db.get(Task,task_id)
    if not task:raise HTTPException(404,'任务不存在')
    if task.execution_backend=='native':
        key=body.request_key if body else None
        if not key:raise HTTPException(422,'原生运行必须携带请求键，请刷新页面后重试')
        try:e,created=enqueue_native(task_id,key)
        except ValueError as exc:raise HTTPException(409,str(exc))
        mode='native'
    else:
        try:e,created=enqueue(task_id)
        except ValueError as exc:raise HTTPException(409,str(exc))
        mode='mock'
    with locked_db() as db:db.add(AuditLog(user_id=user.id,action='task.run',target=e))
    return {'execution_id':e,'created':created,'mode':mode}


@router.post('/tasks/{task_id}/test')
def test_task(task_id:str,user:User=Depends(admin)):
    try:e,created=enqueue(task_id,is_test=True)
    except ValueError as exc:raise HTTPException(409,str(exc))
    with locked_db() as db:db.add(AuditLog(user_id=user.id,action='task.test',target=e))
    return {'execution_id':e,'created':created,'mode':'mock-isolated'}


class RollbackBody(BaseModel):
    script_version_id:str


@router.put('/tasks/{task_id}/version')
def rollback(task_id:str,body:RollbackBody,db:Session=Depends(get_db),user:User=Depends(admin)):
    task=db.get(Task,task_id);version=db.get(ScriptVersion,body.script_version_id)
    if task and task.script_feature_id:raise HTTPException(409,'功能级任务必须按新版本的已审核功能重新创建，不能只切换版本')
    if not task or not version:raise HTTPException(404,'任务或版本不存在')
    old=db.get(ScriptVersion,task.script_version_id)
    if version.package_id!=old.package_id or version.status!='PUBLISHED':raise HTTPException(409,'只能切换到同一脚本包的已发布版本')
    task.script_version_id=version.id;db.add(AuditLog(user_id=user.id,action='task.version.switch',target=task.id,detail={'version':version.id}));db.commit()
    return {'ok':True}


def execution_dict(db,e):
    counts=dict(db.execute(select(WalletTaskResult.status,func.count()).where(WalletTaskResult.execution_id==e.id).group_by(WalletTaskResult.status)).all())
    # runtime_snapshot 只含固定后的运行引用与非敏感参数，界面用它还原参数快照。
    return {**{k:getattr(e,k) for k in ('id','task_id','project_id','script_version_id','period','status','is_test','execution_backend','request_key','state_status','state_review_artifact_id','log_status','state_generation','runtime_snapshot','created_at','started_at','finished_at','retry_count','error')},'task_name':db.get(Task,e.task_id).name,'project_name':db.get(Project,e.project_id).name,'counts':counts,'wallet_count':sum(counts.values())}


@router.get('/executions')
def executions(db:Session=Depends(get_db)):
    return [execution_dict(db,e) for e in db.scalars(select(Execution).order_by(Execution.created_at.desc()).limit(200))]


@router.get('/executions/{execution_id}')
def detail(execution_id:str,db:Session=Depends(get_db)):
    e=db.get(Execution,execution_id)
    if not e:raise HTTPException(404,'执行不存在')
    # attempts 带 log_bytes：界面据此判断是否还有更早的日志可加载。
    candidate=db.get(Artifact,e.state_review_artifact_id) if e.state_review_artifact_id else None
    return {**execution_dict(db,e),'state_review_candidate_sha256':candidate.sha256 if candidate and candidate.kind=='runtime-state-pending' else None,'results':[{k:getattr(r,k) for k in ('wallet_id','address','chain','name','status','message','points','transaction_hash')} for r in db.scalars(select(WalletTaskResult).where(WalletTaskResult.execution_id==e.id))],'attempts':[{k:getattr(a,k) for k in ('id','number','status','process_exit_code','log','log_bytes','started_at','finished_at')} for a in db.scalars(select(ExecutionAttempt).where(ExecutionAttempt.execution_id==e.id).order_by(ExecutionAttempt.number))]}


def state_candidate(db,e):
    if e.execution_backend!='native' or e.state_status!='PENDING_REVIEW' or not e.state_review_artifact_id:
        raise HTTPException(409,'该执行没有等待复核的状态候选')
    candidate=db.get(Artifact,e.state_review_artifact_id)
    if candidate is None or candidate.kind!='runtime-state-pending':
        raise HTTPException(409,'状态候选工件不存在或类型不符')
    return candidate


@router.get('/executions/{execution_id}/state-candidate')
def download_state_candidate(execution_id:str,db:Session=Depends(get_db)):
    e=db.get(Execution,execution_id)
    if e is None:raise HTTPException(404,'执行不存在')
    candidate=state_candidate(db,e)
    try:
        files=decode_files(read_blob(candidate.path,candidate.sha256))
        archive=io.BytesIO()
        with zipfile.ZipFile(archive,'w',compression=zipfile.ZIP_DEFLATED) as output:
            for name,data in sorted(files.items()):
                if safe_path(name)!=name:raise ValueError('invalid path')
                output.writestr(name,data)
    except Exception:
        raise HTTPException(409,'状态候选工件校验失败，请保留执行记录并人工核查') from None
    archive.seek(0)
    return StreamingResponse(iter([archive.getvalue()]),media_type='application/octet-stream',
                             headers={'Content-Disposition':f'attachment; filename="airdrop-{e.id}-state-candidate.zip"'})


class StateReviewBody(BaseModel):
    candidate_sha256:str=Field(pattern='^[0-9a-f]{64}$')
    confirmed:Literal[True]
    note:str=Field(min_length=5,max_length=500)


@router.post('/executions/{execution_id}/state-review/commit')
def commit_state_review(execution_id:str,body:StateReviewBody,user:User=Depends(admin)):
    note=body.note.strip()
    if len(note)<5:raise HTTPException(422,'复核记录至少 5 个字符')
    with artifact_lock(),locked_db() as db:
        e=db.get(Execution,execution_id)
        if e is None:raise HTTPException(404,'执行不存在')
        candidate=state_candidate(db,e)
        if e.status not in {'COMPLETED','FAILED','TIMEOUT','CANCELLED'}:
            raise HTTPException(409,'执行尚未结束或仍需先核对容器，暂不能合并状态候选')
        if candidate.sha256!=body.candidate_sha256:
            raise HTTPException(409,'状态候选摘要已改变，请重新下载并复核')
        snapshot=e.runtime_snapshot or {}
        try:
            spec=NativeRuntimeSpec.model_validate(snapshot.get('runtime') or {})
            produced=decode_files(read_blob(candidate.path,candidate.sha256))
        except Exception:
            raise HTTPException(409,'运行定义或状态候选校验失败，禁止合并') from None
        version=db.get(ScriptVersion,e.script_version_id)
        if version is None:raise HTTPException(409,'脚本版本不存在，禁止合并状态')
        state=runtime_state.space(db,e.project_id,version.package_id,spec.state_schema_version)
        if state is None or state.locked_execution_id!=e.id:
            raise HTTPException(409,'状态空间锁不属于本次执行，请人工核查后再处理')
        try:
            keys=runtime_inputs.state_keys(db,e,spec)
            merged,stats=runtime_state.merge(db,e,spec,produced,keys)
        except (ValueError,HTTPException) as exc:
            detail=exc.detail if isinstance(exc,HTTPException) else str(exc)
            raise HTTPException(409,'状态候选与当前状态无法安全合并：'+str(detail)[:300]) from None
        result=runtime_state.commit(db,e,spec,merged,state.generation)
        if not result.get('committed'):
            raise HTTPException(409,'状态代次已改变，请刷新后重新复核')
        e.state_review_artifact_id=None
        e.error=e.error.replace('状态回写待处理：','状态候选经人工复核：',1)
        clean_note=redact(note)[0][:500]
        db.add(AuditLog(user_id=user.id,action='runtime.state.review.commit',target=e.id,
                        detail={'artifact_id':candidate.id,'artifact_sha256':candidate.sha256,
                                'generation':result['generation'],'files':sorted(produced),
                                'stats':stats,'note':clean_note}))
        return {'ok':True,'state_status':e.state_status,'generation':result['generation']}


@router.post('/executions/{execution_id}/cancel')
def cancel(execution_id:str,user:User=Depends(admin)):
    with locked_db() as db:
        e=db.get(Execution,execution_id)
        if not e:raise HTTPException(404,'执行不存在')
        if e.status not in {'QUEUED','PREPARING','RUNNING','STOPPING'}:raise HTTPException(409,'该执行已结束')
        if e.status=='STOPPING':return {'ok':True,'message':'停止流程已在进行中'}
        e.cancel_requested=True
        if e.status=='QUEUED':
            e.status='CANCELLED';e.finished_at=time.time()
            for r in db.scalars(select(WalletTaskResult).where(WalletTaskResult.execution_id==e.id,WalletTaskResult.status=='QUEUED')):r.status='CANCELLED'
        elif e.execution_backend=='native':
            # 原生路径先进入停止流程：Worker 发送 SIGINT，超时后再强制终止。
            e.status='STOPPING'
        db.add(AuditLog(user_id=user.id,action='execution.cancel',target=e.id))
    return {'ok':True,'message':'已请求停止，等待进程退出与状态收集完成'}


@router.post('/executions/{execution_id}/retry')
def retry_execution(execution_id:str,user:User=Depends(admin)):
    try:count=retry(execution_id)
    except ValueError as exc:raise HTTPException(409,str(exc))
    with locked_db() as db:db.add(AuditLog(user_id=user.id,action='execution.retry',target=execution_id,detail={'wallets':count}))
    return {'queued_wallets':count}


class ReconcileBody(BaseModel):
    confirm_mock_runner_stopped:bool=False
    confirm_container_stopped:bool=False
    note:str=Field(min_length=5,max_length=500)


@router.post('/executions/{execution_id}/reconcile')
def reconcile(execution_id:str,body:ReconcileBody,user:User=Depends(admin)):
    # 这里必须用平台的会话工厂；sqlalchemy.orm.Session 未绑定引擎，不能直接实例化。
    from backend.db import Session as session_factory
    with session_factory() as probe:
        backend=probe.get(Execution,execution_id)
        if not backend:raise HTTPException(404,'执行不存在')
        native=backend.execution_backend=='native'
        attempt_id=backend.current_attempt_id
    if native:
        if not body.confirm_container_stopped:raise HTTPException(422,'请先确认容器已停止并完成状态核查')
        from runner import controller
        from worker import native_runner
        try:state=native_runner.status(attempt_id)
        except controller.ControllerError as exc:
            raise HTTPException(409,'无法确认容器状态，禁止直接解除锁：'+str(exc)) from None
        if state.get('running'):raise HTTPException(409,'容器仍在运行，请先停止容器再完成核查')
    elif not body.confirm_mock_runner_stopped:
        raise HTTPException(422,'请先确认模拟 Runner 已停止对应脚本')
    with locked_db() as db:
        e=db.get(Execution,execution_id)
        if not e or e.status not in {'UNKNOWN','MANUAL_REQUIRED'}:raise HTTPException(409,'该执行无需核查')
        e.status='CANCELLED';e.error='人工核查并终止：'+body.note
        e.state_status='UNCOLLECTED' if e.state_status=='LOCKED' else e.state_status
        if e.current_attempt_id:
            attempt=db.get(ExecutionAttempt,e.current_attempt_id)
            if attempt and attempt.status in {'RUNNING','UNKNOWN','MANUAL_REQUIRED'}:attempt.status='CANCELLED'
        db.execute(delete(WalletLock).where(WalletLock.execution_id==e.id))
        from backend.queue import release_state_lock
        release_state_lock(db,e)
        db.add(AuditLog(user_id=user.id,action='execution.reconcile',target=e.id,detail={'note':body.note}))
    return {'ok':True,'message':'本周期记录保留，不自动再次提交'}


@router.get('/executions/{execution_id}/logs')
def logs(execution_id:str,attempt_id:str|None=None,cursor:int=0,limit_bytes:int|None=None,db:Session=Depends(get_db)):
    """按字节游标增量读取日志；重复分段不重复返回。"""
    e=db.get(Execution,execution_id)
    if not e:raise HTTPException(404,'执行不存在')
    attempt=runtime_logs.attempt_of(db,e,attempt_id)
    return runtime_logs.page(db,e,attempt,cursor,limit_bytes)


@router.get('/executions/{execution_id}/logs/download')
def download_logs(execution_id:str,attempt_id:str|None=None,db:Session=Depends(get_db)):
    e=db.get(Execution,execution_id)
    if not e:raise HTTPException(404,'执行不存在')
    attempt=runtime_logs.attempt_of(db,e,attempt_id)
    name=f'airdrop-{e.id}-{attempt.id}.log'
    return StreamingResponse(runtime_logs.download_chunks(e.id,attempt.id),media_type='text/plain; charset=utf-8',
                             headers={'Content-Disposition':f'attachment; filename="{name}"'})
