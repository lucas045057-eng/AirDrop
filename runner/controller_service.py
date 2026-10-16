"""受信 Runner Controller 服务端。

只接受平台自己拼装的请求：Docker 命令、镜像、挂载与宿主路径都来自后端记录，
请求里没有任何用户命令行、宿主路径或 shell。Worker 通过本地 socket 提交运行
定义与本次输入，控制器负责建目录、起容器、流转日志、收集状态和清理。
"""
import base64
import json
import os
import socket
import threading
import time
from pathlib import Path
from backend.config import settings
from runner import environment, log_stream, native

PROTOCOL = 'airdrop-controller-v1'
MAX_MESSAGE = 4 * 1024 * 1024
LABEL_FILTER = 'label'


class RequestError(RuntimeError):
    """请求本身不合法，返回明确错误而不是堆栈。"""


def text(value, name='字段', limit=200) -> str:
    data = str(value or '').strip()
    if not data:
        raise RequestError(f'{name} 不能为空')
    if len(data) > limit:
        raise RequestError(f'{name} 过长')
    return data


def digest(value, name='摘要') -> str:
    data = str(value or '').strip()
    if len(data) != 64 or any(character not in '0123456789abcdef' for character in data):
        raise RequestError(f'{name} 必须是 64 位十六进制摘要')
    return data


def decode_files(payload: dict) -> dict:
    files = payload.get('files') or {}
    if not isinstance(files, dict) or len(files) > native.MAX_FILES:
        raise RequestError('输入文件结构不合法')
    decoded = {}
    for name, value in files.items():
        try:
            decoded[str(name)] = base64.b64decode(str(value), validate=True)
        except (ValueError, TypeError):
            raise RequestError('输入文件编码不合法：' + str(name)[:80]) from None
    return decoded


def op_ping(payload: dict) -> dict:
    status = {'docker': False, 'protocol': PROTOCOL, 'time': time.time()}
    try:
        version = native.version()
    except native.NativeError as exc:
        status['error'] = str(exc)
        return status
    status.update({'docker': True, 'version': version.get('server') or version.get('client'),
                   'client': version.get('client'), 'arch': version.get('arch')})
    status['base_image'] = settings.native_runtime_base_image
    status['base_image_present'] = native.base_image_status(settings.native_runtime_base_image)
    status['network'] = environment.network_name()
    status['network_ready'] = native.network_exists(environment.network_name())
    status['isolation'] = {'ok': bool(environment._verification['ok']), 'reason': environment._verification['reason']}
    status['run_root'] = str(settings.native_run_root)
    return status


def op_image(payload: dict) -> dict:
    image_id = text(payload.get('image_id'), 'image_id', 200)
    return {'exists': native.image_exists(image_id), 'image_id': image_id}


def op_environment(payload: dict) -> dict:
    """启动前检查：Docker、网络、隔离验证与磁盘水位。"""
    return environment.prepare_runtime(payload.get('image_id'))


def op_build(payload: dict, emit) -> dict:
    environment_key = digest(payload.get('environment_key'), '环境键')
    base_image = text(payload.get('base_image'), 'base_image')
    python_version = text(payload.get('python_version'), 'python_version', 10)
    dependencies = payload.get('dependencies') or []
    if not isinstance(dependencies, list) or not dependencies:
        raise RequestError('构建请求缺少依赖声明文件')

    def output(line):
        emit({'event': 'log', 'data': str(line)[-2000:]})

    files = decode_files(payload)
    result = native.build_image(environment_key, base_image, python_version, dependencies, files, output)
    emit({'event': 'log', 'data': '构建完成：' + str(result['image_id'])[:80] + '\n'})
    return {'image_id': result['image_id'], 'tag': result['tag'], 'lock': result['lock']}


def op_start(payload: dict, emit) -> dict:
    """建立目录、写入输入并启动容器；同一 attempt 重复提交返回同一容器。"""
    attempt_id = native.require_id(payload.get('attempt_id'))
    execution_id = native.require_id(payload.get('execution_id'), 'execution_id')
    image_id = text(payload.get('image_id'), 'image_id', 200)
    network = text(payload.get('network') or settings.native_network, 'network', 100)
    if network != environment.network_name():
        raise RequestError('只允许使用平台专用容器网络')
    if not native.image_exists(image_id):
        raise RequestError('运行环境镜像不存在，请先完成依赖构建')
    existing = native.find_container(attempt_id)
    if existing:
        return {'container_id': existing, 'reused': True, 'attempt_id': attempt_id}
    ok, reason = native.disk_floor_ok()
    if not ok:
        raise RequestError(reason)
    files = decode_files(payload)
    verification = environment.verify_isolation(image_id)
    if not verification['ok']:
        raise RequestError('网络隔离未通过验证，禁止启动业务容器：' + str(verification.get('reason'))[:200])
    root = native.write_inputs(attempt_id, files)
    owned = native.grant_container_access(root)
    arguments = [str(value) for value in (payload.get('arguments') or [])]
    if any('\x00' in value for value in arguments):
        raise native.NativeError('命令行参数不允许包含空字节')
    environment_variables = {str(name): str(value) for name, value in (payload.get('environment') or {}).items()}
    for name in environment_variables:
        if not name.replace('_', '').isalnum() or not name.isupper():
            raise RequestError('环境变量名必须是大写字母、数字与下划线：' + name[:60])
    container_id = native.create_container(attempt_id, execution_id, image_id, str(payload.get('subdir') or ''),
                                           arguments, environment_variables, settings.native_tmpfs_mib, network)
    emit({'event': 'platform', 'data': '容器已创建；启动时先接入实时输出。平台不使用自动重试，停止只终止本次进程。'})
    return {'container_id': container_id, 'reused': False, 'attempt_id': attempt_id,
            'workdir_owned_by_container_user': owned}


