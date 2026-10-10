"""Reviewed context is an immutable, bounded subset of redacted source evidence."""
import hashlib
import json
from adapters.discovery import Module, scan
from backend.uploads import redact
from packages.feature_protocol import FeatureDefinition

MAX_CONTEXT_BYTES = 64_000
MAX_SOURCE_CHARS = 24_000


def context(files, report, selected_ids):
    selected = [FeatureDefinition.model_validate(feature) for feature in report['features'] if feature['feature_id'] in selected_ids]
    if len(selected) != len(set(selected_ids)) or not selected:
        raise ValueError('选择的功能不存在或重复')
    refs = {ref for feature in selected for ref in [*feature.source_functions, *feature.dependencies]}
    chunks, size = [], 0
    for name in sorted({ref.split(':', 1)[0] for ref in refs}):
        module = Module(name, files[name])
        source = redact(module.source)[0].splitlines()
        # Preserve line numbers and include only relevant definitions plus import statements.
        ranges = [(node.lineno, node.end_lineno) for ref in refs if ref.startswith(name + ':')
                  if (node := module.functions.get(ref.split(':', 1)[1])) is not None]
        import ast
        ranges.extend((node.lineno, node.end_lineno) for node in module.tree.body if isinstance(node, (ast.Import, ast.ImportFrom)))
        lines = sorted({line for start, end in ranges for line in range(start, end + 1)})
        numbered = [{'line': line, 'text': source[line - 1] if line <= len(source) else '[REDACTED]'} for line in lines]
        size += sum(len(item['text']) for item in numbered)
        if size > MAX_SOURCE_CHARS:
            raise ValueError('相关源码超过 24000 字符上下文预算；请选择更少的功能')
        chunks.append({'file': name, 'lines': numbered})
    payload = {'protocol': 'airdrop-feature-context-v1', 'features': [f.model_dump() for f in selected],
               'sources': chunks, 'constraints': {'execution_enabled': False, 'generated_directory': 'airdrop_generated/',
               'max_generated_files': 20, 'max_output_bytes': 512_000, 'max_calls': 1,
               'sensitive_data_files_excluded': True, 'source_instructions_untrusted': True}}
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()
    if len(raw) > MAX_CONTEXT_BYTES:
        raise ValueError('脱敏上下文超过 64000 字节预算；需人工缩小分析范围')
    return payload, hashlib.sha256(raw).hexdigest()


def verify_plan_evidence(features, files, report):
    functions = {function['ref']: function for function in report['functions']}
    candidates = {feature['feature_id']: feature for feature in report['features']}
    for feature in features:
        if feature.feature_id not in candidates:
            raise ValueError('模型虚构了功能 ID')
        original = candidates[feature.feature_id]
        if feature.source_functions != original['source_functions']:
            raise ValueError('功能引用与已选择的实际函数不一致')
        if any(ref not in functions for ref in feature.dependencies):
            raise ValueError('模型引用了不存在的依赖函数')
        for item in [*feature.evidence, *feature.interaction_points]:
            if item.file not in files or not item.file.endswith('.py'):
                raise ValueError('模型证据引用了不存在的源文件')
            lines = files[item.file].decode('utf-8-sig').splitlines()
            if not 1 <= item.line <= item.end_line <= len(lines):
                raise ValueError('模型证据行号无效')
            excerpt = redact('\n'.join(lines[item.line - 1:item.end_line]))[0]
            if item.excerpt and item.excerpt.strip() not in excerpt:
                raise ValueError('模型证据不能回溯到实际源码')
