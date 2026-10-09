import asyncio
import base64
import json
import os
import shutil
import time
from pathlib import Path
from sqlalchemy import select
from backend.config import settings
from backend.db import Session
from backend.models import Execution,Task,ScriptVersion,WalletTaskResult
from backend.models import ExecutionAttempt
from backend.scripts_api import source_files
from backend.queue import claim,heartbeat,finalize
from runner.service import atomic_json
from backend.uploads import redact


def payload(execution_id):
    with Session() as db:
        e=db.get(Execution,execution_id);task=db.get(Task,e.task_id);version=db.get(ScriptVersion,e.script_version_id)
        return {'execution_id':e.id,'manifest':version.manifest,'batch_size':task.batch_size,'files':{n:base64.b64encode(v).decode() for n,v in source_files(db,version).items()},'wallets':[{'wallet_id':r.wallet_id,'address':r.address,'chain':r.chain,'name':r.name,'invite_code':''} for r in db.scalars(select(WalletTaskResult).where(WalletTaskResult.execution_id==e.id,WalletTaskResult.status=='RUNNING'))]}


async def execute(execution_id,attempt_id):
    folder=settings.runner_dir/attempt_id;folder.mkdir(parents=True,exist_ok=True)
    try:
        body=await asyncio.to_thread(payload,execution_id)
        atomic_json(folder/'request.json',body)
        deadline=time.monotonic()+body['manifest']['timeout_seconds']+20
        while not (folder/'response.json').exists():
            progress=folder/'progress.json'
            if progress.exists():
                log=json.loads(progress.read_text(encoding='utf-8')).get('log','')
                def save_progress():
                    with Session.begin() as db:
                        e=db.get(Execution,execution_id)
                        if e and e.status=='RUNNING' and e.current_attempt_id==attempt_id:
                            db.get(ExecutionAttempt,attempt_id).log=redact(log[-64000:])[0]
                await asyncio.to_thread(save_progress)
            alive=await asyncio.to_thread(heartbeat,execution_id,attempt_id,settings.worker_id)
            if not alive:(folder/'cancel').touch()
            if time.monotonic()>deadline:
                (folder/'cancel').touch()
                await asyncio.to_thread(finalize,execution_id,attempt_id,{'status':'MANUAL_REQUIRED','results':[],'error':'Runner 无响应，禁止盲目重放；请检查 Runner','log':'','exit_code':None})
                return
            await asyncio.sleep(1)
        response=json.loads((folder/'response.json').read_text(encoding='utf-8'))
        await asyncio.to_thread(finalize,execution_id,attempt_id,response)
    except Exception as exc:
        await asyncio.to_thread(finalize,execution_id,attempt_id,{'status':'MANUAL_REQUIRED','results':[],'error':f'Worker 通信异常: {type(exc).__name__}','log':'','exit_code':None})
    finally:
        # Only the task's own random directory can be removed; no source paths are accepted.
        if folder.parent.resolve()==settings.runner_dir.resolve() and (folder/'response.json').exists():
            shutil.rmtree(folder)


async def main():
    from backend.scheduler import tick
    settings.runner_dir.mkdir(parents=True,exist_ok=True);active=set()
    while True:
        await asyncio.to_thread(tick)
        health=settings.runner_dir/'heartbeat.json'
        ready=False
        try:ready=time.time()-json.loads(health.read_text())['at']<10
        except (OSError,ValueError,KeyError):pass
        if ready:
            claimed=await asyncio.to_thread(claim,settings.worker_id)
            if claimed:
                task=asyncio.create_task(execute(*claimed));active.add(task);task.add_done_callback(active.discard)
        await asyncio.sleep(.5)


if __name__=='__main__':asyncio.run(main())
