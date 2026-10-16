import pytest
from adapters.standardize import rules,apply_replacements
from backend.ai import preview
from tests.test_uploads import zipped


@pytest.mark.parametrize('path',[r'E:\Web3\wallets.xlsx','/home/user/wallets.csv','./data/accounts.json'])
def test_exact_path_rewrite(path):
    original={'config.py':f"WALLET_PATH={path!r}\n".encode(),'main.py':b'from config import WALLET_PATH\n'}
    adapted,diff,analysis=rules(original,{path:'inputs/wallets.csv'})
    assert "inputs/wallets.csv" in adapted['config.py'].decode()
    assert adapted['main.py']==original['main.py']
    assert analysis['errors']==[] and diff


def test_transaction_changes_blocked():
    with pytest.raises(ValueError,match='交易'):
        apply_replacements({'tx.py':b'sign_transaction(payload)'},{'tx.py':'sign_transaction(changed)'})


def test_redacted_preview():
    payload,digest=preview({'main.py':b"api_key='sk-abcdefghijklmnop'\n"},{})
    assert 'sk-abc' not in str(payload) and len(digest)==64


def test_version_approval_and_publish_gate(logged):
    upload=logged.post('/api/scripts/upload',files={'file':('project.zip',zipped({'run.py':b"if __name__ == '__main__': pass\n",'config.py':b"PATH='/home/user/wallets.csv'\n"}))}).json()
    manifest={'package_id':upload['package_id'],'package_version':1,'entrypoint':'run.py'}
    adapted=logged.post(f"/api/scripts/{upload['id']}/adapt",json={'manifest':manifest,'path_mappings':{'/home/user/wallets.csv':'inputs/wallets.csv'}})
    assert adapted.status_code==200,adapted.text
    result=adapted.json()
    assert result['version']==2 and result['status']=='REVIEW_REQUIRED'
    assert logged.get(f"/api/scripts/{result['id']}/diff").json()['diff']
    assert logged.post(f"/api/scripts/{result['id']}/publish").status_code==409
    assert logged.post(f"/api/scripts/{result['id']}/approve").json()['status']=='APPROVED'


def test_manual_adaptation_upload_creates_scanned_version_in_same_package(logged):
    original = logged.post('/api/scripts/upload', files={'file': ('project.zip', zipped({
        'src/run.py': b'def run():\n    return "original"\n',
        'src/config.py': b'RETRY = 2\n',
    }))}).json()
    response = logged.post(
        f"/api/scripts/{original['id']}/manual-adaptations",
        files={'file': ('manual-fix.zip', zipped({
            'src/run.py': b'def run():\n    return "adapted"\n',
        }))},
        data={'note': 'Preserve the existing flow', 'scan': 'true'},
    )
    assert response.status_code == 200, response.text
    adapted = response.json()
    assert adapted['package_id'] == original['package_id']
    assert adapted['version'] == original['version'] + 1
    assert adapted['status'] == 'REVIEW_REQUIRED'
    assert adapted['analysis']['manual_adaptation']['source_version_id'] == original['id']
    assert adapted['analysis_job']['source_version_id'] == adapted['id']
    assert logged.get(f"/api/scripts/{adapted['id']}/diff").json()['diff']
    new_source = logged.get(f"/api/scripts/{adapted['id']}/source").json()
    old_source = logged.get(f"/api/scripts/{original['id']}/source").json()
    assert 'adapted' in new_source['src/run.py']
    assert 'original' in old_source['src/run.py']
