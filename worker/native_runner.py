"""原生运行在 Worker 侧的编排。

Worker 负责把已审核定义、本次钱包、项目资料与状态投影装成输入，交给受信
控制器执行；日志事件与状态文件都通过本地 socket 传输，私钥不出现在队列
JSON、请求文件或数据库明文里。
"""
import hashlib
import base64
import json
from fastapi import HTTPException
from sqlalchemy import select
from backend.config import settings
from backend.db import Session
from backend.models import Execution, RuntimeBuild, ScriptVersion, Task
from backend.scripts_api import source_files
from backend import runtime_inputs
from packages.runtime_protocol import ARGUMENT_PLACEHOLDER, NativeRuntimeSpec, validate_against_files
from runner import controller

CONTAINER_WORKDIR = '/work'
CALLABLE_LAUNCHER_NAME = 'airdrop_runtime_launcher.py'
CALLABLE_PARAMETERS_NAME = 'airdrop_runtime_parameters.json'
CALLABLE_LAUNCHER = b'''import asyncio
import importlib
import importlib.util
import inspect
import json
import pathlib
import sys

root = pathlib.Path.cwd().resolve()
entry = pathlib.Path(sys.argv[1]).resolve()
if not entry.is_relative_to(root):
    raise SystemExit("entrypoint escapes the run work directory")
sys.path.insert(0, str(root))
relative = entry.relative_to(root)
parts = relative.with_suffix("").parts
if relative.name == "__init__.py":
    parts = parts[:-1]
package = parts if all(part.isidentifier() for part in parts) else ()
if package and all((root.joinpath(*package[:index]) / "__init__.py").is_file()
                   for index in range(1, len(package))):
    module = importlib.import_module(".".join(package))
else:
    spec = importlib.util.spec_from_file_location("_airdrop_user_entry", entry)
    if spec is None or spec.loader is None:
        raise SystemExit("cannot load reviewed entrypoint")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
function = getattr(module, sys.argv[2])
parameters = json.loads(pathlib.Path(sys.argv[3]).read_text(encoding="utf-8"))
result = function(**parameters)
if inspect.isawaitable(result):
    asyncio.run(result)
'''


def spec_of(db, execution: Execution) -> NativeRuntimeSpec:
    """运行定义来自执行快照（入队时固定），没有快照时回落到任务配置。"""
    task = db.get(Task, execution.task_id)
    snapshot = execution.runtime_snapshot or (task.feature_config_snapshot if task else None) or {}
    body = snapshot.get('runtime') or snapshot
    if not body:
        raise HTTPException(409, '该执行没有固定的运行定义，无法在原生路径运行')
    return NativeRuntimeSpec.model_validate(body)


def image_for(db, execution: Execution):
    """运行镜像优先使用入队时固定的环境；没有快照时回落到功能当前绑定的环境。"""
    from backend.models import ScriptFeature
    from types import SimpleNamespace
    snapshot = execution.runtime_snapshot or {}
    fixed = str(snapshot.get('image_id') or '')
    if fixed:
        build = db.get(RuntimeBuild, snapshot.get('runtime_build_id')) if snapshot.get('runtime_build_id') else None
        return build if build is not None else SimpleNamespace(id=None, status='READY', image_id=fixed)
    task = db.get(Task, execution.task_id)
    feature = db.get(ScriptFeature, task.script_feature_id) if task and task.script_feature_id else None
    if feature is None:
        feature = db.scalar(select(ScriptFeature).where(ScriptFeature.version_id == execution.script_version_id).limit(1))
    if feature is None:
        raise HTTPException(409, '该执行没有绑定脚本功能，无法确认运行环境')
    build = db.get(RuntimeBuild, feature.runtime_build_id) if feature.runtime_build_id else None
    if build is None or build.status != 'READY' or not build.image_id:
        raise HTTPException(409, '运行环境尚未构建完成，请先准备依赖环境')
    return build


def substitute(text: str, *, wallet_target: str, workdir: str, parameters: dict) -> str:
    def replace(match):
        kind, name = match.group(1), match.group(2)
        if kind == 'wallet_path':
            if not wallet_target:
                raise HTTPException(409, '运行定义使用了钱包路径占位符但没有钱包绑定')
            return CONTAINER_WORKDIR + '/' + wallet_target
        if kind == 'workdir':
            return workdir
        if name not in parameters:
            raise HTTPException(409, '命令行参数缺少参数值：' + str(name))
        return str(parameters[name])
    return ARGUMENT_PLACEHOLDER.sub(replace, text)