def op_attach(payload: dict, emit) -> dict:
    """持续读取容器输出并转发；返回进程退出码与真实容器状态。"""
    attempt_id = native.require_id(payload.get('attempt_id'))
    container_id = text(payload.get('container_id'), 'container_id', 128)
    labels = native.container_labels(container_id)
    if labels.get(native.ATTEMPT_LABEL) != attempt_id:
        raise RequestError('容器不属于这次 attempt，拒绝读取输出')
    timeout = int(payload.get('timeout_seconds') or settings.native_timeout_seconds)
    secrets = [str(value) for value in (payload.get('secrets') or [])]
    redactor = log_stream.Redactor(secrets)
    total = {'bytes': 0}

    def chunk(block: bytes):
        total['bytes'] += len(block)
        text_value = redactor.feed(block)
        if text_value:
            emit({'event': 'log', 'data': base64.b64encode(text_value.encode()).decode(), 'encoding': 'base64'})

    deadline = time.monotonic() + timeout
    emit({'event': 'platform', 'data': '正在接入容器启动输出。'})
    result = native.attach(container_id, chunk, idle_timeout=max(120, timeout), deadline=deadline,
                           on_stop=lambda reason: emit({'event': 'platform', 'data': '运行超过声明时限，已按超时停止容器。'}))
    tail = redactor.flush()
    if tail:
        emit({'event': 'log', 'data': base64.b64encode(tail.encode()).decode(), 'encoding': 'base64'})
    emit({'event': 'platform', 'data': '进程已退出（退出码 ' + str(result.get('exit_code')) + '）。'})
    return {'attempt_id': attempt_id, 'container_id': container_id, 'exit_code': result.get('exit_code'),
            'state': result.get('state'), 'timed_out': bool(result.get('timed_out')),
            'oom_killed': bool(result.get('oom_killed')), 'log_bytes': total['bytes'],
            'redacted': redactor.count}


def op_status(payload: dict) -> dict:
    container_id = str(payload.get('container_id') or '').strip()
    attempt_id = payload.get('attempt_id')
    if not container_id and attempt_id:
        container_id = native.find_container(native.require_id(attempt_id)) or ''
    if not container_id:
        return {'found': False, 'state': 'MISSING', 'running': False}
    state = native.inspect_container(container_id)
    return {'found': not state.get('missing'), **state}


def op_stop(payload: dict) -> dict:
    """按声明信号停止：先软中断，超过等待时间后强制终止。"""
    container_id = str(payload.get('container_id') or '').strip()
    attempt_id = payload.get('attempt_id')
    if not container_id and attempt_id:
        container_id = native.find_container(native.require_id(attempt_id)) or ''
    if not container_id:
        return {'stopped': True, 'found': False, 'state': 'MISSING', 'reason': '容器不存在或已经结束'}
    initial = native.inspect_container(container_id)
    if initial.get('status') == 'CREATED':
        native.remove_container(container_id)
        return {'stopped': True, 'found': True, 'state': 'EXITED', 'exit_code': None,
                'forced': False, 'not_started': True}
    signal_name = str(payload.get('signal') or 'SIGINT').upper()
    if signal_name not in {'SIGINT', 'SIGTERM', 'SIGKILL'}:
        raise RequestError('停止信号不被允许：' + signal_name[:20])
    escalate = bool(payload.get('escalate', True))
    grace = max(1, min(600, int(payload.get('grace_seconds') or 60)))
    native.kill_container(container_id, signal_name)
    deadline = time.time() + grace
    while time.time() < deadline:
        state = native.inspect_container(container_id)
        if not state.get('running'):
            return {'stopped': True, 'found': True, 'state': state.get('status'), 'exit_code': state.get('exit_code'),
                    'forced': False}
        time.sleep(0.5)
    if not escalate:
        # 由调用方决定是否升级到更强信号，控制器不擅自 SIGKILL。
        state = native.inspect_container(container_id)
        return {'stopped': False, 'found': True, 'state': state.get('status'), 'exit_code': state.get('exit_code'),
                'forced': False, 'needs_escalation': True}
    native.kill_container(container_id, 'SIGKILL')
    state = native.inspect_container(container_id)
    return {'stopped': not state.get('running'), 'found': True, 'state': state.get('status'),
            'exit_code': state.get('exit_code'), 'forced': True}


