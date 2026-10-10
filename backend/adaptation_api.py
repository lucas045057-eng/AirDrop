from fastapi import APIRouter,Depends,HTTPException
from pydantic import BaseModel,Field
from sqlalchemy import select,func
from sqlalchemy.orm import Session
from backend.auth import admin
from backend.db import get_db
from backend.models import ScriptVersion,ScriptAdaptation,SystemSetting,AuditLog,User
from backend.scripts_api import artifact,source_files,version_dict
from backend.storage import artifact_lock,encode_files,cipher
from backend.ai import CompatibleProvider,preview
from adapters.standardize import apply_replacements,rules
from packages.protocol import Manifest

router=APIRouter(prefix='/scripts',tags=['脚本适配'],dependencies=[Depends(admin)])


def get_version(db,version_id):
    version=db.get(ScriptVersion,version_id)
    if not version:raise HTTPException(404,'版本不存在')
    return version


@router.get('/{version_id}/ai-preview')
def ai_preview(version_id:str,db:Session=Depends(get_db)):
    with artifact_lock():
        version=get_version(db,version_id)
        try:payload,digest=preview(source_files(db,version),version.analysis)
        except (ValueError,UnicodeError) as exc:raise HTTPException(422,'无法生成安全脱敏预览，请人工处理') from exc
    return {'payload':payload,'sha256':digest,'external_send_requires_review':True}


class AdaptRequest(BaseModel):
    manifest:Manifest
    provider:str=Field(default='rules',pattern='^(rules|compatible)$')
    path_mappings:dict[str,str]=Field(default_factory=dict)
    reviewed_preview_sha256:str|None=None


@router.post('/{version_id}/adapt')
async def adapt(version_id:str,body:AdaptRequest,db:Session=Depends(get_db),user:User=Depends(admin)):
    with artifact_lock():
        version=get_version(db,version_id);files=source_files(db,version)
        user_id=user.id
    if body.manifest.package_id!=version.package_id:raise HTTPException(422,'Manifest 包 ID 不匹配')
    if body.manifest.entrypoint not in files or any(s not in files for s in body.manifest.steps):raise HTTPException(422,'指定入口或步骤不存在')
    if version.analysis['interactive']:raise HTTPException(422,'检测到交互，请使用“AI 智能分析”识别功能并生成非交互入口；原始版本保留')
    if version.analysis['platform']!='linux':raise HTTPException(422,'检测到 Windows 特有依赖，请人工移植')
    if len(body.path_mappings)>30 or any(target!=body.manifest.wallet_path for target in body.path_mappings.values()):raise HTTPException(422,'路径映射必须指向 Manifest 钱包输入路径')
    try:
        adapted,diff,analysis=rules(files,body.path_mappings)
        plan={'level':'L3' if diff else 'L1','path_mappings':body.path_mappings}
        if body.provider!='rules':
            payload,digest=preview(files,version.analysis)
            if body.provider=='compatible':
                if body.reviewed_preview_sha256!=digest:raise ValueError('外部 AI 发送前必须审核当前脱敏预览并提交 SHA-256')
                config=db.get(SystemSetting,'ai')
                if not config:raise ValueError('尚未配置 AI Provider')
                provider=CompatibleProvider(config.value['url'],config.value['model'],cipher().decrypt(config.value['encrypted_key'].encode()).decode())
            proposal=await provider.adapt(payload)
            # Redacted source can never replace an original credential or restore a real secret.
            if proposal['replacements'] and analysis['secret_findings']:
                raise ValueError('源代码含凭证，外部补丁不可自动应用；请先人工移除凭证再上传')
            adapted,diff,analysis=apply_replacements(adapted,proposal['replacements'])
            plan=proposal['plan']
            if body.path_mappings:
                adapted,rule_diff,analysis=rules(adapted,body.path_mappings);diff+=rule_diff
        adapted,diff,analysis=apply_replacements(files,{name:value.decode('utf-8-sig') for name,value in adapted.items() if name.endswith('.py')})
        with artifact_lock():
            # External AI may have yielded while the source version was removed.
            db.rollback()
            version=get_version(db,version_id)
            next_version=(db.scalar(select(func.max(ScriptVersion.version)).where(ScriptVersion.package_id==version.package_id)) or 0)+1
            body.manifest.package_version=next_version
            source=artifact(db,encode_files(adapted),'adapted-sources')
            result=ScriptVersion(package_id=version.package_id,version=next_version,original_artifact_id=version.original_artifact_id,source_artifact_id=source.id,status='REVIEW_REQUIRED',analysis=analysis,manifest=body.manifest.model_dump())
            db.add(result);db.flush()
            db.add(ScriptAdaptation(source_version_id=version.id,result_version_id=result.id,provider=body.provider,diff=diff,plan=plan))
            db.add(AuditLog(user_id=user_id,action='script.adapt',target=result.id,detail={'provider':body.provider}));db.commit()
            return version_dict(db,result)
    except (ValueError,SyntaxError,UnicodeError) as exc:raise HTTPException(422,str(exc))


@router.get('/{version_id}/diff')
def diff(version_id:str,db:Session=Depends(get_db)):
    obj=db.scalar(select(ScriptAdaptation).where(ScriptAdaptation.result_version_id==version_id))
    if not obj:raise HTTPException(404,'该版本没有适配 Diff')
    return {'diff':obj.diff,'plan':obj.plan,'approved':bool(obj.approved_by)}


@router.post('/{version_id}/approve')
def approve(version_id:str,db:Session=Depends(get_db),user:User=Depends(admin)):
    version=get_version(db,version_id)
    from backend.models import ScriptFeature
    if db.scalar(select(ScriptFeature.id).where(ScriptFeature.version_id==version_id).limit(1)):
        raise HTTPException(409,'智能适配版本必须在分析详情逐项审核，不允许通过旧版接口绕过功能审核')
    if version.status not in {'REVIEW_REQUIRED','APPROVED','TESTED','PUBLISHED'}:raise HTTPException(409,'请先适配此版本')
    adaptation=db.scalar(select(ScriptAdaptation).where(ScriptAdaptation.result_version_id==version_id))
    adaptation.approved_by=user.id
    if version.status=='REVIEW_REQUIRED':version.status='APPROVED'
    db.add(AuditLog(user_id=user.id,action='script.diff.approve',target=version_id));db.commit()
    return version_dict(db,version)


@router.post('/{version_id}/publish')
def publish(version_id:str,db:Session=Depends(get_db),user:User=Depends(admin)):
    from backend.models import Artifact
    version=get_version(db,version_id)
    source=db.get(Artifact,version.source_artifact_id)
    adaptation=db.scalar(select(ScriptAdaptation).where(ScriptAdaptation.result_version_id==version_id))
    if version.status not in {'TESTED','PUBLISHED'} or version.tested_sha256!=source.sha256 or not adaptation or not adaptation.approved_by:
        raise HTTPException(409,'发布须通过当前版本的隔离测试与 Diff 审核')
    version.status='PUBLISHED';db.add(AuditLog(user_id=user.id,action='script.publish',target=version_id));db.commit()
    return version_dict(db,version)
