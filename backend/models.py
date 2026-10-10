import time
import uuid
from sqlalchemy import Boolean, CheckConstraint, Float, ForeignKey, Integer, JSON, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column
from backend.db import Base


def uid():
    return uuid.uuid4().hex


class User(Base):
    __tablename__ = "users"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=uid)
    username: Mapped[str] = mapped_column(String(100), unique=True)
    password_hash: Mapped[str] = mapped_column(Text)
    role: Mapped[str] = mapped_column(String(20), default="admin")


class LoginSession(Base):
    __tablename__ = "login_sessions"
    token_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    csrf: Mapped[str] = mapped_column(String(64))
    expires_at: Mapped[float] = mapped_column(Float)


class AuditLog(Base):
    __tablename__ = "audit_logs"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=uid)
    user_id: Mapped[str | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    action: Mapped[str] = mapped_column(String(100), index=True)
    target: Mapped[str] = mapped_column(String(100))
    created_at: Mapped[float] = mapped_column(Float, default=time.time)
    detail: Mapped[dict] = mapped_column(JSON, default=dict)


class ScriptPackage(Base):
    __tablename__ = "script_packages"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=uid)
    name: Mapped[str] = mapped_column(String(200))
    created_at: Mapped[float] = mapped_column(Float, default=time.time)


class Artifact(Base):
    __tablename__ = "artifacts"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=uid)
    path: Mapped[str] = mapped_column(Text)
    sha256: Mapped[str] = mapped_column(String(64), index=True)
    kind: Mapped[str] = mapped_column(String(30))
    created_at: Mapped[float] = mapped_column(Float, default=time.time)


class ScriptVersion(Base):
    __tablename__ = "script_versions"
    __table_args__ = (UniqueConstraint('package_id','version'),)
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=uid)
    package_id: Mapped[str] = mapped_column(ForeignKey('script_packages.id'), index=True)
    version: Mapped[int] = mapped_column(Integer)
    original_artifact_id: Mapped[str] = mapped_column(ForeignKey('artifacts.id'))
    source_artifact_id: Mapped[str] = mapped_column(ForeignKey('artifacts.id'))
    status: Mapped[str] = mapped_column(String(30), default='UPLOADED')
    analysis: Mapped[dict] = mapped_column(JSON)
    manifest: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    tested_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[float] = mapped_column(Float, default=time.time)


class Wallet(Base):
    __tablename__ = 'wallets'
    __table_args__ = (UniqueConstraint('chain','address'),)
    id: Mapped[str] = mapped_column(String(32),primary_key=True,default=uid)
    chain: Mapped[str] = mapped_column(String(10), index=True)
    address: Mapped[str] = mapped_column(String(100),index=True)
    name: Mapped[str] = mapped_column(String(100),default='')
    tags: Mapped[list] = mapped_column(JSON,default=list)
    enabled: Mapped[bool] = mapped_column(Boolean,default=True)
    is_test: Mapped[bool] = mapped_column(Boolean,default=True)
    backup_verified: Mapped[bool] = mapped_column(Boolean,default=False)
    created_at: Mapped[float] = mapped_column(Float,default=time.time)


class WalletSecret(Base):
    __tablename__ = 'wallet_secrets'
    wallet_id: Mapped[str] = mapped_column(ForeignKey('wallets.id',ondelete='CASCADE'),primary_key=True)
    ciphertext: Mapped[str] = mapped_column(Text)


class WalletGroup(Base):
    __tablename__ = 'wallet_groups'
    id: Mapped[str] = mapped_column(String(32),primary_key=True,default=uid)
    name: Mapped[str] = mapped_column(String(100),unique=True)


class WalletGroupMember(Base):
    __tablename__ = 'wallet_group_members'
    group_id: Mapped[str] = mapped_column(ForeignKey('wallet_groups.id',ondelete='CASCADE'),primary_key=True)
    wallet_id: Mapped[str] = mapped_column(ForeignKey('wallets.id',ondelete='CASCADE'),primary_key=True)


