"""测试用假 docker 客户端。

只实现控制器用到的那部分子命令，并把容器状态写进 FAKE_DOCKER_STATE 目录，
让 Windows 开发机也能验证命令拼装、输出流、停止与状态收集，而不需要真的
Docker。它不是产品代码，只在测试里通过 settings.native_docker_bin 挂载。
"""
import ast
import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path

STATE = Path(os.environ['FAKE_DOCKER_STATE'])
CONTAINERS = STATE / 'containers'
IMAGES = STATE / 'images.json'
NETWORKS = STATE / 'networks.json'
LOG = STATE / 'commands.log'
CHUNK = int(os.environ.get('FAKE_DOCKER_ATTACH_CHUNK', '0'))
DELAY = float(os.environ.get('FAKE_DOCKER_ATTACH_DELAY', '0'))
VALUE_FLAGS = {'name', 'label', 'network', 'cap-drop', 'security-opt', 'pids-limit', 'memory', 'memory-swap',
               'cpus', 'tmpfs', 'mount', 'user', 'env-file', 'workdir', 'log-driver', 'entrypoint', 'format',
               'iidfile', 'file', 'tag', 'build-arg', 'subnet', 'filter', 'signal', 'host', 'pull', 'platform', 'progress',
               'opt', 'driver', 'label', 'log-opt', 'tail'}


def load(path, default):
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return default


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix('.tmp')
    temp.write_text(json.dumps(value, ensure_ascii=False), encoding='utf-8')
    os.replace(temp, path)


def guard(path):
    marker = path.with_suffix('.lock')
    for _ in range(300):
        try:
            os.close(os.open(marker, os.O_CREAT | os.O_EXCL))
            return marker
        except FileExistsError:
            time.sleep(0.01)
    raise RuntimeError('假 docker 状态锁超时')


def release(marker):
    marker.unlink(missing_ok=True)


def digest(value):
    return hashlib.sha256(str(value).encode()).hexdigest()


def split(args):
    """拆出选项与位置参数；镜像总是第一个位置参数，其后是容器内命令。"""
    options, positional, index = {}, [], 0
    while index < len(args):
        item = args[index]
        if item.startswith('--'):
            name = item[2:]
            if '=' in name:
                name, _, value = name.partition('=')
                options[name] = value
                index += 1
                continue
            if name in VALUE_FLAGS and index + 1 < len(args):
                options[name] = args[index + 1]
                index += 2
                continue
            options[name] = True
            index += 1
            continue
        positional.append(item)
        index += 1
    return options, positional


def container_path(container_id):
    return CONTAINERS / f'{container_id}.json'


def container(container_id):
    return load(container_path(container_id), None)


def update(container_id, **values):
    marker = guard(container_path(container_id))
    try:
        data = container(container_id) or {}
        data.update(values)
        save(container_path(container_id), data)
    finally:
        release(marker)


def exit_with(container_id, code):
    update(container_id, running=False, status='exited', exit_code=code)


def write_state(state):
    """模拟脚本在工作目录里写状态文件。"""
    payload = os.environ.get('FAKE_DOCKER_WRITE_STATE')
    if not payload:
        return
    source = ''
    for part in str(state.get('mount') or '').split(','):
        if part.startswith('source='):
            source = part[len('source='):]
    if not source:
        return
    for name, content in json.loads(payload):
        target = Path(source) / name
        target.parent.mkdir(parents=True, exist_ok=True)
        # 模拟容器内 Linux 写入：必须保持 LF，不能触发 Windows 换行翻译。
        target.write_bytes(content.encode('utf-8'))


def command_version(args):
    print('27.5.1|27.5.1|amd64')


def command_image(args):
    action, rest = args[0], args[1:]
    options, positional = split(rest)
    images = load(IMAGES, [])
    if action == 'inspect':
        target = positional[-1] if positional else ''
        if target not in images:
            print('Error: No such image: ' + target, file=sys.stderr)
            return 1
        print(digest(target) if options.get('format') else json.dumps([{'Id': target}]))
        return 0
    print('Error: 假 docker 不支持的镜像命令：' + action, file=sys.stderr)
    return 125


def command_network(args):
    action, rest = args[0], args[1:]
    options, positional = split(rest)
    if action == 'create':
        name = positional[-1]
        marker = guard(NETWORKS)
        try:
            networks = load(NETWORKS, {})
            networks[name] = {'id': digest(name), 'subnet': options.get('subnet', ''),
                              'enable_icc': options.get('opt', '')}
            save(NETWORKS, networks)
        finally:
            release(marker)
        print(digest(name))
        return 0
    networks = load(NETWORKS, {})
    name = positional[0] if positional else ''
    if name not in networks:
        print('Error: no such network ' + name, file=sys.stderr)
        return 1
    fmt = options.get('format')
    if fmt == '{{(index .IPAM.Config 0).Gateway}}':
        print('172.31.240.1')
    elif fmt == '{{(index .IPAM.Config 0).Subnet}}':
        print(networks[name]['subnet'])
    elif fmt == '{{.Id}}':
        print(networks[name]['id'])
    else:
        print(json.dumps([networks[name]]))
    return 0


