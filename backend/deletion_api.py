"""Confirmed physical removal with foreign-reference and execution-state guards."""
from contextlib import nullcontext
from pathlib import Path
from typing import Annotated, Literal
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import delete, func, or_, select
from sqlalchemy.exc import IntegrityError
from backend.auth import admin
from backend.config import settings
from backend.models import (
    Artifact, AuditLog, Execution, ExecutionAttempt, ExecutionReceipt, GasBudget,
    PointSnapshot, Project, ProjectGroupBinding, ProjectWalletBinding, Schedule,
    ScheduleOccurrence, ScriptAdaptation, ScriptPackage, ScriptVersion, SystemSetting,
    Task, TransactionIntent, TransactionPolicy, User, Wallet, WalletGroup,
    WalletGroupMember, WalletLock, WalletSecret, WalletTaskResult, Workflow, WorkflowStep)
from backend.queue import locked_db
from backend.storage import artifact_lock

router = APIRouter(tags=['永久删除'], dependencies=[Depends(admin)])
TERMINAL = {'SUCCESS', 'PARTIAL_SUCCESS', 'FAILED', 'TIMEOUT', 'CANCELLED'}


class Confirmation(BaseModel):
    confirmed: Literal[True]


class WalletBatchConfirmation(Confirmation):
    wallet_ids: list[Annotated[str, Field(min_length=1, max_length=32)]] = Field(min_length=1, max_length=1000)


WALLET_REFERENCES = (
    ('项目绑定', ProjectWalletBinding), ('执行结果', WalletTaskResult),
    ('积分历史', PointSnapshot), ('执行锁', WalletLock), ('交易凭据', TransactionIntent),
)


def count(db, model, condition):
    return db.scalar(select(func.count()).select_from(model).where(condition))


def guard(db, references):
    used = [f'{label}（{n}）' for label, model, condition in references
            if (n := count(db, model, condition))]
    if used:raise HTTPException(409, '仍被引用，不能删除：' + '、'.join(used) + '。请先解除关联或清理相关记录。')


def remove_rows(db, removed, model, condition):
    removed[model.__tablename__] = db.execute(delete(model).where(condition)).rowcount


def safe_artifact_path(obj):
    path = Path(obj.path)
    root = (settings.data_dir / 'artifacts').resolve()
    if path.is_symlink() or path.parent.resolve() != root or path.name != obj.sha256 + '.enc':
        raise HTTPException(409, '工件路径异常，停止删除，请检查存储配置')
    return path


