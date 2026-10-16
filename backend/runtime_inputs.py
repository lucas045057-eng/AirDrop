"""原生运行的输入组装：本次选中的钱包、项目账号资料、参数与状态投影。

这里只生成容器运行需要的文件内容，不写进 API 响应或普通队列 JSON；私钥与
账号凭据只在 Worker 内解密，并通过本地 socket 交给受信控制器。
"""
import base64
import hashlib
import json
from fastapi import HTTPException
from sqlalchemy import select
from backend.models import (Artifact, Execution, Project, ProjectScriptState, ScriptVersion, Task, Wallet,
                            WalletSecret, WalletTaskResult)
from backend.storage import artifact_lock, decode_files, encode_files, read_blob, write_blob
from backend.vault import decrypt_wallet
from adapters.wallets import encode as encode_wallets
from packages.runtime_protocol import NativeRuntimeSpec
from backend import account_imports, runtime_state

MAX_INPUT_BYTES = 8 * 1024 * 1024
# 快照里额外保存本次真正交给脚本的凭据值，供控制器精确遮蔽；该条目不会进入容器。
SECRETS_ENTRY = 'airdrop-input-secrets.json'


def selected_wallets(db, execution: Execution):
    """本次执行固定的钱包集合；只包含该 execution 的钱包结果行。

    结果行主键是随机 uid，按主键排序会得到不稳定的顺序，因此按地址排序，
    保证钱包文件与 input_sha256 在重复运行时可复现。
    """
    rows = db.execute(select(Wallet, WalletSecret).join(WalletTaskResult, WalletTaskResult.wallet_id == Wallet.id)
                      .outerjoin(WalletSecret, WalletSecret.wallet_id == Wallet.id)
                      .where(WalletTaskResult.execution_id == execution.id)
                      .order_by(Wallet.address)).all()
    return rows


def parameter_values(spec: NativeRuntimeSpec, snapshot: dict) -> dict:
    declared = {parameter.name: parameter for parameter in spec.parameters}
    provided = dict((snapshot or {}).get('parameters') or {})
    unknown = set(provided) - set(declared)
    if unknown:
        raise HTTPException(409, '运行参数不在已审核定义中：' + ', '.join(sorted(unknown)))
    values = {}
    for name, parameter in declared.items():
        if name in provided:
            values[name] = provided[name]
        elif parameter.has_default and not parameter.sensitive:
            values[name] = parameter.default
        elif parameter.required:
            raise HTTPException(409, '缺少必填参数：' + name)
    for name, value in values.items():
        if isinstance(value, (dict, list)):
            raise HTTPException(409, '参数必须以文本形式交付：' + name)
    return values


def field_value(column: str, role: str, wallet: Wallet, secret: str, account: dict, parameters: dict):
    private_key = secret or ''
    if role == 'identity':
        if column in {'wallet_id', 'id'}:
            return wallet.id
        if column in {'name', 'wallet_name'}:
            return wallet.name
        return wallet.address
    if role == 'credential':
        if not private_key:
            raise HTTPException(409, '钱包缺少私钥，无法按运行定义交付：' + wallet.address)
        return private_key
    if column.startswith('param:'):
        name = column.split(':', 1)[1]
        if name not in parameters:
            raise HTTPException(409, '运行定义引用了未声明的参数：' + name)
        return str(parameters[name])
    if column.startswith('config:'):
        column = column.split(':', 1)[1]
    if column in account:
        return str(account[column])
    if column in parameters:
        return str(parameters[column])
    return ''


def wallet_records(db, execution: Execution, spec: NativeRuntimeSpec, parameters: dict):
    """按 wallet_binding 生成钱包文件；字段缺失按未提供处理，不猜测内容。"""
    binding = spec.wallet_binding
    if binding is None:
        return []
    accounts = account_imports.rows_for(db, execution.project_id,
                                        db.get(ScriptVersion, execution.script_version_id).package_id,
                                        [wallet.id for wallet, _ in selected_wallets(db, execution)])
    records = []
    for wallet, secret_row in selected_wallets(db, execution):
        if wallet.chain != (binding.chain or wallet.chain):
            raise HTTPException(409, '本次钱包链类型与运行定义不一致：' + wallet.address)
        secret = decrypt_wallet(wallet, secret_row.ciphertext) if secret_row else ''
        account = accounts.get(wallet.id, {})
        records.append({column: field_value(column, role, wallet, secret, account, parameters)
                        for column, role in binding.fields.items()})
    return records


def state_keys(db, execution: Execution, spec: NativeRuntimeSpec):
    """只有本次选中的账号可以进入或回写状态文件。"""
    keys = {}
    for binding in spec.state_bindings:
        if binding.account_key_kind == 'wallet_id':
            keys[binding.path] = {wallet.id for wallet, _ in selected_wallets(db, execution)}
        else:
            keys[binding.path] = {wallet.address for wallet, _ in selected_wallets(db, execution)}
    return keys


def patch_file_fields(files: dict, spec: NativeRuntimeSpec, parameters: dict) -> dict:
    """把参数写进已声明文件的字段。

    只支持 JSON 对象工件：其他格式需要人工薄入口，平台不猜文件结构。
    """
    patched = dict(files)
    for binding in spec.configuration_bindings:
        if binding.destination != 'file_field':
            continue
        source = patched.get(binding.target)
        if source is None:
            raise HTTPException(409, '运行定义引用的文件不在本版本工件中：' + binding.target)
        if binding.parameter not in parameters:
            raise HTTPException(409, '文件字段绑定缺少参数值：' + binding.parameter)
        try:
            body = json.loads(source.decode('utf-8-sig'))
        except (UnicodeError, ValueError):
            raise HTTPException(409, '文件字段绑定目前只支持 JSON 对象，请改用人工适配：' + binding.target) from None
        if not isinstance(body, dict):
            raise HTTPException(409, '文件字段绑定要求目标文件是 JSON 对象：' + binding.target)
        body[binding.field] = parameters[binding.parameter]
        patched[binding.target] = json.dumps(body, ensure_ascii=False, indent=2).encode()
    return patched


