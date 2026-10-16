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
from backend.storage import artifact_fingerprint, artifact_lock, decode_files, read_blob
from packages.feature_protocol import CAPABILITY_REASONS, FeatureDefinition
from packages.protocol import Manifest
from packages.runtime_protocol import NativeRuntimeSpec, runtime_digest, validate_against_files


def approval_digest(feature):
    return digest({'definition': feature.definition, 'code_sha256': feature.code_sha256})


def feature_dict(feature):
    return {'id': feature.id, 'version_id': feature.version_id, 'draft_id': feature.draft_id,
            'definition': feature.definition, 'status': feature.status, 'code_sha256': feature.code_sha256,
            'review_sha256': approval_digest(feature), 'approved_at': feature.approved_at,
            'runtime_spec': feature.runtime_spec, 'runtime_status': feature.runtime_status,
            'runtime_approved_at': feature.runtime_approved_at, 'runtime_build_id': feature.runtime_build_id}


def feature_files(db, feature):
    """Decoded code artifact behind a draft feature or a saved version feature."""
    if feature.draft_id:
        draft = db.get(ScriptTransformationDraft, feature.draft_id)
        artifact = db.get(Artifact, draft.output_artifact_id)
    else:
        version = db.get(ScriptVersion, feature.version_id)
        artifact = db.get(Artifact, version.source_artifact_id)
    with artifact_lock():
        return decode_files(read_blob(artifact.path, artifact.sha256))


def runtime_view(db, feature):
    """Runtime definition plus the exact digest a reviewer must confirm."""
    from backend.config import settings
    spec = NativeRuntimeSpec.model_validate(feature.runtime_spec) if feature.runtime_spec else None
    view = {'id': feature.id, 'version_id': feature.version_id, 'draft_id': feature.draft_id,
            'runtime_spec': feature.runtime_spec, 'runtime_status': feature.runtime_status,
            'runtime_approved_at': feature.runtime_approved_at, 'runtime_build_id': feature.runtime_build_id,
            'review_sha256': feature.runtime_review_sha256, 'approved_sha256': feature.runtime_review_sha256,
            'capabilities': list(spec.capabilities) if spec else [],
            'readiness': spec.readiness() if spec else ['尚未生成运行定义'],
            'execution_enabled': settings.native_execution_enabled, 'environment': None}
    if feature.runtime_build_id:
        from backend.runtime_builds import build_view
        from backend.models import RuntimeBuild
        build = db.get(RuntimeBuild, feature.runtime_build_id)
        if build is not None:
            view['environment'] = build_view(db, build)
    if spec is not None:
        files = feature_files(db, feature)
        view['review_sha256'] = runtime_digest_for(feature, spec, files)
        view['parameters_schema'] = spec.schema()
        view['entrypoint_present'] = spec.entrypoint in files
    return view


def runtime_digest_for(feature, spec, files):
    referenced = [spec.entrypoint, *spec.dependency_files]
    referenced += [binding.target for binding in spec.configuration_bindings if binding.destination == 'file_field']
    generated = {name: hashlib.sha256(files[name]).hexdigest() for name in sorted(set(referenced)) if name in files}
    return runtime_digest(spec.model_dump(mode='json'), code_sha256=feature.code_sha256,
                          definition_digest=approval_digest(feature), generated_files=generated)


def parse_runtime(payload):
    """运行定义必须符合协议；非法输入返回 422 而不是未处理异常。"""
    try:
        return NativeRuntimeSpec.model_validate(payload)
    except ValueError as exc:
        errors = exc.errors() if hasattr(exc, 'errors') else []
        message = str(errors[0].get('msg', '')).removeprefix('Value error, ') if errors else ''
        raise HTTPException(422, '运行定义无效：' + (message or '请检查字段与取值')) from None


def structural_blockers(definition):
    """Unresolved structure still blocks running; declared capabilities do not."""
    return [reason for reason in definition.blocking_reasons if reason not in CAPABILITY_REASONS]


