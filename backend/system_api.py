import time
from datetime import datetime
from zoneinfo import ZoneInfo
from typing import Literal
from fastapi import APIRouter,Depends,HTTPException
from pydantic import BaseModel,Field
from sqlalchemy import select,func
from sqlalchemy.orm import Session
from backend.auth import admin
from backend.config import settings as app_settings
from backend.db import get_db
from backend.models import *
from backend.storage import cipher
from backend.ai import validate_url
from backend.queue import locked_db

router=APIRouter(tags=['系统设置'],dependencies=[Depends(admin)])


@router.get('/dashboard')
def dashboard(db:Session=Depends(get_db)):
    today=datetime.now(ZoneInfo('Asia/Shanghai')).replace(hour=0,minute=0,second=0,microsecond=0).timestamp()
    counts=dict(db.execute(select(Execution.status,func.count()).where(Execution.created_at>=today).group_by(Execution.status)).all())
    return {'projects':db.scalar(select(func.count()).select_from(Project)),'wallets':db.scalar(select(func.count()).select_from(Wallet)),'scripts':db.scalar(select(func.count()).select_from(ScriptPackage)),'today_tasks':sum(counts.values()),'today_success':counts.get('SUCCESS',0),'today_failed':sum(counts.get(s,0) for s in ['FAILED','TIMEOUT','UNKNOWN','MANUAL_REQUIRED']),'running':db.scalar(select(func.count()).select_from(Execution).where(Execution.status=='RUNNING')),'counts':counts}


@router.get('/settings')
def settings_view(db:Session=Depends(get_db)):
    ai=db.get(SystemSetting,'ai');security=db.get(SystemSetting,'security');execution=db.get(SystemSetting,'execution')
    from backend.runtime_builds import controller_status
    native=controller_status() if app_settings.native_execution_enabled else {'available':False,'online':False,
        'reason':'原生执行开关未启用（AIRDROP_NATIVE_EXECUTION_ENABLED）'}
    return {'capabilities':{'execution_enabled':app_settings.execution_enabled,'real_signing_enabled':False,'mode':'management',
                            'native_execution_enabled':app_settings.native_execution_enabled,
                            'native_available':bool(native.get('available')),'native_reason':str(native.get('reason') or '')},
            'ai':{'provider':'compatible','url':ai.value['url'],'model':ai.value['model'],'key_configured':True} if ai else {'provider':'rules','key_configured':False},'security':security.value if security else {'emergency_stop':False,'real_signing_enabled':False},'execution':execution.value if execution else {'global_concurrency':app_settings.global_concurrency},'native':native,'backup':{'wallets':db.scalar(select(func.count()).select_from(Wallet)),'verified':db.scalar(select(func.count()).select_from(Wallet).where(Wallet.backup_verified==True))},'notifications':{'channel':'dashboard','external_delivery_implemented':False},'workers':[{'id':w.id,'status':'ONLINE' if time.time()-w.heartbeat_at<30 else 'OFFLINE','heartbeat_at':w.heartbeat_at} for w in db.scalars(select(Worker))]}


class AIConfig(BaseModel):
    url:str=Field(max_length=500)
    model:str=Field(min_length=1,max_length=100)
    api_key:str=Field(min_length=1,max_length=2000)


@router.put('/settings/ai')
def ai_config(body:AIConfig,db:Session=Depends(get_db),user:User=Depends(admin)):
    try:validate_url(body.url)
    except (ValueError,OSError) as exc:raise HTTPException(422,'AI 地址无法验证或不是允许的公网 HTTPS 地址') from exc
    obj=db.get(SystemSetting,'ai') or SystemSetting(key='ai')
    obj.value={'url':body.url,'model':body.model,'encrypted_key':cipher().encrypt(body.api_key.encode()).decode()}
    db.add(obj);db.add(AuditLog(user_id=user.id,action='settings.ai',target='ai'));db.commit();return {'ok':True}


class SecurityConfig(BaseModel):
    emergency_stop:bool


@router.put('/settings/security')
def security(body:SecurityConfig,user:User=Depends(admin)):
    with locked_db() as db:
        obj=db.get(SystemSetting,'security') or SystemSetting(key='security')
        obj.value={'emergency_stop':body.emergency_stop,'real_signing_enabled':False};db.add(obj)
        if body.emergency_stop:
            for e in db.scalars(select(Execution).where(Execution.status.in_(['RUNNING','QUEUED','PREPARING']))):
                e.cancel_requested=True
                if e.status=='QUEUED':
                    e.status='CANCELLED';e.finished_at=time.time()
                    for r in db.scalars(select(WalletTaskResult).where(WalletTaskResult.execution_id==e.id,WalletTaskResult.status=='QUEUED')):r.status='CANCELLED'
                elif e.execution_backend=='native':
                    e.status='STOPPING'
        db.add(AuditLog(user_id=user.id,action='security.emergency_stop',target='system',detail={'enabled':body.emergency_stop}))
    return {'ok':True}


class ExecutionConfig(BaseModel):
    global_concurrency:int=Field(ge=1,le=2)


@router.put('/settings/execution')
def execution_config(body:ExecutionConfig,db:Session=Depends(get_db),user:User=Depends(admin)):
    obj=db.get(SystemSetting,'execution') or SystemSetting(key='execution');obj.value=body.model_dump();db.add(obj)
    db.add(AuditLog(user_id=user.id,action='settings.execution',target='execution'));db.commit();return {'ok':True}


@router.get('/audit')
def audit(db:Session=Depends(get_db)):
    return [{k:getattr(a,k) for k in ('id','action','target','created_at','detail')} for a in db.scalars(select(AuditLog).order_by(AuditLog.created_at.desc()).limit(200))]


@router.get('/projects/{project_id}/points')
def points(project_id:str,db:Session=Depends(get_db)):
    return [{k:getattr(p,k) for k in ('wallet_id','points','created_at','execution_id')} for p in db.scalars(select(PointSnapshot).where(PointSnapshot.project_id==project_id).order_by(PointSnapshot.created_at.desc()).limit(1000))]
