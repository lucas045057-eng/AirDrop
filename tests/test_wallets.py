import pytest
from backend import vault
from backend.db import Session
from backend.models import Wallet,WalletSecret


@pytest.mark.parametrize('chain',['EVM','SOLANA'])
def test_generation_and_encryption(logged,chain):
    result=logged.post('/api/wallets/generate',json={'chain':chain,'count':10,'group_name':chain})
    assert result.status_code==200,result.text
    wallets=logged.get('/api/wallets').json()
    assert len(wallets)==10 and len({w['address'] for w in wallets})==10
    assert 'private_key' not in str(wallets)
    with Session() as db:
        wallet=db.get(Wallet,wallets[0]['id']); secret=db.get(WalletSecret,wallet.id)
        plaintext=vault.decrypt_wallet(wallet,secret.ciphertext)
        assert plaintext not in secret.ciphertext
        assert vault.identity(chain,plaintext)[0]==wallet.address


def test_import_deduplicates_and_validates(logged):
    _,key=vault.generate('EVM')
    response=logged.post('/api/wallets/import',json={'chain':'EVM','private_keys':[key,key],'group_name':'import'})
    assert response.json()['created']==1 and response.json()['duplicates']==1
    result=logged.post('/api/wallets/import',json={'chain':'SOLANA','private_keys':[key],'group_name':'bad'})
    assert result.status_code==422 and key not in result.text


def test_backup_restore_and_binding(logged):
    group=logged.post('/api/wallets/generate',json={'chain':'EVM','count':100,'group_name':'test100'}).json()['group_id']
    project=logged.post('/api/projects',json={'name':'test'}).json()
    assert logged.put(f"/api/projects/{project['id']}/wallets",json={'group_ids':[group]}).json()['wallet_count']==100
    password='backup-password-12345'
    backup=logged.post('/api/wallets/backup',json={'password':password})
    assert backup.status_code==200
    assert b'private_key' not in backup.content
    bad=logged.post('/api/wallets/restore',files={'file':('backup.enc',backup.content)},data={'password':'wrong-password-123','verify_only':'true'})
    assert bad.status_code==422
    good=logged.post('/api/wallets/restore',files={'file':('backup.enc',backup.content)},data={'password':password,'verify_only':'true'})
    assert good.json()['verified']==100
    assert all(w['backup_verified'] for w in logged.get('/api/wallets').json())
