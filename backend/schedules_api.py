from fastapi import APIRouter,Depends,HTTPException
from pydantic import BaseModel,Field,field_validator
from typing import Literal
from zoneinfo import ZoneInfo,ZoneInfoNotFoundError
from sqlalchemy import select
from sqlalchemy.orm import Session
from backend.auth import admin
from backend.db import get_db
from backend.models import Task,Schedule,ScheduleOccurrence,AuditLog,User
from backend.scheduler import next_run
from backend.config import settings

router=APIRouter(prefix='/schedules',tags=['自动调度'],dependencies=[Depends(admin)])


class ScheduleBody(BaseModel):
    task_id:str
    timezone:str='Asia/Shanghai'
    hour:int=Field(ge=0,le=23)
    minute:int=Field(ge=0,le=59)
    weekdays:list[int]=Field(default_factory=lambda:list(range(7)),min_length=1,max_length=7)
    enabled:bool=False
    misfire_policy:Literal['skip','catch_up_once']='skip'
    @field_validator('timezone')
    @classmethod
    def zone(cls,v):
        try:ZoneInfo(v)
        except ZoneInfoNotFoundError:raise ValueError('无效时区')
        return v
    @field_validator('weekdays')
    @classmethod
    def days(cls,v):
        if any(d<0 or d>6 for d in v) or len(set(v))!=len(v):raise ValueError('星期须为不重复的 0–6')
        return v


def save(db,obj,body):
    if body.enabled and not settings.execution_enabled:
        raise HTTPException(409,'管理模式只能保存停用的计划，自动执行尚未开放')
    if not db.get(Task,body.task_id):raise HTTPException(404,'任务不存在')
    for k,v in body.model_dump().items():setattr(obj,k,v)
    obj.next_run_at=next_run(body.timezone,body.hour,body.minute,body.weekdays)
    db.add(obj);db.flush()


@router.get('')
def schedules(db:Session=Depends(get_db)):
    return [{k:getattr(s,k) for k in ('id','task_id','timezone','hour','minute','weekdays','enabled','misfire_policy','next_run_at','last_run_at')} for s in db.scalars(select(Schedule))]


@router.post('')
def create(body:ScheduleBody,db:Session=Depends(get_db),user:User=Depends(admin)):
    obj=Schedule();save(db,obj,body)
    db.add(AuditLog(user_id=user.id,action='schedule.create',target=obj.id,detail={'enabled':body.enabled}));db.commit();return {'id':obj.id}


@router.put('/{schedule_id}')
def update(schedule_id:str,body:ScheduleBody,db:Session=Depends(get_db),user:User=Depends(admin)):
    obj=db.get(Schedule,schedule_id)
    if not obj:raise HTTPException(404,'计划不存在')
    save(db,obj,body);db.add(AuditLog(user_id=user.id,action='schedule.update',target=obj.id,detail={'enabled':body.enabled}));db.commit();return {'ok':True}


@router.get('/history')
def history(db:Session=Depends(get_db)):
    return [{k:getattr(s,k) for k in ('id','schedule_id','due_at','status','execution_id','message')} for s in db.scalars(select(ScheduleOccurrence).order_by(ScheduleOccurrence.due_at.desc()).limit(200))]
