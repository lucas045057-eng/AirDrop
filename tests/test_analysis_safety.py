import asyncio
import copy
import socket
import time
from pathlib import Path
import httpx
import pytest
from sqlalchemy import func, select
from backend.ai import CompatibleProvider, PinnedTransport, validate_url
from backend.analysis_jobs import claim, update
from backend.db import Session
from backend.models import Artifact, ScriptAnalysisJob, ScriptFeature, ScriptTransformationDraft, ScriptVersion, SystemSetting
from worker.adapter_worker import AdapterWorker
from tests.test_analysis_workflow import ai_config, approve, detail, discovery, draft, save, start
from tests.test_feature_discovery import MENU
from tests.test_feature_transformation import FakeProvider
from adapters.discovery import scan
from adapters.transformation import rules_plan


async def prepared(logged, provider, source=MENU):
    version, ids = await discovery(logged,source)
    ai_config()
    job = start(logged, version, 'reviewed-model', 'TRANSFORM', ids, True)
    worker = AdapterWorker(provider_factory=lambda config: provider)
    await worker.run_once()
    preview = logged.get('/api/analysis-jobs/' + job['id'] + '/preview').json()
    assert logged.post('/api/analysis-jobs/' + job['id'] + '/approve-ai', json={'confirmed': True, 'reviewed_preview_sha256': preview['sha256']}).status_code == 200
    return version, job, worker


@pytest.mark.parametrize('proposal', [{'broken': True}, {'features': []}, {'execution_enabled': True}])
async def test_invalid_model_output_leaves_no_partial_versions(logged, proposal):
    provider = FakeProvider(proposal)
    version, job, worker = await prepared(logged, provider)
    await worker.run_once()
    assert detail(logged, job)['status'] == 'FAILED'
    assert provider.calls == 1
    with Session() as db:
        assert db.scalar(select(func.count()).select_from(ScriptVersion)) == 1
        assert db.scalar(select(func.count()).select_from(ScriptTransformationDraft)) == 0
        assert db.scalar(select(func.count()).select_from(ScriptFeature)) == 0
    assert not await worker.run_once()
    assert logged.get('/api/scripts/' + version['id'] + '/source').json()['menu.py'] == MENU
    assert logged.get('/api/health').status_code == 200


async def test_timeout_model_never_auto_replays(logged):
    class Provider:
        calls = 0
        async def plan(self, payload):
            self.calls += 1
            raise TimeoutError('test-only-sensitive-response')
    provider = Provider()
    _, job, worker = await prepared(logged, provider)
    await worker.run_once()
    result = detail(logged, job)
    assert result['status'] == 'FAILED' and 'TimeoutError' in result['error']
    assert 'test-only-sensitive-response' not in result['error']
    assert not await worker.run_once() and provider.calls == 1


async def test_total_time_budget_cancels_hung_model_and_preserves_source(logged,monkeypatch):
    from backend.config import settings
    class Provider:
        cancelled=False
        async def plan(self,payload):
            try: await asyncio.Future()
            except asyncio.CancelledError:
                self.cancelled=True; raise
    provider=Provider()
    _,job,worker=await prepared(logged,provider)
    monkeypatch.setattr(settings,'adapter_timeout_seconds',5)
    await asyncio.wait_for(worker.run_once(),10)
    result=detail(logged,job)
    assert result['status']=='FAILED' and '总处理时限' in result['error']
    assert provider.cancelled and len(logged.get('/api/scripts').json())==1


async def test_draft_definition_change_requires_new_analysis(logged):
    _,_,data=await draft(logged)
    feature=data['features'][0]
    with Session.begin() as db:
        row=db.get(ScriptFeature,feature['id']);row.definition={**row.definition,'name':'changed-after-validation'}
    latest=logged.get('/api/transformation-drafts/'+data['id']).json()['features'][0]
    assert approve(logged,latest).status_code==409


async def test_non_admin_cannot_access_analysis_or_approve(logged):
    version,ids=await discovery(logged)
    from backend.models import User
    with Session.begin() as db: db.scalar(select(User)).role='viewer'
    assert logged.get('/api/analysis-jobs').status_code==403
    assert logged.get('/api/analysis/capabilities').status_code==403
    assert logged.post('/api/scripts/'+version['id']+'/analysis-jobs',json={'request_key':'forbidden'}).status_code==403