class Project(Base):
    __tablename__ = 'projects'
    id: Mapped[str] = mapped_column(String(32),primary_key=True,default=uid)
    name: Mapped[str] = mapped_column(String(200),unique=True)
    description: Mapped[str] = mapped_column(Text,default='')
    timezone: Mapped[str] = mapped_column(String(100),default='Asia/Shanghai')
    reset_hour: Mapped[int] = mapped_column(Integer,default=0)
    concurrency: Mapped[int] = mapped_column(Integer,default=1)
    created_at: Mapped[float] = mapped_column(Float,default=time.time)


class ProjectWalletBinding(Base):
    __tablename__ = 'project_wallet_bindings'
    project_id: Mapped[str] = mapped_column(ForeignKey('projects.id',ondelete='CASCADE'),primary_key=True)
    wallet_id: Mapped[str] = mapped_column(ForeignKey('wallets.id'),primary_key=True)


class ProjectGroupBinding(Base):
    __tablename__ = 'project_group_bindings'
    project_id: Mapped[str] = mapped_column(ForeignKey('projects.id',ondelete='CASCADE'),primary_key=True)
    group_id: Mapped[str] = mapped_column(ForeignKey('wallet_groups.id'),primary_key=True)


class ScriptAdaptation(Base):
    __tablename__='script_adaptations'
    id: Mapped[str]=mapped_column(String(32),primary_key=True,default=uid)
    source_version_id: Mapped[str]=mapped_column(ForeignKey('script_versions.id'))
    result_version_id: Mapped[str]=mapped_column(ForeignKey('script_versions.id'),unique=True)
    provider: Mapped[str]=mapped_column(String(30))
    diff: Mapped[str]=mapped_column(Text)
    plan: Mapped[dict]=mapped_column(JSON)
    approved_by: Mapped[str|None]=mapped_column(ForeignKey('users.id'),nullable=True)
    created_at: Mapped[float]=mapped_column(Float,default=time.time)


class SystemSetting(Base):
    __tablename__='system_settings'
    key: Mapped[str]=mapped_column(String(100),primary_key=True)
    value: Mapped[dict]=mapped_column(JSON)


class Workflow(Base):
    __tablename__='workflows'
    id: Mapped[str]=mapped_column(String(32),primary_key=True,default=uid)
    project_id: Mapped[str]=mapped_column(ForeignKey('projects.id'))
    name: Mapped[str]=mapped_column(String(100))


class WorkflowStep(Base):
    __tablename__='workflow_steps'
    workflow_id: Mapped[str]=mapped_column(ForeignKey('workflows.id'),primary_key=True)
    position: Mapped[int]=mapped_column(Integer,primary_key=True)
    entrypoint: Mapped[str]=mapped_column(String(300))


class Task(Base):
    __tablename__='tasks'
    id: Mapped[str]=mapped_column(String(32),primary_key=True,default=uid)
    project_id: Mapped[str]=mapped_column(ForeignKey('projects.id'),index=True)
    script_version_id: Mapped[str]=mapped_column(ForeignKey('script_versions.id'))
    script_feature_id: Mapped[str|None]=mapped_column(ForeignKey('script_features.id'), nullable=True)
    feature_config_snapshot: Mapped[dict|None]=mapped_column(JSON, nullable=True)
    workflow_id: Mapped[str|None]=mapped_column(ForeignKey('workflows.id'),nullable=True)
    name: Mapped[str]=mapped_column(String(200))
    batch_size: Mapped[int]=mapped_column(Integer,default=100)
    max_retries: Mapped[int]=mapped_column(Integer,default=0)
    enabled: Mapped[bool]=mapped_column(Boolean,default=True)


