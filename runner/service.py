"""Secret-free, network-disabled Runner. OS isolation is supplied by Compose, not Python."""
import asyncio
import base64
import json
import os
import re
import signal
import sys
import tempfile
import time
from pathlib import Path,PurePosixPath
from adapters.wallets import encode
from packages.protocol import Manifest,WalletResult

BUS=Path(os.environ.get('AIRDROP_RUNNER_DIR','data/runner'))
OUTPUT_LIMIT=1024*1024


def atomic_json(path,value):
    path.parent.mkdir(parents=True,exist_ok=True)
    temp=path.with_suffix('.tmp');temp.write_text(json.dumps(value,ensure_ascii=False),encoding='utf-8');os.replace(temp,path)


def limits():
    if sys.platform=='linux':
        import resource
        resource.setrlimit(resource.RLIMIT_CPU,(310,310))
        resource.setrlimit(resource.RLIMIT_FSIZE,(16*1024*1024,16*1024*1024))
        resource.setrlimit(resource.RLIMIT_NOFILE,(128,128))
        resource.setrlimit(resource.RLIMIT_NPROC,(32,32))


async def terminate(process):
    try:
        # Kill the Linux process group even if its parent already exited, so normal
        # background children cannot hold stdout open or outlive the task.
        if sys.platform=='linux':os.killpg(process.pid,signal.SIGKILL)
        elif process.returncode is None:process.kill()
    except ProcessLookupError:pass
    if process.returncode is None:
        await process.wait()


async def run_process(root,entry,args,env,timeout,cancel,progress=None):
    command=[sys.executable,'-I',str(Path(__file__).with_name('child.py').resolve()),str(root),entry,*args]
    kwargs={'start_new_session':True,'preexec_fn':limits} if sys.platform=='linux' else {}
    process=await asyncio.create_subprocess_exec(*command,cwd=root,env=env,stdin=asyncio.subprocess.DEVNULL,stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.STDOUT,**kwargs)
    chunks=[];size=0;error='';status='DONE';start=time.monotonic()
    async def reader():
        nonlocal size
        while chunk:=await process.stdout.read(4096):
            size+=len(chunk)
            # Keep draining after the limit so terminating a noisy child cannot deadlock.
            if size>OUTPUT_LIMIT:continue
            chunks.append(chunk)
    reading=asyncio.create_task(reader())
    try:
        while process.returncode is None:
            if progress:progress(b''.join(chunks).decode('utf-8','replace')[-64000:])
            if cancel():status='CANCELLED';error='管理员取消执行';break
            if time.monotonic()-start>timeout:status='TIMEOUT';error='脚本执行超过时限';break
            if size>OUTPUT_LIMIT:status='FAILED';error='日志输出超过限制';break
            await asyncio.sleep(.05)
        await terminate(process)
        await reading
        if size>OUTPUT_LIMIT and status=='DONE':
            status='FAILED';error='日志输出超过限制'
        text=b''.join(chunks).decode('utf-8','replace')
        results=[]
        for line in text.splitlines():
            if line.startswith('AIRDROP_RESULT:'):
                try:results.append(WalletResult.model_validate_json(line.split(':',1)[1]).model_dump(mode='json'))
                except ValueError:status='FAILED';error='脚本输出了无效的结果协议'
        if process.returncode!=0 and status=='DONE':status='FAILED';error=f'脚本进程退出码 {process.returncode}'
        return {'status':status,'exit_code':process.returncode,'results':results,'log':text[-64000:],'error':error}
    finally:
        await terminate(process)
        if not reading.done():reading.cancel()


