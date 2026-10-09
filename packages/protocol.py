from enum import StrEnum
from pathlib import PurePosixPath
from typing import Literal
from pydantic import BaseModel,ConfigDict,Field,field_validator,model_validator


class Status(StrEnum):
    QUEUED='QUEUED'
    RUNNING='RUNNING'
    SUCCESS='SUCCESS'
    PARTIAL_SUCCESS='PARTIAL_SUCCESS'
    FAILED='FAILED'
    TIMEOUT='TIMEOUT'
    CANCELLED='CANCELLED'
    SKIPPED='SKIPPED'
    ALREADY_DONE='ALREADY_DONE'
    MANUAL_REQUIRED='MANUAL_REQUIRED'
    UNKNOWN='UNKNOWN'


class Manifest(BaseModel):
    model_config=ConfigDict(extra='forbid')
    package_id:str
    package_version:int=Field(ge=1)
    entrypoint:str
    execution_platform:Literal['linux']='linux'
    runner_type:Literal['python_mock']='python_mock'
    dependencies:list[str]=Field(default_factory=list,max_length=100)
    wallet_chain:Literal['EVM','SOLANA']='EVM'
    wallet_input_mode:Literal['file','environment','cli']='file'
    wallet_format:Literal['TXT','CSV','XLSX','JSON']='CSV'
    wallet_field_mapping:dict[str,str]=Field(default_factory=lambda:{'address':'address','name':'name'})
    wallet_path:str='inputs/wallets.csv'
    environment_name:str='AIRDROP_WALLET_PATH'
    arguments:list[str]=Field(default_factory=list,max_length=20)
    timeout_seconds:int=Field(default=60,ge=1,le=300)
    result_schema:Literal['airdrop-wallet-result-v1']='airdrop-wallet-result-v1'
    allowed_operations:list[Literal['mock','read']]=Field(default_factory=lambda:['mock'])
    required_permissions:list[Literal['mock_files']]=Field(default_factory=lambda:['mock_files'])
    execution_mode:Literal['single','batch','workflow']='batch'
    steps:list[str]=Field(default_factory=list,max_length=10)
    retry_safe:bool=False
    high_risk_compatibility:bool=False

    @field_validator('entrypoint','wallet_path')
    @classmethod
    def relative_path(cls,value):
        p=PurePosixPath(value)
        if p.is_absolute() or '..' in p.parts or '\\' in value or ':' in value:
            raise ValueError('Manifest 路径必须是安全相对路径')
        return value

    @field_validator('steps')
    @classmethod
    def step_paths(cls,values):
        return [cls.relative_path(v) for v in values]

    @field_validator('wallet_field_mapping')
    @classmethod
    def explicit_fields(cls,value):
        allowed={'address','private_key','name','invite_code','wallet_id','chain'}
        if not value or len(value)>20 or any(v not in allowed for v in value.values()):
            raise ValueError('必须明确映射到标准字段，account 等歧义字段不可自动猜测')
        if any(len(k)>100 for k in value):raise ValueError('字段名过长')
        return value

    @field_validator('environment_name')
    @classmethod
    def environment(cls,value):
        if not value.isidentifier() or value in {'PATH','HOME','PYTHONPATH','LD_PRELOAD','LD_LIBRARY_PATH'}:
            raise ValueError('不允许覆盖系统环境变量')
        return value

    @model_validator(mode='after')
    def risky(self):
        if 'private_key' in self.wallet_field_mapping.values() and not self.high_risk_compatibility:
            raise ValueError('读取私钥须明确选择高风险模拟兼容模式')
        if self.execution_mode=='workflow' and not self.steps:
            raise ValueError('工作流必须提供步骤')
        return self


class WalletResult(BaseModel):
    model_config=ConfigDict(extra='forbid')
    address:str=Field(max_length=100)
    status:Status
    message:str=Field(default='',max_length=2000)
    points:float|None=None
    transaction_hash:str|None=Field(default=None,max_length=200)

    @field_validator('status')
    @classmethod
    def terminal(cls,value):
        if value in {Status.QUEUED,Status.RUNNING}:raise ValueError('结果必须是终态')
        return value
