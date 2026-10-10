"""Human approval and version commits share the existing artifact/version system."""
import hashlib
import json
import time
from fastapi import HTTPException
from sqlalchemy import func, select
from backend.analysis_jobs import digest, require_source
from backend.models import Artifact, AuditLog, ScriptAdaptation, ScriptAnalysisJob, ScriptFeature, ScriptTransformationDraft, ScriptVersion
from backend.queue import locked_db
from backend.scripts_api import version_dict
from backend.storage import artifact_fingerprint, artifact_lock
from packages.feature_protocol import FeatureDefinition
from packages.protocol import Manifest


def approval_digest(feature):
    return digest({'definition': feature.definition, 'code_sha256': feature.code_sha256})


def feature_dict(feature):
    return {'id': feature.id, 'version_id': feature.version_id, 'draft_id': feature.draft_id,
            'definition': feature.definition, 'status': feature.status, 'code_sha256': feature.code_sha256,
            'review_sha256': approval_digest(feature), 'approved_at': feature.approved_at}


def verify_draft(db, draft):
    job = db.get(ScriptAnalysisJob, draft.job_id); require_source(db, job)
    if job.cancel_requested or job.status == 'CANCELLED':
        raise HTTPException(409, '分析任务已取消，草稿不能提交')
    output = db.get(Artifact, draft.output_artifact_id)
    if output.sha256 != draft.output_sha256:
        raise HTTPException(409, '草稿代码摘要已改变，旧审核失效')
    # Integrity checks cover encrypted storage corruption too, before approval or save.
    try:
        if artifact_fingerprint(output) != draft.summary.get('output_cipher_sha256'):
            raise ValueError('工件改变')
    except (OSError, ValueError):
        raise HTTPException(409, '草稿工件已损坏或改变，审核失效') from None
    summary = {key: value for key, value in draft.summary.items() if key != 'analysis'}
    if digest({'source_sha256': draft.source_sha256, 'output_sha256': draft.output_sha256, 'summary': summary}) != draft.review_sha256:
        raise HTTPException(409, '草稿配置发生改变，旧审核失效')
    return job


def require_feature_in_draft(feature, draft):
    original = next((item for item in draft.summary['features'] if item['feature_id'] == feature.feature_key), None)
    if feature.definition != original or feature.code_sha256 != draft.output_sha256:
        raise HTTPException(409, '功能定义与已校验草稿不一致，需重新分析')


def review(feature_id, action, reviewed_sha256, user_id):
    with artifact_lock(), locked_db() as db:
        feature = db.get(ScriptFeature, feature_id)
        if feature is None or feature.draft_id is None:
            raise HTTPException(404, '待审核草稿功能不存在')
        draft = db.get(ScriptTransformationDraft, feature.draft_id)
        if draft.result_version_id:
            raise HTTPException(409, '该草稿已经保存，功能定义不可修改')
        job = verify_draft(db, draft)
        require_feature_in_draft(feature, draft)
        if approval_digest(feature) != reviewed_sha256:
            raise HTTPException(409, '功能配置或代码摘要已改变，请重新审核')
        definition = FeatureDefinition.model_validate(feature.definition)
        if action == 'APPROVED':
            if definition.blocking_reasons or definition.risk_level == 'high' or not definition.entrypoint or definition.automation_status != 'REVIEW_REQUIRED':
                raise HTTPException(409, '该功能存在高风险、未知结构或未解决交互，须人工处理后重新适配')
            feature.approval_sha256 = reviewed_sha256; feature.approved_by = user_id; feature.approved_at = time.time()
        else:
            feature.approval_sha256 = None; feature.approved_by = None; feature.approved_at = None
        feature.status = action
        db.flush()
        eligible = list(db.scalars(select(ScriptFeature).where(ScriptFeature.draft_id == draft.id)))
        job.status = 'APPROVED' if any(item.status == 'APPROVED' for item in eligible) else 'REVIEW_REQUIRED'
        db.add(AuditLog(user_id=user_id, action='feature.' + action.lower(), target=feature.id,
                        detail={'review_sha256': reviewed_sha256}))
        return feature_dict(feature)