class Schedule(Base):
    __tablename__='schedules'
    id: Mapped[str]=mapped_column(String(32),primary_key=True,default=uid)
    task_id: Mapped[str]=mapped_column(ForeignKey('tasks.id'),index=True)
    timezone: Mapped[str]=mapped_column(String(100))
    hour: Mapped[int]=mapped_column(Integer)
    minute: Mapped[int]=mapped_column(Integer)
    weekdays: Mapped[list]=mapped_column(JSON,default=lambda:list(range(7)))
    enabled: Mapped[bool]=mapped_column(Boolean,default=False)
    misfire_policy: Mapped[str]=mapped_column(String(20),default='skip')
    next_run_at: Mapped[float]=mapped_column(Float,index=True)
    last_run_at: Mapped[float|None]=mapped_column(Float,nullable=True)


class Execution(Base):
    __tablename__='executions'
    __table_args__=(UniqueConstraint('task_id','period'),)
    id: Mapped[str]=mapped_column(String(32),primary_key=True,default=uid)
    task_id: Mapped[str]=mapped_column(ForeignKey('tasks.id'),index=True)
    project_id: Mapped[str]=mapped_column(ForeignKey('projects.id'),index=True)
    script_version_id: Mapped[str]=mapped_column(ForeignKey('script_versions.id'))
    period: Mapped[str]=mapped_column(String(200))
    status: Mapped[str]=mapped_column(String(30),default='QUEUED',index=True)
    is_test: Mapped[bool]=mapped_column(Boolean,default=False)
    cancel_requested: Mapped[bool]=mapped_column(Boolean,default=False)
    worker_id: Mapped[str|None]=mapped_column(String(100),nullable=True)
    lease_until: Mapped[float|None]=mapped_column(Float,nullable=True,index=True)
    available_at: Mapped[float]=mapped_column(Float,default=time.time)
    current_attempt_id: Mapped[str|None]=mapped_column(String(32),nullable=True)
    retry_count: Mapped[int]=mapped_column(Integer,default=0)
    created_at: Mapped[float]=mapped_column(Float,default=time.time,index=True)
    started_at: Mapped[float|None]=mapped_column(Float,nullable=True)
    finished_at: Mapped[float|None]=mapped_column(Float,nullable=True)
    error: Mapped[str]=mapped_column(Text,default='')


class ExecutionReceipt(Base):
    __tablename__='execution_receipts'
    task_id: Mapped[str]=mapped_column(ForeignKey('tasks.id'),primary_key=True)
    period: Mapped[str]=mapped_column(String(200),primary_key=True)
    execution_id: Mapped[str|None]=mapped_column(ForeignKey('executions.id',ondelete='SET NULL'),nullable=True)
    created_at: Mapped[float]=mapped_column(Float,default=time.time)


class ExecutionAttempt(Base):
    __tablename__='execution_attempts'
    id: Mapped[str]=mapped_column(String(32),primary_key=True,default=uid)
    execution_id: Mapped[str]=mapped_column(ForeignKey('executions.id'),index=True)
    number: Mapped[int]=mapped_column(Integer)
    status: Mapped[str]=mapped_column(String(30),default='RUNNING')
    process_exit_code: Mapped[int|None]=mapped_column(Integer,nullable=True)
    log: Mapped[str]=mapped_column(Text,default='')
    started_at: Mapped[float]=mapped_column(Float,default=time.time)
    finished_at: Mapped[float|None]=mapped_column(Float,nullable=True)


class WalletTaskResult(Base):
    __tablename__='wallet_task_results'
    __table_args__=(UniqueConstraint('execution_id','wallet_id'),)
    id: Mapped[str]=mapped_column(String(32),primary_key=True,default=uid)
    execution_id: Mapped[str]=mapped_column(ForeignKey('executions.id'),index=True)
    wallet_id: Mapped[str]=mapped_column(ForeignKey('wallets.id'),index=True)
    address: Mapped[str]=mapped_column(String(100))
    chain: Mapped[str]=mapped_column(String(10))
    name: Mapped[str]=mapped_column(String(100),default='')
    status: Mapped[str]=mapped_column(String(30),default='QUEUED')
    message: Mapped[str]=mapped_column(Text,default='')
    points: Mapped[float|None]=mapped_column(Float,nullable=True)
    transaction_hash: Mapped[str|None]=mapped_column(String(200),nullable=True)


