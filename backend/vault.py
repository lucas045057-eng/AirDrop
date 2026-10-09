"""Authenticated encryption, bound to wallet identity; no secret export in wallet APIs."""
import base64
import json
import secrets
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.hashes import SHA256
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
from eth_account import Account
from solders.keypair import Keypair
from backend.config import settings


def identity(chain: str, private_key: str):
    try:
        if chain == 'EVM':
            account = Account.from_key(private_key)
            return account.address, account.key.hex()
        if chain == 'SOLANA':
            pair = Keypair.from_base58_string(private_key)
            return str(pair.pubkey()),str(pair)
    except Exception:
        raise ValueError('钱包私钥格式或链类型错误') from None
    raise ValueError('仅支持 EVM 和 SOLANA')


def generate(chain):
    if chain == 'EVM':
        return identity(chain,Account.create().key.hex())
    if chain == 'SOLANA':
        pair = Keypair()
        return str(pair.pubkey()),str(pair)
    raise ValueError('链类型无效')


def key():
    return base64.urlsafe_b64decode(settings.master_key_file.read_bytes().strip())


def associated(wallet):
    return f'airdrop-v2:{wallet.id}:{wallet.chain}:{wallet.address}'.encode()


def encrypt_wallet(wallet, private_key):
    nonce = secrets.token_bytes(12)
    return base64.b64encode(nonce+AESGCM(key()).encrypt(nonce,private_key.encode(),associated(wallet))).decode()


def decrypt_wallet(wallet, ciphertext):
    value=base64.b64decode(ciphertext,validate=True)
    return AESGCM(key()).decrypt(value[:12],value[12:],associated(wallet)).decode()


def backup_key(password, salt):
    if len(password)<12:
        raise ValueError('备份密码至少 12 字符')
    return PBKDF2HMAC(algorithm=SHA256(),length=32,salt=salt,iterations=600000).derive(password.encode())


def seal_backup(records, password):
    salt,nonce=secrets.token_bytes(16),secrets.token_bytes(12)
    ciphertext=AESGCM(backup_key(password,salt)).encrypt(nonce,json.dumps(records).encode(),b'airdrop-v2-backup-v1')
    return json.dumps({'format':'airdrop-v2-backup-v1','salt':base64.b64encode(salt).decode(),'nonce':base64.b64encode(nonce).decode(),'data':base64.b64encode(ciphertext).decode()}).encode()


def open_backup(data, password):
    try:
        body=json.loads(data)
        if body['format']!='airdrop-v2-backup-v1': raise ValueError()
        value=AESGCM(backup_key(password,base64.b64decode(body['salt'],validate=True))).decrypt(base64.b64decode(body['nonce'],validate=True),base64.b64decode(body['data'],validate=True),b'airdrop-v2-backup-v1')
        records=json.loads(value)
        if not isinstance(records,list) or len(records)>1000: raise ValueError()
        seen=set()
        for record in records:
            address,_=identity(record['chain'],record['private_key'])
            if address != record['address'] or (record['chain'],address) in seen: raise ValueError()
            seen.add((record['chain'],address))
        return records
    except (ValueError,KeyError,TypeError,InvalidTag):
        raise ValueError('备份校验失败：密码错误、文件损坏或钱包不匹配') from None
