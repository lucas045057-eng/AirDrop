"""Native接入与运行定义 API：版本追加、人工适配、运行定义与运行审核。

These endpoints never import or execute uploaded code. They bind an existing
artifact to an explicit runtime definition that a human reviews before any
container is started.
"""
import difflib
import json
import time
from typing import Literal
from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from backend.auth import admin
from backend.db import get_db
from backend.feature_reviews import feature_files, feature_dict, review_runtime, runtime_view, set_runtime
from backend.models import Artifact, AuditLog, RuntimeBuild, ScriptAdaptation, ScriptFeature, ScriptPackage, ScriptVersion, User, Worker, uid
from backend.queue import locked_db
from backend.runtime_builds import build_view, prepare, require_build, retry_build
from backend.scripts_api import artifact, prepare_source, read_upload, source_files, store_version, version_dict
from backend.storage import artifact_lock, encode_files
from backend.uploads import MAX_UPLOAD, redact, unpack
from packages.runtime_protocol import NativeRuntimeSpec

router = APIRouter(tags=['原生运行'], dependencies=[Depends(admin)])
ADAPTATION_SUFFIXES = ('.py', '.txt')
ADAPTATION_FILES = 300


def get_package(db, package_id):
    package = db.get(ScriptPackage, package_id)
    if package is None:
        raise HTTPException(404, '脚本包不存在')
    return package


def get_version(db, version_id):
    version = db.get(ScriptVersion, version_id)
    if version is None:
        raise HTTPException(404, '版本不存在')
    return version


@router.post('/scripts/{package_id}/versions')
async def add_version(package_id: str, file: UploadFile = File(...), db: Session = Depends(get_db), user: User = Depends(admin)):
    """Append a new version to an existing package instead of creating a second package."""
    package = get_package(db, package_id)
    data = await read_upload(file)
    prepared, analysis = await prepare_source(file.filename or '', data)
    with artifact_lock():
        return store_version(db, package, prepared, analysis, user.id, status='UPLOADED')


class RuntimeUpdate(BaseModel):
    model_config = ConfigDict(extra='forbid')
    runtime: dict
    confirmed: Literal[True]


class RuntimeReview(BaseModel):
    model_config = ConfigDict(extra='forbid')
    reviewed_sha256: str = Field(pattern='^[0-9a-f]{64}$')
    confirmed: Literal[True]


@router.get('/script-features/{feature_id}/runtime')
def runtime_detail(feature_id: str, db: Session = Depends(get_db)):
    with artifact_lock():
        feature = db.get(ScriptFeature, feature_id)
        if feature is None:
            raise HTTPException(404, '功能不存在')
        return runtime_view(db, feature)


@router.put('/script-features/{feature_id}/runtime')
def update_runtime(feature_id: str, body: RuntimeUpdate, user: User = Depends(admin)):
    """Any edit invalidates the previous runtime approval, including field mappings."""
    return set_runtime(feature_id, body.runtime, user.id)


@router.post('/script-features/{feature_id}/runtime-review')
def approve_runtime(feature_id: str, body: RuntimeReview, user: User = Depends(admin)):
    return review_runtime(feature_id, body.reviewed_sha256, user.id)


class BuildRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    feature_id: str = Field(min_length=1, max_length=32)


@router.post('/runtime-builds')
def create_build(body: BuildRequest, db: Session = Depends(get_db), user: User = Depends(admin)):
    """准备依赖环境；同一环境键复用已成功镜像，不重复安装。"""
    with artifact_lock():
        feature = db.get(ScriptFeature, body.feature_id)
        if feature is None:
            raise HTTPException(404, '功能不存在')
        if not feature.runtime_spec:
            raise HTTPException(409, '请先提交运行定义，再准备依赖环境')
        spec = NativeRuntimeSpec.model_validate(feature.runtime_spec)
        files = feature_files(db, feature)
        build, reused = prepare(db, feature, spec, files, user.id)
        db.commit()
        return {**build_view(db, build), 'reused': reused, 'runtime': runtime_view(db, feature)}


@router.get('/runtime-builds/{build_id}')
def build_status(build_id: str, db: Session = Depends(get_db)):
    return build_view(db, require_build(db, build_id))


@router.post('/runtime-builds/{build_id}/retry')
def rebuild(build_id: str, user: User = Depends(admin)):
    with artifact_lock():
        with locked_db() as db:
            build = retry_build(db, build_id, user.id)
            return build_view(db, build)


def adaptation_targets(files: dict, base: dict):
    """Only Python sources and dependency declarations may arrive as an adaptation."""
    if len(files) > ADAPTATION_FILES:
        raise HTTPException(422, '人工适配文件数量过多')
    accepted = {}
    for name, value in files.items():
        lowered = name.lower()
        if not (lowered.endswith('.py') or lowered.endswith('.txt') and 'requirement' in lowered):
            raise HTTPException(422, '人工适配只接受 Python 源码与 requirements 依赖声明：' + name)
        if name in base and base[name] == value:
            continue
        accepted[name] = value
    return accepted


def merge_sources(base: dict, accepted: dict):
    merged = dict(base)
    merged.update(accepted)
    diff = []
    for name in sorted(accepted):
        if name in base:
            diff.extend(difflib.unified_diff(base[name].decode('utf-8', 'replace').splitlines(),
                                             accepted[name].decode('utf-8', 'replace').splitlines(),
                                             fromfile='原版/' + name, tofile='适配/' + name, lineterm=''))
    return merged, '\n'.join(diff[:4000]) or '人工适配新增文件：' + ', '.join(sorted(set(accepted) - set(base)))