def injected_secrets(db, execution: Execution, spec: NativeRuntimeSpec, parameters: dict) -> list[str]:
    """本次真正交给脚本的凭据值；只在控制器内存里用于精确遮蔽。"""
    values = set()
    if spec.wallet_binding is not None:
        records = wallet_records(db, execution, spec, parameters)
        for record in records:
            for column, role in spec.wallet_binding.fields.items():
                if role in {'credential', 'account_config', 'project_config'}:
                    values.add(str(record.get(column) or ''))
    for parameter in spec.parameters:
        if parameter.sensitive and parameter.name in parameters:
            values.add(str(parameters[parameter.name]))
    return sorted(value for value in values if len(value) >= 6)


def wallet_files(db, execution: Execution, spec: NativeRuntimeSpec, parameters: dict) -> dict:
    """按 wallet_binding 生成的本次钱包文件内容。"""
    if spec.wallet_binding is None:
        return {}
    records = wallet_records(db, execution, spec, parameters)
    payload = encode_wallets(records, spec.wallet_binding.format,
                             {column: column for column in spec.wallet_binding.fields})
    return {spec.wallet_binding.target_path: payload}


def freeze(db, execution: Execution, spec: NativeRuntimeSpec, parameters: dict) -> dict:
    """入队时把本次钱包资料固定成加密快照；运行只使用这份快照。"""
    files = wallet_files(db, execution, spec, parameters)
    secrets = injected_secrets(db, execution, spec, parameters)
    total = sum(len(value) for value in files.values())
    if total > MAX_INPUT_BYTES:
        raise HTTPException(409, '本次运行输入超过 8 MiB，请减少钱包数量')
    payload = {**files, SECRETS_ENTRY: json.dumps(secrets, ensure_ascii=False).encode()}
    path, digest = write_blob(encode_files(payload))
    artifact = Artifact(path=path, sha256=digest, kind='runtime-input')
    db.add(artifact)
    db.flush()
    execution.input_snapshot_artifact_id = artifact.id
    addresses = [wallet.address for wallet, _ in selected_wallets(db, execution)]
    return {'files': files, 'secrets': secrets, 'addresses': addresses, 'records': len(addresses),
            'input_sha256': hashlib.sha256(json.dumps(sorted(
                (name, hashlib.sha256(value).hexdigest()) for name, value in files.items())).encode()).hexdigest()}


def frozen(db, execution: Execution) -> tuple[dict, list[str]]:
    """读回入队时固定的钱包快照；缺少或校验失败时拒绝运行。"""
    artifact = db.get(Artifact, execution.input_snapshot_artifact_id) if execution.input_snapshot_artifact_id else None
    if artifact is None:
        raise HTTPException(409, '该运行没有固定的输入快照，禁止在没有审核输入的情况下启动')
    with artifact_lock():
        payload = decode_files(read_blob(artifact.path, artifact.sha256))
    secrets = payload.pop(SECRETS_ENTRY, b'[]')
    try:
        values = json.loads(secrets.decode())
    except ValueError:
        raise HTTPException(409, '输入快照中的凭据索引损坏，禁止启动') from None
    return payload, [str(value) for value in values]


def build_stream(db, execution: Execution):
    """组装一次运行的全部输入；返回结构与字节，均不落盘。"""
    task = db.get(Task, execution.task_id)
    version = db.get(ScriptVersion, execution.script_version_id)
    snapshot = execution.runtime_snapshot or task.feature_config_snapshot or {}
    spec = NativeRuntimeSpec.model_validate(snapshot.get('runtime') or snapshot)
    parameters = parameter_values(spec, snapshot)
    # 钱包与凭据用入队时固定的加密快照；状态投影在取得状态锁后按当前代次生成。
    if execution.input_snapshot_artifact_id:
        files, secrets = frozen(db, execution)
    else:
        files = wallet_files(db, execution, spec, parameters)
        secrets = injected_secrets(db, execution, spec, parameters)
    records = len(selected_wallets(db, execution))
    keys = state_keys(db, execution, spec)
    files.update(runtime_state.project(db, execution, spec, keys))
    total = sum(len(value) for value in files.values())
    if total > MAX_INPUT_BYTES:
        raise HTTPException(409, '本次运行输入超过 8 MiB，请减少钱包数量')
    return {'files': files, 'wallet_file': spec.wallet_binding.target_path if spec.wallet_binding else None,
            'parameters': parameters, 'records': records,
            'secrets': secrets,
            'wallet_ids': [wallet.id for wallet, _ in selected_wallets(db, execution)],
            'wallet_addresses': [wallet.address for wallet, _ in selected_wallets(db, execution)],
            'input_sha256': hashlib.sha256(json.dumps(sorted(
                (name, hashlib.sha256(value).hexdigest()) for name, value in files.items())).encode()).hexdigest()}


def io_files(payload: dict) -> dict:
    """本地 socket 传输格式：文件名到 base64；不写入数据库。"""
    return {name: base64.b64encode(value).decode() for name, value in payload['files'].items()}