def require_native_approvable(feature, definition):
    """The analysis approval of a capability-bearing feature needs a declared runtime."""
    blockers = structural_blockers(definition)
    if blockers:
        raise HTTPException(409, '该功能仍有未解决的结构问题：' + '；'.join(blockers[:3]))
    if not definition.entrypoint:
        raise HTTPException(409, '该功能没有可运行的入口，须人工适配后重新扫描')
    capability_only = bool(definition.blocking_reasons) and all(
        reason in CAPABILITY_REASONS for reason in definition.blocking_reasons
    )
    if definition.automation_status != 'REVIEW_REQUIRED' and not capability_only:
        raise HTTPException(409, '该功能的结构尚未解决，须人工处理后重新适配')
    if definition.risk_level == 'high' or capability_only:
        spec = NativeRuntimeSpec.model_validate(feature.runtime_spec) if feature.runtime_spec else None
        if spec is None or not spec.capabilities:
            raise HTTPException(409, '该功能涉及凭证、认证、网络或资金操作，须先在运行定义中明确声明能力，再做运行审核')


def set_runtime(feature_id, payload, user_id):
    """Store a runtime definition edit; any previous runtime approval becomes stale."""
    spec = parse_runtime(payload)
    with artifact_lock(), locked_db() as db:
        feature = db.get(ScriptFeature, feature_id)
        if feature is None:
            raise HTTPException(404, '功能不存在')
        try:
            validate_against_files(spec, feature_files(db, feature))
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None
        feature.runtime_spec = spec.model_dump(mode='json')
        feature.runtime_status = 'DRAFT'
        feature.runtime_review_sha256 = None
        feature.runtime_approved_by = None
        feature.runtime_approved_at = None
        db.add(AuditLog(user_id=user_id, action='runtime.definition.update', target=feature.id,
                        detail={'entrypoint': spec.entrypoint, 'capabilities': list(spec.capabilities)}))
        return runtime_view(db, feature)


def review_runtime(feature_id, reviewed_sha256, user_id):
    with artifact_lock(), locked_db() as db:
        feature = db.get(ScriptFeature, feature_id)
        if feature is None or not feature.runtime_spec:
            raise HTTPException(404, '该功能尚未生成运行定义')
        spec = parse_runtime(feature.runtime_spec)
        notes = spec.readiness()
        if notes:
            raise HTTPException(409, '运行定义仍包含未解决项：' + '；'.join(notes[:3]))
        files = feature_files(db, feature)
        validate_against_files(spec, files)
        current = runtime_digest_for(feature, spec, files)
        if current != reviewed_sha256:
            raise HTTPException(409, '运行定义或代码摘要已改变，请重新审核')
        definition = FeatureDefinition.model_validate(feature.definition)
        if structural_blockers(definition) or not definition.entrypoint:
            raise HTTPException(409, '功能结构尚未解决，不能通过运行审核')
        feature.runtime_review_sha256 = current
        feature.runtime_approved_by = user_id
        feature.runtime_approved_at = time.time()
        feature.runtime_status = 'APPROVED'
        db.add(AuditLog(user_id=user_id, action='runtime.review.approve', target=feature.id,
                        detail={'runtime_sha256': current, 'entrypoint': spec.entrypoint}))
        return runtime_view(db, feature)


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
            # Structure problems still block; declared capabilities are covered by the runtime review.
            require_native_approvable(feature, definition)
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
            saved_feature = ScriptFeature(version_id=result.id, feature_key=feature.feature_key, definition=feature.definition,
                version_manifest_sha256=digest(result.manifest), cipher_sha256=draft.summary['output_cipher_sha256'],
                code_sha256=feature.code_sha256, approval_sha256=feature.approval_sha256, status='APPROVED',
                approved_by=feature.approved_by, approved_at=feature.approved_at)
            db.add(saved_feature); db.flush()
            if feature.runtime_spec:
                saved_feature.runtime_spec = feature.runtime_spec
                spec = NativeRuntimeSpec.model_validate(feature.runtime_spec)
                if feature.runtime_approved_by and not spec.readiness():
                    # Same code, definition and runtime definition: the approval still applies.
                    saved_feature.runtime_review_sha256 = runtime_digest_for(saved_feature, spec, feature_files(db, saved_feature))
                    saved_feature.runtime_approved_by = feature.runtime_approved_by
                    saved_feature.runtime_approved_at = feature.runtime_approved_at
                    saved_feature.runtime_status = 'APPROVED'
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
    if feature.runtime_spec:
        runtime = NativeRuntimeSpec.model_validate(feature.runtime_spec)
        runtime_parameters = {parameter.name: parameter for parameter in runtime.parameters}
        mismatched = [parameter.name for parameter in definition.parameters
                      if parameter.name in runtime_parameters
                      and parameter.sensitive != runtime_parameters[parameter.name].sensitive]
        if mismatched:
            raise HTTPException(409, '参数敏感属性必须与已审核运行定义一致：' + ', '.join(sorted(mismatched)))
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
