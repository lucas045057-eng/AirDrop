from typing import Literal
from fastapi import APIRouter, Depends, File, Form, HTTPException, Response, UploadFile
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from backend.auth import admin
from backend.db import get_db
from backend.models import AuditLog, User, Wallet, WalletGroup, WalletGroupMember, WalletSecret
from backend import vault

router=APIRouter(prefix='/wallets',tags=['钱包中心'],dependencies=[Depends(admin)])


def public(wallet):
    return {k:getattr(wallet,k) for k in ('id','chain','address','name','tags','enabled','is_test','backup_verified')}


class CreateWallets(BaseModel):
    chain: Literal['EVM','SOLANA']='EVM'
    count: int = Field(default=10,ge=1,le=1000)
    group_name: str | None = Field(default=None,min_length=1,max_length=100)
    group_id: str | None = Field(default=None,min_length=1,max_length=32)


class ImportWallets(BaseModel):
    chain: Literal['EVM','SOLANA']
    private_keys: list[str] = Field(min_length=1,max_length=1000)
    group_name: str | None = Field(default=None,min_length=1,max_length=100)
    group_id: str | None = Field(default=None,min_length=1,max_length=32)
    test_only: bool = True


def group(db,name):
    obj=db.scalar(select(WalletGroup).where(WalletGroup.name==name))
    if not obj:
        obj=WalletGroup(name=name);db.add(obj);db.flush()
    return obj


def resolve_group(db,group_id,name,default_name=None):
    if group_id is not None:
        if name is not None:
            raise HTTPException(422,'请选择已有分组或填写新分组名称，不能同时指定两者')
        if not 1 <= len(group_id) <= 32:
            raise HTTPException(422,'分组选择无效')
        target=db.get(WalletGroup,group_id)
        if not target:
            raise HTTPException(404,'所选钱包分组已不存在，请刷新后重新选择')
        return target
    normalized=(name if name is not None else default_name or '').strip()
    if not 1 <= len(normalized) <= 100:
        raise HTTPException(422,'分组名称须为 1–100 个字符，不能只包含空格')
    return group(db,normalized)


def add_wallet(db,chain,address,private_key,group_obj,is_test=True,name=''):
    obj=db.scalar(select(Wallet).where(Wallet.chain==chain,Wallet.address==address))
    created=False
    if not obj:
        obj=Wallet(chain=chain,address=address,name=name,is_test=is_test)
        db.add(obj);db.flush()
        db.add(WalletSecret(wallet_id=obj.id,ciphertext=vault.encrypt_wallet(obj,private_key)))
        created=True
    if not db.get(WalletGroupMember,(group_obj.id,obj.id)):
        db.add(WalletGroupMember(group_id=group_obj.id,wallet_id=obj.id))
    return obj,created


@router.post('/generate')
def create(body:CreateWallets,db:Session=Depends(get_db),user:User=Depends(admin)):
    target=resolve_group(db,body.group_id,body.group_name,'默认分组')
    for i in range(body.count):
        address,private=vault.generate(body.chain)
        add_wallet(db,body.chain,address,private,target,name=f'{target.name} #{i+1}')
    db.add(AuditLog(user_id=user.id,action='wallet.generate',target=target.id,detail={'count':body.count,'chain':body.chain}))
    db.commit()
    return {'created':body.count,'group_id':target.id,'backup_required':True,'message':'请下载加密备份并进行恢复验证；平台尚未开放脚本执行或链上交易。'}


@router.post('/import')
def import_wallets(body:ImportWallets,db:Session=Depends(get_db),user:User=Depends(admin)):
    try:
        records=[vault.identity(body.chain,value) for value in body.private_keys]
    except ValueError as exc:
        raise HTTPException(422,str(exc))
    target=resolve_group(db,body.group_id,body.group_name)
    count=0
    for address,private in records:
        _,created=add_wallet(db,body.chain,address,private,target,body.test_only)
        count+=created
    db.add(AuditLog(user_id=user.id,action='wallet.import',target=target.id,detail={'count':count}))
    db.commit()
    return {'created':count,'duplicates':len(records)-count,'group_id':target.id,'backup_required':True}


@router.post('/import-file')
async def import_file(file:UploadFile=File(...),chain:str=Form(...),group_name:str|None=Form(None),mapping_json:str=Form(...),group_id:str|None=Form(None),db:Session=Depends(get_db),user:User=Depends(admin)):
    import json
    from adapters.wallets import decode
    suffix=(file.filename or '').rsplit('.',1)[-1].upper()
    try:
        mapping=json.loads(mapping_json)
        if not isinstance(mapping,dict) or any(v not in {'address','private_key','name','chain','invite_code'} for v in mapping.values()):raise ValueError('字段映射无效')
        records=decode(await file.read(2*1024*1024+1),suffix,mapping,chain)
        if any('private_key' not in r for r in records):raise ValueError('导入文件缺少私钥字段；平台不猜测 account 字段')
        pairs=[vault.identity(chain,r['private_key']) for r in records]
        if any('address' in r and r['address']!=pair[0] for r,pair in zip(records,pairs)):raise ValueError('私钥与地址不匹配')
    except (ValueError,TypeError) as exc:raise HTTPException(422,str(exc))
    target=resolve_group(db,group_id,group_name);count=0
    for record,(address,key) in zip(records,pairs):
        _,created=add_wallet(db,chain,address,key,target,False,record.get('name',''))
        count+=created
    db.add(AuditLog(user_id=user.id,action='wallet.import.file',target=target.id,detail={'count':count}));db.commit()
    return {'created':count,'group_id':target.id,'backup_required':True}


