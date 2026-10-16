"""受信控制器的 Docker 操作。

控制器只接受平台生成的标识与已审核运行定义：镜像、命令、挂载和宿主路径都由
这里拼装，上传内容与用户输入不会进入 docker argv；所有命令都是 argv 列表，
不经过 shell。运行目录固定在宿主 tmpfs 下，容器只挂载本次运行自己的目录。
"""
import hashlib
import json
import os
import queue
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path, PurePosixPath
from packaging.requirements import InvalidRequirement, Requirement
from backend.config import settings

CONTAINER_WORKDIR = '/work'
ATTEMPT_LABEL = 'airdrop.attempt'
EXECUTION_LABEL = 'airdrop.execution'
PROTOCOL_LABEL = 'airdrop.protocol'
MANAGED_LABEL = 'airdrop.managed'
ID_PATTERN = re.compile(r'^[0-9a-f]{32}$')
DEPENDENCY_FILE = re.compile(r'^requirements[A-Za-z0-9._-]*\.txt$')
MAX_FILES = 400
MAX_INPUT_BYTES = 64 * 1024 * 1024
MAX_STATE_BYTES = 16 * 1024 * 1024
READ_CHUNK = 65536
BUILD_LOG_TAIL = 8000


class NativeError(RuntimeError):
    """控制器无法完成这次操作。"""


class NativeUnavailable(NativeError):
    """Docker 不可用或本机不支持容器运行。"""


class NativeTimeout(NativeError):
    """操作超过声明时限。"""


def binary() -> list[str]:
    parts = [str(part) for part in settings.native_docker_bin if str(part).strip()]
    if not parts:
        raise NativeUnavailable('未配置 Docker 客户端路径')
    if settings.native_docker_host:
        parts = [*parts, '--host', settings.native_docker_host]
    return parts


def require_id(value, name='attempt_id') -> str:
    text = str(value or '')
    if not ID_PATTERN.fullmatch(text):
        raise NativeError(f'{name} 必须是平台生成的 32 位标识')
    return text


def run_root() -> Path:
    root = Path(settings.native_run_root)
    root.mkdir(parents=True, exist_ok=True)
    return root


def attempt_dir(attempt_id: str) -> Path:
    """本次运行的私有宿主目录；只允许平台生成的标识作为目录名。"""
    return run_root() / require_id(attempt_id)


def disk_floor_ok() -> tuple[bool, str]:
    """磁盘低水位检查：低于配置下限时拒绝启动新运行。"""
    try:
        root = run_root()
        free = shutil.disk_usage(root).free
    except OSError as exc:
        return False, '运行目录不可用：' + (exc.strerror or type(exc).__name__)
    floor = settings.native_disk_floor_mib * 1024 * 1024
    if free < floor:
        return False, f'运行目录可用空间不足（剩余 {free // (1024 * 1024)} MiB，下限 {settings.native_disk_floor_mib} MiB）'
    return True, ''


def _execute(args, timeout=None, on_output=None, check=False, input_bytes=None, cwd=None):
    """运行 docker 子进程；输出按到达顺序回调，绝不使用 shell。"""
    try:
        process = subprocess.Popen([*binary(), *args], stdin=subprocess.PIPE if input_bytes is not None else subprocess.DEVNULL,
                                   stdout=subprocess.PIPE, stderr=subprocess.STDOUT, cwd=cwd)
    except (OSError, ValueError) as exc:
        raise NativeUnavailable('Docker 客户端无法启动：' + (str(exc.strerror or exc) if exc.strerror else str(exc))) from None
    collected, started = [], time.monotonic()
    if input_bytes is not None:
        try:
            process.stdin.write(input_bytes)
            process.stdin.close()
        except OSError:
            pass
    try:
        while True:
            block = process.stdout.readline()
            if not block:
                break
            text = block.decode('utf-8', 'replace')
            collected.append(text)
            del collected[:-2000]
            if on_output:
                on_output(text)
            if timeout is not None and time.monotonic() - started > timeout:
                process.kill()
                process.wait()
                raise NativeTimeout('Docker 命令超过时限：' + ' '.join(args[:2]))
        code = process.wait()
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()
        if process.stdout:
            process.stdout.close()
    output = ''.join(collected)
    if check and code != 0:
        raise NativeError('Docker 命令失败（退出码 {0}）：{1}'.format(code, output.strip()[-500:] or ' '.join(args[:2])))
    return {'code': code, 'output': output}


