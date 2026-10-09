import io
import stat
import tarfile
import zipfile
import pytest
from backend.uploads import analyze, unpack, redact


def zipped(files):
    stream = io.BytesIO()
    with zipfile.ZipFile(stream,'w') as archive:
        for name,value in files.items():
            # ZipInfo normalizes backslashes on Windows: preserve hostile raw names.
            item = zipfile.ZipInfo(name)
            item.filename = name
            archive.writestr(item,value)
    return stream.getvalue()


def test_static_never_executes(tmp_path):
    target = tmp_path/'executed'
    source = f"from pathlib import Path\nPath({str(target)!r}).touch()\nif __name__ == '__main__': pass"
    result = analyze(unpack('run.py',source.encode()))
    assert result['entrypoints'][0]['path']=='run.py'
    assert not target.exists()


@pytest.mark.parametrize('name',['../evil.py','/evil.py','E:/evil.py','a\\evil.py','a/../../evil.py','.env'])
def test_zip_traversal(name):
    with pytest.raises(ValueError): unpack('p.zip',zipped({name:'pass'}))


def test_zip_duplicate_and_symlink():
    with pytest.raises(ValueError): unpack('p.zip',zipped({'A.py':'pass','a.py':'pass'}))
    stream = io.BytesIO()
    with zipfile.ZipFile(stream,'w') as archive:
        item=zipfile.ZipInfo('main.py'); item.external_attr=(stat.S_IFLNK|0o777)<<16
        archive.writestr(item,'/etc/passwd')
    with pytest.raises(ValueError): unpack('p.zip',stream.getvalue())


def test_multi_entrypoints_and_paths():
    result=analyze(unpack('p.zip',zipped({'worker.py':"if __name__ == '__main__': pass",'run.py':"if __name__ == '__main__':\n open(r'E:\\Web3\\wallets.xlsx')",'requirements.txt':'httpx'})))
    assert len(result['entrypoints'])==2
    assert result['wallet_paths'][0]['absolute']
    assert result['dependencies'][0]['file']=='requirements.txt'


def test_tar_and_corrupt():
    stream=io.BytesIO()
    with tarfile.open(fileobj=stream,mode='w:gz') as archive:
        item=tarfile.TarInfo('entry.py'); item.size=4
        archive.addfile(item,io.BytesIO(b'pass'))
    assert unpack('p.tar.gz',stream.getvalue())=={'entry.py':b'pass'}
    with pytest.raises(ValueError): unpack('p.zip',b'broken')


def test_redaction():
    secret='a'*64
    clean,count=redact(f"PRIVATE_KEY='{secret}'\ntoken='sk-abcdefghijklmnop'\nprint('hello')")
    assert secret not in clean and 'sk-abc' not in clean and count==2


def test_multiline_and_dictionary_credentials_redacted():
    source="api_key = (\n 'very-sensitive-value'\n)\nconfig={'cookie': 'real-cookie-value'}\n"
    clean,count=redact(source)
    assert 'very-sensitive-value' not in clean and 'real-cookie-value' not in clean
    assert count>=2


def test_structured_protocol_source_is_not_mistaken_for_a_mnemonic():
    source="import json\nprint('AIRDROP_RESULT:'+json.dumps({'address':'fixture-address','status':'SUCCESS','points':0}))\n"
    assert redact(source)[1]==0
    phrase='abandon ability able about above absent absorb abstract absurd abuse access accident'
    assert phrase not in redact(f"words={phrase!r}")[0]


def test_limits_and_tar_links():
    with pytest.raises(ValueError):unpack('p.zip',zipped({('/'.join(['deep']*11))+'/main.py':'pass'}))
    with pytest.raises(ValueError):unpack('p.zip',zipped({f'{i}.py':'pass' for i in range(301)}))
    with pytest.raises(ValueError):unpack('p.py',b'x'*(2*1024*1024+1))
    stream=io.BytesIO()
    with tarfile.open(fileobj=stream,mode='w:gz') as archive:
        info=tarfile.TarInfo('main.py');info.type=tarfile.SYMTYPE;info.linkname='/etc/passwd';archive.addfile(info)
    with pytest.raises(ValueError):unpack('p.tar.gz',stream.getvalue())


def test_authenticated_upload(logged):
    response=logged.post('/api/scripts/upload',files={'file':('hello.py',b"print('hello')",'text/plain')})
    assert response.status_code==200,response.text
    result=response.json()
    assert result['status']=='UPLOADED'
    assert len(result['sha256'])==64
    assert logged.get('/api/scripts').json()[0]['id']==result['id']
