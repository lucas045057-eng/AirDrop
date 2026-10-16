"""Loopback-only browser QA with injected fake AI. Never a production provider option.

两套夹具都在这里，且都要求显式隔离的本地环境：
1) 智能分析：注入假 AI Provider（AIRDROP_BROWSER_QA=local-test-only）；
2) 原生运行：假 docker 客户端 + 本机回环控制器 + 受控样本数据（AIRDROP_QA_NATIVE=local-test-only）。
"""
import asyncio
import json
import os
import sys
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path
if os.environ.get('AIRDROP_BROWSER_QA') != 'local-test-only':
    raise RuntimeError('This fixture requires an explicitly isolated local QA environment')
from backend.main import app, lifespan as original_lifespan
from backend.db import Session
from backend.models import SystemSetting, ScriptFeature, User
from backend.storage import cipher
from backend.auth import admin
from fastapi import Depends
from worker.process_limits import own_peak_rss
from packages.feature_protocol import FeatureDefinition
from adapters.transformation import wrapper
from worker.adapter_worker import AdapterWorker


class FakeProvider:
    async def plan(self, payload):
        import json
        from pathlib import Path
        from backend.config import settings
        counter=settings.data_dir/'fake-calls.json'
        calls=json.loads(counter.read_text()) if counter.exists() else 0
        counter.write_text(json.dumps(calls+1))
        if any('model_failure' in str(f['source_functions']) for f in payload['features']):
            return {'invalid': True}
        if any('slow_function' in str(f['source_functions']) for f in payload['features']):
            await asyncio.Future()
        files={}
        for chunk in payload['sources']:
            lines=['']*max(item['line'] for item in chunk['lines'])
            for item in chunk['lines']: lines[item['line']-1]=item['text']
            files[chunk['file']]='\n'.join(lines).encode()
        generated={}; features=[]
        for item in payload['features']:
            f=FeatureDefinition.model_validate(item)
            if not f.blocking_reasons:
                generated_code=wrapper(f,files); generated[f.entrypoint]=generated_code
            features.append(f.model_dump())
        return {'features':features,'generated_files':generated,'replacements':{},'unresolved':[]}


@asynccontextmanager
async def qa_lifespan(app):
    async with original_lifespan(app):
        with Session.begin() as db:
            db.add(SystemSetting(key='ai',value={'url':'https://qa-model.example.invalid/v1','model':'browser-test-only','encrypted_key':cipher().encrypt(b'fake-test-only').decode()}))
            if NATIVE_QA:
                from backend.config import settings
                from backend.feature_reviews import approval_digest
                user = db.query(User).filter_by(username=settings.admin_username).one()
                feature = db.get(ScriptFeature, NATIVE['context']['feature_id'])
                feature.approved_by = user.id
                feature.approved_at = time.time()
                feature.approval_sha256 = approval_digest(feature)
        yield

app.router.lifespan_context=qa_lifespan

@app.get('/api/qa/resources',dependencies=[Depends(admin)])
def resources():
    return {'api_peak_rss_bytes':own_peak_rss()}

async def worker_loop():
    import json
    from pathlib import Path
    from backend.config import settings
    worker=AdapterWorker(provider_factory=lambda config:FakeProvider())
    while True:
        if await worker.run_once():
            (settings.data_dir/'worker-resources.json').write_text(json.dumps({'worker_parent_peak_rss_bytes':own_peak_rss()}))
        await asyncio.sleep(.1)


# ---------------------------------------------------------------------------
# 原生运行夹具（AIRDROP_QA_NATIVE=local-test-only）
#
# 浏览器验收需要真实链路：受控样本 → 依赖环境 → 容器启动 → 大日志 → 状态回写。
# 这里把 docker 换成测试用假客户端，并在 API 进程里跑一个受控控制器线程（Worker
# 仍通过回环 socket 连接，与部署形态一致）。数据全部由夹具自己生成，不读取任何
# 真实钱包或凭据。
# ---------------------------------------------------------------------------
NATIVE_QA = os.environ.get('AIRDROP_QA_NATIVE') == 'local-test-only'
NATIVE_SPEC = {
    'protocol': 'airdrop-native-runtime-v1',
    'entrypoint': 'main.py',
    'wallet_binding': {'format': 'CSV', 'target_path': 'inputs/wallets.csv', 'chain': 'EVM',
                       'fields': {'address': 'identity', 'private_key': 'credential'}},
    'state_bindings': [{'path': 'state.csv', 'format': 'CSV', 'account_key': 'address'}],
    'capabilities': ['selected_wallet_private_keys'],
    'timeout_seconds': 600,
}
NATIVE_CODE = (
    '"""受控原生样本：只读平台生成的输入文件，不访问任何外部服务。"""\n'
    'import csv\n'
    'import pathlib\n'
    '\n'
    'ROOT = pathlib.Path(__file__).resolve().parent\n'
    '\n'
    'def wallets():\n'
    '    with (ROOT / "inputs" / "wallets.csv").open(newline="", encoding="utf-8") as handle:\n'
    '        return [row["address"] for row in csv.DictReader(handle)]\n'
    '\n'
    'def main():\n'
    '    print("QA-NATIVE-START")\n'
    '    for address in wallets():\n'
    '        print("check-in", address[:10])\n'
    '    print("QA-NATIVE-END")\n'
    '\n'
    'if __name__ == "__main__":\n'
    '    main()\n'
)
NATIVE = {}


