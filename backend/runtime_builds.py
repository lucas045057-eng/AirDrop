"""原生运行依赖环境：环境键、构建记录与镜像缓存。

构建在受信控制器里完成，构建环境不携带钱包、主密钥或数据库；这里只保存
环境键、锁定结果、镜像标识和构建日志尾部。
"""
import base64
import hashlib
import json
import platform
import time
from pathlib import PurePosixPath
from fastapi import HTTPException
from sqlalchemy import select
from backend.config import settings
from backend.models import Artifact, AuditLog, RuntimeBuild, ScriptFeature, ScriptVersion
from backend.storage import artifact_lock, encode_files, read_blob, write_blob
from packages.runtime_protocol import NativeRuntimeSpec
from runner import controller

DECLARATIONS = {'requirements.txt', 'pyproject.toml', 'requirements.in', 'poetry.lock', 'pipfile'}
MAX_DEPENDENCY_FILES = 10
LOG_TAIL = 20_000
BUILD_STALE_GRACE_SECONDS = 60


def dependency_sources(spec: NativeRuntimeSpec, files: dict) -> list[str]:
    """已声明的依赖文件；未声明时按惯例文件名识别，绝不猜测内容。"""
    if spec.dependency_files:
        return list(spec.dependency_files)
    found = []
    for name in sorted(files):
        lowered = PurePosixPath(name).name.lower()
        if lowered in DECLARATIONS or lowered.startswith('requirements-') or lowered.startswith('requirements_'):
            found.append(name)
    return found[:MAX_DEPENDENCY_FILES]


def dependency_digest(sources: list[str], files: dict) -> str:
    return hashlib.sha256(json.dumps({name: hashlib.sha256(files[name]).hexdigest() for name in sources},
                                     sort_keys=True).encode()).hexdigest()


def environment_key(base_image: str, python_version: str, arch: str, dependencies: str) -> str:
    """基础镜像摘要 + Python 版本 + 平台架构 + 依赖声明指纹。"""
    return hashlib.sha256(json.dumps({'base_image': base_image, 'python_version': python_version,
                                      'arch': arch, 'dependencies': dependencies}, sort_keys=True).encode()).hexdigest()


def controller_status() -> dict:
    """当前部署能做什么；只读，任何失败都返回原因而不是抛出。"""
    if not settings.native_execution_enabled:
        return {'available': False, 'online': False, 'reason': '原生执行开关未启用（AIRDROP_NATIVE_EXECUTION_ENABLED）'}
    result = controller.online()
    if result is None:
        return {'available': False, 'online': False,
                'reason': '执行控制器未就绪或不可达：' + str(settings.native_socket_path)}
    if not result.get('docker'):
        return {'available': False, 'online': True, 'arch': result.get('arch'), 'reason': '执行控制器无法访问 Docker'}
    return {'available': True, 'online': True, 'arch': result.get('arch'), 'docker': True,
            'version': result.get('version'), 'base_image_present': bool(result.get('base_image_present'))}


def image_available(image_id: str | None) -> bool:
    if not image_id or not settings.native_execution_enabled:
        return False
    try:
        return bool(controller.call('image', {'image_id': image_id}, timeout=10).get('exists'))
    except controller.ControllerError:
        return False


def environment_arch(base_image: str) -> str:
    status = controller_status()
    return str(status.get('arch') or platform.machine() or 'unknown')


def save_lock(db, image_lock: str, build):
    """锁定结果以加密工件保存，供运行审核与排障核对。"""
    path, digest = write_blob(image_lock.encode())
    artifact = Artifact(path=path, sha256=digest, kind='runtime-lock')
    db.add(artifact)
    db.flush()
    build.lock_artifact_id = artifact.id
    build.lock_sha256 = digest