class WalletLock(Base):
    __tablename__='wallet_locks'
    wallet_id: Mapped[str]=mapped_column(ForeignKey('wallets.id'),primary_key=True)
    execution_id: Mapped[str]=mapped_column(ForeignKey('executions.id'),index=True)


class Worker(Base):
    __tablename__='workers'
    id: Mapped[str]=mapped_column(String(100),primary_key=True)
    heartbeat_at: Mapped[float]=mapped_column(Float)
    status: Mapped[str]=mapped_column(String(30))


class PointSnapshot(Base):
    __tablename__='point_snapshots'
    id: Mapped[str]=mapped_column(String(32),primary_key=True,default=uid)
    project_id: Mapped[str]=mapped_column(ForeignKey('projects.id'),index=True)
    wallet_id: Mapped[str]=mapped_column(ForeignKey('wallets.id'),index=True)
    execution_id: Mapped[str]=mapped_column(ForeignKey('executions.id'))
    points: Mapped[float]=mapped_column(Float)
    created_at: Mapped[float]=mapped_column(Float,default=time.time)


class ScheduleOccurrence(Base):
    __tablename__='schedule_occurrences'
    __table_args__=(UniqueConstraint('schedule_id','due_at'),)
    id: Mapped[str]=mapped_column(String(32),primary_key=True,default=uid)
    schedule_id: Mapped[str]=mapped_column(ForeignKey('schedules.id'),index=True)
    due_at: Mapped[float]=mapped_column(Float)
    status: Mapped[str]=mapped_column(String(30))
    execution_id: Mapped[str|None]=mapped_column(ForeignKey('executions.id'),nullable=True)
    message: Mapped[str]=mapped_column(Text,default='')


class TransactionPolicy(Base):
    __tablename__='transaction_policies'
    project_id: Mapped[str]=mapped_column(ForeignKey('projects.id'),primary_key=True)
    chains: Mapped[list]=mapped_column(JSON)
    contracts: Mapped[list]=mapped_column(JSON)
    functions: Mapped[list]=mapped_column(JSON)
    max_value: Mapped[int]=mapped_column(Integer)
    daily_gas_budget: Mapped[int]=mapped_column(Integer)
    max_approval: Mapped[int]=mapped_column(Integer)


class GasBudget(Base):
    __tablename__='gas_budgets'
    project_id: Mapped[str]=mapped_column(ForeignKey('projects.id'),primary_key=True)
    day: Mapped[str]=mapped_column(String(20),primary_key=True)
    spent: Mapped[int]=mapped_column(Integer,default=0)


class TransactionIntent(Base):
    __tablename__='transaction_intents'
    idempotency_key: Mapped[str]=mapped_column(String(100),primary_key=True)
    project_id: Mapped[str]=mapped_column(ForeignKey('projects.id'),index=True)
    wallet_id: Mapped[str]=mapped_column(ForeignKey('wallets.id'),index=True)
    payload_hash: Mapped[str]=mapped_column(String(64))
    chain: Mapped[str]=mapped_column(String(100))
    nonce: Mapped[int]=mapped_column(Integer)
    state: Mapped[str]=mapped_column(String(30))
    mock_hash: Mapped[str]=mapped_column(String(100))
    created_at: Mapped[float]=mapped_column(Float,default=time.time)