def version():
    """控制器能力探测：版本、架构、基础镜像是否已在本地。"""
    result = _execute(['version', '--format', '{{.Client.Version}}|{{.Server.Version}}|{{.Server.Arch}}'])
    if result['code'] != 0:
        raise NativeUnavailable('无法连接 Docker：' + result['output'].strip()[-300:])
    client, server, arch = (result['output'].strip().split('|') + ['', '', ''])[:3]
    return {'client': client, 'server': server, 'arch': arch or 'unknown'}


def image_exists(image_id) -> bool:
    if not image_id:
        return False
    return _execute(['image', 'inspect', str(image_id)]).get('code') == 0


def image_present(reference: str) -> bool:
    return _execute(['image', 'inspect', str(reference)]).get('code') == 0


def image_digest(reference: str):
    result = _execute(['image', 'inspect', '--format', '{{.Id}}', str(reference)])
    return result['output'].strip() if result['code'] == 0 else ''


def image_freeze(image_id: str) -> str:
    """镜像内已安装依赖的冻结清单，作为环境锁的一部分。"""
    result = _execute(['run', '--rm', '--network', 'none', '--entrypoint', 'python', str(image_id),
                       '-m', 'pip', 'freeze'], timeout=settings.native_build_timeout_seconds)
    if result['code'] != 0:
        raise NativeError('无法读取镜像内的依赖清单：' + result['output'].strip()[-300:])
    return result['output'].strip()


def find_container(attempt_id: str):
    result = _execute(['ps', '-aq', '--filter', f'label={ATTEMPT_LABEL}={require_id(attempt_id)}'], check=True)
    lines = [line.strip() for line in result['output'].splitlines() if line.strip()]
    return lines[0] if lines else None


def container_ids(label: str):
    result = _execute(['ps', '-aq', '--filter', f'label={MANAGED_LABEL}=1', '--filter', f'label={label}'], check=True)
    return [line.strip() for line in result['output'].splitlines() if line.strip()]


def inspect_container(container_id: str) -> dict:
    result = _execute(['inspect', '--format', '{{json .State}}', container_id])
    if result['code'] != 0:
        return {'status': 'MISSING', 'running': False, 'exit_code': None, 'missing': True}
    try:
        state = json.loads(result['output'].strip().splitlines()[-1])
    except (ValueError, IndexError):
        raise NativeError('无法解析容器状态')
    return {'status': str(state.get('Status') or 'unknown').upper(), 'running': bool(state.get('Running')),
            'exit_code': state.get('ExitCode'), 'oom_killed': bool(state.get('OOMKilled')),
            'started_at': state.get('StartedAt'), 'finished_at': state.get('FinishedAt'), 'missing': False}


def container_labels(container_id: str) -> dict:
    result = _execute(['inspect', '--format', '{{json .Config.Labels}}', container_id])
    if result['code'] != 0:
        return {}
    try:
        return json.loads(result['output'].strip().splitlines()[-1]) or {}
    except (ValueError, IndexError):
        return {}


def write_inputs(attempt_id: str, files: dict) -> Path:
    """把本次运行的代码与输入文件写进私有目录；路径先做工件内相对校验。"""
    root = attempt_dir(attempt_id)
    if root.exists():
        shutil.rmtree(root)
    root.mkdir(parents=True, exist_ok=True)
    os.chmod(root, 0o700)
    total = 0
    if len(files) > MAX_FILES:
        raise NativeError(f'本次运行文件数量超过 {MAX_FILES}')
    for name, data in files.items():
        path = PurePosixPath(str(name))
        if path.is_absolute() or '..' in path.parts or '\\' in str(name) or ':' in str(name) or not path.parts:
            raise NativeError('运行输入包含非法路径：' + str(name)[:120])
        total += len(data)
        if total > MAX_INPUT_BYTES:
            raise NativeError('本次运行输入超过 64 MiB')
        target = root.joinpath(*path.parts)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        os.chmod(target, 0o600)
    return root


def grant_container_access(root: Path):
    """容器内以非 root 运行，需要能读取目录与输入文件。"""
    user = str(settings.native_container_user or '')
    if not user:
        return
    if not hasattr(os, 'chown'):
        # 非 POSIX 宿主只用于本地受控验证，容器用户归属在这里无从设置。
        return None
    try:
        uid, _, gid = user.partition(':')
        for current, folders, names in os.walk(root):
            os.chown(current, int(uid), int(gid or uid))
            for name in folders:
                os.chown(Path(current) / name, int(uid), int(gid or uid))
            for name in names:
                os.chown(Path(current) / name, int(uid), int(gid or uid))
        os.chmod(root, 0o700)
    except (OSError, ValueError, AttributeError):
        # 控制器不是 root 时退化为组可读，并让调用方把这点写进运行记录。
        for current, folders, names in os.walk(root):
            for name in [*folders, *names]:
                try:
                    os.chmod(Path(current) / name, 0o660)
                except OSError:
                    pass
        os.chmod(root, 0o770)
        return False
    return True


