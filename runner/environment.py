"""原生运行环境：专用网络、隔离验证与依赖构建。

容器允许业务所需的外网访问，但平台管理面、宿主敏感网段与云元数据地址必须
先被阻断。宿主防火墙规则由部署脚本在目标主机上执行（控制器没有 NET_ADMIN），
控制器只负责在业务启动前用探针复核这些规则确实生效。
"""
import json
import secrets
import time
from backend.config import settings
from runner import native

BRIDGE_PREFIX = 'br-'
PROBE_TIMEOUT = 3
_verification = {'at': 0.0, 'ok': False, 'reason': '尚未验证', 'image': None}


def network_name() -> str:
    return str(settings.native_network)


def ensure_network(on_output=None) -> dict:
    """创建本次部署专用的容器网络；已存在时核对子网。"""
    name = network_name()
    if native.network_exists(name):
        subnet = native.network_subnet(name)
        if subnet and settings.native_network_subnet and subnet != settings.native_network_subnet:
            raise native.NativeError(f'已有网络 {name} 的子网是 {subnet}，与配置 {settings.native_network_subnet} 不一致')
        return {'created': False, 'name': name, 'subnet': subnet}
    result = native._execute(['network', 'create', '--driver', 'bridge', '--subnet', settings.native_network_subnet,
                              '--opt', 'com.docker.network.bridge.enable_icc=false',
                              '--label', f'{native.MANAGED_LABEL}=1', name], on_output=on_output)
    if result['code'] != 0:
        raise native.NativeError('容器网络创建失败：' + (result['output'].strip()[-300:] or name))
    return {'created': True, 'name': name, 'subnet': native.network_subnet(name) or settings.native_network_subnet}


def bridge_name() -> str:
    """Docker 网桥接口名；防火墙规则需要按接口生效。"""
    result = native._execute(['network', 'inspect', '--format', '{{.Id}}', network_name()])
    if result['code'] != 0 or not result['output'].strip():
        raise native.NativeError('无法取得网络标识：' + network_name())
    return BRIDGE_PREFIX + result['output'].strip().splitlines()[-1][:12]


def firewall_rules() -> list[str]:
    """转发规则拦截敏感目标，INPUT 规则封锁容器到宿主的全部端口。"""
    interface = bridge_name()
    rules = ['iptables -N AIRDROP-NATIVE 2>/dev/null || true', 'iptables -F AIRDROP-NATIVE']
    for target in settings.native_blocked_targets:
        host = str(target).split(':')[0]
        rules.append(f'iptables -A AIRDROP-NATIVE -d {host} -j DROP')
    for cidr in settings.native_blocked_cidrs:
        rules.append(f'iptables -A AIRDROP-NATIVE -d {cidr} -j DROP')
    rules.append(f'iptables -C DOCKER-USER -i {interface} -j AIRDROP-NATIVE 2>/dev/null || '
                 f'iptables -I DOCKER-USER 1 -i {interface} -j AIRDROP-NATIVE')
    rules.extend(['iptables -N AIRDROP-NATIVE-INPUT 2>/dev/null || true',
                  'iptables -F AIRDROP-NATIVE-INPUT',
                  'iptables -A AIRDROP-NATIVE-INPUT -j DROP',
                  f'iptables -C INPUT -i {interface} -j AIRDROP-NATIVE-INPUT 2>/dev/null || '
                  f'iptables -I INPUT 1 -i {interface} -j AIRDROP-NATIVE-INPUT'])
    return rules


def probe_script() -> str:
    targets = json.dumps(list(settings.native_blocked_targets))
    return ('# AIRDROP_META_PROBE\nimport json,socket\n'
            f'targets=json.loads({targets!r})\n'
            'result={}\n'
            'for item in targets:\n'
            '    host,_,port=item.partition(":")\n'
            '    handle=socket.socket()\n'
            '    handle.settimeout(float(' + str(PROBE_TIMEOUT) + '))\n'
            '    try:\n'
            '        handle.connect((host,int(port or 80)));result[item]="REACHABLE"\n'
            '    except TimeoutError:\n'
            '        result[item]="BLOCKED_TIMEOUT"\n'
            '    except ConnectionRefusedError:\n'
            '        result[item]="CONNECTION_REFUSED"\n'
            '    except OSError:\n'
            '        result[item]="PROBE_ERROR"\n'
            '    finally:\n'
            '        handle.close()\n'
            'print("AIRDROP_PROBE:"+json.dumps(result))\n')