def materialize(db, execution: Execution) -> dict:
    """装配一次运行的全部输入；不写数据库、不落盘。"""
    version = db.get(ScriptVersion, execution.script_version_id)
    spec = spec_of(db, execution)
    parameters = runtime_inputs.parameter_values(spec, execution.runtime_snapshot
                                                 or (db.get(Task, execution.task_id).feature_config_snapshot or {}))
    sensitive_in_argv = {parameter.name for parameter in spec.parameters if parameter.sensitive}
    declared = set(parameters)
    for argument in spec.arguments:
        used = {match.group(2) for match in ARGUMENT_PLACEHOLDER.finditer(argument.value) if match.group(1) == 'parameter'}
        if used & sensitive_in_argv:
            raise HTTPException(409, '敏感参数不能通过命令行交付：' + ', '.join(sorted(used & sensitive_in_argv)))
        if not used <= declared:
            raise HTTPException(409, '命令行参数引用了未声明的参数')
    code = source_files(db, version)
    validate_against_files(spec, code)
    stream = runtime_inputs.build_stream(db, execution)
    files = dict(code)
    for name, data in stream['files'].items():
        if name in files:
            raise HTTPException(409, '生成的输入与代码工件路径冲突：' + name)
        files[name] = data
    files = runtime_inputs.patch_file_fields(files, spec, parameters)
    for binding in spec.configuration_bindings:
        if binding.destination == 'environment' and binding.parameter not in parameters:
            raise HTTPException(409, '环境变量绑定缺少参数值：' + binding.parameter)
    workdir = CONTAINER_WORKDIR + (('/' + spec.workdir.strip('/')) if spec.workdir else '')
    wallet_target = spec.wallet_binding.target_path if spec.wallet_binding else ''
    if spec.callable:
        workdir_prefix = spec.workdir.strip('/') + '/' if spec.workdir else ''
        launcher_path = workdir_prefix + CALLABLE_LAUNCHER_NAME
        parameters_path = workdir_prefix + CALLABLE_PARAMETERS_NAME
        if launcher_path in files or parameters_path in files:
            raise HTTPException(409, '脚本工件占用了平台 callable 启动文件名，请先重命名冲突文件')
        files[launcher_path] = CALLABLE_LAUNCHER
        files[parameters_path] = json.dumps(parameters, ensure_ascii=False, sort_keys=True).encode()
        arguments = [f'{CONTAINER_WORKDIR}/{launcher_path}', f'{CONTAINER_WORKDIR}/{spec.entrypoint}',
                     spec.callable, f'{CONTAINER_WORKDIR}/{parameters_path}']
    else:
        arguments = [f'{CONTAINER_WORKDIR}/{spec.entrypoint}']
    arguments += [substitute(argument.value, wallet_target=wallet_target, workdir=workdir,
                             parameters=parameters) for argument in spec.arguments]
    environment = {binding.target: str(parameters[binding.parameter]) for binding in spec.configuration_bindings
                   if binding.destination == 'environment'}
    fingerprint = hashlib.sha256(json.dumps(sorted((name, hashlib.sha256(data).hexdigest())
                                                   for name, data in files.items())).encode()).hexdigest()
    return {'spec': spec, 'files': files, 'arguments': arguments, 'environment': environment, 'subdir': spec.workdir,
            'timeout_seconds': int(spec.timeout_seconds or settings.native_timeout_seconds),
            'state_paths': [binding.path for binding in spec.state_bindings if binding.write_back],
            'secrets': list(stream.get('secrets') or []),
            'input_sha256': fingerprint, 'parameters': parameters, 'version_id': version.id}


def start(attempt_id: str, execution_id: str, image_id: str, prepared: dict, on_event=None) -> dict:
    """把这次运行交给控制器；重复调用返回同一容器，不复制运行。"""
    payload = {'attempt_id': attempt_id, 'execution_id': execution_id, 'image_id': image_id,
               'network': settings.native_network, 'subdir': prepared['subdir'],
               'arguments': prepared['arguments'], 'environment': prepared['environment'],
               'files': {name: base64.b64encode(data).decode() for name, data in prepared['files'].items()}}
    # 启动阶段也会产生平台事件（容器已启动），必须进日志，不能丢在客户端。
    return controller.call('start', payload, timeout=120, on_event=on_event)


def attach(attempt_id: str, container_id: str, timeout_seconds: int, secrets: list, on_event) -> dict:
    """持续读取容器输出直到进程退出；事件由调用方决定落库方式。"""
    return controller.stream('attach', {'attempt_id': attempt_id, 'container_id': container_id,
                                        'timeout_seconds': timeout_seconds, 'secrets': list(secrets)},
                             on_event, idle_timeout=max(120, timeout_seconds))


def collect(attempt_id: str, state_paths: list) -> dict:
    return controller.call('collect', {'attempt_id': attempt_id, 'paths': list(state_paths)}, timeout=120)


def stop(attempt_id: str, signal: str = None, grace_seconds: int = None, container_id: str = None,
         escalate: bool = True) -> dict:
    """发送停止信号；escalate=False 时等待超时后返回 needs_escalation，不直接强杀。"""
    return controller.call('stop', {'attempt_id': attempt_id, 'container_id': container_id,
                                    'signal': signal or 'SIGINT', 'grace_seconds': grace_seconds or 60,
                                    'escalate': bool(escalate)},
                           timeout=(grace_seconds or 60) + 30)


def status(attempt_id: str, container_id: str = None) -> dict:
    return controller.call('status', {'attempt_id': attempt_id, 'container_id': container_id}, timeout=30)


def cleanup(attempt_id: str, container_id: str = None) -> dict:
    return controller.call('cleanup', {'attempt_id': attempt_id, 'container_id': container_id}, timeout=60)


def run_attempt(attempt_id: str, execution_id: str, image_id: str, prepared: dict, on_event=None,
                timeout_seconds: int = None) -> dict:
    """一次完整运行：启动容器、读取日志、收集状态；落库由调用方完成。"""
    handle = start(attempt_id, execution_id, image_id, prepared, on_event)
    outcome = attach(attempt_id, handle['container_id'], timeout_seconds or prepared['timeout_seconds'],
                     prepared['secrets'], on_event or (lambda event: None))
    outcome['container_id'] = handle['container_id']
    outcome['reused_container'] = bool(handle.get('reused'))
    outcome['state_files'] = {}
    if prepared['state_paths']:
        outcome['state_files'] = collect(attempt_id, prepared['state_paths'])['files']
    return outcome


def environment_status(db=None) -> dict:
    from backend.runtime_builds import controller_status
    return controller_status()