def bind_feature(feature, build):
    """把功能指向本次环境；锁定摘要变化会使旧运行审核失效。"""
    spec = NativeRuntimeSpec.model_validate(feature.runtime_spec) if feature.runtime_spec else None
    changed = bool(spec and spec.dependency_lock_sha256 != build.lock_sha256)
    feature.runtime_build_id = build.id
    if spec is not None and build.lock_sha256:
        feature.runtime_spec = spec.model_copy(update={'dependency_lock_sha256': build.lock_sha256}).model_dump(mode='json')
    if changed:
        feature.runtime_status = 'DRAFT'
        feature.runtime_review_sha256 = None
        feature.runtime_approved_by = None
        feature.runtime_approved_at = None
    return changed


def prepare(db, feature, spec: NativeRuntimeSpec, files: dict, user_id):
    """建立或复用依赖环境；返回 (build, reused)。"""
    status = controller_status()
    if not status['available']:
        raise HTTPException(409, '依赖环境构建不可用：' + status['reason'])
    sources = dependency_sources(spec, files)
    digest_value = dependency_digest(sources, files)
    key = environment_key(settings.native_runtime_base_image, spec.python_version,
                          str(status.get('arch') or 'unknown'), digest_value)
    build = db.scalar(select(RuntimeBuild).where(RuntimeBuild.environment_key == key))
    if build is not None and build.status == 'READY' and image_available(build.image_id):
        changed = bind_feature(feature, build)
        db.add(AuditLog(user_id=user_id, action='runtime.build.reuse', target=build.id,
                        detail={'feature_id': feature.id, 'reapproval_required': changed}))
        return build, True
    if build is None:
        version = db.get(ScriptVersion, feature.version_id) if feature.version_id else None
        build = RuntimeBuild(package_id=version.package_id if version else None, environment_key=key,
                             python_version=spec.python_version,
                             base_image=settings.native_runtime_base_image, dependency_files=sources)
        db.add(build)
        db.flush()
    else:
        # 镜像已不存在或上次失败：同一环境键重新构建，保留历史日志。
        build.status = 'PENDING'
        build.error = ''
        build.started_at = None
        build.finished_at = None
    bind_feature(feature, build)
    db.add(AuditLog(user_id=user_id, action='runtime.build.create', target=build.id,
                    detail={'feature_id': feature.id, 'environment_key': key, 'dependencies': sources}))
    return build, False


def build_view(db, build):
    return {'id': build.id, 'status': build.status, 'environment_key': build.environment_key,
            'python_version': build.python_version, 'base_image': build.base_image,
            'dependency_files': list(build.dependency_files or []), 'lock_sha256': build.lock_sha256,
            'image_id': build.image_id, 'log': build.log, 'error': build.error,
            'created_at': build.created_at, 'started_at': build.started_at, 'finished_at': build.finished_at}


def require_build(db, build_id) -> RuntimeBuild:
    build = db.get(RuntimeBuild, build_id)
    if build is None:
        raise HTTPException(404, '环境构建记录不存在')
    return build


def claim_build(worker_id='adapter-1'):
    """一次只推进一个构建；构建并发由配置约束。"""
    from backend.queue import locked_db
    with locked_db() as db:
        fail_expired_builds(db, time.time())
        active = db.scalar(select(RuntimeBuild.id).where(RuntimeBuild.status == 'BUILDING').limit(1))
        if active:
            return None
        build = db.scalar(select(RuntimeBuild).where(RuntimeBuild.status == 'PENDING').order_by(RuntimeBuild.created_at).limit(1))
        if build is None:
            return None
        build.status = 'BUILDING'
        build.started_at = time.time()
        build.log = ''
        return build.id


def build_lease_expired(build, now=None):
    if build.status != 'BUILDING' or build.started_at is None:
        return False
    limit = max(1, settings.native_build_timeout_seconds) + BUILD_STALE_GRACE_SECONDS
    return (now or time.time()) - build.started_at > limit


def fail_expired_builds(db, now=None):
    """Release builds left BUILDING after the controller's bounded build window."""
    now = now or time.time()
    expired = [build for build in db.scalars(select(RuntimeBuild).where(RuntimeBuild.status == 'BUILDING'))
               if build_lease_expired(build, now)]
    for build in expired:
        build.status = 'FAILED'
        build.error = '构建 Worker 超时或失联，未收到控制器最终结果；请检查控制器后重试'
        build.finished_at = now
        db.add(AuditLog(action='runtime.build.expired', target=build.id,
                        detail={'started_at': build.started_at, 'expired_at': now}))
    return len(expired)


