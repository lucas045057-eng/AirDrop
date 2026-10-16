"""Trusted Runner Controller 客户端。

Worker 只通过本地 Unix socket 与控制器通信，请求固定为平台生成的标识与
已审核的定义；控制器不接受用户提供的 shell 命令或宿主路径。这里不导入
任何上传代码。
"""
import json
import socket
from pathlib import Path
from backend.config import settings

PROTOCOL = 'airdrop-controller-v1'
MAX_MESSAGE = 4 * 1024 * 1024


class ControllerError(RuntimeError):
    """控制器返回了明确错误。"""


class ControllerUnavailable(ControllerError):
    """控制器未部署、未启动或无法连接。"""


class _Reader:
    def __init__(self, sock):
        self.sock = sock
        self.buffer = b''

    def line(self) -> dict:
        while b'\n' not in self.buffer:
            block = self.sock.recv(65536)
            if not block:
                raise ControllerError('执行控制器连接提前关闭')
            self.buffer += block
            if len(self.buffer) > MAX_MESSAGE:
                raise ControllerError('执行控制器响应超出限制')
        line, self.buffer = self.buffer.split(b'\n', 1)
        try:
            return json.loads(line)
        except ValueError:
            raise ControllerError('执行控制器返回了非法响应') from None


def socket_path():
    value = str(settings.native_socket_path)
    # tcp:// 用于开发机上的受控验证，保持原样；其他情况按文件系统路径处理。
    return value if value.startswith('tcp://') else Path(value)


def _connect(timeout: float):
    path = str(socket_path())
    # 生产使用本地 Unix socket；tcp://127.0.0.1:port 只用于开发机上的受控验证。
    if path.startswith('tcp://'):
        host, _, port = path[len('tcp://'):].partition(':')
        if host not in {'127.0.0.1', 'localhost', '::1'}:
            raise ControllerUnavailable('执行控制器只允许本机回环地址')
        handle = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    else:
        handle = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    handle.settimeout(timeout)
    try:
        handle.connect((host, int(port)) if path.startswith('tcp://') else path)
    except OSError as exc:
        handle.close()
        raise ControllerUnavailable('无法连接执行控制器（' + (exc.strerror or type(exc).__name__) + '）') from None
    return handle


def _send(handle, operation: str, payload: dict):
    body = json.dumps({'protocol': PROTOCOL, 'op': operation, 'payload': payload}, ensure_ascii=False)
    handle.sendall((body + '\n').encode())


def call(operation: str, payload: dict, timeout: float = 30.0, on_event=None) -> dict:
    """一条请求一条响应；非流式操作的事件交给 on_event，不静默丢弃。"""
    handle = _connect(timeout)
    try:
        _send(handle, operation, payload)
        reader = _Reader(handle)
        while True:
            message = reader.line()
            if message.get('type') == 'event':
                if on_event:
                    on_event(message)
                continue
            if message.get('ok'):
                return message.get('result') or {}
            raise ControllerError(str(message.get('error') or '执行控制器返回错误')[:500])
    except socket.timeout:
        raise ControllerError('执行控制器响应超时') from None
    except OSError as exc:
        raise ControllerError('执行控制器连接中断（' + (exc.strerror or type(exc).__name__) + '）') from None
    finally:
        handle.close()


def stream(operation: str, payload: dict, on_event, idle_timeout: float = 120.0) -> dict:
    """持续读取事件直到终态；超过空闲时间没有消息即视为控制器失联。"""
    handle = _connect(idle_timeout)
    try:
        _send(handle, operation, payload)
        reader = _Reader(handle)
        while True:
            message = reader.line()
            if message.get('type') == 'event':
                on_event(message)
                continue
            if message.get('ok'):
                return message.get('result') or {}
            raise ControllerError(str(message.get('error') or '执行控制器返回错误')[:500])
    except socket.timeout:
        raise ControllerError('执行控制器长时间没有输出，已按失联处理') from None
    except OSError as exc:
        raise ControllerError('执行控制器连接中断（' + (exc.strerror or type(exc).__name__) + '）') from None
    finally:
        handle.close()


def online(timeout: float = 3.0):
    """探测控制器；返回结果字典或 None。"""
    try:
        return call('ping', {}, timeout)
    except ControllerError:
        return None