def remove(kind, identity, user):
    models = {'script': ScriptVersion, 'wallet': Wallet, 'group': WalletGroup,
              'project': Project, 'task': Task, 'schedule': Schedule, 'execution': Execution,
              'ai': SystemSetting}
    removed, files, pending = {}, [], 0
    try:
        with artifact_lock() if kind == 'script' else nullcontext():
            with locked_db() as db:
                obj = db.get(models[kind], identity)
                if not obj:raise HTTPException(404, '记录不存在或已删除')
                if kind == 'script':
                    guard(db, [('任务', Task, Task.script_version_id == identity),
                               ('执行历史', Execution, Execution.script_version_id == identity),
                               ('后续适配版本', ScriptAdaptation, ScriptAdaptation.source_version_id == identity)])
                    artifact_ids = {obj.original_artifact_id, obj.source_artifact_id}
                    package_id = obj.package_id
                    remove_rows(db, removed, ScriptAdaptation, ScriptAdaptation.result_version_id == identity)
                    db.delete(obj);db.flush()
                    for artifact_id in artifact_ids:
                        if count(db, ScriptVersion, or_(ScriptVersion.original_artifact_id == artifact_id,
                                                      ScriptVersion.source_artifact_id == artifact_id)):continue
                        artifact = db.get(Artifact, artifact_id)
                        path = safe_artifact_path(artifact)
                        db.delete(artifact);db.flush()
                        removed['artifacts'] = removed.get('artifacts', 0) + 1
                        if not count(db, Artifact, Artifact.path == str(path)):files.append(path)
                    if not count(db, ScriptVersion, ScriptVersion.package_id == package_id):
                        db.delete(db.get(ScriptPackage, package_id));removed['script_packages'] = 1
                elif kind == 'wallet':
                    guard(db, [(label, model, model.wallet_id == identity) for label, model in WALLET_REFERENCES])
                    remove_rows(db, removed, WalletGroupMember, WalletGroupMember.wallet_id == identity)
                    remove_rows(db, removed, WalletSecret, WalletSecret.wallet_id == identity)
                    db.delete(obj)
                elif kind == 'group':
                    guard(db, [('项目分组绑定', ProjectGroupBinding, ProjectGroupBinding.group_id == identity)])
                    remove_rows(db, removed, WalletGroupMember, WalletGroupMember.group_id == identity)
                    db.delete(obj)
                elif kind == 'project':
                    guard(db, [('任务', Task, Task.project_id == identity),
                               ('执行历史', Execution, Execution.project_id == identity),
                               ('工作流', Workflow, Workflow.project_id == identity),
                               ('积分历史', PointSnapshot, PointSnapshot.project_id == identity),
                               ('交易凭据', TransactionIntent, TransactionIntent.project_id == identity)])
                    for model in (ProjectGroupBinding, ProjectWalletBinding, TransactionPolicy, GasBudget):
                        remove_rows(db, removed, model, model.project_id == identity)
                    db.delete(obj)
                elif kind == 'task':
                    guard(db, [('执行历史', Execution, Execution.task_id == identity),
                               ('定时计划', Schedule, Schedule.task_id == identity)])
                    workflow_id = obj.workflow_id
                    remove_rows(db, removed, ExecutionReceipt, ExecutionReceipt.task_id == identity)
                    db.delete(obj);db.flush()
                    if workflow_id and not count(db, Task, Task.workflow_id == workflow_id):
                        remove_rows(db, removed, WorkflowStep, WorkflowStep.workflow_id == workflow_id)
                        db.delete(db.get(Workflow, workflow_id));removed['workflows'] = 1
                elif kind == 'schedule':
                    remove_rows(db, removed, ScheduleOccurrence, ScheduleOccurrence.schedule_id == identity)
                    db.delete(obj)
                elif kind == 'execution':
                    if obj.status not in TERMINAL:
                        raise HTTPException(409, '只能删除已结束的执行记录；排队、运行中、结果未知或待人工核查的记录须先停止或完成核查。')
                    guard(db, [('钱包执行锁', WalletLock, WalletLock.execution_id == identity),
                               ('定时调度历史', ScheduleOccurrence, ScheduleOccurrence.execution_id == identity)])
                    if not obj.is_test:
                        receipt = db.get(ExecutionReceipt, (obj.task_id, obj.period))
                        if receipt:receipt.execution_id = None
                        else:db.add(ExecutionReceipt(task_id=obj.task_id, period=obj.period,
                                                     execution_id=None, created_at=obj.created_at))
                    # Also clear stale receipt links defensively before removing the execution.
                    for receipt in db.scalars(select(ExecutionReceipt).where(ExecutionReceipt.execution_id == identity)):
                        receipt.execution_id = None
                    db.flush()
                    for model in (PointSnapshot, WalletTaskResult, ExecutionAttempt):
                        remove_rows(db, removed, model, model.execution_id == identity)
                    db.delete(obj)
                elif kind == 'ai':db.delete(obj)
                removed[models[kind].__tablename__] = 1
                db.add(AuditLog(user_id=user.id, action=kind + '.delete', target=identity,
                                detail={'permanent': True, 'removed': removed}))
                db.commit()
            # DB commit precedes filesystem cleanup, while writers remain excluded.
            for path in set(files):
                try:path.unlink(missing_ok=True)
                except OSError:pending += 1
    except IntegrityError as exc:
        raise HTTPException(409, '记录存在关联或刚被其他操作引用，删除已撤销，请刷新后检查关联。') from exc
    return {'ok': True, 'removed': removed, 'artifact_files_pending': pending,
            'warning': '记录已删除，但部分加密工件文件清理失败，请联系管理员检查存储权限。' if pending else ''}


