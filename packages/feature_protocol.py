"""Versioned, bounded data contracts. None of these contracts grant execution rights."""
import re
import json
import math
from typing import Any, Literal
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from backend.uploads import safe_path

# Reasons that describe a declared capability rather than an unresolved structure.
# The native runtime review must cover them explicitly instead of blocking them forever.
CAPABILITY_REASONS = ('调用链涉及凭证、认证、代理、安全检查或资金操作；禁止自动应用',)


class StrictModel(BaseModel):
    model_config = ConfigDict(extra='forbid')


class Evidence(StrictModel):
    file: str = Field(max_length=300)
    line: int = Field(ge=1)
    end_line: int = Field(ge=1)
    type: str = Field(max_length=60)
    function: str = Field(default='', max_length=200)
    excerpt: str = Field(default='', max_length=500)
    confidence: float = Field(ge=0, le=1)
    confidence_kind: Literal['rule', 'model_estimate'] = 'rule'
    feasibility: Literal['POSSIBLE', 'REVIEW_REQUIRED', 'MANUAL_REQUIRED', 'UNKNOWN']
    manual_required: bool

    @field_validator('file')
    @classmethod
    def file_path(cls, value):
        return safe_path(value)

    @model_validator(mode='after')
    def ordered(self):
        if self.end_line < self.line:
            raise ValueError('证据行号范围无效')
        return self


class Parameter(StrictModel):
    name: str = Field(pattern=r'^[A-Za-z_][A-Za-z0-9_]{0,63}$')
    type: Literal['string', 'integer', 'number', 'boolean', 'array', 'object', 'unknown'] = 'unknown'
    required: bool = True
    default: Any = None
    has_default: bool = False
    sensitive: bool = False
    source: Literal['function', 'cli', 'environment', 'config', 'interaction', 'manual']
    constraints: dict = Field(default_factory=dict)
    validation: str = Field(default='依据源码确认；不猜测缺失值', max_length=300)

    @field_validator('constraints')
    @classmethod
    def bounded_constraints(cls, value):
        if set(value) - {'minimum', 'maximum', 'minLength', 'maxLength', 'enum'} or len(json.dumps(value)) > 4000:
            raise ValueError('参数约束超出允许协议')
        for key, bound in value.items():
            if key == 'enum':
                if not isinstance(bound, list) or len(bound) > 30 or any(not isinstance(item, (str, int, float, bool)) or isinstance(item,float) and not math.isfinite(item) for item in bound):
                    raise ValueError('枚举约束无效')
            elif not isinstance(bound, (int, float)) or isinstance(bound, bool) or not math.isfinite(bound) or key in {'minLength', 'maxLength'} and (not isinstance(bound, int) or not 0 <= bound <= 1000):
                raise ValueError('参数边界无效')
        if value.get('minimum', float('-inf')) > value.get('maximum', float('inf')) or value.get('minLength', 0) > value.get('maxLength', 1000):
            raise ValueError('参数边界顺序无效')
        return value


class WalletInput(StrictModel):
    mode: Literal['CSV', 'TXT', 'JSON', 'XLSX', 'environment', 'cli', 'custom', 'unknown'] = 'unknown'
    source: str = Field(default='', max_length=300)
    fields: dict[str, Literal['identity', 'credential', 'account_config', 'project_config', 'feature_parameter']] = Field(default_factory=dict, max_length=30)
    missing_fields: list[str] = Field(default_factory=list, max_length=30)
    private_credentials_unavailable: Literal[True] = True


class FeatureDefinition(StrictModel):
    protocol: Literal['airdrop-feature-v1'] = 'airdrop-feature-v1'
    feature_id: str = Field(pattern=r'^[a-zA-Z][a-zA-Z0-9_]{0,63}$')
    name: str = Field(min_length=1, max_length=100)
    description: str = Field(max_length=1000)
    entrypoint_candidate: str = Field(max_length=300)
    source_functions: list[str] = Field(min_length=1, max_length=40)
    dependencies: list[str] = Field(default_factory=list, max_length=40)
    shared_state: list[str] = Field(default_factory=list, max_length=40)
    parameters: list[Parameter] = Field(default_factory=list, max_length=30)
    interaction_points: list[Evidence] = Field(default_factory=list, max_length=50)
    evidence: list[Evidence] = Field(min_length=1, max_length=50)
    wallet_input: WalletInput = Field(default_factory=WalletInput)
    risk_level: Literal['low', 'medium', 'high', 'unknown'] = 'unknown'
    confidence: float = Field(ge=0, le=1)
    confidence_kind: Literal['rule', 'model_estimate'] = 'rule'
    automation_status: Literal['REVIEW_REQUIRED', 'MANUAL_REQUIRED', 'UNKNOWN'] = 'REVIEW_REQUIRED'
    blocking_reasons: list[str] = Field(default_factory=list, max_length=40)
    entrypoint: str | None = Field(default=None, max_length=300)
    input_schema: dict = Field(default_factory=lambda: {'type': 'object', 'properties': {}, 'required': []})
    execution_mode: Literal['single', 'batch'] = 'batch'
    permissions: list[Literal['source_read', 'wallet_identity', 'account_configuration']] = Field(default_factory=list, max_length=3)

    @field_validator('entrypoint_candidate', 'entrypoint')
    @classmethod
    def paths(cls, value):
        return safe_path(value) if value is not None else None

    @model_validator(mode='after')
    def no_guessing(self):
        names = [parameter.name for parameter in self.parameters]
        if len(names) != len(set(names)):
            raise ValueError('参数名称重复')
        for parameter in self.parameters:
            if parameter.sensitive and parameter.has_default:
                raise ValueError('敏感参数不能包含默认凭证')
        return self


class ModelPlan(StrictModel):
    features: list[FeatureDefinition] = Field(min_length=1, max_length=20)
    generated_files: dict[str, str] = Field(default_factory=dict, max_length=20)
    replacements: dict[str, str] = Field(default_factory=dict, max_length=8)
    unresolved: list[str] = Field(default_factory=list, max_length=40)


def parameter_schema(parameters: list[Parameter]) -> dict:
    properties, required = {}, []
    for parameter in parameters:
        spec = {'title': parameter.name, 'x-sensitive': parameter.sensitive,
                'x-source': parameter.source, 'description': parameter.validation}
        if parameter.type != 'unknown':
            spec['type'] = parameter.type
        else:
            spec['x-manual-type-required'] = True
        if parameter.has_default and not parameter.sensitive:
            spec['default'] = parameter.default
        spec.update({key: value for key, value in parameter.constraints.items()
                     if key in {'minimum', 'maximum', 'minLength', 'maxLength', 'enum'}})
        properties[parameter.name] = spec
        if parameter.required:
            required.append(parameter.name)
    return {'type': 'object', 'additionalProperties': False, 'properties': properties, 'required': required}
