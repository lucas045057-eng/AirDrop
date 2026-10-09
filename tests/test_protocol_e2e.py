"""Complete protocol regressions using exclusively developer-owned, trusted mock fixtures.
Local subprocess tests are not evidence of container/VM security isolation.
"""
import base64
import io
import tarfile
import pytest
from sqlalchemy import select
from backend.db import Session
from backend.models import Execution,ScriptVersion
from backend.queue import claim,finalize
from worker.main import payload
from runner.service import run_job
from tests.test_uploads import zipped

READERS={'CSV':"import csv\nrows=list(csv.DictReader(open(path,encoding='utf-8-sig')))",'TXT':"rows=[{'address':x.strip()} for x in open(path) if x.strip()]",'JSON':"rows=json.load(open(path))",'XLSX':"from openpyxl import load_workbook\nrows=[{'address':x[0]} for x in list(load_workbook(path,read_only=True).active.values)[1:]]"}


@pytest.mark.parametrize('format,pack,mode',[('CSV','py','file'),('CSV','zip','file'),('TXT','zip','environment'),('XLSX','zip','cli'),('JSON','tar.gz','file')])
async def test_complete_protocol_flow(logged,format,pack,mode):
    group=logged.post('/api/wallets/generate',json={'count':100 if pack=='py' else 10,'group_name':'fixture'}).json()['group_id']
    project=logged.post('/api/projects',json={'name':'fixture'}).json()
    assert logged.put(f"/api/projects/{project['id']}/wallets",json={'group_ids':[group]}).status_code==200
    suffix=format.lower();old='E:\\Web3\\wallets.'+suffix
    reader=READERS[format]
    loader='path=WALLET_PATH' if mode=='file' else "path=os.environ['CUSTOM_WALLET_PATH']" if mode=='environment' else 'path=sys.argv[1]'
    body="import os,json,sys\n"+loader+'\n'+reader+"\nfor row in rows:\n print('AIRDROP_RESULT:'+json.dumps({'address':row['address'],'status':'SUCCESS','points':10}))\n"
    if pack=='py':files={'checkin.py':f'WALLET_PATH={old!r}\n'.encode()+body.encode()};entry='checkin.py'
    else:
        files={'config.py':f'WALLET_PATH={old!r}\n'.encode(),'login.py':b'def login():\n print("fixture login")\n','task.py':('from config import WALLET_PATH\n'+body).encode(),'main.py':b"from login import login\nif __name__ == '__main__':\n login()\n import task\n",'requirements.txt':b'openpyxl' if format=='XLSX' else b''};entry='main.py'
    if pack=='py':data=files[entry]
    elif pack=='zip':data=zipped(files)
    else:
        output=io.BytesIO()
        with tarfile.open(fileobj=output,mode='w:gz') as archive:
            for name,value in files.items():
                info=tarfile.TarInfo(name);info.size=len(value);archive.addfile(info,io.BytesIO(value))
        data=output.getvalue()
    uploaded=logged.post('/api/scripts/upload',files={'file':('checkin.'+pack,data)}).json()
    assert uploaded['analysis']['entrypoints'][0]['path']==entry
    manifest={'package_id':uploaded['package_id'],'package_version':1,'entrypoint':entry,'wallet_format':format,'wallet_path':'inputs/wallets.'+suffix,'wallet_field_mapping':{'address':'address'},'wallet_input_mode':mode,'environment_name':'CUSTOM_WALLET_PATH','arguments':['{wallet_path}'] if mode=='cli' else [],'retry_safe':True,'dependencies':['openpyxl'] if format=='XLSX' else []}
    adapted=logged.post(f"/api/scripts/{uploaded['id']}/adapt",json={'provider':'rules','manifest':manifest,'path_mappings':{old:manifest['wallet_path']}})
    assert adapted.status_code==200,adapted.text
    version=adapted.json();assert logged.post(f"/api/scripts/{version['id']}/approve").status_code==200
    task=logged.post('/api/tasks',json={'project_id':project['id'],'script_version_id':version['id'],'name':'fixture'}).json()['id']
    test=logged.post(f'/api/tasks/{task}/test').json()['execution_id']
    claimed,attempt=claim('fixture-worker');assert claimed==test
    response=await run_job(payload(test))
    assert response['exit_code']==0,response
    assert len(response['results'])==(100 if pack=='py' else 10)
    assert finalize(test,attempt,response)
    assert logged.post(f"/api/scripts/{version['id']}/publish").status_code==200
    first=logged.post(f'/api/tasks/{task}/run').json();duplicate=logged.post(f'/api/tasks/{task}/run').json()
    assert first['execution_id']==duplicate['execution_id'] and not duplicate['created']
    e,attempt=claim('fixture-worker')
    response=await run_job(payload(e));finalize(e,attempt,response)
    detail=logged.get('/api/executions/'+e).json()
    assert detail['status']=='SUCCESS' and len(detail['results'])==len(response['results'])