def op_collect(payload: dict) -> dict:
    attempt_id = native.require_id(payload.get('attempt_id'))
    paths = [str(value) for value in (payload.get('paths') or [])]
    if len(paths) > 40:
        raise RequestError('状态文件数量超过限制')
    collected, missing = {}, []
    for name, value in native.collect_state(attempt_id, paths).items():
        if value is None:
            missing.append(name)
            continue
        collected[name] = {'sha256': value['sha256'], 'size': value['size'],
                           'data': base64.b64encode(value['data']).decode()}
    return {'attempt_id': attempt_id, 'files': collected, 'missing': missing}


def op_cleanup(payload: dict) -> dict:
    attempt_id = native.require_id(payload.get('attempt_id'))
    native.cleanup(attempt_id, payload.get('container_id'))
    return {'attempt_id': attempt_id, 'cleaned': True}


OPERATIONS = {'ping': op_ping, 'image': op_image, 'environment': op_environment, 'build': op_build,
              'start': op_start, 'attach': op_attach, 'status': op_status, 'stop': op_stop,
              'collect': op_collect, 'cleanup': op_cleanup}
STREAMING = {'build', 'start', 'attach'}


def dispatch(request: dict, emit=lambda event: None) -> dict:
    if not isinstance(request, dict):
        raise RequestError('请求必须是对象')
    if request.get('protocol') != PROTOCOL:
        raise RequestError('协议版本不匹配')
    operation = str(request.get('op') or '')
    handler = OPERATIONS.get(operation)
    if handler is None:
        raise RequestError('不支持的操作：' + operation[:40])
    payload = request.get('payload') or {}
    if not isinstance(payload, dict):
        raise RequestError('payload 必须是对象')
    if operation in STREAMING:
        return handler(payload, emit)
    return handler(payload)


def handle_connection(handle, address=None):
    buffer = b''
    try:
        handle.settimeout(None)
        while True:
            try:
                block = handle.recv(65536)
            except OSError:
                return
            if not block:
                return
            buffer += block
            if len(buffer) > MAX_MESSAGE:
                send(handle, {'ok': False, 'error': '请求超出大小限制'})
                return
            while b'\n' in buffer:
                line, buffer = buffer.split(b'\n', 1)
                if not line.strip():
                    continue
                process_line(handle, line)
    finally:
        try:
            handle.close()
        except OSError:
            pass


def process_line(handle, line: bytes):
    lock = threading.Lock()

    def emit(event):
        with lock:
            send(handle, {'type': 'event', **event})

    try:
        request = json.loads(line)
    except ValueError:
        send(handle, {'ok': False, 'error': '请求不是合法 JSON'})
        return
    try:
        result = dispatch(request, emit)
    except RequestError as exc:
        send(handle, {'ok': False, 'error': str(exc)[:500]})
    except native.NativeError as exc:
        send(handle, {'ok': False, 'error': str(exc)[:500]})
    except Exception as exc:  # 控制器不把内部堆栈或路径细节交给调用方
        send(handle, {'ok': False, 'error': '控制器内部错误：' + type(exc).__name__})
    else:
        send(handle, {'ok': True, 'result': result})


def send(handle, message: dict):
    body = (json.dumps(message, ensure_ascii=False) + '\n').encode()
    try:
        handle.sendall(body)
    except OSError:
        pass


def serve(address=None, backlog=16):
    """监听本地 socket；只允许本机调用，不做远程暴露。"""
    target = str(address or settings.native_socket_path)
    if target.startswith('tcp://'):
        host, _, port = target[len('tcp://'):].partition(':')
        if host not in {'127.0.0.1', 'localhost', '::1'}:
            raise RuntimeError('控制器只允许监听本机回环地址')
        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server.bind((host, int(port)))
    else:
        if not hasattr(socket, 'AF_UNIX'):
            raise RuntimeError('当前平台不支持 Unix socket；开发机上请显式使用 tcp://127.0.0.1:<端口>')
        path = Path(target)
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            if path.is_socket():
                path.unlink()
            else:
                raise RuntimeError('控制器 socket 路径已被占用：' + target)
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(target)
        os.chmod(target, 0o660)
        if hasattr(os, 'chown'):
            os.chown(target, -1, settings.native_socket_gid)
    server.listen(backlog)
    try:
        while True:
            try:
                handle, peer = server.accept()
            except OSError:
                break
            threading.Thread(target=handle_connection, args=(handle, peer), daemon=True).start()
    finally:
        server.close()


if __name__ == '__main__':
    serve()
