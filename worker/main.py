import asyncio
import base64
import json
import os
import shutil
import time
from pathlib import Path
from sqlalchemy import select
from fastapi import HTTPException
from backend.config import settings
from backend.db import Session
from backend.models import Artifact,AuditLog,Execution,Task,ScriptVersion,WalletTaskResult
from backend.models import ExecutionAttempt
from backend.scripts_api import source_files
from backend.queue import claim,claim_native,finalize,finalize_native,heartbeat,locked_db,mark_running
from backend import runtime_logs,runtime_state,runtime_inputs
from backend.storage import artifact_lock,encode_files,write_blob
from worker import native_runner
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


def failure_message(exc):
    """把内部异常收敛成可展示的中文原因，不暴露堆栈与内部路径。"""
    if isinstance(exc, HTTPException):
        return str(exc.detail)
    if isinstance(exc, (ValueError, OSError)):
        return str(exc)
    return '原生运行无法启动（' + type(exc).__name__ + '）'


def native_payload(execution_id):
    """装配本次运行的容器输入与镜像；不写数据库、不落盘。"""
    with Session() as db:
        execution = db.get(Execution, execution_id)
        if execution is None:
            raise ValueError('执行记录不存在')
        return native_runner.materialize(db, execution), str(native_runner.image_for(db, execution).image_id)


def preserve_state_candidate(db, execution, produced, reason, current_generation=None):
    """加密保留待核查产物及其来源代次，避免运行目录清理后无法恢复。"""
    path, digest = write_blob(encode_files(produced))
    artifact = Artifact(path=path, sha256=digest, kind='runtime-state-pending')
    db.add(artifact)
    db.flush()
    execution.state_review_artifact_id = artifact.id
    execution.state_status = 'PENDING_REVIEW'
    message = '状态回写待处理：' + str(reason)
    execution.error = (execution.error + ('；' if execution.error else '') + message)[:2000]
    db.add(AuditLog(action='runtime.state.review', target=execution.id,
                    detail={'reason': str(reason)[:300], 'artifact_id': artifact.id,
                            'artifact_sha256': digest, 'expected_generation': execution.state_generation,
                            'current_generation': current_generation,
                            'files': sorted(produced)}))


def save_state(execution_id, attempt_id, collected, prepared):
    """把收集到的状态文件按本次账号白名单合并，并按代次条件提交。"""
    produced = {name: base64.b64decode(entry['data']) for name, entry in collected.items()}
    if not produced:
        return
    with artifact_lock(), locked_db() as db:
        execution = db.get(Execution, execution_id)
        if execution is None or execution.current_attempt_id != attempt_id:
            return
        if execution.status not in {'RUNNING', 'STOPPING', 'PREPARING'}:
            return
        spec = prepared['spec']
        try:
            keys = runtime_inputs.state_keys(db, execution, spec)
            merged, stats = runtime_state.merge(db, execution, spec, produced, keys)
            result = runtime_state.commit(db, execution, spec, merged, execution.state_generation)
        except (ValueError, HTTPException) as exc:
            message = failure_message(exc)
            state = runtime_state.space(db, execution.project_id,
                                        db.get(ScriptVersion, execution.script_version_id).package_id,
                                        spec.state_schema_version)
            preserve_state_candidate(db, execution, produced, message,
                                     state.generation if state is not None else None)
            return
        if not result.get('committed'):
            state = runtime_state.space(db, execution.project_id,
                                        db.get(ScriptVersion, execution.script_version_id).package_id,
                                        spec.state_schema_version)
            preserve_state_candidate(db, execution, produced, result.get('reason', '状态提交未完成'),
                                     state.generation if state is not None else None)
            return
        execution.state_review_artifact_id = None
        db.add(AuditLog(action='runtime.state.merged', target=execution.id,
                        detail={'files': sorted(produced), 'stats': stats, 'generation': result.get('generation')}))


def stop_flow(attempt_id, container_id, spec):
    """停止过程：先脚本声明的信号，再 SIGTERM，最终强制终止残留进程。"""
    signal = (spec.stop_signal if spec is not None else 'SIGINT') or 'SIGINT'
    grace = int(spec.stop_grace_seconds) if spec is not None else 60
    result = native_runner.stop(attempt_id, signal=signal, grace_seconds=grace, container_id=container_id, escalate=False)
    if result.get('stopped') or result.get('found') is False:
        return result
    result = native_runner.stop(attempt_id, signal='SIGTERM', grace_seconds=settings.native_stop_grace_seconds,
                                container_id=container_id, escalate=False)
    if result.get('stopped') or result.get('found') is False:
        return result
    return native_runner.stop(attempt_id, signal='SIGKILL', grace_seconds=settings.native_stop_grace_seconds,
                              container_id=container_id, escalate=True)