def command_build(args):
    options, positional = split(args)
    allowed = {'network', 'progress', 'load', 'iidfile', 'build-arg', 'tag', 'file'}
    unsupported = set(options) - allowed
    if unsupported:
        print('Error: unsupported build options: ' + ', '.join(sorted(unsupported)), file=sys.stderr)
        return 125
    if options.get('network') != 'default' or options.get('progress') != 'plain' or not options.get('load'):
        print('Error: build must use default network, plain progress, and local image output', file=sys.stderr)
        return 125
    context = Path(positional[-1])
    dockerfile = Path(str(options.get('file', context / 'Dockerfile')))
    print('#1 [internal] load build definition from Dockerfile')
    print('#2 [internal] load metadata for ' + str(options.get('build-arg', '')))
    print('#3 installing declared dependencies')
    requirements = context / 'airdrop-dependencies' / 'requirements.txt'
    if requirements.is_file() and 'AIRDROP-FAKE-FAIL' in requirements.read_text(encoding='utf-8'):
        print('ERROR: dependency resolution failed', file=sys.stderr)
        return 1
    image_id = 'sha256:' + digest(options.get('tag') or context.name)
    if options.get('iidfile'):
        Path(str(options['iidfile'])).write_text(image_id, encoding='utf-8')
    marker = guard(IMAGES)
    try:
        images = load(IMAGES, [])
        images.extend([image_id, str(options.get('tag', ''))])
        save(IMAGES, images)
    finally:
        release(marker)
    print('Successfully tagged ' + str(options.get('tag', '')))
    return 0


def command_run(args):
    options, positional = split(args)
    command = positional[1:]
    if command[:1] == ['-c']:
        code = command[1] if len(command) > 1 else ''
        if 'AIRDROP_HOST_PROBE_SERVER' in code:
            container_id = digest(options.get('name', code))
            save(container_path(container_id), {'id': container_id, 'name': options.get('name', ''),
                                                'labels': {}, 'running': True, 'status': 'running',
                                                'exit_code': None, 'removed': False,
                                                'network': options.get('network'), 'log_driver': 'none'})
            print(container_id)
            return 0
        if 'AIRDROP_HOST_PROBE_CLIENT' in code:
            state = 'READY' if options.get('network') == 'host' else 'BLOCKED_TIMEOUT'
            print('AIRDROP_HOST_PROBE:' + state)
            return 0
        if 'AIRDROP_META_PROBE' in code:
            expression = code.split('targets=json.loads(', 1)[1].split(')\n', 1)[0]
            targets = json.loads(ast.literal_eval(expression))
            results = {}
            for item in targets:
                host, _, port = item.partition(':')
                if host != '127.0.0.1':
                    results[item] = 'BLOCKED_TIMEOUT'
                    continue
                # Port 9 is the default fixture probe; keep its fake firewall result host-independent.
                if int(port or 80) == 9:
                    results[item] = 'BLOCKED_TIMEOUT'
                    continue
                if int(port or 80) == 1:
                    results[item] = 'CONNECTION_REFUSED'
                    continue
                import socket
                try:
                    with socket.create_connection((host, int(port or 80)), timeout=0.2):
                        results[item] = 'REACHABLE'
                except ConnectionRefusedError:
                    results[item] = 'CONNECTION_REFUSED'
                except TimeoutError:
                    results[item] = 'BLOCKED_TIMEOUT'
                except OSError:
                    results[item] = 'PROBE_ERROR'
            print('AIRDROP_PROBE:' + json.dumps(results))
            return 0
        result = subprocess.run([sys.executable, *command], capture_output=True, text=True)
        sys.stdout.write(result.stdout)
        sys.stderr.write(result.stderr)
        return result.returncode
    if command[:2] == ['-m', 'pip']:
        print('openpyxl==3.1.5\nrequests==2.32.3')
        return 0
    print('fake container output')
    return 0


def command_create(args):
    options, positional = split(args)
    image, command = positional[0], positional[1:]
    labels = {}
    for index, item in enumerate(args):
        if item == '--label':
            name, _, value = args[index + 1].partition('=')
            labels[name] = value
    environment = {}
    if options.get('env-file'):
        for line in Path(str(options['env-file'])).read_text(encoding='utf-8').splitlines():
            name, _, value = line.partition('=')
            environment[name] = value
    container_id = digest(labels.get('airdrop.attempt', 'unknown'))
    save(container_path(container_id), {'id': container_id, 'name': options.get('name', ''), 'labels': labels,
                                        'image': image, 'command': command, 'running': False, 'exit_code': None,
                                        'status': 'created', 'signal': None, 'removed': False,
                                        'mount': options.get('mount', ''), 'network': options.get('network', ''),
                                        'read_only': bool(options.get('read-only')), 'cap_drop': options.get('cap-drop'),
                                        'pids_limit': options.get('pids-limit'), 'memory': options.get('memory'),
                                        'memory_swap': options.get('memory-swap'), 'cpus': options.get('cpus'),
                                        'tmpfs': options.get('tmpfs'), 'security_opt': options.get('security-opt'),
                                        'user': options.get('user'), 'workdir': options.get('workdir'),
                                        'log_driver': options.get('log-driver'), 'env_file': options.get('env-file'),
                                        'environment': environment, 'created_at': time.time()})
    print(container_id)
    return 0


