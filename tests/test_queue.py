import time
from concurrent.futures import ThreadPoolExecutor
import pytest
from sqlalchemy import select
from backend.db import Session
from backend.models import Execution,WalletTaskResult,WalletLock,Schedule,ScheduleOccurrence
from backend.queue import claim,enqueue,finalize,heartbeat,locked_db,recover_db,retry
from backend.scheduler import tick,next_run


@pytest.fixture
def task_setup(logged):
    group=logged.post('/api/wallets/generate',json={'count':10,'group_name':'queue'}).json()['group_id']
    project=logged.post('/api/projects',json={'name':'queue','timezone':'Asia/Shanghai','reset_hour':8}).json()
    logged.put(f"/api/projects/{project['id']}/wallets",json={'group_ids':[group]})
    script=logged.post('/api/scripts/upload',files={'file':('main.py',b"if __name__ == '__main__': pass\n")}).json()
    version=logged.post(f"/api/scripts/{script['id']}/adapt",json={'manifest':{'package_id':script['package_id'],'package_version':1,'entrypoint':'main.py','retry_safe':True}}).json()
    logged.post(f"/api/scripts/{version['id']}/approve")
    task=logged.post('/api/tasks',json={'project_id':project['id'],'script_version_id':version['id'],'name':'mock'}).json()
    return logged,task['id'],version['id']


def results(execution_id,status='SUCCESS'):
    with Session() as db:
        return [{'address':r.address,'status':status,'points':10} for r in db.scalars(select(WalletTaskResult).where(WalletTaskResult.execution_id==execution_id))]


def publish_task(setup):
    logged,task_id,version_id=setup
    e,_=enqueue(task_id,is_test=True);claim_id,attempt=claim('test-worker')
    assert claim_id==e
    finalize(e,attempt,{'status':'DONE','exit_code':0,'results':results(e),'log':''})
    assert logged.post(f'/api/scripts/{version_id}/publish').status_code==200


def test_publish_idempotency_atomic_claim_and_snapshot(task_setup):
    publish_task(task_setup)
    _,task_id,_=task_setup
    with ThreadPoolExecutor(max_workers=8) as pool:
        ids=list(pool.map(lambda _:enqueue(task_id),range(8)))
    assert len({e for e,_ in ids})==1 and sum(created for _,created in ids)==1
    with ThreadPoolExecutor(max_workers=6) as pool:
        claimed=list(pool.map(lambda i:claim(f'w{i}'),range(6)))
    assert sum(c is not None for c in claimed)==1
    with Session() as db:
        assert len(results(ids[0][0]))==10


def test_exit_zero_without_wallet_results_is_unknown(task_setup):
    _,task_id,_=task_setup;e,_=enqueue(task_id,is_test=True);_,attempt=claim('w')
    finalize(e,attempt,{'status':'DONE','exit_code':0,'results':[]})
    with Session() as db:assert db.get(Execution,e).status=='UNKNOWN'


def test_partial_retry_only_failed_wallets(task_setup):
    _,task_id,_=task_setup;e,_=enqueue(task_id,is_test=True);_,attempt=claim('w')
    rows=results(e);rows[-1]['status']='FAILED'
    finalize(e,attempt,{'status':'DONE','exit_code':0,'results':rows})
    with Session() as db:assert db.get(Execution,e).status=='PARTIAL_SUCCESS'
    assert retry(e)==1
    _,second=claim('w')
    with Session() as db:
        pending=list(db.scalars(select(WalletTaskResult).where(WalletTaskResult.execution_id==e,WalletTaskResult.status=='RUNNING')))
        assert len(pending)==1
    finalize(e,second,{'status':'DONE','exit_code':0,'results':[{'address':pending[0].address,'status':'SUCCESS'}]})
    with Session() as db:assert db.get(Execution,e).status=='SUCCESS'


def test_restart_unknown_retains_locks_and_fences_late_results(task_setup):
    _,task_id,_=task_setup;e,_=enqueue(task_id,is_test=True);_,attempt=claim('w')
    with locked_db() as db:
        db.get(Execution,e).lease_until=time.time()-1
        assert recover_db(db,time.time())==1
    assert not finalize(e,attempt,{'status':'DONE','exit_code':0,'results':results(e)})
    with Session() as db:
        assert db.get(Execution,e).status=='UNKNOWN'
        assert db.scalar(select(WalletLock).where(WalletLock.execution_id==e)) is not None
    with pytest.raises(ValueError):retry(e)


@pytest.mark.parametrize('status',['TIMEOUT','CANCELLED','FAILED'])
def test_failure_status_preserved(task_setup,status):
    _,task_id,_=task_setup;e,_=enqueue(task_id,is_test=True);_,attempt=claim('w')
    finalize(e,attempt,{'status':status,'exit_code':-9,'results':[],'error':'injected'})
    with Session() as db:assert db.get(Execution,e).status==status


