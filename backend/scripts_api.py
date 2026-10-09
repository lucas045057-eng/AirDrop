import hashlib
from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from sqlalchemy import select
from sqlalchemy.orm import Session
from backend.auth import admin
from backend.db import get_db
from backend.models import Artifact, AuditLog, ScriptPackage, ScriptVersion, User
from backend.storage import artifact_lock, decode_files, encode_files, read_blob, write_blob
from backend.uploads import MAX_UPLOAD, analyze, redact, unpack

router = APIRouter(prefix='/scripts', tags=['脚本中心'], dependencies=[Depends(admin)])


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
    return {'id':version.id,'package_id':version.package_id,'name':db.get(ScriptPackage,version.package_id).name,'version':version.version,'status':version.status,'analysis':version.analysis,'manifest':version.manifest,'sha256':obj.sha256,'created_at':version.created_at}


@router.post('/upload')
async def upload(file: UploadFile = File(...), db: Session = Depends(get_db), user: User = Depends(admin)):
    data = await file.read(MAX_UPLOAD+1)
    try:
        files = unpack(file.filename or '',data)
        analysis = analyze(files)
    except ValueError as exc:
        raise HTTPException(422,str(exc))
    with artifact_lock():
        original = artifact(db,data,'original')
        source = artifact(db,encode_files(files),'sources')
        package = ScriptPackage(name=(file.filename or 'project')[:200])
        db.add(package)
        db.flush()
        version = ScriptVersion(package_id=package.id,version=1,original_artifact_id=original.id,source_artifact_id=source.id,analysis=analysis)
        db.add(version)
        db.flush()
        db.add(AuditLog(user_id=user.id,action='script.upload',target=version.id,detail={'sha256':hashlib.sha256(data).hexdigest(),'files':len(files)}))
        db.commit()
        return version_dict(db,version)


@router.get('')
def list_scripts(db: Session = Depends(get_db)):
    return [version_dict(db,v) for v in db.scalars(select(ScriptVersion).order_by(ScriptVersion.created_at.desc()))]


@router.get('/{version_id}/source')
def view_source(version_id: str, db: Session = Depends(get_db)):
    with artifact_lock():
        version = db.get(ScriptVersion,version_id)
        if not version:
            raise HTTPException(404,'版本不存在')
        return {name:redact(value.decode('utf-8','replace'))[0] for name,value in source_files(db,version).items() if name.endswith(('.py','.txt','.toml','.json','.ini','.md'))}


@router.post('/{version_id}/analyze')
def reanalyze(version_id:str,db:Session=Depends(get_db),user:User=Depends(admin)):
    with artifact_lock():
        version=db.get(ScriptVersion,version_id)
        if not version:raise HTTPException(404,'版本不存在')
        version.analysis=analyze(source_files(db,version))
        db.add(AuditLog(user_id=user.id,action='script.analyze.refresh',target=version_id));db.commit()
        return version_dict(db,version)
