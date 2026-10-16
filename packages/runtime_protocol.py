"""Native execution contract: airdrop-native-runtime-v1.

The contract says how one approved script version is started in its own
container: which file runs, how this run's wallets and project accounts are
written, which parameters land where, and which files come back as state.

Nothing in this module grants execution rights. A runtime approval digest
binds the code artifact, the feature definition, this runtime specification
and the dependency lock; any change makes the previous approval stale.
"""
import hashlib
import json
import re
from typing import Any, Literal
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from backend.uploads import safe_path
from packages.feature_protocol import Parameter, parameter_schema

PROTOCOL = 'airdrop-native-runtime-v1'
SUPPORTED_PYTHON = ('3.12',)
EXECUTION_MODES = ('script_managed',)
STOP_SIGNALS = ('SIGINT', 'SIGTERM', 'SIGQUIT')
CAPABILITY_VALUES = ('external_network', 'selected_wallet_private_keys', 'browser_assisted')
WALLET_ROLES = ('identity', 'credential', 'account_config', 'project_config', 'feature_parameter')
ARGUMENT_PLACEHOLDER = re.compile(r'\{([A-Za-z_][A-Za-z0-9_]*)(?::([A-Za-z_][A-Za-z0-9_]{0,63}))?\}')
NAME = r'^[A-Za-z_][A-Za-z0-9_]{0,63}$'
ENV_NAME = r'^[A-Z][A-Z0-9_]{0,63}$'
MAX_ARGUMENTS = 60
MAX_ARGUMENT_LENGTH = 500


class StrictModel(BaseModel):
    model_config = ConfigDict(extra='forbid')


def canonical(value) -> bytes:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':')).encode()


def digest(value) -> str:
    return hashlib.sha256(canonical(value)).hexdigest()


def artifact_path(value: str) -> str:
    """Artifact-relative path inside the run work directory."""
    return safe_path(value)


class ArgumentSpec(StrictModel):
    """One argv entry; placeholders are substituted without a shell."""
    value: str = Field(min_length=1, max_length=MAX_ARGUMENT_LENGTH)

    @field_validator('value')
    @classmethod
    def placeholders(cls, text):
        for match in ARGUMENT_PLACEHOLDER.finditer(text):
            kind, name = match.group(1), match.group(2)
            if kind == 'parameter':
                if not name:
                    raise ValueError('参数占位符缺少参数名')
            elif kind not in {'wallet_path', 'workdir'}:
                raise ValueError('运行定义只允许 wallet_path、workdir 与 parameter:<名称> 占位符')
        if '{' in ARGUMENT_PLACEHOLDER.sub('', text) or '}' in ARGUMENT_PLACEHOLDER.sub('', text):
            raise ValueError('命令行参数包含无法识别的占位符')
        return text


class ConfigurationBinding(StrictModel):
    """Where an approved parameter lands: an environment variable or a file field."""
    parameter: str = Field(pattern=NAME)
    destination: Literal['environment', 'file_field']
    target: str = Field(max_length=300)
    field: str | None = Field(default=None, pattern=NAME)

    @model_validator(mode='after')
    def consistent(self):
        if self.destination == 'environment':
            if not re.fullmatch(ENV_NAME, self.target):
                raise ValueError('环境变量名必须是大写字母、数字与下划线')
            if self.field is not None:
                raise ValueError('环境变量绑定不接受字段名')
        else:
            artifact_path(self.target)
            if self.field is None:
                raise ValueError('文件字段绑定必须声明字段名')
        return self


class WalletBinding(StrictModel):
    """How this run's selected wallets become the file the script already reads."""
    format: Literal['CSV', 'TXT', 'JSON', 'XLSX']
    target_path: str = Field(max_length=300)
    fields: dict[str, Literal['identity', 'credential', 'account_config', 'project_config', 'feature_parameter']] = Field(min_length=1, max_length=30)
    chain: Literal['EVM', 'SOLANA'] | None = None

    @field_validator('target_path')
    @classmethod
    def path(cls, value):
        return artifact_path(value)

    @model_validator(mode='after')
    def coherent(self):
        if self.format == 'TXT' and len(self.fields) != 1:
            raise ValueError('TXT 一行一个字段，只能绑定一个字段')
        if not any(role == 'identity' for role in self.fields.values()):
            raise ValueError('钱包文件必须包含地址字段用于核对本次账号')
        return self


