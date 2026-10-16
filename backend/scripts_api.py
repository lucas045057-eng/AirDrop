import hashlib
import asyncio
import threading
from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from backend.auth import admin
from backend.db import get_db
from backend.models import Artifact, AuditLog, ScriptPackage, ScriptVersion, User
from backend.storage import artifact_lock, decode_files, encode_files, read_blob, write_blob
from backend.uploads import MAX_UPLOAD, analyze, redact, unpack

router = APIRouter(prefix='/scripts', tags=['脚本中心'], dependencies=[Depends(admin)])
_upload_slot = threading.BoundedSemaphore(1)


def artifact(db, data, kind):
    path,digest = write_blob(data)
    obj = Artifact(path=path,sha256=digest,kind=kind)
    db.add(obj)
    db.flush()
    return obj


def source_files(db, version):
    obj = db.get(Artifact,version.source_artifact_id)
    return decode_files(read_blob(obj.path,obj.sha256))


def version_dict(db, version):
    obj = db.get(Artifact,version.source_artifact_id)
    from backend.models import ScriptFeature
    from sqlalchemy import func
    return {'id':version.id,'package_id':version.package_id,'name':db.get(ScriptPackage,version.package_id).name,'version':version.version,'status':version.status,'analysis':version.analysis,'manifest':version.manifest,'sha256':obj.sha256,'created_at':version.created_at,
            'feature_count': db.scalar(select(func.count()).select_from(ScriptFeature).where(ScriptFeature.version_id==version.id))}


@router.post('/upload')
async def upload(file: UploadFile = File(...), db: Session = Depends(get_db), user: User = Depends(admin)):
    data = await read_upload(file)
    prepared, analysis = await prepare_source(file.filename or '', data)
    with artifact_lock():
        package = ScriptPackage(name=(file.filename or 'project')[:200])
        db.add(package)
        db.flush()
        return store_version(db, package, prepared, analysis, user.id)


async def read_upload(file: UploadFile) -> bytes:
    data = await file.read(MAX_UPLOAD + 1)
    if len(data) > MAX_UPLOAD:
        raise HTTPException(422, '上传文件超过 10 MiB')
    return data


async def prepare_source(filename: str, data: bytes):
    """Bounded, isolated parsing of an uploaded package; shared by every upload path."""
    if not _upload_slot.acquire(blocking=False):
        raise HTTPException(429, '另一个源码上传正在处理，请稍后重试')
    try:
        from worker.process_limits import run_child
        prepared, resources = await run_child('upload', {'filename': filename or ''}, data, memory_mib=96, timeout=30)
    except (ValueError, TimeoutError) as exc:
        raise HTTPException(422, str(exc)) from exc
    finally:
        _upload_slot.release()
    return prepared, {**prepared['analysis'], 'upload_resources': resources}


def store_version(db, package, prepared, analysis, user_id, status='UPLOADED'):
    """Append a new version to an existing script package; the original stays archived."""
    original = Artifact(**prepared['original'], kind='original')
    source = Artifact(**prepared['sources'], kind='sources')
    db.add_all([original, source]); db.flush()
    number = (db.scalar(select(func.max(ScriptVersion.version)).where(ScriptVersion.package_id == package.id)) or 0) + 1
    version = ScriptVersion(package_id=package.id, version=number, original_artifact_id=original.id,
                            source_artifact_id=source.id, analysis=analysis, status=status)
    db.add(version)
    db.flush()
    db.add(AuditLog(user_id=user_id, action='script.upload', target=version.id,
                    detail={'sha256': original.sha256, 'files': prepared['files'], 'version': number}))
    db.commit()
    return version_dict(db, version)

@router.get('')
def list_scripts(db: Session = Depends(get_db)):
    return [version_dict(db,v) for v in db.scalars(select(ScriptVersion).order_by(ScriptVersion.created_at.desc()))]


@router.get('/{version_id}/source')
def view_source(version_id: str, db: Session = Depends(get_db)):
    with artifact_lock():
        version = db.get(ScriptVersion,version_id)
        if not version:
            raise HTTPException(404,'版本不存在')
        return {name:redact(value.decode('utf-8','replace'))[0] if name.endswith('.py') or name.endswith('requirements.txt')
                else '[数据文件已隔离：不展示实际内容，不发送外部模型]'
                for name,value in source_files(db,version).items()}


@router.post('/{version_id}/analyze')
def reanalyze(version_id:str,db:Session=Depends(get_db),user:User=Depends(admin)):
    from backend.analysis_jobs import create
    from backend.models import uid
    return create(version_id, uid(), {'operation':'DISCOVER','selected_features':[],'use_ai':False}, user.id)