def test_cancel_and_emergency(task_setup):
    logged,task_id,_=task_setup;e,_=enqueue(task_id,is_test=True)
    assert logged.post(f'/api/executions/{e}/cancel').status_code==200
    assert claim('w') is None
    e,_=enqueue(task_id,is_test=True);_,attempt=claim('w')
    logged.put('/api/settings/security',json={'emergency_stop':True})
    assert not heartbeat(e,attempt,'w')
    with pytest.raises(ValueError,match='Emergency'):enqueue(task_id,is_test=True)


def test_scheduler_off_by_default_duplicate_and_misfire(task_setup):
    publish_task(task_setup)
    logged,task_id,_=task_setup
    response=logged.post('/api/schedules',json={'task_id':task_id,'hour':8,'minute':5})
    sid=response.json()['id']
    with locked_db() as db:db.get(Schedule,sid).next_run_at=time.time()-10
    tick()
    with Session() as db:assert not list(db.scalars(select(ScheduleOccurrence)))
    with locked_db() as db:
        s=db.get(Schedule,sid);s.enabled=True;s.next_run_at=time.time()-10
    tick();tick()
    with Session() as db:assert len(list(db.scalars(select(ScheduleOccurrence))))==1
    with locked_db() as db:db.get(Schedule,sid).next_run_at=time.time()-600
    tick()
    with Session() as db:
        history=list(db.scalars(select(ScheduleOccurrence)))
        assert len(history)==2 and history[-1].status=='SKIPPED'


def test_timezone_dst():
    from datetime import datetime
    from zoneinfo import ZoneInfo
    after=datetime(2026,3,7,23,0,tzinfo=ZoneInfo('America/New_York')).timestamp()
    actual=datetime.fromtimestamp(next_run('America/New_York',2,30,list(range(7)),after),ZoneInfo('America/New_York'))
    assert actual.day==9 and actual.hour==2


def test_global_concurrency_and_fixed_snapshots_under_contention(task_setup):
    logged,task_id,_=task_setup
    from backend.models import Project,Task
    wallets=logged.get('/api/wallets').json()
    with locked_db() as db:
        project_id=db.get(Task,task_id).project_id
        db.get(Project,project_id).concurrency=3
    ids=[]
    for subset in [wallets[:5],wallets[5:]]:
        logged.put(f'/api/projects/{project_id}/wallets',json={'wallet_ids':[w['id'] for w in subset]})
        ids.append(enqueue(task_id,is_test=True)[0])
    group=logged.post('/api/wallets/generate',json={'count':5,'group_name':'extra'}).json()['group_id']
    logged.put(f'/api/projects/{project_id}/wallets',json={'group_ids':[group]})
    ids.append(enqueue(task_id,is_test=True)[0])
    with ThreadPoolExecutor(max_workers=8) as pool:
        claims=list(pool.map(lambda i:claim(f'load-{i}'),range(40)))
    assert len([c for c in claims if c])==2
    assert all(len(results(e))==5 for e in ids)
    with Session() as db:
        assert len(list(db.scalars(select(Execution).where(Execution.status=='RUNNING'))))==2
        assert len(list(db.scalars(select(WalletLock))))==10


def test_transaction_claims_in_mock_results_require_manual_review(task_setup):
    _,task_id,_=task_setup;e,_=enqueue(task_id,is_test=True);_,attempt=claim('w')
    rows=results(e);rows[0]['transaction_hash']='fake-real-transaction-claim'
    finalize(e,attempt,{'status':'DONE','exit_code':0,'results':rows})
    with Session() as db:
        assert db.get(Execution,e).status=='MANUAL_REQUIRED'
        assert db.scalar(select(WalletLock).where(WalletLock.execution_id==e)) is not None


def test_configured_concurrency_matches_settings_and_claims(task_setup,monkeypatch):
    logged,task_id,_=task_setup
    from backend.config import settings
    monkeypatch.setattr(settings,'global_concurrency',1)
    assert logged.get('/api/settings').json()['execution']['global_concurrency']==1
    from backend.models import Project,Task
    wallets=logged.get('/api/wallets').json()
    with locked_db() as db:
        project_id=db.get(Task,task_id).project_id
        db.get(Project,project_id).concurrency=2
    for subset in [wallets[:5],wallets[5:]]:
        logged.put(f'/api/projects/{project_id}/wallets',json={'wallet_ids':[w['id'] for w in subset]})
        enqueue(task_id,is_test=True)
    assert claim('first') is not None and claim('second') is None
    assert logged.put('/api/settings/execution',json={'global_concurrency':2}).status_code==200
    assert logged.get('/api/settings').json()['execution']['global_concurrency']==2
    assert claim('second') is not None
