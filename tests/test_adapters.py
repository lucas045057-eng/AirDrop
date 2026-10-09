import pytest
from adapters.wallets import encode,decode,load_file
from backend.vault import generate
from packages.protocol import Manifest


@pytest.mark.parametrize('format',['TXT','CSV','XLSX','JSON'])
def test_format_roundtrip(format):
    records=[{'address':generate('EVM')[0],'name':f'test{i}','chain':'EVM'} for i in range(10)]
    mapping={'钱包地址':'address'} if format=='TXT' else {'wallet_address':'address','name':'name'}
    assert decode(encode(records,format,mapping),format,mapping,'EVM')==[{k:v for k,v in r.items() if k in mapping.values()} for r in records]


def test_missing_field_corrupt_file_and_wrong_chain(tmp_path):
    with pytest.raises(ValueError,match='字段缺失'):decode(b'wrong\nabc\n','CSV',{'address':'address'},'EVM')
    with pytest.raises(ValueError):decode(b'broken','XLSX',{'address':'address'},'EVM')
    with pytest.raises(ValueError,match='路径不存在'):load_file(tmp_path/'missing.csv','CSV',{'address':'address'},'EVM')
    sol=generate('SOLANA')[0]
    with pytest.raises(ValueError):decode(f'address\n{sol}\n'.encode(),'CSV',{'address':'address'},'EVM')
    with pytest.raises(ValueError,match='混用'):decode(b'[{"chain":"SOLANA","address":"x"}]','JSON',{'chain':'chain','address':'address'},'EVM')


def test_manifest_rejects_guessing_paths_and_private_keys():
    base={'package_id':'p','package_version':1,'entrypoint':'main.py'}
    with pytest.raises(ValueError):Manifest(**base,wallet_path='E:\\wallets.csv')
    with pytest.raises(ValueError):Manifest(**base,wallet_field_mapping={'account':'account'})
    with pytest.raises(ValueError):Manifest(**base,wallet_field_mapping={'PrivateKey':'private_key'})
