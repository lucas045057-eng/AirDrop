import base64
import json
import pytest
from runner.service import run_job


def job(source,format='CSV',mode='batch',timeout=5,count=10,extra=None):
    records=[{'wallet_id':f'{i:032x}','address':f'mock-address-{i}','chain':'EVM','name':str(i)} for i in range(count)]
    manifest={'package_id':'p','package_version':1,'entrypoint':'main.py','wallet_format':format,'wallet_path':'inputs/wallets.'+format.lower(),'execution_mode':mode,'timeout_seconds':timeout,'wallet_field_mapping':{'address':'address'}}
    return {'execution_id':'fixture','manifest':manifest,'files':{'main.py':base64.b64encode(source.encode()).decode(),**(extra or {})},'wallets':records,'batch_size':100}


@pytest.mark.parametrize('format',['TXT','CSV','XLSX','JSON'])
async def test_trusted_format_fixtures(format):
    readers={'TXT':"rows=[{'address':x.strip()} for x in open(path) if x.strip()]",'CSV':"import csv\nrows=list(csv.DictReader(open(path,encoding='utf-8-sig')))",'JSON':"rows=json.load(open(path))",'XLSX':"from openpyxl import load_workbook\nrows=[{'address':x[0]} for x in list(load_workbook(path,read_only=True).active.values)[1:]]"}
    source="import os,json\npath=os.environ['AIRDROP_WALLET_PATH']\n"+readers[format]+"\nfor row in rows:\n print('AIRDROP_RESULT:'+json.dumps({'address':row['address'],'status':'SUCCESS','points':1}))\n"
    response=await run_job(job(source,format))
    assert response['exit_code']==0,response
    assert len(response['results'])==10


async def test_100_wallets_bounded_batches():
    source="import json,csv,os\nfor row in csv.DictReader(open(os.environ['AIRDROP_WALLET_PATH'],encoding='utf-8-sig')):\n print('AIRDROP_RESULT:'+json.dumps({'address':row['address'],'status':'SUCCESS'}))"
    payload=job(source,count=100);payload['batch_size']=25
    response=await run_job(payload)
    assert len(response['results'])==100 and response['exit_code']==0


async def test_process_crash_timeout_cancel_and_partial():
    crash=await run_job(job('raise RuntimeError("mock crash")'))
    assert crash['status']=='FAILED' and crash['exit_code']!=0
    timeout=await run_job(job('import time\ntime.sleep(5)',timeout=1))
    assert timeout['status']=='TIMEOUT'
    cancel=await run_job(job('import time\ntime.sleep(5)'),lambda:True)
    assert cancel['status']=='CANCELLED'


async def test_environment_is_not_inherited(monkeypatch):
    monkeypatch.setenv('AIRDROP_ADMIN_PASSWORD','must-not-leak')
    response=await run_job(job("import os\nprint(os.environ.get('AIRDROP_ADMIN_PASSWORD','absent'))"))
    assert 'must-not-leak' not in response['log'] and 'absent' in response['log']


async def test_output_bound():
    response=await run_job(job("while True: print('x'*4096)",timeout=3))
    assert response['status']=='FAILED' and len(response['log'])<=64000


async def test_output_bound_after_fast_process_exit():
    response=await run_job(job("print('x'*1200000,flush=True)",timeout=3))
    assert response['status']=='FAILED' and len(response['log'])<=64000


async def test_multistep_workflow_preserves_failure():
    source="import json,csv,os\nfor row in csv.DictReader(open(os.environ['AIRDROP_WALLET_PATH'],encoding='utf-8-sig')):\n print('AIRDROP_RESULT:'+json.dumps({'address':row['address'],'status':'FAILED'}))"
    payload=job(source)
    success=source.replace("'FAILED'","'SUCCESS'")
    payload['files']['finish.py']=base64.b64encode(success.encode()).decode()
    payload['manifest'].update(execution_mode='workflow',steps=['main.py','finish.py'])
    response=await run_job(payload)
    assert len(response['results'])==10 and all(r['status']=='FAILED' for r in response['results'])