@router.post('/scripts/{version_id}/manual-adaptations')
async def manual_adaptation(version_id: str, file: UploadFile = File(...), runtime: str = Form(''),
                            note: str = Form(''), scan: bool = Form(True),
                            db: Session = Depends(get_db), user: User = Depends(admin)):
    """Upload reviewed adaptation files on top of an existing version and re-scan it."""
    version = get_version(db, version_id)
    data = await file.read(MAX_UPLOAD + 1)
    if len(data) > MAX_UPLOAD:
        raise HTTPException(422, '上传文件超过 10 MiB')
    with artifact_lock():
        base = source_files(db, version)
    try:
        incoming = unpack(file.filename or 'adaptation.zip', data)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    accepted = adaptation_targets(incoming, base)
    if not accepted:
        raise HTTPException(422, '适配文件与现有版本没有差异')
    hints = {}
    if runtime.strip():
        try:
            payload = json.loads(runtime)
        except ValueError:
            raise HTTPException(422, '运行定义不是合法 JSON') from None
        if not isinstance(payload, dict) or len(payload) > 20:
            raise HTTPException(422, '运行定义应按功能键提交，最多 20 项')
        for key, spec in payload.items():
            try:
                hints[str(key)] = NativeRuntimeSpec.model_validate(spec).model_dump(mode='json')
            except ValueError as exc:
                raise HTTPException(422, f'运行定义无效（{key}）：{exc}') from None
    merged, diff = merge_sources(base, accepted)
    with artifact_lock():
        db.rollback()
        version = get_version(db, version_id)
        source = artifact(db, encode_files(merged), 'manual-sources')
        number = (db.scalar(select(func.max(ScriptVersion.version)).where(ScriptVersion.package_id == version.package_id)) or 0) + 1
        analysis = {**version.analysis, 'manual_adaptation': {'files': sorted(accepted), 'note': redact(note)[0][:1000],
                                                              'at': time.time(), 'source_version_id': version.id}}
        result = ScriptVersion(package_id=version.package_id, version=number, original_artifact_id=version.original_artifact_id,
                               source_artifact_id=source.id, analysis=analysis, runtime_hints=hints or None,
                               status='REVIEW_REQUIRED')
        db.add(result); db.flush()
        db.add(ScriptAdaptation(source_version_id=version.id, result_version_id=result.id, provider='manual', diff=diff,
                                plan={'note': redact(note)[0][:1000], 'files': sorted(accepted), 'runtime_hints': sorted(hints)}))
        db.add(AuditLog(user_id=user.id, action='script.manual-adaptation', target=result.id,
                        detail={'source_version_id': version.id, 'files': sorted(accepted), 'hints': sorted(hints)}))
        db.commit()
        created = version_dict(db, result)
    if scan:
        from backend.analysis_jobs import create
        created['analysis_job'] = create(result.id, uid(), {'operation': 'DISCOVER', 'selected_features': [], 'use_ai': False}, user.id)
    return created


class RuntimeHint(BaseModel):
    model_config = ConfigDict(extra='forbid')
    feature_key: str = Field(pattern=r'^[a-zA-Z][A-Za-z0-9_]{0,63}$')
    runtime: dict


@router.post('/scripts/{version_id}/runtime-hints')
def set_hints(version_id: str, body: list[RuntimeHint], db: Session = Depends(get_db), user: User = Depends(admin)):
    """Store reviewed runtime definitions that the next scan must reuse verbatim."""
    if len(body) > 20:
        raise HTTPException(422, '运行定义最多 20 项')
    version = get_version(db, version_id)
    hints = {}
    for item in body:
        try:
            hints[item.feature_key] = NativeRuntimeSpec.model_validate(item.runtime).model_dump(mode='json')
        except ValueError as exc:
            raise HTTPException(422, f'运行定义无效（{item.feature_key}）：{exc}') from None
    version.runtime_hints = hints or None
    db.add(AuditLog(user_id=user.id, action='runtime.hints.update', target=version.id, detail={'keys': sorted(hints)}))
    db.commit()
    return {'version_id': version.id, 'feature_keys': sorted(hints)}


@router.get('/runtime/capabilities')
def capabilities(db: Session = Depends(get_db)):
    """What the deployment can actually do right now; the UI shows this verbatim."""
    from backend.config import settings
    from backend.runtime_builds import controller_status
    worker = db.get(Worker, 'adapter-1')
    status = controller_status()
    return {'native_execution_enabled': settings.native_execution_enabled,
            'adapter_worker_online': bool(worker and time.time() - worker.heartbeat_at < 30),
            'controller': status,
            'execution_available': bool(settings.native_execution_enabled and status.get('available')),
            'build_timeout_seconds': settings.native_build_timeout_seconds,
            'python_version': '3.12',
            'limits': {'timeout_seconds': settings.native_timeout_seconds, 'memory_mib': settings.native_memory_mib,
                       'cpus': settings.native_cpus, 'pids': settings.native_pids,
                       'global_concurrency': settings.native_global_concurrency},
            'wallet_source': '本次所选钱包按运行定义写入脚本原文件字段'}
