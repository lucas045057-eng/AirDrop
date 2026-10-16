"""Rule-based draft of a native runtime definition.

The draft only reuses structures the static analysis could actually confirm.
Everything the rules cannot decide is written into ``unresolved`` so that the
definition cannot be approved or run until a human fixes it.
"""
from pathlib import PurePosixPath
from packages.feature_protocol import FeatureDefinition
from packages.runtime_protocol import NativeRuntimeSpec

WALLET_FORMATS = {'CSV', 'TXT', 'JSON', 'XLSX'}
STATE_SUFFIXES = {'.csv': 'CSV', '.json': 'JSON', '.xlsx': 'XLSX'}
GENERATED_PREFIX = 'airdrop_generated/'
NETWORK_FINDINGS = {'NETWORK'}


def common_workdir(names: list[str], entrypoint: str) -> str:
    """A single top-level package folder becomes the working directory."""
    tops = {PurePosixPath(name).parts[0] for name in names if '/' in name}
    nested = [name for name in names if '/' in name]
    if not nested or len(tops) != 1:
        return ''
    top = next(iter(tops))
    if not entrypoint.startswith(top + '/'):
        return ''
    return top


def wallet_binding(definition: FeatureDefinition, analysis: dict):
    wallet = definition.wallet_input
    if wallet.mode not in WALLET_FORMATS or not wallet.source:
        return None
    fields = {name: role for name, role in wallet.fields.items() if role in {'identity', 'credential', 'account_config', 'project_config', 'feature_parameter'}}
    if not any(role == 'identity' for role in fields.values()):
        return None
    chain = analysis.get('wallet_chain')
    return {'format': wallet.mode, 'target_path': wallet.source, 'fields': fields,
            'chain': chain if chain in {'EVM', 'SOLANA'} else None}


def state_bindings(report: dict, wallet_path: str | None):
    bindings = []
    for item in report.get('excluded_data_files', []):
        name = item.get('file', '')
        suffix = PurePosixPath(name).suffix.lower()
        if suffix not in STATE_SUFFIXES or name == wallet_path:
            continue
        bindings.append({'path': name, 'format': STATE_SUFFIXES[suffix], 'account_key': 'address',
                         'account_key_kind': 'address', 'project': True, 'write_back': True, 'merge': 'merge_by_key'})
    return bindings[:20]


def draft_spec(definition: dict, report: dict | None = None, hint: dict | None = None, analysis: dict | None = None) -> dict | None:
    """Return a runtime draft for one discovered feature, or None when unusable."""
    report = report or {}
    analysis = analysis or {}
    if hint is not None:
        return NativeRuntimeSpec.model_validate(hint).model_dump(mode='json')
    feature = FeatureDefinition.model_validate(definition)
    unresolved: list[str] = []
    entrypoint = feature.entrypoint or feature.entrypoint_candidate
    if not entrypoint.endswith('.py'):
        unresolved.append('未确认可运行的 Python 入口文件')
    names = list(report.get('file_tree', []))
    workdir = common_workdir(names, entrypoint) if names else ''
    generated = entrypoint.startswith(GENERATED_PREFIX)
    callable_name = 'run_feature' if generated else None
    binding = wallet_binding(feature, analysis)
    if binding is None:
        unresolved.append('钱包输入格式或地址字段未确认，请在运行定义中明确')
    if binding and 'credential' in binding['fields'].values() and binding['chain'] is None:
        unresolved.append('需要交付私钥但链类型未确认')
    for parameter in feature.parameters:
        if parameter.type == 'unknown':
            unresolved.append('参数类型未知：' + parameter.name)
        if parameter.source == 'interaction':
            unresolved.append('参数来自交互输入，需要人工改成显式配置：' + parameter.name)
    if feature.parameters and not generated:
        unresolved.append('入口为脚本自身命令行，需要确认参数如何传给脚本')
    capabilities = []
    if any(finding.get('type') in NETWORK_FINDINGS for finding in report.get('findings', [])):
        capabilities.append('external_network')
    if binding and 'credential' in binding['fields'].values():
        capabilities.append('selected_wallet_private_keys')
    spec = {'entrypoint': entrypoint, 'workdir': workdir, 'callable': callable_name,
            'parameters': [parameter.model_dump(mode='json') for parameter in feature.parameters],
            'wallet_binding': binding,
            'state_bindings': state_bindings(report, binding['target_path'] if binding else None),
            'capabilities': sorted(set(capabilities)), 'result_mode': 'logs_only',
            # A draft carrying unresolved items is stored for review, never approved.
            'unresolved': list(dict.fromkeys(unresolved))[:40]}
    try:
        return NativeRuntimeSpec.model_validate(spec).model_dump(mode='json')
    except ValueError:
        # A skeleton the contract rejects is not stored as a half-valid definition;
        # the human path is the manual adaptation entry with an explicit runtime.
        return None