def native_settings():
    """把本进程的原生能力指向假 docker 与本机回环控制器。"""
    from backend.config import settings
    from backend.vault import identity
    from tests.fakes import FAKE_DOCKER
    settings.native_execution_enabled = True
    settings.native_docker_bin = [sys.executable, str(FAKE_DOCKER)]
    settings.native_docker_host = ''
    settings.native_network_subnet = '172.31.240.0/24'
    settings.native_stop_grace_seconds = 1
    settings.native_blocked_targets = ['127.0.0.1:9']
    settings.native_isolation_check_seconds = 30
    settings.native_socket_path = str(os.environ.get('AIRDROP_NATIVE_SOCKET_PATH') or 'tcp://127.0.0.1:8765')
    settings.native_run_root = Path(os.environ.get('AIRDROP_NATIVE_RUN_ROOT') or (Path(settings.data_dir) / 'runs'))
    os.environ['FAKE_DOCKER_AUTO_EXIT'] = '1'
    os.environ['FAKE_DOCKER_ATTACH_CHUNK'] = '65536'
    os.environ['FAKE_DOCKER_OUTPUT_FILE'] = str(Path(settings.data_dir) / 'qa-native-output.log')
    address = identity('EVM', '0x' + '01' * 32)[0]
    os.environ['FAKE_DOCKER_WRITE_STATE'] = json.dumps(
        [['state.csv', f'address,points\n{address},12\n']], ensure_ascii=False)


def native_controller():
    """控制器线程：只监听本机回环地址，接口与部署里的 runner.controller_service 一致。"""
    from backend.config import settings
    from runner import controller, controller_service
    threading.Thread(target=controller_service.serve, args=(str(settings.native_socket_path),), daemon=True).start()
    for _ in range(100):
        if controller.online(timeout=1):
            return
        time.sleep(.1)
    raise RuntimeError('QA 原生控制器未能启动：' + str(settings.native_socket_path))


def native_heartbeat():
    """Worker 的就绪门读 Runner 的 heartbeat.json；夹具只写存活标记，不跑旧 Runner。"""
    from backend.config import settings
    def beat():
        while True:
            try:
                settings.runner_dir.mkdir(parents=True, exist_ok=True)
                (settings.runner_dir / 'heartbeat.json').write_text(
                    json.dumps({'at': time.time(), 'qa': True}), encoding='utf-8')
            except OSError:
                pass
            time.sleep(2)
    threading.Thread(target=beat, daemon=True).start()


def native_log_file():
    """假容器输出的大日志：验证增量加载与完整下载，不是产品日志格式。"""
    from backend.config import settings
    path = Path(settings.data_dir) / 'qa-native-output.log'
    if not path.exists():
        lines = ['QA-NATIVE-START\n']
        # 大于日志分页尺寸且小于界面保留窗口，浏览器可以逐页回到开头。
        lines += [f'[qa] 第 {index} 行：受控样本输出，用于验证大日志增量加载与完整下载。\n'
                  for index in range(1, 4201)]
        lines.append('QA-NATIVE-END\n')
        path.write_text(''.join(lines), encoding='utf-8')
    os.environ['FAKE_DOCKER_OUTPUT_FILE'] = str(path)
    os.environ['FAKE_DOCKER_EXIT_CODE'] = '0'
    return path


