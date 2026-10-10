"""Loopback-only browser QA with injected fake AI. Never a production provider option."""
import asyncio
import os
from contextlib import asynccontextmanager
if os.environ.get('AIRDROP_BROWSER_QA') != 'local-test-only':
    raise RuntimeError('This fixture requires an explicitly isolated local QA environment')
from backend.main import app, lifespan as original_lifespan
from backend.db import Session
from backend.models import SystemSetting
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

if __name__=='__main__':
    import sys
    if '--worker' in sys.argv:
        asyncio.run(worker_loop())
    else:
        import uvicorn
        uvicorn.run(app,host='127.0.0.1',port=int(os.environ['AIRDROP_QA_API_PORT']),log_level='warning')
