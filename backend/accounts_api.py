"""项目账号资料 API：上传预览、逐项冲突确认、查询与带修订号的更新。"""
import json
import time
from typing import Literal
from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session
from backend.auth import admin
from backend.db import get_db
from backend.models import AccountImportDraft, AuditLog, Project, ProjectAccount, ScriptPackage, User, Wallet
from backend.storage import artifact_lock
from backend import account_imports

router = APIRouter(tags=['项目账号'], dependencies=[Depends(admin)])


def get_project(db, project_id) -> Project:
    project = db.get(Project, project_id)
    if project is None:
        raise HTTPException(404, '项目不存在')
    return project


def get_package(db, package_id) -> ScriptPackage:
    package = db.get(ScriptPackage, package_id)
    if package is None:
        raise HTTPException(404, '脚本包不存在')
    return package


def draft_view(draft: AccountImportDraft):
    return {'id': draft.id, 'project_id': draft.project_id, 'package_id': draft.package_id,
            'filename': draft.filename, 'field_mapping': draft.field_mapping,
            'preview': draft.preview, 'preview_sha256': draft.preview_sha256,
            'consumed_at': draft.consumed_at, 'result': draft.result, 'expires_at': draft.expires_at}


@router.post('/projects/{project_id}/account-imports')
async def upload_accounts(project_id: str, package_id: str = Form(...), chain: str = Form(...),
                          field_mapping: str = Form(...), file: UploadFile = File(...),
                          db: Session = Depends(get_db), user: User = Depends(admin)):
    """上传项目资料并立即生成遮蔽预览；原文件加密保存为有期限草稿。"""
    get_project(db, project_id)
    get_package(db, package_id)
    try:
        mapping = json.loads(field_mapping)
    except ValueError:
        raise HTTPException(422, '字段映射不是合法 JSON') from None
    body = await file.read(4 * 1024 * 1024 + 1)
    with artifact_lock():
        try:
            draft = account_imports.load_rows(db, project_id, package_id,
                                              {'filename': file.filename, 'content': body, 'chain': chain,
                                               'field_mapping': mapping}, user.id)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None
        db.commit()
        return draft_view(draft)


@router.get('/account-imports/{draft_id}')
def import_preview(draft_id: str, db: Session = Depends(get_db)):
    draft = db.get(AccountImportDraft, draft_id)
    if draft is None:
        raise HTTPException(404, '导入草稿不存在')
    return draft_view(draft)


class CommitBody(BaseModel):
    model_config = ConfigDict(extra='forbid')
    preview_sha256: str = Field(pattern='^[0-9a-f]{64}$')
    decisions: dict[str, Literal['overwrite', 'skip', 'clear']] = Field(default_factory=dict)
    confirmed: Literal[True]


@router.post('/account-imports/{draft_id}/commit')
def commit_import(draft_id: str, body: CommitBody, db: Session = Depends(get_db), user: User = Depends(admin)):
    """一次事务写入；重复确认返回同一结果。"""
    with artifact_lock():
        draft = db.get(AccountImportDraft, draft_id)
        if draft is None:
            raise HTTPException(404, '导入草稿不存在')
        result = account_imports.commit(db, draft, body.preview_sha256, body.decisions, user.id)
        db.commit()
        return {'draft_id': draft.id, **result}


@router.get('/projects/{project_id}/accounts')
def list_accounts(project_id: str, package_id: str, db: Session = Depends(get_db)):
    get_project(db, project_id)
    rows = db.execute(select(ProjectAccount, Wallet).join(Wallet, Wallet.id == ProjectAccount.wallet_id)
                      .where(ProjectAccount.project_id == project_id, ProjectAccount.package_id == package_id)
                      .order_by(ProjectAccount.updated_at.desc())).all()
    return [account_imports.account_view(account, wallet) for account, wallet in rows]


class AccountUpdate(BaseModel):
    model_config = ConfigDict(extra='forbid')
    revision: int = Field(ge=0)
    public_config: dict[str, str] = Field(default_factory=dict)
    clear: list[str] = Field(default_factory=list, max_length=40)
    confirmed: Literal[True]


@router.put('/projects/{project_id}/accounts/{account_id}')
def update_account(project_id: str, account_id: str, body: AccountUpdate,
                   db: Session = Depends(get_db), user: User = Depends(admin)):
    """带修订号的更新：旧修订号不能覆盖新资料。"""
    with artifact_lock():
        account = db.get(ProjectAccount, account_id)
        if account is None or account.project_id != project_id:
            raise HTTPException(404, '账号资料不存在')
        if account.revision != body.revision:
            raise HTTPException(409, '账号资料已被修改，请刷新后重试')
        wallet = db.get(Wallet, account.wallet_id)
        unknown = [name for name in body.clear if account_imports.is_sensitive(name)]
        if unknown:
            raise HTTPException(422, '敏感字段不能在资料编辑中清空：' + ', '.join(unknown))
        merged = {**account.public_config, **account_imports.open_config(account)}
        for name in body.clear:
            merged.pop(name, None)
        for name, value in body.public_config.items():
            if account_imports.is_sensitive(name):
                raise HTTPException(422, '敏感字段请通过导入流程更新：' + name)
            if isinstance(value, str) and len(value) <= 500:
                merged[name] = value
            else:
                raise HTTPException(422, '资料字段必须是 500 字符以内的文本：' + name)
        account.public_config = {name: value for name, value in merged.items() if not account_imports.is_sensitive(name)}
        encrypted = {name: value for name, value in merged.items() if account_imports.is_sensitive(name)}
        account.encrypted_config = account_imports.seal(project_id, account.package_id, wallet.id, encrypted) if encrypted else ''
        account.revision += 1
        account.source = 'manual'
        account.updated_at = time.time()
        db.add(AuditLog(user_id=user.id, action='account.update', target=account.id, detail={'revision': account.revision}))
        db.commit()
        return account_imports.account_view(account, wallet)