def native_seed():
    """建立可运行的原生样本：农民……（夹具）"""
    import hashlib
    from backend.config import settings
    from backend.feature_reviews import approval_digest, runtime_digest_for
    from backend.analysis_jobs import digest as manifest_digest
    from backend.models import (Artifact, Project, ProjectAccount, ProjectWalletBinding, RuntimeBuild,
                                ScriptFeature, ScriptPackage, ScriptVersion, Wallet, WalletSecret)
    from backend.storage import artifact_fingerprint, encode_files, write_blob
    from backend.vault import encrypt_wallet, identity
    from packages.feature_protocol import FeatureDefinition
    from packages.runtime_protocol import NativeRuntimeSpec
    from tests.fakes import make_image
    state = Path(os.environ['FAKE_DOCKER_STATE'])
    state.mkdir(parents=True, exist_ok=True)
    images = state / 'images.json'
    if not images.exists():
        images.write_text(json.dumps([settings.native_runtime_base_image]), encoding='utf-8')
    image_id = make_image(state, None, key='a' * 64)
    keys = ['0x' + f'{index:02x}' * 32 for index in range(1, 5)]
    normalized_keys = [identity('EVM', private_key)[1] for private_key in keys]
    with Session.begin() as db:
        wallets = []
        for index in range(len(keys)):
            address, private_key = identity('EVM', keys[index])
            wallet = Wallet(chain='EVM', address=address, name=f'qa-native-{index + 1}', is_test=True)
            db.add(wallet)
            db.flush()
            db.add(WalletSecret(wallet_id=wallet.id, ciphertext=encrypt_wallet(wallet, private_key)))
            wallets.append(wallet)
        project = Project(name='QA 原生项目', description='浏览器验收夹具：受控样本与假 docker')
        package = ScriptPackage(name='qa-native-sample')
        db.add_all([project, package])
        db.flush()
        for wallet in wallets[:2]:
            db.add(ProjectWalletBinding(project_id=project.id, wallet_id=wallet.id))
        path, digest = write_blob(encode_files({'main.py': NATIVE_CODE.encode()}))
        artifact = Artifact(path=path, sha256=digest, kind='sources')
        db.add(artifact)
        db.flush()
        version = ScriptVersion(package_id=package.id, version=1, original_artifact_id=artifact.id,
                                source_artifact_id=artifact.id, analysis={}, status='PUBLISHED',
                                manifest={'package_id': package.id, 'package_version': 1, 'entrypoint': 'main.py',
                                          'retry_safe': True})
        db.add(version)
        db.flush()
        lock_sha256 = hashlib.sha256(b'qa-native-lock').hexdigest()
        build = RuntimeBuild(package_id=package.id, environment_key='b' * 64, image_id=image_id, status='READY',
                             python_version='3.12', base_image=settings.native_runtime_base_image,
                             dependency_files=['requirements.txt'], lock_sha256=lock_sha256)
        db.add(build)
        db.flush()
        spec = {**NATIVE_SPEC, 'dependency_lock_sha256': lock_sha256}
        evidence = {'file': 'main.py', 'line': 1, 'end_line': 1, 'type': 'entrypoint', 'function': 'main',
                    'excerpt': 'def main():', 'confidence': 1.0, 'feasibility': 'POSSIBLE', 'manual_required': False}
        checkin_definition = FeatureDefinition(feature_id='checkin', name='QA 原生签到',
                                               description='仅用于本地浏览器验收的原生签到样本。',
                                               entrypoint_candidate='main.py', source_functions=['main'],
                                               evidence=[evidence], confidence=1.0, risk_level='low',
                                               entrypoint='main.py').model_dump()
        feature = ScriptFeature(version_id=version.id, feature_key='checkin',
                                definition=checkin_definition,
                                code_sha256=digest, version_manifest_sha256=manifest_digest(version.manifest),
                                cipher_sha256=artifact_fingerprint(artifact), status='APPROVED', runtime_spec=spec,
                                runtime_status='APPROVED', runtime_build_id=build.id)
        db.add(feature)
        db.flush()
        feature.approval_sha256 = approval_digest(feature)
        feature.runtime_review_sha256 = runtime_digest_for(feature, NativeRuntimeSpec.model_validate(spec),
                                                          {'main.py': NATIVE_CODE.encode()})
        report_definition = FeatureDefinition(feature_id='report', name='QA 报表导出',
                                              description='用于验证缺少运行定义时不能创建任务。',
                                              entrypoint_candidate='main.py', source_functions=['main'],
                                              evidence=[evidence], confidence=1.0, risk_level='low',
                                              entrypoint='main.py').model_dump()
        draft = ScriptFeature(version_id=version.id, feature_key='report',
                              definition=report_definition, code_sha256='c' * 64,
                              status='APPROVED')
        db.add(draft)
        db.flush()
        # 冲突预览需要既有资料：一个字段不同（冲突），一个完全一致（复用）。
        db.add(ProjectAccount(project_id=project.id, package_id=package.id, wallet_id=wallets[0].id,
                              public_config={'proxy': 'http://127.0.0.1:9000'}, encrypted_config='', revision=1,
                              source='qa-seed'))
        db.add(ProjectAccount(project_id=project.id, package_id=package.id, wallet_id=wallets[1].id,
                              public_config={'proxy': 'http://127.0.0.1:9100'}, encrypted_config='', revision=1,
                              source='qa-seed'))
        addresses = [wallet.address for wallet in wallets]
        rows = [['address', 'private_key', 'proxy'],
                [addresses[0], normalized_keys[0], 'http://127.0.0.1:9001'],
                [addresses[1], normalized_keys[1], 'http://127.0.0.1:9100'],
                [addresses[2], normalized_keys[2], 'http://127.0.0.1:9200'],
                [addresses[3], normalized_keys[0], 'http://127.0.0.1:9300']]
        NATIVE['context'] = {'project_id': project.id, 'project_name': project.name,
                             'package_id': package.id, 'version_id': version.id,
                             'feature_id': feature.id, 'draft_feature_id': draft.id, 'build_id': build.id,
                             'image_id': image_id, 'log_file': str(native_log_file()),
                             'wallets': [{'id': wallet.id, 'address': wallet.address, 'name': wallet.name}
                                         for wallet in wallets]}
        NATIVE['import_csv'] = '\n'.join(','.join(row) for row in rows) + '\n'
        os.environ['FAKE_DOCKER_WRITE_STATE'] = json.dumps(
            [['state.csv', f'address,points\n{addresses[0]},12\n']], ensure_ascii=False)