def verify_host_input(image_id: str) -> dict:
    """Verify the host INPUT policy against a temporary, known-live TCP listener."""
    gateway = native.network_gateway(network_name())
    if not gateway:
        return {'ok': False, 'status': 'NO_GATEWAY', 'reason': '无法取得专用网络网关，未验证宿主 INPUT 隔离'}
    port = 40000 + secrets.randbelow(20000)
    server = ('# AIRDROP_HOST_PROBE_SERVER\n'
              'import socket,sys,time\n'
              'listener=socket.socket()\n'
              'listener.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1)\n'
              'listener.bind((sys.argv[1],int(sys.argv[2])))\n'
              'listener.listen(8)\n'
              'listener.settimeout(0.5)\n'
              'deadline=time.monotonic()+30\n'
              'while time.monotonic()<deadline:\n'
              '    try: connection,_=listener.accept()\n'
              '    except socket.timeout: continue\n'
              '    try: connection.sendall(b"AIRDROP-HOST-PROBE")\n'
              '    finally: connection.close()\n')
    client = ('# AIRDROP_HOST_PROBE_CLIENT\n'
              'import socket,sys\n'
              'host,port,mode=sys.argv[1],int(sys.argv[2]),sys.argv[3]\n'
              'connection=socket.socket()\n'
              'connection.settimeout(3)\n'
              'try:\n'
              '    connection.connect((host,port))\n'
              '    if mode=="host" and connection.recv(64)!=b"AIRDROP-HOST-PROBE": status="BAD_REPLY"\n'
              '    else: status="READY" if mode=="host" else "REACHABLE"\n'
              'except TimeoutError: status="BLOCKED_TIMEOUT"\n'
              'except ConnectionRefusedError: status="CONNECTION_REFUSED"\n'
              'except OSError: status="PROBE_ERROR"\n'
              'finally: connection.close()\n'
              'print("AIRDROP_HOST_PROBE:"+status)\n')
    server_id = ''
    try:
        created = native._execute(['run', '--rm', '--detach', '--name',
                                   'airdrop-host-probe-' + secrets.token_hex(6), '--network', 'host',
                                   '--read-only', '--cap-drop', 'ALL',
                                   '--security-opt', 'no-new-privileges=true', '--pids-limit', '8',
                                   '--memory', '64m', '--log-driver', 'none', '--entrypoint', 'python',
                                   image_id, '-c', server, gateway, str(port)], timeout=15)
        if created['code'] != 0 or not created['output'].strip():
            return {'ok': False, 'status': 'SERVER_START_FAILED',
                    'reason': '无法启动受控宿主监听器，未验证 INPUT 隔离'}
        server_id = created['output'].strip().splitlines()[-1]
        host = native._execute(['run', '--rm', '--network', 'host', '--read-only', '--cap-drop', 'ALL',
                                '--security-opt', 'no-new-privileges=true', '--pids-limit', '8', '--memory', '64m',
                                '--log-driver', 'none', '--entrypoint', 'python', image_id, '-c', client,
                                gateway, str(port), 'host'], timeout=10)
        if host['code'] != 0 or 'AIRDROP_HOST_PROBE:READY' not in host['output']:
            return {'ok': False, 'status': 'LISTENER_NOT_CONFIRMED',
                    'reason': '受控宿主监听器未能从宿主网络访问，隔离探针无效'}
        isolated = native._execute(['run', '--rm', '--network', network_name(), '--read-only', '--cap-drop', 'ALL',
                                    '--security-opt', 'no-new-privileges=true', '--pids-limit', '8', '--memory', '64m',
                                    '--log-driver', 'none', '--entrypoint', 'python', image_id, '-c', client,
                                    gateway, str(port), 'bridge'], timeout=10)
        marker = 'AIRDROP_HOST_PROBE:'
        status = next((line.split(marker, 1)[1].strip() for line in isolated['output'].splitlines()
                       if marker in line), 'PROBE_ERROR')
        ok = isolated['code'] == 0 and status == 'BLOCKED_TIMEOUT'
        return {'ok': ok, 'status': status, 'target': f'{gateway}:{port}',
                'reason': '宿主 INPUT 隔离已通过受控监听验证' if ok else
                          f'容器到宿主受控监听器未被 INPUT 规则阻断（{status}）'}
    except native.NativeError as exc:
        return {'ok': False, 'status': 'PROBE_ERROR', 'reason': '宿主 INPUT 隔离验证失败：' + str(exc)[:200]}
    finally:
        if server_id:
            native.remove_container(server_id)