def read_workdir_file(attempt_id: str, name: str) -> bytes:
    """只读取本次运行目录内的文件，供状态回写与排障使用。"""
    root = attempt_dir(attempt_id).resolve()
    path = PurePosixPath(str(name))
    if path.is_absolute() or '..' in path.parts or '\\' in str(name) or not path.parts:
        raise NativeError('状态路径非法：' + str(name)[:120])
    target = root.joinpath(*path.parts)
    resolved = target.resolve()
    if root != resolved and root not in resolved.parents:
        raise NativeError('状态路径越出本次运行目录：' + str(name)[:120])
    if not resolved.is_file() or resolved.is_symlink():
        return None
    data = resolved.read_bytes()
    if len(data) > MAX_STATE_BYTES:
        raise NativeError('状态文件超过 16 MiB：' + str(name)[:120])
    return data


def collect_state(attempt_id: str, paths: list) -> dict:
    collected = {}
    for name in paths:
        data = read_workdir_file(attempt_id, name)
        if data is None:
            collected[str(name)] = None
            continue
        collected[str(name)] = {'sha256': hashlib.sha256(data).hexdigest(), 'size': len(data), 'data': data}
    return collected


def cleanup(attempt_id: str, container_id=None):
    """删除容器与本次运行目录；重复调用安全。"""
    attempt = require_id(attempt_id)
    ids = [container_id] if container_id else []
    ids.extend(container_ids(f'{ATTEMPT_LABEL}={attempt}'))
    for value in [item for item in ids if item]:
        _execute(['rm', '-f', str(value)])
    root = Path(settings.native_run_root)
    try:
        target = attempt_dir(attempt)
        if target.parent.resolve() != root.resolve():
            raise NativeError('拒绝清理非运行目录')
        shutil.rmtree(target, ignore_errors=True)
    except OSError as exc:
        raise NativeError('运行目录清理失败：' + (exc.strerror or type(exc).__name__)) from None


def build_dockerfile(dependencies: list[str], python_version: str) -> bytes:
    """Download wheels from the configured index, then install offline without build hooks."""
    names = []
    for name in dependencies:
        pure = PurePosixPath(str(name))
        if pure.is_absolute() or '..' in pure.parts or not DEPENDENCY_FILE.fullmatch(pure.name):
            raise NativeError('依赖构建只接受 requirements*.txt：' + str(name)[:120])
        names.append(pure.name)
    if not names:
        raise NativeError('缺少依赖声明文件，无法构建运行环境')
    lines = ['ARG AIRDROP_BASE_IMAGE', 'FROM ${AIRDROP_BASE_IMAGE}',
             'ARG AIRDROP_PYPI_INDEX_URL',
             'ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1',
             'COPY airdrop-dependencies/ /tmp/airdrop-dependencies/',
             'USER root', 'RUN mkdir -p /tmp/airdrop-wheelhouse']
    requirements = ' '.join(f'-r /tmp/airdrop-dependencies/{name}' for name in names)
    lines.append('RUN python -m pip download --only-binary=:all: '
                 '--index-url="${AIRDROP_PYPI_INDEX_URL}" --dest /tmp/airdrop-wheelhouse ' + requirements)
    lines.append('RUN python -m pip install --no-cache-dir --no-index '
                 '--find-links=/tmp/airdrop-wheelhouse ' + requirements)
    lines.append('RUN rm -rf /tmp/airdrop-dependencies /tmp/airdrop-wheelhouse')
    return ('\n'.join(lines) + '\n').encode()


def validate_dependency_requirements(dependencies: list[str], files: dict) -> None:
    """Allow package specifiers only; forbid pip directives and arbitrary source URLs."""
    for name in dependencies:
        try:
            source = files[name].decode('utf-8-sig')
        except (KeyError, UnicodeError):
            raise NativeError('依赖声明文件缺失或不是 UTF-8：' + str(name)[:120]) from None
        for number, raw in enumerate(source.splitlines(), 1):
            line = raw.partition('#')[0].strip()
            if not line:
                continue
            try:
                requirement = Requirement(line)
            except InvalidRequirement:
                raise NativeError(f'只允许标准 Python 包要求；请移除 pip 指令或 URL（{name}:{number}）') from None
            if requirement.url:
                raise NativeError(f'只允许标准 Python 包要求；不接受依赖 URL（{name}:{number}）')


