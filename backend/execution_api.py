import time
from fastapi import APIRouter,Depends,HTTPException
from pydantic import BaseModel,Field
from sqlalchemy import select,func,delete
from sqlalchemy.orm import Session
from backend.auth import admin
from backend.db import get_db
from backend.models import *
from backend.queue import enqueue,retry,locked_db
from packages.protocol import Manifest

router=APIRouter(tags=['执行中心'],dependencies=[Depends(admin)])


class TaskBody(BaseModel):
    project_id:str
    script_version_id:str
    name:str=Field(min_length=1,max_length=200)
    batch_size:int=Field(default=100,ge=1,le=1000)
    max_retries:int=Field(default=0,ge=0,le=3)


@router.post('/tasks')
def create_task(body:TaskBody,db:Session=Depends(get_db),user:User=Depends(admin)):
    p=db.get(Project,body.project_id);v=db.get(ScriptVersion,body.script_version_id)
    if not p or not v:raise HTTPException(404,'项目或脚本版本不存在')
    if not v.manifest:raise HTTPException(422,'须先适配脚本')
    m=Manifest.model_validate(v.manifest)
    workflow=None
    if m.execution_mode=='workflow':
        workflow=Workflow(project_id=p.id,name=body.name);db.add(workflow);db.flush()
        for i,entry in enumerate(m.steps):db.add(WorkflowStep(workflow_id=workflow.id,position=i,entrypoint=entry))
    task=Task(**body.model_dump(),workflow_id=workflow.id if workflow else None);db.add(task);db.flush()
    db.add(AuditLog(user_id=user.id,action='task.create',target=task.id));db.commit()
    return {'id':task.id}


@router.get('/tasks')
def tasks(db:Session=Depends(get_db)):
    return [{k:getattr(t,k) for k in ('id','name','project_id','script_version_id','batch_size','max_retries','enabled')} for t in db.scalars(select(Task))]


@router.post('/tasks/{task_id}/run')
def run_task(task_id:str,user:User=Depends(admin)):
    try:e,created=enqueue(task_id)
    except ValueError as exc:raise HTTPException(409,str(exc))
    with locked_db() as db:db.add(AuditLog(user_id=user.id,action='task.run',target=e))
    return {'execution_id':e,'created':created,'mode':'mock'}


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
    if not task or not version:raise HTTPException(404,'任务或版本不存在')
    old=db.get(ScriptVersion,task.script_version_id)
    if version.package_id!=old.package_id or version.status!='PUBLISHED':raise HTTPException(409,'只能切换到同一脚本包的已发布版本')
    task.script_version_id=version.id;db.add(AuditLog(user_id=user.id,action='task.version.switch',target=task.id,detail={'version':version.id}));db.commit()
    return {'ok':True}


def execution_dict(db,e):
    counts=dict(db.execute(select(WalletTaskResult.status,func.count()).where(WalletTaskResult.execution_id==e.id).group_by(WalletTaskResult.status)).all())
    return {**{k:getattr(e,k) for k in ('id','task_id','project_id','script_version_id','period','status','is_test','created_at','started_at','finished_at','retry_count','error')},'task_name':db.get(Task,e.task_id).name,'project_name':db.get(Project,e.project_id).name,'counts':counts,'wallet_count':sum(counts.values())}


@router.get('/executions')
def executions(db:Session=Depends(get_db)):
    return [execution_dict(db,e) for e in db.scalars(select(Execution).order_by(Execution.created_at.desc()).limit(200))]


@router.get('/executions/{execution_id}')
def detail(execution_id:str,db:Session=Depends(get_db)):
    e=db.get(Execution,execution_id)
    if not e:raise HTTPException(404,'执行不存在')
    return {**execution_dict(db,e),'results':[{k:getattr(r,k) for k in ('wallet_id','address','chain','name','status','message','points','transaction_hash')} for r in db.scalars(select(WalletTaskResult).where(WalletTaskResult.execution_id==e.id))],'attempts':[{k:getattr(a,k) for k in ('id','number','status','process_exit_code','log','started_at','finished_at')} for a in db.scalars(select(ExecutionAttempt).where(ExecutionAttempt.execution_id==e.id).order_by(ExecutionAttempt.number))]}


@router.post('/executions/{execution_id}/cancel')
def cancel(execution_id:str,user:User=Depends(admin)):
    with locked_db() as db:
        e=db.get(Execution,execution_id)
        if not e:raise HTTPException(404,'执行不存在')
        if e.status not in {'QUEUED','RUNNING'}:raise HTTPException(409,'该执行已结束')
        e.cancel_requested=True
        if e.status=='QUEUED':
            e.status='CANCELLED';e.finished_at=time.time()
            for r in db.scalars(select(WalletTaskResult).where(WalletTaskResult.execution_id==e.id,WalletTaskResult.status=='QUEUED')):r.status='CANCELLED'
        db.add(AuditLog(user_id=user.id,action='execution.cancel',target=e.id))
    return {'ok':True}


@router.post('/executions/{execution_id}/retry')
def retry_execution(execution_id:str,user:User=Depends(admin)):
    try:count=retry(execution_id)
    except ValueError as exc:raise HTTPException(409,str(exc))
    with locked_db() as db:db.add(AuditLog(user_id=user.id,action='execution.retry',target=execution_id,detail={'wallets':count}))
    return {'queued_wallets':count}


class ReconcileBody(BaseModel):
    confirm_mock_runner_stopped:bool
    note:str=Field(min_length=5,max_length=500)


@router.post('/executions/{execution_id}/reconcile')
def reconcile(execution_id:str,body:ReconcileBody,user:User=Depends(admin)):
    if not body.confirm_mock_runner_stopped:raise HTTPException(422,'请先确认模拟 Runner 已停止对应脚本')
    with locked_db() as db:
        e=db.get(Execution,execution_id)
        if not e or e.status not in {'UNKNOWN','MANUAL_REQUIRED'}:raise HTTPException(409,'该执行无需核查')
        e.status='CANCELLED';e.error='人工核查并终止：'+body.note
        db.execute(delete(WalletLock).where(WalletLock.execution_id==e.id))
        db.add(AuditLog(user_id=user.id,action='execution.reconcile',target=e.id,detail={'note':body.note}))
    return {'ok':True,'message':'本周期记录保留，不自动再次提交'}