def verify_isolation(image_id: str, on_output=None, force=False) -> dict:
    """在业务容器启动前复核网络策略；未通过时不允许启动运行。"""
    global _verification
    now = time.time()
    if not force and _verification['ok'] and image_id == _verification['image'] \
            and now - _verification['at'] < settings.native_isolation_check_seconds:
        return {'ok': True, 'cached': True, 'reason': _verification['reason'], 'checked_at': _verification['at']}
    if not image_id:
        raise native.NativeError('缺少用于隔离验证的镜像标识')
    ensure_network(on_output)
    result = native._execute(['run', '--rm', '--network', network_name(), '--read-only', '--cap-drop', 'ALL',
                              '--security-opt', 'no-new-privileges=true', '--pids-limit', '32',
                              '--memory', '128m', '--label', f'{native.MANAGED_LABEL}=1',
                              '--entrypoint', 'python', str(image_id), '-c', probe_script()],
                             timeout=settings.native_probe_timeout_seconds, on_output=on_output)
    if result['code'] != 0:
        raise native.NativeError('隔离验证容器无法运行：' + result['output'].strip()[-300:])
    payload = {}
    for line in result['output'].splitlines():
        if line.startswith('AIRDROP_PROBE:'):
            try:
                payload = json.loads(line.split(':', 1)[1])
            except ValueError:
                payload = {}
    if not payload:
        raise native.NativeError('隔离验证没有返回结果，禁止启动业务容器')
    unverified = sorted(target for target, state in payload.items() if state != 'BLOCKED_TIMEOUT')
    host_input = verify_host_input(image_id)
    ok = not unverified and host_input['ok']
    reasons = []
    if unverified:
        reasons.append('以下转发目标未证明确实被阻断：' + ', '.join(unverified))
    if not host_input['ok']:
        reasons.append(host_input['reason'])
    reason = '转发与宿主 INPUT 隔离均已通过验证' if ok else '；'.join(reasons)
    _verification = {'at': now, 'ok': ok, 'image': image_id, 'reason': reason}
    return {'ok': ok, 'cached': False, 'targets': payload, 'host_input': host_input, 'checked_at': now,
            'reason': _verification['reason']}


def probe_image() -> str:
    """隔离验证用镜像：优先本地基础镜像，其次最近一次成功构建的运行环境。"""
    if native.image_present(settings.native_runtime_base_image):
        return settings.native_runtime_base_image
    return ''


def prepare_runtime(image_id=None, on_output=None) -> dict:
    """一次运行的启动前环境检查：Docker、网络、隔离验证与磁盘水位。"""
    version = native.version()
    network = ensure_network(on_output)
    image = image_id if (image_id and native.image_exists(image_id)) else probe_image()
    isolation = verify_isolation(image, on_output) if image else {'ok': False, 'reason': '本地缺少可用于验证的基础镜像'}
    disk_ok, disk_reason = native.disk_floor_ok()
    usable = isolation['ok'] and disk_ok
    return {'docker': version, 'network': network, 'bridge': bridge_name(), 'isolation': isolation,
            'disk_ok': disk_ok, 'disk_reason': disk_reason, 'usable': usable,
            'reason': '' if usable else '；'.join(reason for reason in
                                                 [isolation.get('reason', '') if not isolation['ok'] else '',
                                                  disk_reason if not disk_ok else ''] if reason)}