def command_start(args):
    options, positional = split(args)
    container_id = positional[0]
    if container(container_id) is None:
        print('Error: No such container: ' + container_id, file=sys.stderr)
        return 1
    update(container_id, running=True, status='running', started_at=time.time())
    if options.get('attach'):
        return stream_output(container_id)
    print(container_id)
    return 0


def stream_output(container_id):
    source = os.environ.get('FAKE_DOCKER_OUTPUT_FILE')
    payload = Path(source).read_bytes() if source else b'fake container output\n'
    auto_exit = (os.environ.get('FAKE_DOCKER_AUTO_EXIT', '1') == '1'
                 or os.environ.get('FAKE_DOCKER_EXIT_ON_START', '0') == '1')
    exit_code = int(os.environ.get('FAKE_DOCKER_EXIT_CODE', '0'))
    deadline = time.time() + float(os.environ.get('FAKE_DOCKER_HOLD_SECONDS', '20'))
    offset = 0
    while True:
        state = container(container_id) or {}
        if state.get('status') in {'exited', 'dead', 'removed'}:
            break
        if state.get('signal'):
            exit_with(container_id, 137 if state['signal'] == 'SIGKILL' else 130)
            break
        if not state.get('running'):
            print('Error: container is not running', file=sys.stderr)
            return 1
        if offset < len(payload):
            size = CHUNK or len(payload)
            sys.stdout.buffer.write(payload[offset:offset + size])
            sys.stdout.flush()
            offset += size
            if DELAY:
                time.sleep(DELAY)
            continue
        if auto_exit:
            write_state(state)
            exit_with(container_id, exit_code)
            break
        if time.time() > deadline:
            print('Error: 假 docker attach 等待停止超时', file=sys.stderr)
            return 1
        time.sleep(0.02)
    return 0


def command_attach(args):
    options, positional = split(args)
    return stream_output(positional[0])


def command_inspect(args):
    options, positional = split(args)
    container_id = positional[0]
    data = container(container_id)
    if data is None:
        print('Error: No such object: ' + container_id, file=sys.stderr)
        return 1
    state = {'Status': data.get('status', 'created'), 'Running': bool(data.get('running')),
             'ExitCode': data.get('exit_code'), 'OOMKilled': bool(data.get('oom_killed')),
             'StartedAt': '', 'FinishedAt': ''}
    fmt = options.get('format')
    if fmt == '{{json .State}}':
        print(json.dumps(state))
    elif fmt == '{{json .Config.Labels}}':
        print(json.dumps(data.get('labels', {})))
    else:
        print(json.dumps([{'State': state, 'Config': {'Labels': data.get('labels', {})}}]))
    return 0


def command_ps(args):
    wanted = []
    for index, item in enumerate(args):
        if item == '--filter':
            name, _, value = args[index + 1].partition('=')
            if name == 'label':
                label, _, label_value = value.partition('=')
                wanted.append((label, label_value))
    for path in sorted(CONTAINERS.glob('*.json')):
        data = load(path, {})
        if data.get('removed'):
            continue
        if all(data.get('labels', {}).get(name) == value for name, value in wanted):
            print(data.get('id'))
    return 0


def command_kill(args):
    options, positional = split(args)
    container_id = positional[0]
    if container(container_id) is None:
        print('Error: No such container: ' + container_id, file=sys.stderr)
        return 1
    signal_name = str(options.get('signal', 'SIGKILL'))
    update(container_id, signal=signal_name)
    if signal_name == 'SIGKILL':
        exit_with(container_id, 137)
    return 0


def command_rm(args):
    for container_id in [item for item in args if not item.startswith('-')]:
        if container(container_id) is not None:
            update(container_id, removed=True, running=False, status='removed')
    return 0


def main(argv):
    STATE.mkdir(parents=True, exist_ok=True)
    CONTAINERS.mkdir(parents=True, exist_ok=True)
    argv = list(argv)
    if argv[:1] == ['--host']:
        argv = argv[2:]
    if argv[:2] == ['buildx', 'build']:
        subcommand, args = 'build', argv[2:]
    else:
        subcommand, args = (argv[0] if argv else ''), argv[1:]
    with LOG.open('a', encoding='utf-8') as handle:
        handle.write(json.dumps([subcommand, *args]) + '\n')
    handlers = {'version': command_version, 'image': command_image, 'network': command_network,
                'build': command_build, 'run': command_run, 'create': command_create, 'start': command_start,
                'attach': command_attach, 'inspect': command_inspect, 'ps': command_ps, 'kill': command_kill,
                'rm': command_rm}
    handler = handlers.get(subcommand)
    if handler is None:
        print('假 docker 不支持的子命令：' + subcommand, file=sys.stderr)
        return 125
    return handler(args)


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