def save(draft_id, feature_ids, review_sha256, user_id):
    with artifact_lock(), locked_db() as db:
        draft = db.get(ScriptTransformationDraft, draft_id)
        if draft is None:
            raise HTTPException(404, '草稿不存在')
        if draft.result_version_id:
            result = db.get(ScriptVersion, draft.result_version_id)
            return version_dict(db, result)
        job = verify_draft(db, draft)
        if draft.review_sha256 != review_sha256:
            raise HTTPException(409, '草稿摘要不匹配，请重新审核 Diff')
        if len(feature_ids) != len(set(feature_ids)):
            raise HTTPException(422, '不能重复选择功能')
        features = list(db.scalars(select(ScriptFeature).where(ScriptFeature.draft_id == draft.id, ScriptFeature.id.in_(feature_ids))))
        if not features or len(features) != len(feature_ids):
            raise HTTPException(422, '所选功能不属于此草稿')
        for feature in features:
            require_feature_in_draft(feature, draft)
            if feature.status != 'APPROVED' or not feature.approved_by or feature.approval_sha256 != approval_digest(feature):
                raise HTTPException(409, '所有保存的功能必须完成当前代码及配置的逐项审核')
        source = db.get(ScriptVersion, draft.source_version_id)
        number = db.scalar(select(func.max(ScriptVersion.version)).where(ScriptVersion.package_id == source.package_id)) + 1
        manifest = Manifest(package_id=source.package_id, package_version=number,
                            entrypoint=features[0].definition['entrypoint'], allowed_operations=['read'], required_permissions=[])
        result = ScriptVersion(package_id=source.package_id, version=number, original_artifact_id=source.original_artifact_id,
            source_artifact_id=draft.output_artifact_id, analysis=draft.summary['analysis'], manifest=manifest.model_dump(), status='APPROVED')
        db.add(result); db.flush()
        for feature in features:
            db.add(ScriptFeature(version_id=result.id, feature_key=feature.feature_key, definition=feature.definition,
                version_manifest_sha256=digest(result.manifest), cipher_sha256=draft.summary['output_cipher_sha256'],
                code_sha256=feature.code_sha256, approval_sha256=feature.approval_sha256, status='APPROVED',
                approved_by=feature.approved_by, approved_at=feature.approved_at))
        db.add(ScriptAdaptation(source_version_id=source.id, result_version_id=result.id,
            provider='compatible' if job.config.get('use_ai') else 'rules', diff='功能级 Diff 在智能分析详情中分页展示。',
            plan={'smart_job_id': job.id, 'draft_id': draft.id, 'feature_keys': [f.feature_key for f in features]}, approved_by=user_id))
        draft.result_version_id = result.id; draft.status = 'COMPLETED'; job.status = 'COMPLETED'; job.finished_at = time.time()
        db.add(AuditLog(user_id=user_id, action='analysis.version.save', target=result.id,
                        detail={'source_version_id': source.id, 'features': [f.feature_key for f in features], 'review_sha256': review_sha256}))
        return version_dict(db, result)


def task_snapshot(db, feature, version, values, wallet_input):
    if feature.version_id != version.id or feature.status != 'APPROVED' or not feature.approved_by:
        raise HTTPException(409, '功能未审核或不属于所选版本')
    source = db.get(Artifact, version.source_artifact_id)
    try:
        intact = artifact_fingerprint(source) == feature.cipher_sha256
    except (OSError, ValueError):
        intact = False
    if not intact or source.sha256 != feature.code_sha256 or feature.approval_sha256 != approval_digest(feature) or digest(version.manifest) != feature.version_manifest_sha256:
        raise HTTPException(409, '功能代码或配置变动导致审核失效')
    definition = FeatureDefinition.model_validate(feature.definition)
    schema = definition.input_schema
    if set(values) - set(schema['properties']):
        raise HTTPException(422, '包含未知功能参数')
    prepared = {}
    for parameter in definition.parameters:
        if parameter.name not in values:
            if parameter.required:
                raise HTTPException(422, '缺少必填参数：' + parameter.name)
            if parameter.has_default:
                prepared[parameter.name] = parameter.default
            continue
        value = values[parameter.name]
        if parameter.sensitive:
            import re
            if not isinstance(value, dict) or set(value) != {'reference'} or not isinstance(value['reference'], str) or not re.fullmatch('[A-Za-z][A-Za-z0-9_-]{0,80}', value['reference']):
                raise HTTPException(422, '敏感参数只能保存凭证引用，不能保存实际值')
        else:
            expected = {'integer': int, 'number': (int, float), 'boolean': bool, 'string': str, 'array': list, 'object': dict}.get(parameter.type)
            if expected and (not isinstance(value, expected) or parameter.type in {'integer', 'number'} and isinstance(value, bool)):
                raise HTTPException(422, '参数类型错误：' + parameter.name)
            constraints = parameter.constraints
            if 'enum' in constraints and value not in constraints['enum']:
                raise HTTPException(422, '参数不在允许范围：' + parameter.name)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                import math
                if not math.isfinite(value):
                    raise HTTPException(422, '数值参数必须有限：' + parameter.name)
                if 'minimum' in constraints and value < constraints['minimum'] or 'maximum' in constraints and value > constraints['maximum']:
                    raise HTTPException(422, '参数数值超出约束：' + parameter.name)
            if isinstance(value, str) and (len(value) > min(1000, constraints.get('maxLength', 1000)) or len(value) < constraints.get('minLength', 0)):
                raise HTTPException(422, '参数长度超出约束：' + parameter.name)
        prepared[parameter.name] = value
    snapshot = {'protocol': definition.protocol, 'feature_id': feature.feature_key, 'feature_name': definition.name, 'entrypoint': definition.entrypoint,
                'input_schema': schema, 'parameters': prepared, 'wallet_input': wallet_input or definition.wallet_input.model_dump(),
                'source_sha256': feature.code_sha256, 'approval_sha256': feature.approval_sha256, 'execution_enabled': False}
    if len(json.dumps(snapshot).encode()) > 32_000:
        raise HTTPException(422, '功能配置超过 32000 字节')
    return snapshot