class ScriptAnalysisJob(Base):
    __tablename__ = 'script_analysis_jobs'
    __table_args__ = (UniqueConstraint('source_version_id', 'request_key'),)
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=uid)
    source_version_id: Mapped[str] = mapped_column(ForeignKey('script_versions.id'), index=True)
    source_artifact_id: Mapped[str] = mapped_column(ForeignKey('artifacts.id'))
    source_sha256: Mapped[str] = mapped_column(String(64))
    request_key: Mapped[str] = mapped_column(String(100))
    operation: Mapped[str] = mapped_column(String(20), default='DISCOVER')
    status: Mapped[str] = mapped_column(String(40), default='PENDING', index=True)
    config: Mapped[dict] = mapped_column(JSON, default=dict)
    result: Mapped[dict] = mapped_column(JSON, default=dict)
    preview_artifact_id: Mapped[str|None] = mapped_column(ForeignKey('artifacts.id'), nullable=True)
    preview_sha256: Mapped[str|None] = mapped_column(String(64), nullable=True)
    reviewed_preview_sha256: Mapped[str|None] = mapped_column(String(64), nullable=True)
    ai_sent: Mapped[bool] = mapped_column(Boolean, default=False)
    cancel_requested: Mapped[bool] = mapped_column(Boolean, default=False)
    lease_token: Mapped[str|None] = mapped_column(String(32), nullable=True)
    lease_until: Mapped[float|None] = mapped_column(Float, nullable=True, index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str] = mapped_column(Text, default='')
    created_by: Mapped[str] = mapped_column(ForeignKey('users.id'))
    created_at: Mapped[float] = mapped_column(Float, default=time.time)
    started_at: Mapped[float|None] = mapped_column(Float, nullable=True)
    finished_at: Mapped[float|None] = mapped_column(Float, nullable=True)


class ScriptTransformationDraft(Base):
    __tablename__ = 'script_transformation_drafts'
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=uid)
    job_id: Mapped[str] = mapped_column(ForeignKey('script_analysis_jobs.id'), unique=True)
    source_version_id: Mapped[str] = mapped_column(ForeignKey('script_versions.id'), index=True)
    source_artifact_id: Mapped[str] = mapped_column(ForeignKey('artifacts.id'))
    source_sha256: Mapped[str] = mapped_column(String(64))
    output_artifact_id: Mapped[str] = mapped_column(ForeignKey('artifacts.id'))
    diff_artifact_id: Mapped[str] = mapped_column(ForeignKey('artifacts.id'))
    output_sha256: Mapped[str] = mapped_column(String(64))
    review_sha256: Mapped[str] = mapped_column(String(64))
    summary: Mapped[dict] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(30), default='REVIEW_REQUIRED')
    result_version_id: Mapped[str|None] = mapped_column(ForeignKey('script_versions.id'), nullable=True)
    created_at: Mapped[float] = mapped_column(Float, default=time.time)


class ScriptFeature(Base):
    __tablename__ = 'script_features'
    __table_args__ = (UniqueConstraint('version_id', 'feature_key'), UniqueConstraint('draft_id', 'feature_key'),
                     CheckConstraint('(version_id IS NULL) <> (draft_id IS NULL)', name='ck_feature_one_owner'))
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=uid)
    version_id: Mapped[str|None] = mapped_column(ForeignKey('script_versions.id'), nullable=True, index=True)
    draft_id: Mapped[str|None] = mapped_column(ForeignKey('script_transformation_drafts.id'), nullable=True, index=True)
    feature_key: Mapped[str] = mapped_column(String(64))
    definition: Mapped[dict] = mapped_column(JSON)
    code_sha256: Mapped[str] = mapped_column(String(64))
    version_manifest_sha256: Mapped[str|None] = mapped_column(String(64), nullable=True)
    cipher_sha256: Mapped[str|None] = mapped_column(String(64), nullable=True)
    approval_sha256: Mapped[str|None] = mapped_column(String(64), nullable=True)
    status: Mapped[str] = mapped_column(String(30), default='REVIEW_REQUIRED')
    approved_by: Mapped[str|None] = mapped_column(ForeignKey('users.id'), nullable=True)
    approved_at: Mapped[float|None] = mapped_column(Float, nullable=True)