async def run_job(payload,cancel=lambda:False,progress=None):
    manifest=Manifest.model_validate(payload['manifest'])
    files=payload['files'];records=payload['wallets'];batch_size=payload.get('batch_size',100)
    if len(files)>300 or not records or len(records)>1000:raise ValueError('Runner 输入规模不合法')
    total=0;all_results=[];logs=[];exit_code=0;overall='DONE';error=''
    deadline=time.monotonic()+manifest.timeout_seconds
    with tempfile.TemporaryDirectory(prefix='airdrop-run-') as directory:
        root=Path(directory).resolve()
        for name,encoded in files.items():
            path=PurePosixPath(name)
            if path.is_absolute() or '..' in path.parts or '\\' in name or ':' in name:raise ValueError('Runner 拒绝非法路径')
            value=base64.b64decode(encoded,validate=True);total+=len(value)
            if total>50*1024*1024:raise ValueError('Runner 输入体积过大')
            target=root/name;target.parent.mkdir(parents=True,exist_ok=True);target.write_bytes(value)
        if manifest.wallet_path in files:raise ValueError('钱包映射路径与上传文件冲突')
        wallet_path=root/manifest.wallet_path;wallet_path.parent.mkdir(parents=True,exist_ok=True)
        size=1 if manifest.execution_mode=='single' else batch_size
        for offset in range(0,len(records),size):
            batch=[dict(r) for r in records[offset:offset+size]]
            if manifest.high_risk_compatibility:
                for record in batch:record['private_key']='MOCK_PRIVATE_KEY_DO_NOT_SIGN_'+record['wallet_id']
            wallet_path.write_bytes(encode(batch,manifest.wallet_format,manifest.wallet_field_mapping))
            env={'PATH':os.environ.get('PATH',''),'HOME':directory,'TMPDIR':directory,'PYTHONIOENCODING':'utf-8','AIRDROP_MODE':'mock','AIRDROP_WALLET_PATH':str(wallet_path),'AIRDROP_EXECUTION_ID':payload['execution_id']}
            if manifest.wallet_input_mode=='environment':env[manifest.environment_name]=str(wallet_path)
            args=[arg.replace('{wallet_path}',str(wallet_path)) for arg in manifest.arguments]
            entries=manifest.steps if manifest.execution_mode=='workflow' else [manifest.entrypoint]
            final_batch={};step_failed=False
            for entry in entries:
                remaining=max(.01,deadline-time.monotonic())
                response=await run_process(root,entry,args,env,remaining,cancel,progress)
                logs.append(response['log']);exit_code=response['exit_code']
                step_results={r['address']:r for r in response['results']}
                if len(step_results)!=len(response['results']):
                    response['status']='FAILED';response['error']='同一步骤包含重复地址结果'
                for r in batch:
                    address=r['address']
                    result=step_results.get(address,{'address':address,'status':'UNKNOWN','message':'步骤未输出钱包结果'})
                    previous=final_batch.get(address)
                    if previous and previous['status'] not in {'SUCCESS','ALREADY_DONE','SKIPPED'}:continue
                    final_batch[address]=result
                if response['status']!='DONE':
                    overall=response['status'];error=response['error'];step_failed=True
                    # Missing outputs carry explicit timeout/cancel/failure, not a false business success.
                    for r in batch:
                        if r['address'] not in step_results:final_batch[r['address']]={'address':r['address'],'status':overall,'message':error}
                    break
            all_results.extend(final_batch.values())
            if step_failed:break
        return {'status':overall,'exit_code':exit_code,'results':all_results,'log':'\n'.join(logs)[-64000:],'error':error}


async def serve():
    if sys.platform!='linux' or os.environ.get('AIRDROP_ISOLATED_RUNNER')!='1':
        raise RuntimeError('上传脚本只能在明确隔离的 Linux Runner 容器中执行')
    BUS.mkdir(parents=True,exist_ok=True);active={}
    while True:
        atomic_json(BUS/'heartbeat.json',{'at':time.time(),'pid':os.getpid()})
        for folder in BUS.iterdir():
            if not folder.is_dir() or not re.fullmatch('[0-9a-f]{32}',folder.name):continue
            request=folder/'request.json'
            if not request.exists() or (folder/'response.json').exists() or folder.name in active:continue
            if len(active)>=2:break
            async def execute(folder=folder):
                try:
                    if (folder/'started.json').exists():
                        response={'status':'MANUAL_REQUIRED','results':[],'exit_code':None,'error':'Runner 中断后请求不自动重放','log':''}
                    else:
                        atomic_json(folder/'started.json',{'at':time.time()})
                        payload=json.loads((folder/'request.json').read_text(encoding='utf-8'))
                        response=await run_job(payload,lambda:(folder/'cancel').exists(),lambda log:atomic_json(folder/'progress.json',{'log':log}))
                    atomic_json(folder/'response.json',response)
                except Exception as exc:
                    atomic_json(folder/'response.json',{'status':'FAILED','results':[],'exit_code':None,'error':f'Runner 输入或运行异常: {type(exc).__name__}','log':''})
            active[folder.name]=asyncio.create_task(execute())
        for key,task in list(active.items()):
            if task.done():task.result();del active[key]
        await asyncio.sleep(.2)


if __name__=='__main__':asyncio.run(serve())