class StateBinding(StrictModel):
    """A state file that is projected into the run and merged back afterwards."""
    path: str = Field(max_length=300)
    format: Literal['CSV', 'JSON', 'XLSX', 'TXT']
    account_key: str = Field(pattern=NAME, max_length=64)
    account_key_kind: Literal['address', 'wallet_id'] = 'address'
    project: bool = True
    write_back: bool = True
    merge: Literal['merge_by_key', 'replace_selected'] = 'merge_by_key'

    @field_validator('path')
    @classmethod
    def safe(cls, value):
        return artifact_path(value)

    @model_validator(mode='after')
    def usable(self):
        if not self.write_back and not self.project:
            raise ValueError('状态文件至少需要投影或回写中的一项')
        if self.format == 'TXT':
            raise ValueError('TXT 状态无法按键合并，请使用 CSV、JSON 或 XLSX，或转人工适配')
        return self


class NativeRuntimeSpec(StrictModel):
    protocol: Literal['airdrop-native-runtime-v1'] = PROTOCOL
    entrypoint: str = Field(min_length=1, max_length=300)
    workdir: str = Field(default='', max_length=300)
    callable: str | None = Field(default=None, pattern=r'^[A-Za-z_][A-Za-z0-9_]{0,63}$')
    python_version: Literal['3.12'] = '3.12'
    dependency_files: list[str] = Field(default_factory=list, max_length=10)
    dependency_lock_sha256: str | None = Field(default=None, pattern=r'^[0-9a-f]{64}$')
    arguments: list[ArgumentSpec] = Field(default_factory=list, max_length=MAX_ARGUMENTS)
    parameters: list[Parameter] = Field(default_factory=list, max_length=30)
    configuration_bindings: list[ConfigurationBinding] = Field(default_factory=list, max_length=40)
    wallet_binding: WalletBinding | None = None
    state_bindings: list[StateBinding] = Field(default_factory=list, max_length=40)
    state_schema_version: int = Field(default=1, ge=1, le=100)
    capabilities: list[Literal['external_network', 'selected_wallet_private_keys', 'browser_assisted']] = Field(default_factory=list, max_length=3)
    execution_mode: Literal['script_managed'] = 'script_managed'
    result_mode: Literal['logs_only', 'native_summary'] = 'logs_only'
    timeout_seconds: int | None = Field(default=None, ge=1, le=86_400)
    stop_signal: Literal['SIGINT', 'SIGTERM', 'SIGQUIT'] = 'SIGINT'
    stop_grace_seconds: int = Field(default=60, ge=1, le=600)
    unresolved: list[str] = Field(default_factory=list, max_length=40)

    @field_validator('entrypoint')
    @classmethod
    def entrypoint_path(cls, value):
        path = artifact_path(value)
        if not path.endswith('.py'):
            raise ValueError('入口必须是上传工件内的 .py 文件')
        return path

    @field_validator('workdir')
    @classmethod
    def workdir_path(cls, value):
        if value in {'', '.'}:
            return ''
        return artifact_path(value)

    @field_validator('dependency_files')
    @classmethod
    def dependency_paths(cls, values):
        return [artifact_path(value) for value in values]

    @model_validator(mode='after')
    def coherent(self):
        declared = {parameter.name for parameter in self.parameters}
        for binding in self.configuration_bindings:
            if binding.parameter not in declared:
                raise ValueError('绑定引用了未声明的参数：' + binding.parameter)
        used = {match.group(2) for argument in self.arguments for match in ARGUMENT_PLACEHOLDER.finditer(argument.value) if match.group(1) == 'parameter'}
        if not used <= declared:
            raise ValueError('命令行参数引用了未声明的参数：' + ', '.join(sorted(used - declared)))
        if self.workdir and not self.entrypoint.startswith(self.workdir.rstrip('/') + '/'):
            raise ValueError('入口必须位于声明的工作目录内')
        credentials = [role for role in (self.wallet_binding.fields.values() if self.wallet_binding else []) if role == 'credential']
        if credentials and 'selected_wallet_private_keys' not in self.capabilities:
            raise ValueError('钱包文件包含私钥字段时必须在能力中声明 selected_wallet_private_keys')
        paths = [self.entrypoint] + [binding.target for binding in self.configuration_bindings if binding.destination == 'file_field'] \
            + [binding.path for binding in self.state_bindings] + ([self.wallet_binding.target_path] if self.wallet_binding else [])
        if len(paths) != len(set(paths)):
            raise ValueError('运行定义中的目标路径重复')
        if self.callable and self.arguments:
            raise ValueError('可调用入口由平台调用函数，不再拼接命令行参数')
        return self

    def schema(self) -> dict:
        return parameter_schema(self.parameters)

    def readiness(self) -> list[str]:
        """Reasons this definition cannot be approved for execution yet."""
        notes = list(self.unresolved)
        credentials = [role for role in (self.wallet_binding.fields.values() if self.wallet_binding else []) if role == 'credential']
        if credentials and self.wallet_binding.chain is None:
            notes.append('交付私钥但未声明链类型；请在运行定义中选择 EVM 或 SOLANA')
        if self.parameters and not (self.arguments or self.configuration_bindings or self.callable):
            notes.append('声明了参数但没有交付位置；请补充命令行、环境变量或可调用入口')
        return notes

    def review_digest(self, *, code_sha256: str, definition_digest: str, generated_files: dict | None = None) -> str:
        return digest({'protocol': self.protocol, 'runtime': self.model_dump(mode='json'),
                       'code_sha256': code_sha256, 'definition_sha256': definition_digest,
                       'dependency_lock_sha256': self.dependency_lock_sha256,
                       'generated_files': generated_files or {}})


