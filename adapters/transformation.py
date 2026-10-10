"""Generate lightweight entrypoints, validate all model code, preserve original versions."""
import ast
import copy
import difflib
import json
import re
import sys
from adapters.discovery import Module, RISK, scan
from adapters.planning import verify_plan_evidence
from backend.uploads import MAX_FILE, SECRET_NAME, redact, safe_path
from packages.feature_protocol import FeatureDefinition, ModelPlan, parameter_schema

MAX_NEW_FILES = 20
MAX_NEW_FILE_BYTES = 64_000
MAX_OUTPUT_BYTES = 512_000
FORBIDDEN = {'eval', 'exec', '__import__', 'compile', 'getattr', 'setattr', 'input', 'getpass', 'exit', 'quit'}
SYSTEM_MODULES = {'subprocess', 'socket', 'ctypes', 'importlib', 'builtins', 'pip', 'web3', 'eth_account', 'solders', 'solana'}


def module_import(name):
    parts = name[:-3].split('/')
    if parts[-1] == '__init__':
        parts.pop()
    if not parts or any(not part.isidentifier() for part in parts):
        raise ValueError('源模块路径不能转成安全 Python 导入名，请人工调整')
    return '.'.join(parts)


def wrapper(feature, files):
    ref = feature.source_functions[0]
    name, function = ref.split(':', 1)
    module = Module(name, files[name])
    node = module.functions[function]
    if '.' in function or node.args.posonlyargs or node.args.vararg or node.args.kwarg:
        raise ValueError('实例方法、嵌套函数或动态参数需要人工重构')
    parameters = feature.parameters
    feature.input_schema = parameter_schema(parameters)
    feature.entrypoint = 'airdrop_generated/' + feature.feature_id + '_entry.py'
    click_command = any(module.resolve(decorator.func if isinstance(decorator,ast.Call) else decorator) in {'click.command','click.group','click.Group.command'} for decorator in node.decorator_list)
    callable_name = '_business.callback' if click_command else '_business'
    invocation = ('await ' if isinstance(node, ast.AsyncFunctionDef) else '') + callable_name + '(**parameters)'
    async_keyword = 'async ' if isinstance(node, ast.AsyncFunctionDef) else ''
    # The wrapper is only parsed during adaptation. Future invocation is still prohibited.
    return (f'"""Management adaptation: {feature.feature_id}; execution disabled."""\n'
            f'from {module_import(name)} import {function} as _business\n'
            f'{async_keyword}def run_feature(**parameters):\n'
            f'    return {invocation}\n')


def rules_plan(files, report, selected_ids):
    features, generated, unresolved = [], {}, []
    for item in report['features']:
        if item['feature_id'] not in selected_ids:
            continue
        feature = FeatureDefinition.model_validate(item)
        if feature.blocking_reasons:
            unresolved.extend(feature.blocking_reasons)
        else:
            generated[feature.feature_id] = wrapper(feature, files)
        features.append(feature)
    if len(features) != len(set(selected_ids)) or not features:
        raise ValueError('选择的功能无效')
    return ModelPlan(features=features, generated_files={f.entrypoint: generated[f.feature_id] for f in features if f.feature_id in generated},
                     unresolved=list(dict.fromkeys(unresolved))[:40])