@router.delete('/scripts/{identity}')
def script(identity: str, body: Confirmation, user: User = Depends(admin)):
    return remove('script', identity, user)


@router.delete('/wallets/groups/{identity}')
def group(identity: str, body: Confirmation, user: User = Depends(admin)):
    return remove('group', identity, user)


@router.delete('/wallets/{identity}')
def wallet(identity: str, body: Confirmation, user: User = Depends(admin)):
    return remove('wallet', identity, user)


@router.post('/wallets/bulk-delete')
def wallet_batch(body: WalletBatchConfirmation, user: User = Depends(admin)):
    """Delete eligible selected wallets, reporting protected and missing rows separately."""
    identities = list(dict.fromkeys(body.wallet_ids))
    deleted, blocked, missing = [], [], []
    with locked_db() as db:
        wallets = {w.id: w for w in db.scalars(select(Wallet).where(Wallet.id.in_(identities)))}
        references = {identity: [] for identity in identities}
        for label, model in WALLET_REFERENCES:
            for identity, amount in db.execute(select(model.wallet_id, func.count()).where(
                    model.wallet_id.in_(identities)).group_by(model.wallet_id)):
                references[identity].append({'label': label, 'count': amount})
        for identity in identities:
            wallet = wallets.get(identity)
            if not wallet:
                missing.append(identity);continue
            used = references[identity]
            if used:
                blocked.append({'id': identity, 'name': wallet.name, 'address': wallet.address,
                                'references': used, 'reason': '、'.join(f"{r['label']}（{r['count']}）" for r in used)})
                continue
            # A concurrent external writer on PostgreSQL may add a reference.
            # Roll back only this wallet on a foreign-key conflict, preserving the batch.
            name, address = wallet.name, wallet.address
            try:
                with db.begin_nested():
                    db.execute(delete(WalletGroupMember).where(WalletGroupMember.wallet_id == identity))
                    db.execute(delete(WalletSecret).where(WalletSecret.wallet_id == identity))
                    db.delete(wallet);db.flush()
            except IntegrityError:
                blocked.append({'id': identity, 'name': name, 'address': address, 'references': [],
                                'reason': '刚被其他操作引用，请刷新后检查关联'})
            else:deleted.append(identity)
        db.add(AuditLog(user_id=user.id, action='wallet.bulk_delete', target='selected',
                        detail={'permanent': True, 'requested_count': len(identities), 'deleted_ids': deleted,
                                'blocked_count': len(blocked), 'missing_count': len(missing)}))
    return {'ok': True, 'requested_count': len(identities), 'deleted_count': len(deleted),
            'deleted_ids': deleted, 'blocked': blocked, 'missing_ids': missing}


@router.delete('/projects/{identity}')
def project(identity: str, body: Confirmation, user: User = Depends(admin)):
    return remove('project', identity, user)


@router.delete('/tasks/{identity}')
def task(identity: str, body: Confirmation, user: User = Depends(admin)):
    return remove('task', identity, user)


@router.delete('/schedules/{identity}')
def schedule(identity: str, body: Confirmation, user: User = Depends(admin)):
    return remove('schedule', identity, user)


@router.delete('/executions/{identity}')
def execution(identity: str, body: Confirmation, user: User = Depends(admin)):
    return remove('execution', identity, user)


@router.delete('/settings/ai')
def ai(body: Confirmation, user: User = Depends(admin)):
    return remove('ai', 'ai', user)