async def test_cancel_active_ai_call_cancels_only_owned_task(logged):
    class Provider:
        entered = asyncio.Event()
        cancelled = False
        async def plan(self, payload):
            self.entered.set()
            try: await asyncio.Future()
            except asyncio.CancelledError:
                self.cancelled = True
                raise
    provider = Provider()
    version, job, worker = await prepared(logged, provider)
    running = asyncio.create_task(worker.run_once())
    await asyncio.wait_for(provider.entered.wait(), 15)
    assert logged.post('/api/analysis-jobs/' + job['id'] + '/cancel').status_code == 200
    assert logged.get('/api/health').status_code == 200
    await asyncio.wait_for(running, 5)
    assert provider.cancelled and detail(logged, job)['status'] == 'CANCELLED'
    assert len(logged.get('/api/scripts').json()) == 1


async def test_crash_recovery_is_bounded_and_lease_fenced(logged):
    version, _ = await discovery(logged)
    job = start(logged, version, 'crash')
    job_id, old_token = claim('adapter-a')
    assert claim('adapter-b') is None
    with Session.begin() as db: db.get(ScriptAnalysisJob, job_id).lease_until = time.time()-1
    recovered_id, token = claim('adapter-b')
    assert recovered_id == job_id and token != old_token
    assert not update(job_id, old_token, 'COMPLETED')
    with Session.begin() as db: db.get(ScriptAnalysisJob, job_id).lease_until = time.time()-1
    assert claim('adapter-a') is None
    assert detail(logged, job)['status'] == 'MANUAL_REQUIRED'
    assert detail(logged, job)['attempts'] == 2


async def test_ai_sent_crash_never_auto_replays(logged):
    version, _ = await discovery(logged)
    job = start(logged, version, 'crash-ai')
    job_id, _ = claim()
    with Session.begin() as db:
        row = db.get(ScriptAnalysisJob, job_id); row.ai_sent = True; row.lease_until = time.time()-1
    assert claim() is None and detail(logged, job)['status'] == 'MANUAL_REQUIRED'


async def test_concurrent_same_request_is_one_job(logged):
    version, _ = await discovery(logged)
    from backend.analysis_jobs import create
    from backend.models import User
    with Session() as db: user_id = db.scalar(select(User.id))
    config = {'operation':'DISCOVER','selected_features':[],'use_ai':False}
    results = await asyncio.gather(*(asyncio.to_thread(create,version['id'],'duplicate',config,user_id) for _ in range(5)))
    assert len({r['id'] for r in results}) == 1
    assert claim() and claim() is None


async def test_changed_ciphertext_blocks_review_without_external_call(logged):
    version, ids = await discovery(logged); ai_config()
    provider = FakeProvider({}); worker = AdapterWorker(provider_factory=lambda config: provider)
    job = start(logged, version, 'corrupt', 'TRANSFORM', ids, True); await worker.run_once()
    preview = logged.get('/api/analysis-jobs/'+job['id']+'/preview').json()
    with Session() as db: path = db.get(Artifact, db.get(ScriptVersion,version['id']).source_artifact_id).path
    original = Path(path).read_bytes()
    try:
        Path(path).write_bytes(b'corrupted-test-only')
        assert logged.post('/api/analysis-jobs/'+job['id']+'/approve-ai',json={'confirmed':True,'reviewed_preview_sha256':preview['sha256']}).status_code == 409
        assert provider.calls == 0
    finally: Path(path).write_bytes(original)


async def test_provider_config_change_invalidates_send_review(logged):
    version, ids = await discovery(logged); ai_config()
    provider = FakeProvider({}); worker = AdapterWorker(provider_factory=lambda config: provider)
    job = start(logged, version, 'changed-provider', 'TRANSFORM', ids, True); await worker.run_once()
    preview = logged.get('/api/analysis-jobs/'+job['id']+'/preview').json()
    with Session.begin() as db:
        setting = db.get(SystemSetting, 'ai'); setting.value = {**setting.value, 'model': 'changed-test-only'}
    assert logged.post('/api/analysis-jobs/'+job['id']+'/approve-ai',json={'confirmed':True,'reviewed_preview_sha256':preview['sha256']}).status_code == 409
    assert provider.calls == 0