def build_image(environment_key: str, base_image: str, python_version: str, dependencies: list, files: dict,
                on_output=None) -> dict:
    """构建并锁定依赖环境；返回镜像标识与依赖锁定结果。"""
    if not re.fullmatch(r'[0-9a-f]{64}', str(environment_key or '')):
        raise NativeError('环境键必须是平台生成的摘要')
    for name in dependencies:
        if name not in files and PurePosixPath(str(name)).name not in files:
            raise NativeError('依赖声明文件不在本版本工件中：' + str(name)[:120])
    validate_dependency_requirements(list(dependencies), files)
    context = run_root() / ('build-' + environment_key[:24])
    if context.exists():
        shutil.rmtree(context, ignore_errors=True)
    (context / 'airdrop-dependencies').mkdir(parents=True, exist_ok=True)
    for name in dependencies:
        pure = PurePosixPath(str(name))
        source_name = str(name) if str(name) in files else pure.name
        if source_name not in files:
            raise NativeError('依赖声明文件不在本版本工件中：' + str(name)[:120])
        (context / 'airdrop-dependencies' / pure.name).write_bytes(files[source_name])
    dockerfile = context / 'Dockerfile'
    dockerfile.write_bytes(build_dockerfile(list(dependencies), python_version))
    tag = 'airdrop-runtime:' + environment_key[:24]
    iidfile = context / 'image.id'
    try:
        result = _execute(['buildx', 'build', '--network', 'default', '--progress', 'plain', '--load',
                           '--iidfile', str(iidfile), '--build-arg', 'AIRDROP_BASE_IMAGE=' + base_image,
                           '--build-arg', 'AIRDROP_PYPI_INDEX_URL=' + settings.native_pypi_index_url,
                           '--tag', tag, '--file', str(dockerfile), str(context)],
                          timeout=settings.native_build_timeout_seconds, on_output=on_output)
        if result['code'] != 0:
            raise NativeError('依赖构建失败：' + (result['output'].strip()[-800:] or 'docker build 返回非零'))
        image_id = iidfile.read_text(encoding='utf-8').strip() if iidfile.is_file() else ''
        if not image_id:
            image_id = image_digest(tag)
        if not image_id:
            raise NativeError('依赖构建完成但未取得镜像标识')
        lock = {'format': 'airdrop-runtime-lock-v1', 'environment_key': environment_key, 'image': image_id,
                'tag': tag, 'base_image': base_image, 'python_version': python_version,
                'dependencies': sorted(str(name) for name in dependencies), 'freeze': image_freeze(image_id),
                'built_at': time.time()}
        return {'image_id': image_id, 'tag': tag, 'lock': json.dumps(lock, ensure_ascii=False, sort_keys=True),
                'freeze': lock['freeze'], 'log': result['output'][-BUILD_LOG_TAIL:]}
    finally:
        shutil.rmtree(context, ignore_errors=True)


def create_container(attempt_id: str, execution_id: str, image_id: str, subdir: str, arguments: list, environment: dict,
                     tmpfs_mib: int, network: str) -> str:
    """按审核定义创建容器；同一 attempt 重复调用返回同一容器。"""
    attempt, execution = require_id(attempt_id), require_id(execution_id, 'execution_id')
    existing = find_container(attempt)
    if existing:
        return existing
    root = attempt_dir(attempt)
    mount = f'type=bind,source={root},target={CONTAINER_WORKDIR}'
    args = ['create', '--name', 'airdrop-' + attempt[:16],
            '--label', f'{MANAGED_LABEL}=1', '--label', f'{ATTEMPT_LABEL}={attempt}',
            '--label', f'{EXECUTION_LABEL}={execution}', '--label', f'{PROTOCOL_LABEL}=airdrop-native-runtime-v1',
            '--network', str(network), '--read-only', '--cap-drop', 'ALL',
            '--security-opt', 'no-new-privileges=true',
            '--pids-limit', str(settings.native_pids),
            '--memory', f'{settings.native_memory_mib}m', '--memory-swap', f'{settings.native_memory_mib}m',
            '--cpus', str(settings.native_cpus),
            '--tmpfs', f'/tmp:rw,nosuid,nodev,size={tmpfs_mib}m',
            '--mount', mount]
    if settings.native_container_user:
        args += ['--user', str(settings.native_container_user)]
    if environment:
        env_file = root / '.airdrop-env'
        env_file.write_text('\n'.join(f'{name}={value}' for name, value in sorted(environment.items())), encoding='utf-8')
        os.chmod(env_file, 0o600)
        # 环境变量绑定会出现在容器配置里，敏感参数请在运行定义中使用文件字段。
        args += ['--env-file', str(env_file)]
    workdir = CONTAINER_WORKDIR + (('/' + subdir.strip('/')) if subdir else '')
    command = ['python', '-B', '-u', *arguments]
    try:
        result = _execute([*args, '--workdir', workdir, '--log-driver', 'none', str(image_id), *command])
    finally:
        # 容器配置已经拿到这些值，宿主上的临时环境文件不再保留。
        if environment:
            (root / '.airdrop-env').unlink(missing_ok=True)
    if result['code'] != 0:
        raise NativeError('容器创建失败：' + (result['output'].strip()[-500:] or 'docker create 返回非零'))
    container_id = result['output'].strip().splitlines()[-1].strip() if result['output'].strip() else ''
    if not container_id:
        raise NativeError('容器创建完成但未返回容器标识')
    return container_id


