"""脚本状态空间：投影、回写合并与代次保护。

状态空间是 (project_id, package_id, state_schema_version)。运行前只把本次选中
账号的记录投影给脚本，运行后按同一白名单合并，保留其他账号的数据。
"""
import csv
import hashlib
import io
import json
import time
import zipfile
from fastapi import HTTPException
from openpyxl import Workbook, load_workbook
from sqlalchemy import select
from backend.models import Artifact, AuditLog, Execution, ProjectScriptState, ScriptVersion, uid
from backend.storage import artifact_lock, encode_files, decode_files, read_blob, write_blob
from packages.runtime_protocol import NativeRuntimeSpec, StateBinding

MAX_STATE_BYTES = 16 * 1024 * 1024
MAX_STATE_ROWS = 5000


def space(db, project_id, package_id, schema_version, create=False) -> ProjectScriptState | None:
    state = db.scalar(select(ProjectScriptState).where(ProjectScriptState.project_id == project_id,
                                                       ProjectScriptState.package_id == package_id,
                                                       ProjectScriptState.state_schema_version == schema_version))
    if state is None and create:
        state = ProjectScriptState(project_id=project_id, package_id=package_id, state_schema_version=schema_version)
        db.add(state)
        db.flush()
    return state


def current_files(db, state: ProjectScriptState | None, spec: NativeRuntimeSpec) -> dict:
    if state is None or not state.artifact_id:
        return {}
    artifact = db.get(Artifact, state.artifact_id)
    if artifact.sha256 != state.artifact_sha256:
        raise HTTPException(409, '状态工件摘要不一致，请人工核查状态快照')
    with artifact_lock():
        return decode_files(read_blob(artifact.path, artifact.sha256))


def binding_for(spec: NativeRuntimeSpec, path: str) -> StateBinding | None:
    return next((binding for binding in spec.state_bindings if binding.path == path), None)


def read_rows(path: str, data: bytes, binding: StateBinding) -> tuple[list[str], list[dict]]:
    try:
        if binding.format == 'CSV':
            reader = csv.DictReader(io.StringIO(data.decode('utf-8-sig')))
            return list(reader.fieldnames or []), list(reader)
        if binding.format == 'JSON':
            body = json.loads(data.decode('utf-8-sig'))
            if not isinstance(body, list) or any(not isinstance(row, dict) for row in body):
                raise ValueError('JSON 状态必须是对象数组')
            names = []
            for row in body:
                for name in row:
                    if name not in names:
                        names.append(str(name))
            return names, body
        if binding.format == 'XLSX':
            with zipfile.ZipFile(io.BytesIO(data)) as archive:
                if len(archive.infolist()) > 100 or sum(item.file_size for item in archive.infolist()) > 20 * 1024 * 1024:
                    raise ValueError('Excel 状态解压体积过大')
            workbook = load_workbook(io.BytesIO(data), read_only=True, data_only=True, keep_links=False)
            sheet = workbook.active
            if sheet.max_row and sheet.max_row > MAX_STATE_ROWS + 1:
                raise ValueError('状态行数超过 5000')
            iterator = sheet.iter_rows(values_only=True)
            headers = [str(value).strip() if value is not None else '' for value in next(iterator)]
            rows = [dict(zip(headers, values)) for values in iterator if any(value is not None for value in values)]
            workbook.close()
            return headers, rows
    except (UnicodeError, json.JSONDecodeError, zipfile.BadZipFile, OSError, StopIteration) as exc:
        raise ValueError('状态文件损坏或格式不符') from None
    raise ValueError('状态格式不支持：' + binding.format)


def write_rows(path: str, binding: StateBinding, names: list[str], rows: list[dict]) -> bytes:
    if binding.format == 'CSV':
        output = io.StringIO(newline='')
        writer = csv.DictWriter(output, fieldnames=names)
        writer.writeheader()
        writer.writerows([{name: row.get(name, '') for name in names} for row in rows])
        return output.getvalue().encode('utf-8-sig')
    if binding.format == 'JSON':
        return json.dumps([{name: row.get(name, '') for name in names} for row in rows], ensure_ascii=False).encode()
    if binding.format == 'XLSX':
        workbook = Workbook()
        sheet = workbook.active
        sheet.append(names)
        for row in rows:
            sheet.append([row.get(name, '') for name in names])
        buffer = io.BytesIO()
        workbook.save(buffer)
        return buffer.getvalue()
    raise ValueError('状态格式不支持：' + binding.format)


def project(db, execution: Execution, spec: NativeRuntimeSpec, keys: dict) -> dict:
    """只投影本次选中账号的历史记录；没有历史时给出仅表头的文件。"""
    state = space(db, execution.project_id, db.get(ScriptVersion, execution.script_version_id).package_id,
                  spec.state_schema_version)
    files = current_files(db, state, spec)
    projected = {}
    for binding in spec.state_bindings:
        source = files.get(binding.path)
        if source is None:
            continue
        names, rows = read_rows(binding.path, source, binding)
        selected = keys.get(binding.path, set())
        kept = [row for row in rows if str(row.get(binding.account_key, '')) in selected]
        projected[binding.path] = write_rows(binding.path, binding, names, kept)
    return projected