async def test_manifest_change_invalidates_feature_task(logged):
    _, _, data = await draft(logged)
    for f in data['features']: assert approve(logged, f).status_code == 200
    version = save(logged,data).json()
    feature = logged.get('/api/scripts/'+version['id']+'/features').json()[0]
    project = logged.post('/api/projects', json={'name':'manifest-integrity'}).json()
    with Session.begin() as db:
        obj=db.get(ScriptVersion,version['id']); obj.manifest={**obj.manifest,'entrypoint':'menu.py'}
    result=logged.post('/api/tasks',json={'name':'must-block','project_id':project['id'],'script_version_id':version['id'],'script_feature_id':feature['id']})
    assert result.status_code == 409


async def test_cleanup_keeps_saved_version_ciphertext(logged):
    original, job, data = await draft(logged)
    for f in data['features']: assert approve(logged,f).status_code == 200
    saved = save(logged,data).json()
    with Session() as db: path=db.get(Artifact,db.get(ScriptVersion,saved['id']).source_artifact_id).path
    before=Path(path).read_bytes()
    assert logged.request('DELETE','/api/analysis-jobs/'+job['id'],json={'confirmed':True}).status_code == 200
    assert Path(path).read_bytes() == before
    assert logged.get('/api/scripts/'+saved['id']+'/features').status_code == 200
    assert logged.get('/api/scripts/'+original['id']+'/source').status_code == 200


@pytest.mark.parametrize('address',['127.0.0.1','10.0.0.1','169.254.169.254','::1','fc00::1'])
def test_ssrf_private_addresses_rejected(monkeypatch,address):
    monkeypatch.setattr(socket,'getaddrinfo',lambda *a,**kw:[(socket.AF_INET,socket.SOCK_STREAM,0,'',(address,443))])
    with pytest.raises(ValueError,match='内网'):validate_url('https://model.example.invalid/v1')


def test_ssrf_mixed_dns_rejected(monkeypatch):
    monkeypatch.setattr(socket,'getaddrinfo',lambda *a,**kw:[(socket.AF_INET,socket.SOCK_STREAM,0,'',(ip,443)) for ip in ['8.8.8.8','127.0.0.1']])
    with pytest.raises(ValueError): validate_url('https://model.example.invalid')


async def test_dns_pinning_host_and_tls_sni_without_network():
    seen=[]
    async def handler(request):
        seen.append(request)
        return httpx.Response(200,json={'ok':True})
    transport=PinnedTransport('model.example.invalid',443,'8.8.8.8')
    await transport.inner.aclose(); transport.inner=httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        assert (await client.get('https://model.example.invalid/v1')).status_code == 200
        with pytest.raises(ValueError): await client.get('https://other.example.invalid/v1')
    assert seen[0].url.host == '8.8.8.8'
    assert seen[0].headers['Host']=='model.example.invalid'
    assert seen[0].extensions['sni_hostname']=='model.example.invalid'


@pytest.mark.parametrize('status,body',[(302,b'http://127.0.0.1'),(500,b'sensitive-test-only'),(200,b'x'*600001),(200,b'{"choices":[{"message":{"content":"bad json"}}]}')], ids=['redirect','upstream-error','oversized','invalid-json'])
async def test_provider_response_budgets_redirects_and_log_safety(monkeypatch,status,body):
    import backend.ai as module
    monkeypatch.setattr(module,'validate_url',lambda url:['8.8.8.8'])
    seen=[]
    def factory(*args):
        async def handler(request):
            seen.append(request)
            return httpx.Response(status,content=body)
        return httpx.MockTransport(handler)
    monkeypatch.setattr(module,'PinnedTransport',factory)
    with pytest.raises(ValueError) as exc: await CompatibleProvider('https://model.example.invalid/v1','test','fake-key').plan({'reviewed':'test'})
    assert len(seen)==1 and 'sensitive-test-only' not in str(exc.value)