def import_roots(tree):
    roots = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split('.')[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            roots.add((node.module or '').split('.')[0])
    return roots - {''}


def protected_module(module):
    from adapters.discovery import own_nodes
    return any(RISK.search(ast.get_source_segment(module.source, node) or '') or any(
        RISK.search(module.resolve(child.func)) for child in own_nodes(node) if isinstance(child, ast.Call)
    ) for node in module.functions.values())


def validate_extraction(before, old, new):
    """Only remove input assignments, preserving the remainder of the business AST."""
    from adapters.discovery import interaction_kind, own_nodes
    old_names = {a.arg for a in (*old.args.args, *old.args.kwonlyargs)}
    new_names = {a.arg for a in (*new.args.args, *new.args.kwonlyargs)}
    if not old_names <= new_names:
        raise ValueError('参数提取不能删除原有参数')
    class Extract(ast.NodeTransformer):
        def visit_Assign(self, node):
            names = {target.id for target in node.targets if isinstance(target, ast.Name)}
            calls = [child for child in ast.walk(node.value) if isinstance(child, ast.Call)]
            if names and len(names) == len(node.targets) and names <= new_names and any(interaction_kind(before.resolve(call.func)) for call in calls):
                if all(interaction_kind(before.resolve(call.func)) or before.resolve(call.func) in {'int', 'float', 'str', 'bool'} for call in calls):
                    return None
            return self.generic_visit(node)
    expected = Extract().visit(copy.deepcopy(old))
    expected.args = copy.deepcopy(new.args)
    if ast.dump(expected, include_attributes=False) != ast.dump(new, include_attributes=False):
        raise ValueError('有限参数提取不能删除或重写交互以外的业务逻辑，请人工处理')


def validate_generated(name, source, original_imports, local_roots):
    if safe_path(name) != name or not re.fullmatch(r'airdrop_generated/[A-Za-z][A-Za-z0-9_]{0,90}\.py', name):
        raise ValueError('生成文件必须位于 airdrop_generated/ 且只能是单层 Python 文件')
    if len(source.encode()) > MAX_NEW_FILE_BYTES:
        raise ValueError('单个生成文件超过 64000 字节')
    tree = ast.parse(source, filename=name)
    imported = import_roots(tree)
    if imported & SYSTEM_MODULES or imported - original_imports - local_roots - sys.stdlib_module_names:
        raise ValueError('生成代码引入未审核依赖或危险模块')
    if RISK.search(source):
        # Credentials and funds are not an automated adaptation capability.
        raise ValueError('生成代码涉及凭证、认证、安全检查或资金操作，需专门人工处理')
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            from adapters.discovery import dotted
            call = dotted(node.func)
            if call.split('.')[0] in FORBIDDEN or call.endswith(('.system', '.popen', '.urlopen', '.request', '.post', '.get', '.write', '.write_text')):
                raise ValueError('生成代码包含动态执行、交互或未经审核的副作用')
        if isinstance(node, ast.Attribute) and node.attr in {'stdin', 'environ', 'modules'}:
            raise ValueError('禁止生成输入替换、环境开关修改或模块注入')
    if any(not isinstance(node, (ast.Import, ast.ImportFrom, ast.FunctionDef, ast.AsyncFunctionDef)) and
           not (isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant)) for node in tree.body):
        raise ValueError('生成入口只能包含导入和函数定义，禁止导入时执行代码')
    if not any(isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == 'run_feature' for node in tree.body):
        raise ValueError('生成入口缺少 run_feature 函数')
    return tree


