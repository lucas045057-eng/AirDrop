"""Trusted parse-only child. It never imports any uploaded module."""
import json
import os
from pathlib import Path
import sys


def run(operation, args, body):
    from backend.config import settings
    from backend.storage import decode_files, encode_files, read_blob, write_blob, artifact_lock
    from backend.uploads import analyze, unpack
    from adapters.discovery import scan
    from adapters.planning import context
    from adapters.transformation import transform
    if operation == 'upload':
        files = unpack(args['filename'], body)
        analysis = analyze(files)
        with artifact_lock():
            original_path, original_sha = write_blob(body)
            source_path, source_sha = write_blob(encode_files(files))
        return {'original': {'path': original_path, 'sha256': original_sha},
                'sources': {'path': source_path, 'sha256': source_sha}, 'analysis': analysis, 'files': len(files)}
    path = Path(args['source_path'])
    if path.parent.resolve() != (settings.data_dir / 'artifacts').resolve() or path.name != args['source_sha256'] + '.enc' or path.is_symlink():
        raise ValueError('来源工件路径异常')
    files = decode_files(read_blob(str(path), args['source_sha256']))
    print('ADAPTER_STAGE:SCANNING', flush=True)
    report = scan(files)
    if operation == 'scan':
        return report
    selected = args['selected_features']
    if operation == 'context':
        print('ADAPTER_STAGE:DISCOVERING', flush=True)
        payload, digest = context(files, report, selected)
        with artifact_lock():
            blob_path, blob_sha = write_blob(json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(',', ':')).encode())
        return {'path': blob_path, 'sha256': blob_sha, 'preview_sha256': digest}
    from adapters.transformation import rules_plan
    print('ADAPTER_STAGE:GENERATING', flush=True)
    plan = json.loads(body) if body else rules_plan(files, report, selected)
    print('ADAPTER_STAGE:VALIDATING', flush=True)
    adapted, result = transform(files, report, selected, plan)
    with artifact_lock():
        output_path, output_sha = write_blob(encode_files(adapted))
        diff_path, diff_sha = write_blob(json.dumps(result['diffs'], ensure_ascii=False).encode())
    result.pop('diffs')
    return {'output': {'path': output_path, 'sha256': output_sha}, 'diff': {'path': diff_path, 'sha256': diff_sha},
            'summary': result, 'analysis': analyze(adapted)}


def main():
    # Limit before importing AST tooling or decoding attacker-controlled content.
    if sys.platform == 'linux':
        from worker.process_limits import linux_limits
        linux_limits(int(os.environ['AIRDROP_CHILD_MEMORY_MIB']), 60)
    operation, args = sys.argv[1], json.loads(sys.argv[2])
    limit = 10 * 1024**2 + 1 if operation == 'upload' else 512_001
    body = sys.stdin.buffer.read(limit + 1)
    if len(body) > limit:
        raise ValueError('分析输入超过预算')
    try:
        result = run(operation, args, body)
        from worker.process_limits import own_peak_rss
        envelope = {'ok': True, 'result': result, 'resources': {'peak_rss_bytes': own_peak_rss(), 'uploaded_code_executed': False}}
    except (SyntaxError, UnicodeError, MemoryError, RecursionError):
        envelope = {'ok': False, 'error': '源码无法在当前语法或内存预算内静态处理；原版本保留'}
    except ValueError as exc:
        from pydantic import ValidationError
        envelope = {'ok': False, 'error': '模型 JSON 不符合严格功能协议' if isinstance(exc, ValidationError) else str(exc)}
    encoded = json.dumps(envelope, ensure_ascii=False).encode()
    if len(encoded) > 1_500_000:
        encoded = json.dumps({'ok': False, 'error': '分析结果超过输出预算，请缩小功能范围'}, ensure_ascii=False).encode()
    sys.stdout.buffer.write(encoded)


if __name__ == '__main__':
    main()