@router.get('')
def list_wallets(q:str='',group_id:str|None=None,db:Session=Depends(get_db)):
    query=select(Wallet).order_by(Wallet.created_at)
    if q:query=query.where(Wallet.address.contains(q)|Wallet.name.contains(q))
    if group_id:query=query.join(WalletGroupMember).where(WalletGroupMember.group_id==group_id)
    return [public(w) for w in db.scalars(query.limit(1000))]


@router.get('/groups')
def groups(db:Session=Depends(get_db)):
    return [{'id':g.id,'name':g.name,'count':db.scalar(select(func.count()).select_from(WalletGroupMember).where(WalletGroupMember.group_id==g.id))} for g in db.scalars(select(WalletGroup))]


class GroupBody(BaseModel):
    name:str=Field(min_length=1,max_length=100)
    wallet_ids:list[str]=Field(default_factory=list,max_length=1000)


@router.post('/groups')
def create_group(body:GroupBody,db:Session=Depends(get_db)):
    for wallet_id in body.wallet_ids:
        if not db.get(Wallet,wallet_id):raise HTTPException(404,'钱包不存在')
    target=group(db,body.name)
    for wallet_id in set(body.wallet_ids):
        if not db.get(WalletGroupMember,(target.id,wallet_id)):
            db.add(WalletGroupMember(group_id=target.id,wallet_id=wallet_id))
    db.commit()
    return {'id':target.id}


class UpdateWallet(BaseModel):
    enabled:bool
    tags:list[str]=Field(default_factory=list,max_length=20)
    name:str=Field(default='',max_length=100)


@router.put('/{wallet_id}')
def update(wallet_id:str,body:UpdateWallet,db:Session=Depends(get_db),user:User=Depends(admin)):
    wallet=db.get(Wallet,wallet_id)
    if not wallet:raise HTTPException(404,'钱包不存在')
    wallet.enabled, wallet.tags, wallet.name=body.enabled,body.tags,body.name
    db.add(AuditLog(user_id=user.id,action='wallet.update',target=wallet_id))
    db.commit();return public(wallet)


class BackupRequest(BaseModel):
    password:str=Field(min_length=12,max_length=200)


@router.post('/backup')
def backup(body:BackupRequest,db:Session=Depends(get_db),user:User=Depends(admin)):
    records=[]
    for wallet in db.scalars(select(Wallet)):
        records.append({**public(wallet),'private_key':vault.decrypt_wallet(wallet,db.get(WalletSecret,wallet.id).ciphertext)})
    data=vault.seal_backup(records,body.password)
    db.add(AuditLog(user_id=user.id,action='wallet.backup',target='all',detail={'count':len(records)}));db.commit()
    return Response(data,media_type='application/octet-stream',headers={'Content-Disposition':'attachment; filename="airdrop-wallet-backup.enc"','Cache-Control':'no-store'})


@router.post('/restore')
async def restore(file:UploadFile=File(...),password:str=Form(...),verify_only:bool=Form(True),db:Session=Depends(get_db),user:User=Depends(admin)):
    data=await file.read(2*1024*1024+1)
    if len(data)>2*1024*1024:raise HTTPException(413,'备份过大')
    try:records=vault.open_backup(data,password)
    except ValueError as exc:raise HTTPException(422,str(exc))
    restored=0
    target=group(db,'已恢复钱包') if not verify_only else None
    for record in records:
        wallet=db.scalar(select(Wallet).where(Wallet.chain==record['chain'],Wallet.address==record['address']))
        if wallet:
            existing=vault.decrypt_wallet(wallet,db.get(WalletSecret,wallet.id).ciphertext)
            if vault.identity(wallet.chain,existing)!=vault.identity(wallet.chain,record['private_key']):
                raise HTTPException(422,'本地钱包与备份不匹配')
            wallet.backup_verified=True
        elif not verify_only:
            wallet,created=add_wallet(db,record['chain'],record['address'],record['private_key'],target,record.get('is_test',False),record.get('name',''))
            wallet.backup_verified=True;restored+=created
    db.add(AuditLog(user_id=user.id,action='wallet.restore.verify' if verify_only else 'wallet.restore',target='all',detail={'count':len(records),'restored':restored}));db.commit()
    return {'verified':len(records),'restored':restored,'verify_only':verify_only}