async def native_run(execution_id, attempt_id):
    """一次原生运行：启动容器、持续保存日志、收集状态并回写。"""
    log = runtime_logs.SegmentWriter(execution_id, attempt_id)
    try:
        prepared, image = await asyncio.to_thread(native_payload, execution_id)
    except Exception as exc:
        await asyncio.to_thread(finalize_native, execution_id, attempt_id,
                                {'status': 'FAILED', 'exit_code': None, 'error': failure_message(exc),
                                 'log_status': 'COMPLETE'})
        return
    container, outcome, stopping, started, process_started = None, None, False, False, False

    def on_event(event):
        if event.get('event') == 'log':
            data = event.get('data') or ''
            if event.get('encoding') == 'base64':
                try:
                    text = base64.b64decode(str(data)).decode('utf-8', 'replace')
                except ValueError:
                    text = '[平台] 日志分段解码失败。\n'
            else:
                text = str(data)
            log.feed(text)
        else:
            log.feed('[平台] ' + str(event.get('data') or '') + '\n')

    try:
        handle = await asyncio.to_thread(native_runner.start, attempt_id, execution_id, image, prepared, on_event)
        container = handle['container_id']
        started = True
        can_start = await asyncio.to_thread(mark_running, execution_id, attempt_id, container)
        if not can_start:
            stopped = await asyncio.to_thread(stop_flow, attempt_id, container, prepared['spec'])
            outcome = {**stopped, 'status': 'CANCELLED', 'not_started': bool(stopped.get('not_started')),
                       'timed_out': False, 'log_bytes': 0}
        else:
            process_started = True
            call = asyncio.create_task(asyncio.to_thread(native_runner.attach, attempt_id, container,
                                                         prepared['timeout_seconds'], prepared['secrets'], on_event))
            while not call.done():
                await asyncio.sleep(1)
                alive = await asyncio.to_thread(heartbeat, execution_id, attempt_id, settings.worker_id)
                if not stopping and (log.failure or not alive):
                    stopping = True
                    # 采集故障或人工停止都必须真正终止容器，不能继续假装一切正常。
                    await asyncio.to_thread(stop_flow, attempt_id, container, prepared['spec'])
                log.flush()
            outcome = await call
    except Exception as exc:
        # 无法确认进程状态时不清理现场，交由人工核查。
        await asyncio.to_thread(finalize_native, execution_id, attempt_id,
                                {'status': 'UNKNOWN', 'exit_code': None, 'error': '原生运行失联：' + failure_message(exc),
                                 'raw_output_bytes': log.bytes, 'log_status': 'GAP' if log.bytes else ''})
        return
    finally:
        log.flush()
        if outcome is not None:
            try:
                if process_started and prepared['state_paths']:
                    collected = await asyncio.to_thread(native_runner.collect, attempt_id, prepared['state_paths'])
                    await asyncio.to_thread(save_state, execution_id, attempt_id, collected['files'], prepared)
            except Exception as exc:
                raw_output_bytes = outcome.get('log_bytes')
                if raw_output_bytes is None:
                    raw_output_bytes = log.bytes
                await asyncio.to_thread(finalize_native, execution_id, attempt_id,
                                        {'status': 'MANUAL_REQUIRED', 'exit_code': outcome.get('exit_code'),
                                         'error': '状态收集失败：' + failure_message(exc),
                                         'raw_output_bytes': raw_output_bytes,
                                         'log_status': 'GAP'})
                return
        confirmed_stopped = outcome is not None and (
            outcome.get('not_started') or (outcome.get('state') in {'EXITED', 'DEAD'}
                                           and outcome.get('exit_code') is not None))
        if started and container and confirmed_stopped:
            try:
                native_runner.cleanup(attempt_id, container)
            except Exception:
                pass
    log_status = 'STORAGE_FAILED' if log.failure else 'COMPLETE'
    status = 'MANUAL_REQUIRED' if log.failure else str(outcome.get('status') or '')
    raw_output_bytes = outcome.get('log_bytes')
    if raw_output_bytes is None:
        raw_output_bytes = log.bytes
    await asyncio.to_thread(finalize_native, execution_id, attempt_id,
                            {'status': status, 'exit_code': outcome.get('exit_code'), 'timed_out': outcome.get('timed_out'),
                             'error': ('日志保存失败，已停止容器：' + log.failure) if log.failure else '',
                             'raw_output_bytes': raw_output_bytes, 'log_status': log_status})


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
                coroutine=execute(*claimed)
            else:
                native=await asyncio.to_thread(claim_native,settings.worker_id)
                coroutine=native_run(*native) if native else None
            if coroutine is not None:
                task=asyncio.create_task(coroutine)
                active.add(task);task.add_done_callback(active.discard)
        await asyncio.sleep(.5)


if __name__=='__main__':asyncio.run(main())