if NATIVE_QA:
    from pydantic import BaseModel

    class QaNativeEnabled(BaseModel):
        enabled: bool

    native_settings()
    if '--native-worker' not in sys.argv:
        native_seed()
        native_controller()
        native_heartbeat()

    @app.post('/api/qa/native/enabled', dependencies=[Depends(admin)])
    def qa_native_enabled(body: QaNativeEnabled):
        from backend.config import settings
        settings.native_execution_enabled = bool(body.enabled)
        return {'native_execution_enabled': settings.native_execution_enabled}

    @app.get('/api/qa/native/context', dependencies=[Depends(admin)])
    def qa_native_context():
        return NATIVE['context']

    @app.get('/api/qa/native/state', dependencies=[Depends(admin)])
    def qa_native_state():
        from backend.config import settings
        from backend.models import Execution, ExecutionAttempt, Worker
        with Session() as db:
            return {
                'executions': [{'id': row.id, 'status': row.status, 'error': row.error,
                                'worker_id': row.worker_id, 'current_attempt_id': row.current_attempt_id}
                               for row in db.query(Execution).all()],
                'attempts': [{'id': row.id, 'status': row.status, 'error': row.error}
                            for row in db.query(ExecutionAttempt).all()],
                'workers': [{'id': row.id, 'status': row.status, 'heartbeat_at': row.heartbeat_at}
                            for row in db.query(Worker).all()],
                'heartbeat_file': (settings.runner_dir / 'heartbeat.json').exists(),
                'native_enabled': settings.native_execution_enabled,
            }

    @app.get('/api/qa/native/import-csv', dependencies=[Depends(admin)])
    def qa_native_import_csv():
        return {'filename': 'qa-accounts.csv', 'text': NATIVE['import_csv']}


if __name__=='__main__':
    import sys
    if '--worker' in sys.argv:
        asyncio.run(worker_loop())
    elif '--native-worker' in sys.argv:
        # 与部署一致：原生运行在独立 Worker 进程里领取，浏览器关闭后仍继续。
        from worker import main as worker_main
        asyncio.run(worker_main.main())
    else:
        import uvicorn
        from fastapi.responses import FileResponse, JSONResponse
        from fastapi.staticfiles import StaticFiles
        dist = Path(__file__).resolve().parents[2] / 'frontend' / 'dist'
        if (dist / 'index.html').is_file():
            # 浏览器验收直接吃构建产物：不再需要 Vite 开发服务器。
            app.mount('/assets', StaticFiles(directory=dist / 'assets'), name='qa-assets')

            @app.get('/{path:path}')
            def qa_console(path: str):
                target = dist / path
                if path.startswith('api/'):
                    return JSONResponse({'detail': '接口不存在'}, status_code=404)
                if path and target.is_file():
                    return FileResponse(target)
                return FileResponse(dist / 'index.html')
        uvicorn.run(app,host='127.0.0.1',port=int(os.environ['AIRDROP_QA_API_PORT']),log_level='warning')