def account_of(binding: StateBinding, row: dict) -> str:
    value = row.get(binding.account_key)
    return '' if value is None else str(value).strip()


def merge_file(binding: StateBinding, current: bytes | None, produced: bytes, selected: set[str]) -> tuple[bytes, dict]:
    """按账号白名单合并：保留未选账号，拒绝未知账号与重复记录。"""
    names, rows = read_rows(binding.path, produced, binding)
    if not rows and current is None:
        return produced, {'added': 0, 'updated': 0, 'kept': 0}
    seen, incoming = set(), []
    for row in rows:
        key = account_of(binding, row)
        if not key:
            raise ValueError('状态文件缺少账号标识：' + binding.account_key)
        if key not in selected:
            raise ValueError('状态文件包含不在本次选择中的账号：' + key[:20])
        if key in seen:
            raise ValueError('状态文件包含重复账号记录：' + key[:20])
        seen.add(key)
        incoming.append(row)
    if current is None:
        base_names, base_rows = list(names), []
    else:
        base_names, base_rows = read_rows(binding.path, current, binding)
    for name in names:
        if name not in base_names:
            base_names.append(name)
    result = []
    replaced = {account_of(binding, row) for row in incoming}
    for row in base_rows:
        key = account_of(binding, row)
        if key in replaced:
            continue
        result.append(row)
    result.extend(incoming)
    stats = {'added': len(incoming) - len(seen & {account_of(binding, row) for row in base_rows}),
             'updated': len(seen & {account_of(binding, row) for row in base_rows}),
             'kept': len(result) - len(incoming)}
    return write_rows(binding.path, binding, base_names, result), stats


def merge(db, execution: Execution, spec: NativeRuntimeSpec, produced: dict, keys: dict):
    """合并本次产物；返回 (merged_files, stats)。未声明文件一律拒绝。"""
    state = space(db, execution.project_id, db.get(ScriptVersion, execution.script_version_id).package_id,
                  spec.state_schema_version)
    current = current_files(db, state, spec)
    declared = {binding.path for binding in spec.state_bindings if binding.write_back}
    unknown = set(produced) - declared
    if unknown:
        raise ValueError('状态产物包含未声明的文件：' + ', '.join(sorted(unknown)))
    merged, stats = dict(current), {}
    for path, data in produced.items():
        binding = binding_for(spec, path)
        if binding is None or not binding.write_back:
            raise ValueError('状态产物不被允许回写：' + path)
        if len(data) > MAX_STATE_BYTES:
            raise ValueError('状态文件超过 16 MiB：' + path)
        try:
            merged[path], detail = merge_file(binding, current.get(path), data, keys.get(path, set()))
        except ValueError as exc:
            raise ValueError(f'{path}：{exc}') from None
        stats[path] = detail
    return merged, stats


def commit(db, execution: Execution, spec: NativeRuntimeSpec, merged: dict, expected_generation: int):
    """代次条件更新；冲突时保留产物并标记待处理，不覆盖较新的状态。"""
    state = space(db, execution.project_id, db.get(ScriptVersion, execution.script_version_id).package_id,
                  spec.state_schema_version, create=True)
    if expected_generation is not None and state.generation != expected_generation:
        execution.state_status = 'PENDING_REVIEW'
        db.add(AuditLog(action='runtime.state.conflict', target=execution.id,
                        detail={'expected': expected_generation, 'current': state.generation}))
        return {'committed': False, 'generation': state.generation, 'reason': '状态代次已改变'}
    path, digest = write_blob(encode_files(merged))
    artifact = Artifact(path=path, sha256=digest, kind='runtime-state')
    db.add(artifact)
    db.flush()
    state.generation += 1
    state.artifact_id = artifact.id
    state.artifact_sha256 = digest
    state.locked_execution_id = None
    state.updated_at = time.time()
    execution.state_generation = state.generation
    execution.state_status = 'COMMITTED'
    db.add(AuditLog(action='runtime.state.commit', target=execution.id,
                    detail={'generation': state.generation, 'files': sorted(merged)}))
    return {'committed': True, 'generation': state.generation}


def lock(db, execution: Execution, spec: NativeRuntimeSpec):
    """运行写入前固定状态代次；同一状态空间同时只允许一个运行写入。"""
    state = space(db, execution.project_id, db.get(ScriptVersion, execution.script_version_id).package_id,
                  spec.state_schema_version, create=True)
    if state.locked_execution_id and state.locked_execution_id != execution.id:
        holder = db.get(Execution, state.locked_execution_id)
        if holder is not None and (holder.status in {'QUEUED', 'PREPARING', 'RUNNING', 'STOPPING', 'RECONCILING',
                                                     'UNKNOWN', 'MANUAL_REQUIRED'}
                                   or holder.state_status == 'PENDING_REVIEW'):
            raise HTTPException(409, '该脚本状态正在被另一次运行写入，请等待其结束')
    state.locked_execution_id = execution.id
    state.updated_at = time.time()
    execution.state_generation = state.generation
    execution.state_status = 'LOCKED'
    return state.generation


def release(db, execution: Execution):
    if execution.state_status == 'LOCKED':
        execution.state_status = 'UNCOLLECTED'
