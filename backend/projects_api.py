from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from fastapi import APIRouter,Depends,HTTPException
from pydantic import BaseModel,Field,field_validator
from sqlalchemy import select,delete,func
from sqlalchemy.orm import Session
from backend.auth import admin
from backend.db import get_db
from backend.models import Project,ProjectGroupBinding,ProjectWalletBinding,Wallet,WalletGroup,WalletGroupMember,AuditLog,User

router=APIRouter(prefix='/projects',tags=['项目中心'],dependencies=[Depends(admin)])


class ProjectBody(BaseModel):
    name:str=Field(min_length=1,max_length=200)
    description:str=Field(default='',max_length=2000)
    timezone:str='Asia/Shanghai'
    reset_hour:int=Field(default=0,ge=0,le=23)
    concurrency:int=Field(default=1,ge=1,le=8)
    @field_validator('timezone')
    @classmethod
    def timezone_valid(cls,v):
        try:ZoneInfo(v)
        except ZoneInfoNotFoundError:raise ValueError('无效时区')
        return v


def project_dict(db,p):
    return {**{k:getattr(p,k) for k in ('id','name','description','timezone','reset_hour','concurrency')},'wallet_count':db.scalar(select(func.count()).select_from(ProjectWalletBinding).where(ProjectWalletBinding.project_id==p.id)),'group_ids':list(db.scalars(select(ProjectGroupBinding.group_id).where(ProjectGroupBinding.project_id==p.id)))}


@router.get('')
def projects(db:Session=Depends(get_db)):
    return [project_dict(db,p) for p in db.scalars(select(Project).order_by(Project.created_at.desc()))]


@router.post('')
def create(body:ProjectBody,db:Session=Depends(get_db),user:User=Depends(admin)):
    if db.scalar(select(Project).where(Project.name==body.name)):raise HTTPException(409,'项目名称已存在')
    p=Project(**body.model_dump());db.add(p);db.flush()
    db.add(AuditLog(user_id=user.id,action='project.create',target=p.id));db.commit()
    return project_dict(db,p)


class Binding(BaseModel):
    group_ids:list[str]=Field(default_factory=list,max_length=100)
    wallet_ids:list[str]=Field(default_factory=list,max_length=1000)


@router.put('/{project_id}/wallets')
def bind(project_id:str,body:Binding,db:Session=Depends(get_db),user:User=Depends(admin)):
    if not db.get(Project,project_id):raise HTTPException(404,'项目不存在')
    wallet_ids=set(body.wallet_ids)
    for group_id in set(body.group_ids):
        if not db.get(WalletGroup,group_id):raise HTTPException(404,'钱包组不存在')
        wallet_ids.update(db.scalars(select(WalletGroupMember.wallet_id).where(WalletGroupMember.group_id==group_id)))
    if len(wallet_ids)>1000:raise HTTPException(422,'单项目最多绑定 1000 钱包')
    if any(not db.get(Wallet,w) for w in wallet_ids):raise HTTPException(404,'钱包不存在')
    db.execute(delete(ProjectWalletBinding).where(ProjectWalletBinding.project_id==project_id))
    db.execute(delete(ProjectGroupBinding).where(ProjectGroupBinding.project_id==project_id))
    for w in wallet_ids:db.add(ProjectWalletBinding(project_id=project_id,wallet_id=w))
    for g in set(body.group_ids):db.add(ProjectGroupBinding(project_id=project_id,group_id=g))
    db.add(AuditLog(user_id=user.id,action='project.bind',target=project_id,detail={'wallet_count':len(wallet_ids)}));db.commit()
    return {'wallet_count':len(wallet_ids),'snapshot_policy':'领取任务时固定地址快照；分组变更须重新绑定'}