def run_build(build_id):
    """调用控制器构建镜像；返回是否成功。"""
    from backend.queue import locked_db
    from backend.feature_reviews import feature_files
    try:
        with artifact_lock(), locked_db() as db:
            build = db.get(RuntimeBuild, build_id)
            if build is None or build.status != 'BUILDING':
                return False
            feature = db.scalar(select(ScriptFeature).where(ScriptFeature.runtime_build_id == build.id).limit(1))
            if feature is None:
                build.status = 'FAILED'; build.error = '找不到绑定该环境的功能，禁止构建'
                build.finished_at = time.time()
                return False
            spec = NativeRuntimeSpec.model_validate(feature.runtime_spec)
            files = feature_files(db, feature)
            payload = {'environment_key': build.environment_key, 'base_image': build.base_image,
                       'python_version': build.python_version, 'dependencies': list(build.dependency_files or []),
                       'files': {name: base64.b64encode(files[name]).decode() for name in (build.dependency_files or []) if name in files},
                       'limits': {'memory_mib': settings.native_memory_mib, 'cpus': settings.native_cpus,
                                  'pids': settings.native_pids, 'timeout_seconds': settings.native_build_timeout_seconds}}
    except Exception as exc:
        with locked_db() as db:
            build = db.get(RuntimeBuild, build_id)
            if build and build.status == 'BUILDING':
                build.status = 'FAILED'
                build.error = ('构建输入无效：' + str(exc)[:1900]) if isinstance(exc, (ValueError, HTTPException)) else \
                              '构建输入准备失败（' + type(exc).__name__ + '）'
                build.finished_at = time.time()
        return False

    chunks = []

    def event(message):
        if message.get('event') == 'log':
            chunks.append(str(message.get('data', '')))
            del chunks[:-400]

    try:
        result = controller.stream('build', payload, event, idle_timeout=settings.native_build_timeout_seconds)
    except Exception as exc:
        with locked_db() as db:
            build = db.get(RuntimeBuild, build_id)
            if build and build.status == 'BUILDING':
                build.status = 'FAILED'
                build.error = str(exc)[:2000] if isinstance(exc, (ValueError, controller.ControllerError)) else \
                              '依赖环境构建失败（' + type(exc).__name__ + '）'
                build.log = ''.join(chunks)[-LOG_TAIL:]
                build.finished_at = time.time()
        return False
    with artifact_lock(), locked_db() as db:
        build = db.get(RuntimeBuild, build_id)
        if build is None or build.status != 'BUILDING':
            return False
        build.log = ''.join(chunks)[-LOG_TAIL:]
        image_id = str(result.get('image_id') or '')
        image_lock = str(result.get('lock') or '')
        if not image_id or not image_lock:
            build.status = 'FAILED'; build.error = '构建成功但缺少镜像标识或依赖锁定结果'
            build.finished_at = time.time()
            return False
        save_lock(db, image_lock, build)
        build.image_id = image_id
        build.status = 'READY'
        build.error = ''
        build.finished_at = time.time()
        for feature in db.scalars(select(ScriptFeature).where(ScriptFeature.runtime_build_id == build.id)):
            bind_feature(feature, build)
        db.add(AuditLog(action='runtime.build.ready', target=build.id,
                        detail={'image_id': image_id, 'lock_sha256': build.lock_sha256}))
    return True


def retry_build(db, build_id, user_id):
    build = require_build(db, build_id)
    if build.status == 'BUILDING' and not build_lease_expired(build):
        raise HTTPException(409, '环境正在构建，请等待当前构建结束')
    if build.status == 'BUILDING':
        build.status = 'FAILED'
        build.error = '构建 Worker 超时或失联；人工请求重试'
        build.finished_at = time.time()
    build.status = 'PENDING'
    build.error = ''
    build.image_id = None
    db.add(AuditLog(user_id=user_id, action='runtime.build.retry', target=build.id))
    return build