def runtime_digest(spec: dict, *, code_sha256: str, definition_digest: str, generated_files: dict | None = None) -> str:
    """Digest of the exact runtime definition a reviewer confirmed."""
    return NativeRuntimeSpec.model_validate(spec).review_digest(code_sha256=code_sha256, definition_digest=definition_digest,
                                                               generated_files=generated_files)


def validate_against_files(spec: NativeRuntimeSpec, files: dict) -> None:
    """Every path the runtime definition touches must exist in the code artifact."""
    names = set(files)
    if spec.entrypoint not in names:
        raise ValueError('入口文件不存在于该版本的代码工件中')
    if spec.workdir and not any(name.startswith(spec.workdir.rstrip('/') + '/') for name in names):
        raise ValueError('声明的工作目录在工件中不存在：' + spec.workdir)
    missing = [value for value in spec.dependency_files if value not in names]
    if missing:
        raise ValueError('依赖声明文件不存在：' + ', '.join(missing))
    locked = [binding.target for binding in spec.configuration_bindings if binding.destination == 'file_field']
    for path in locked:
        if path not in names:
            raise ValueError('运行定义引用了工件中不存在的文件：' + path)


def summarize(spec: NativeRuntimeSpec) -> dict:
    """Read-only view used by task configuration and execution snapshots."""
    return {'protocol': spec.protocol, 'entrypoint': spec.entrypoint, 'workdir': spec.workdir, 'callable': spec.callable,
            'python_version': spec.python_version, 'parameters_schema': spec.schema(), 'parameters': [p.model_dump() for p in spec.parameters],
            'configuration_bindings': [binding.model_dump() for binding in spec.configuration_bindings],
            'wallet_binding': spec.wallet_binding.model_dump() if spec.wallet_binding else None,
            'state_bindings': [binding.model_dump() for binding in spec.state_bindings],
            'state_schema_version': spec.state_schema_version, 'capabilities': list(spec.capabilities),
            'execution_mode': spec.execution_mode, 'result_mode': spec.result_mode,
            'timeout_seconds': spec.timeout_seconds, 'stop_signal': spec.stop_signal,
            'stop_grace_seconds': spec.stop_grace_seconds, 'dependency_lock_sha256': spec.dependency_lock_sha256}