def transform(files, report, selected_ids, proposal):
    plan = ModelPlan.model_validate(proposal) if not isinstance(proposal, ModelPlan) else proposal
    verify_plan_evidence(plan.features, files, report)
    if {f.feature_id for f in plan.features} != set(selected_ids):
        raise ValueError('模型必须保留所选功能，不能省略或添加未知功能')
    total_output = sum(len(text.encode()) for text in [*plan.generated_files.values(), *plan.replacements.values()])
    if total_output > MAX_OUTPUT_BYTES or len(plan.generated_files) > MAX_NEW_FILES:
        raise ValueError('模型输出超过文件数或总大小预算')
    adapted, diffs, modifications = dict(files), [], []
    originals = {name: Module(name, data) for name, data in files.items() if name.endswith('.py')}
    original_imports = set().union(*(import_roots(module.tree) for module in originals.values()))
    local_roots = {module_import(name).split('.')[0] for name in originals}
    for name, source in plan.replacements.items():
        if safe_path(name) != name or name not in originals or len(source.encode()) > MAX_FILE:
            raise ValueError('补丁只能明确修改原有 Python 文件且不可超限')
        before = originals[name]
        after = Module(name, source.encode())
        if before.source != source:
            if protected_module(before) or protected_module(after) or redact(before.source)[1]:
                raise ValueError('禁止自动修改含凭证、签名、认证、代理或安全检查的模块')
            if set(before.functions) - set(after.functions):
                raise ValueError('补丁移除了原有函数，业务功能不能丢失')
            from adapters.discovery import interaction_kind, own_nodes
            for function, old_node in before.functions.items():
                if ast.dump(old_node, include_attributes=False) == ast.dump(after.functions[function], include_attributes=False):
                    continue
                if not any(isinstance(child, ast.Call) and interaction_kind(before.resolve(child.func)) for child in own_nodes(old_node)):
                    raise ValueError('补丁试图改变非交互业务函数；需人工确认业务逻辑')
                validate_extraction(before, old_node, after.functions[function])
            non_functions = lambda module: [ast.dump(node, include_attributes=False) for node in module.tree.body if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))]
            if non_functions(before) != non_functions(after):
                raise ValueError('补丁不能改变模块配置、共享状态或启动逻辑，请人工处理')
            if import_roots(after.tree) - original_imports - local_roots - sys.stdlib_module_names:
                raise ValueError('补丁引入新依赖，需人工处理')
            if after.top_effects:
                raise ValueError('补丁引入导入时副作用')
        adapted[name] = source.encode()
        modifications.append(name)
    for name, source in plan.generated_files.items():
        if name in files or name.casefold() in {key.casefold() for key in files}:
            raise ValueError('生成文件不能覆盖已有源码')
        validate_generated(name, source, original_imports, local_roots)
        adapted[name] = source.encode()
    fresh = scan(adapted)
    if fresh['errors']:
        raise ValueError('产出代码未通过静态检查')
    after_functions = {function['ref']: function for function in fresh['functions']}
    from adapters.discovery import module_dependencies
    adapted_modules={name:Module(name,data) for name,data in adapted.items() if name.endswith('.py')}
    validated_features = []
    for feature in plan.features:
        original = next(item for item in report['features'] if item['feature_id'] == feature.feature_id)
        reasons = []
        if original['risk_level'] == 'high':
            reasons.append('原始调用链涉及受保护操作，不得由模型降低风险或自动批准')
            if feature.entrypoint and feature.entrypoint in plan.generated_files:
                raise ValueError('高风险功能禁止自动生成调用入口，请人工处理或取消选择该功能')
        if any(point['type'] in {'CONFIRMATION', 'CAPTCHA_OTP', 'FUNDS_CONFIRMATION'} for point in original['interaction_points']):
            reasons.append('原始功能包含确认或验证码，禁止自动绕过')
        if any(ref not in after_functions for ref in feature.source_functions):
            raise ValueError('重构后实际业务函数不存在')
        if feature.entrypoint and feature.entrypoint not in plan.generated_files:
            raise ValueError('功能入口必须对应本次生成文件')
        if not feature.entrypoint:
            reasons.append('没有生成可审核入口')
        source_ref = feature.source_functions[0]
        source_name, source_function = source_ref.split(':', 1)
        source_module = Module(source_name, adapted[source_name])
        from adapters.discovery import parameters
        actual_parameters = parameters(source_module.functions[source_function])
        if {p.name for p in feature.parameters} != {p.name for p in actual_parameters}:
            raise ValueError('功能参数 Schema 与实际函数签名不一致')
        for proposed in feature.parameters:
            actual = next(p for p in actual_parameters if p.name == proposed.name)
            if proposed.sensitive != actual.sensitive or proposed.required != actual.required or proposed.has_default != actual.has_default or (proposed.has_default and proposed.default != actual.default):
                raise ValueError('模型不能猜测默认参数、弱化必填或敏感性')
            if actual.type != 'unknown' and proposed.type != actual.type:
                raise ValueError('参数类型与源码不一致')
        # Static scan of the actual reachable business function, independent of model claims.
        reachable = {source_ref}
        pending = [source_ref]
        while pending and len(reachable) <= 100:
            ref = pending.pop()
            for dependency in fresh['call_graph'].get(ref, []):
                if dependency not in reachable:
                    reachable.add(dependency); pending.append(dependency)
        for ref in reachable:
            filename, function = ref.split(':', 1)
            module = Module(filename, adapted[filename])
            node = module.functions[function]
            from adapters.discovery import interaction_kind, own_nodes
            if any(isinstance(child, ast.Call) and interaction_kind(module.resolve(child.func)) or isinstance(child, ast.Attribute) and module.resolve(child).startswith(('sys.stdin','sys.argv')) for child in own_nodes(node)):
                reasons.append('业务调用链仍包含交互，必须改成显式参数，不可绕过确认')
            if module.top_effects:
                reasons.append('业务模块仍存在导入副作用')
            imported_modules=module_dependencies(adapted_modules,{adapted_modules[filename]})
            if any(m.top_effects for m in imported_modules):
                reasons.append('本地导入或包初始化模块存在副作用，需人工拆分后重新分析')
            if any(m.module_name in {'click','typer','argparse','rich','questionary','inquirer','getpass','sys'} for m in imported_modules):
                reasons.append('本地模块遮蔽 CLI/交互依赖，真实调用语义未知')
            if any(root in {'win32api', 'win32com', 'msvcrt'} for root in import_roots(module.tree)):
                reasons.append('Windows 依赖未经 Linux 移植验证')
            if any(finding['file'] == filename and finding.get('function') == function and finding['type'] == 'DYNAMIC_CALL' for finding in fresh['findings']):
                reasons.append('实际调用链仍包含动态调用，无法确认具体业务与安全边界')
            if any(finding['file'] == filename and finding.get('function') == function and finding['type'] in {'CLI_VALIDATION','CLI_PREREQUISITE'} for finding in fresh['findings']):
                reasons.append('CLI 包含参数验证、确认、复杂类型或分组前置逻辑，不能跳过；需人工保留')
        if feature.entrypoint:
            # A model cannot return a stub that never calls the selected business function.
            generated_tree = ast.parse(plan.generated_files[feature.entrypoint])
            imports = {alias.asname or alias.name: alias.name for node in generated_tree.body if isinstance(node, ast.ImportFrom)
                       and node.module == module_import(source_name) for alias in node.names}
            from adapters.discovery import dotted
            calls = {dotted(child.func).removesuffix('.callback') for child in ast.walk(generated_tree) if isinstance(child, ast.Call)}
            if not any(real == source_function and alias in calls for alias, real in imports.items()):
                raise ValueError('生成入口没有实际调用所选业务函数')
            # Generated adapters may only import and invoke the chosen function.
            # Business changes belong in the bounded extraction validator above.
            expected = ast.parse(wrapper(feature.model_copy(deep=True), adapted))
            actual_nodes = [node for node in generated_tree.body if not (isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str))]
            expected_nodes = [node for node in expected.body if not isinstance(node, ast.Expr)]
            if [ast.dump(node, include_attributes=False) for node in actual_nodes] != [ast.dump(node, include_attributes=False) for node in expected_nodes]:
                raise ValueError('生成入口只能轻量调用所选函数，禁止附加业务、副作用或菜单重放')
        feature.input_schema = parameter_schema(feature.parameters)
        if any(parameter.type == 'unknown' for parameter in feature.parameters):
            reasons.append('参数类型尚未确定，需补充源码类型定义或经过审核的参数提取后重新适配')
        feature.risk_level = 'high' if original['risk_level'] == 'high' else original['risk_level']
        feature.blocking_reasons = list(dict.fromkeys(reasons))
        feature.automation_status = 'MANUAL_REQUIRED' if reasons else 'REVIEW_REQUIRED'
        feature.confidence_kind = 'model_estimate' if not isinstance(proposal, ModelPlan) else feature.confidence_kind
        validated_features.append(feature.model_dump())
    if set(plan.generated_files) != {feature['entrypoint'] for feature in validated_features if feature['entrypoint']}:
        raise ValueError('生成文件必须逐一对应所选功能，禁止附带额外模块')
    for name in sorted(set(plan.generated_files) | set(plan.replacements)):
        before = files.get(name, b'').decode('utf-8-sig')
        after = adapted[name].decode('utf-8-sig')
        delta = ''.join(difflib.unified_diff(before.splitlines(True), after.splitlines(True), fromfile='original/' + name, tofile='adapted/' + name))
        diffs.append({'file': name, 'new': name not in files, 'diff': redact(delta)[0]})
    return adapted, {'features': validated_features, 'diffs': diffs, 'new_files': sorted(plan.generated_files),
                     'modified_files': sorted(modifications), 'new_dependencies': [], 'unresolved': plan.unresolved,
                     'execution_enabled': False, 'source_preserved': True}