def kill_container(container_id: str, signal: str = 'SIGINT', timeout: int = 30):
    _execute(['kill', '--signal', str(signal), str(container_id)], timeout=timeout)


def remove_container(container_id: str):
    _execute(['rm', '-f', str(container_id)])


def attach(container_id: str, on_chunk, idle_timeout: float, deadline=None, on_stop=None) -> dict:
    """在启动容器前接入 stdout/stderr，避免短任务在 attach 前退出而丢失日志。"""
    state = inspect_container(container_id)
    if state.get('status') == 'CREATED':
        command = ['start', '--attach', str(container_id)]
    elif state.get('running'):
        command = ['attach', '--no-stdin', str(container_id)]
    else:
        raise NativeError('容器已结束，无法重新连接未持久化的原始输出；请保留现场并人工核查')
    try:
        process = subprocess.Popen([*binary(), *command],
                                   stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    except (OSError, ValueError) as exc:
        raise NativeUnavailable('无法连接容器输出：' + str(exc)) from None
    blocks = queue.Queue()

    def reader():
        try:
            while True:
                block = os.read(process.stdout.fileno(), READ_CHUNK)
                if not block:
                    break
                blocks.put(block)
        except (OSError, ValueError):
            pass
        finally:
            blocks.put(None)

    thread = threading.Thread(target=reader, name='airdrop-attach-' + str(container_id)[:12], daemon=True)
    thread.start()
    last, timed_out = time.monotonic(), False
    try:
        while True:
            try:
                block = blocks.get(timeout=0.5)
            except queue.Empty:
                if process.poll() is not None and blocks.empty():
                    break
                now = time.monotonic()
                if deadline is not None and now > deadline:
                    timed_out = True
                    if on_stop:
                        on_stop('TIMEOUT')
                    kill_container(container_id, 'SIGTERM')
                    time.sleep(min(5.0, settings.native_stop_grace_seconds))
                    kill_container(container_id, 'SIGKILL')
                    deadline = None
                    last = now + 3600
                    continue
                if idle_timeout and now - last > idle_timeout:
                    raise NativeTimeout('容器输出长时间静止，已按失联处理')
                continue
            if block is None:
                break
            last = time.monotonic()
            on_chunk(block)
    finally:
        if process.poll() is None:
            process.kill()
        process.wait()
        if process.stdout:
            process.stdout.close()
        thread.join(timeout=5)
    state = inspect_container(container_id)
    return {'exit_code': state.get('exit_code'), 'state': state.get('status'), 'oom_killed': state.get('oom_killed'),
            'timed_out': timed_out}


def base_image_status(base_image: str) -> bool:
    return image_present(base_image)


def network_exists(name: str) -> bool:
    return _execute(['network', 'inspect', str(name)]).get('code') == 0


def network_gateway(name: str) -> str:
    result = _execute(['network', 'inspect', '--format', '{{(index .IPAM.Config 0).Gateway}}', str(name)])
    return result['output'].strip() if result['code'] == 0 else ''


def network_subnet(name: str) -> str:
    result = _execute(['network', 'inspect', '--format', '{{(index .IPAM.Config 0).Subnet}}', str(name)])
    return result['output'].strip() if result['code'] == 0 else ''
