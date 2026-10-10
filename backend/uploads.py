"""Untrusted archives are read into bounded memory; never extracted by an archive library."""
import ast
import gzip
import hashlib
import io
import re
import stat
import tarfile
import zipfile
from pathlib import PurePosixPath

MAX_UPLOAD = 10 * 1024 * 1024
MAX_EXPANDED = 50 * 1024 * 1024
MAX_FILES = 300
MAX_FILE = 2 * 1024 * 1024
MAX_DEPTH = 10
SECRET_NAME = re.compile(r"private.?key|secret|mnemonic|seed|api.?key|token|cookie|password|authorization", re.I)
HEX_SECRET = re.compile(r"(?<![0-9a-fA-F])(?:0x)?[0-9a-fA-F]{64}(?![0-9a-fA-F])")
TOKEN_SECRET = re.compile(r"(?:sk-[A-Za-z0-9_-]{12,}|eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+)")
PATH_RE = re.compile(r"(?:[A-Za-z]:[\\/][^\n\"']+|/[^\n\"']+|(?:\./)?[\w.\-/\\]+\.(?:csv|xlsx|txt|json))$")
WALLET_FIELDS = {'private_key','PrivateKey','私钥','address','wallet_address','钱包地址','account','name','invite_code'}


def safe_path(name: str) -> str:
    if not name or '\x00' in name or '\\' in name or ':' in name:
        raise ValueError("压缩包路径包含非法字符")
    path = PurePosixPath(name)
    if path.is_absolute() or '..' in path.parts or len(path.parts) > MAX_DEPTH:
        raise ValueError("拒绝路径穿越、绝对路径或过深目录")
    if any(part.startswith('.') and part not in {'.'} for part in path.parts):
        raise ValueError("不接受隐藏文件/目录；请移除 .git、.env 等文件")
    return path.as_posix()


def unpack(filename: str, data: bytes) -> dict[str, bytes]:
    if len(data) > MAX_UPLOAD:
        raise ValueError("上传文件超过 10 MiB")
    files: dict[str, bytes] = {}
    seen: set[str] = set()
    total = 0

    def add(name, size, read):
        nonlocal total
        name = safe_path(name)
        if name.casefold() in seen:
            raise ValueError("压缩包包含重复文件或大小写冲突")
        seen.add(name.casefold())
        if len(seen) > MAX_FILES or size > MAX_FILE or size < 0:
            raise ValueError("超过文件数量或单文件大小限制")
        total += size
        if total > MAX_EXPANDED:
            raise ValueError("解压后总大小超过 50 MiB")
        value = read(MAX_FILE + 1)
        if len(value) != size:
            raise ValueError("文件损坏或声明大小不符")
        files[name] = value

    try:
        if filename.lower().endswith('.py'):
            add(PurePosixPath(filename).name, len(data), io.BytesIO(data).read)
        elif filename.lower().endswith('.zip'):
            with zipfile.ZipFile(io.BytesIO(data)) as archive:
                if len(archive.infolist()) > MAX_FILES:
                    raise ValueError("压缩条目过多")
                for item in archive.infolist():
                    mode = item.external_attr >> 16
                    safe_path(item.orig_filename.rstrip('/'))
                    safe_path(item.filename.rstrip('/'))
                    if stat.S_ISLNK(mode) or (mode and stat.S_IFMT(mode) not in {0,stat.S_IFREG,stat.S_IFDIR}):
                        raise ValueError("不允许符号链接或特殊文件")
                    if item.flag_bits & 1:
                        raise ValueError("不支持加密 ZIP")
                    if item.is_dir():
                        continue
                    if item.file_size > 1024 * 1024 and item.file_size > max(1,item.compress_size)*100:
                        raise ValueError("压缩比异常，疑似压缩炸弹")
                    with archive.open(item) as stream:
                        add(item.filename, item.file_size, stream.read)
        elif filename.lower().endswith(('.tar.gz','.tgz')):
            # Bound gzip expansion before tarfile parses attacker-controlled PAX headers.
            with gzip.GzipFile(fileobj=io.BytesIO(data)) as compressed:
                expanded=compressed.read(MAX_EXPANDED+1)
            if len(expanded)>MAX_EXPANDED:raise ValueError('tar.gz 解压后体积超过限制')
            with tarfile.open(fileobj=io.BytesIO(expanded), mode='r:') as archive:
                count = 0
                for item in archive:
                    count += 1
                    if count > MAX_FILES:
                        raise ValueError("压缩条目过多")
                    safe_path(item.name.rstrip('/'))
                    if item.isdir():
                        continue
                    if not item.isfile():
                        raise ValueError("不允许链接、设备或特殊文件")
                    stream = archive.extractfile(item)
                    if stream is None:
                        raise ValueError("无法读取压缩条目")
                    with stream:
                        add(item.name,item.size,stream.read)
        else:
            raise ValueError("仅支持 .py、.zip、.tar.gz")
    except (zipfile.BadZipFile, tarfile.TarError, EOFError, OSError) as exc:
        raise ValueError("压缩包损坏") from exc
    if not any(name.endswith('.py') for name in files):
        raise ValueError("上传包必须包含 Python 源文件")
    return files


def redact(source: str) -> tuple[str, int]:
    """Conservative line redaction; external upload still requires explicit preview approval."""
    sensitive_lines=set()
    try:
        tree=ast.parse(source)
        for node in ast.walk(tree):
            value=None
            if isinstance(node,(ast.Assign,ast.AnnAssign)):
                targets=node.targets if isinstance(node,ast.Assign) else [node.target]
                if any(SECRET_NAME.search(ast.unparse(t)) for t in targets) and isinstance(node.value,(ast.Constant,ast.List,ast.Tuple)):
                    value=node.value
            if value and any(isinstance(c,ast.Constant) and isinstance(c.value,str) for c in ast.walk(value)):
                sensitive_lines.update(range(node.lineno,node.end_lineno+1))
            if isinstance(node,ast.Dict):
                for key,value in zip(node.keys,node.values):
                    if isinstance(key,ast.Constant) and isinstance(key.value,str) and SECRET_NAME.search(key.value) and isinstance(value,ast.Constant) and isinstance(value.value,str):
                        sensitive_lines.update(range(value.lineno,value.end_lineno+1))
    except (SyntaxError,RecursionError):pass
    result, count = [], 0
    for number,line in enumerate(source.splitlines(keepends=True),1):
        suspicious = SECRET_NAME.search(line) and re.search(r"[=:]\s*['\"]",line)
        mnemonic = re.search(r"['\"](?:[a-z]{3,10}\s+){11,23}[a-z]{3,10}['\"]",line)
        opaque_literal=re.search(r"['\"][A-Za-z0-9_+/=-]{40,}['\"]",line)
        if suspicious or mnemonic or number in sensitive_lines or opaque_literal:
            result.append('# [REDACTED: sensitive literal]\n')
            count += 1
        else:
            clean, url_count = re.subn(r'([a-zA-Z][a-zA-Z0-9+.-]*://)[^\s/@:]+:[^\s/@]+@', r'\1[REDACTED]@', line)
            clean, n = HEX_SECRET.subn('[REDACTED]',clean)
            clean, m = TOKEN_SECRET.subn('[REDACTED]',clean)
            result.append(clean)
            count += n+m+url_count
    return ''.join(result), count


def analyze(files: dict[str, bytes]) -> dict:
    result = {"files":[],"python_count":0,"entrypoints":[],"imports":{},"dependencies":[],"wallet_paths":[],"wallet_fields":[],"interactive":[],"network":[],"web3":[],"config_files":[],"python_version":None,"platform":"linux","persistent_dirs":[],"input_arguments":[],"wallet_loaders":[],"errors":[],"secret_findings":0}
    fields = set()
    for name, data in sorted(files.items()):
        result['files'].append({"path":name,"size":len(data),"sha256":hashlib.sha256(data).hexdigest()})
        if name.endswith(('requirements.txt','pyproject.toml')):
            result['dependencies'].append({"file":name,"content":redact(data.decode('utf-8','replace'))[0]})
            if name.endswith('pyproject.toml'):
                import tomllib
                try:result['python_version']=tomllib.loads(data.decode('utf-8')).get('project',{}).get('requires-python')
                except (tomllib.TOMLDecodeError,UnicodeError):result['errors'].append({'file':name,'error':'pyproject.toml 无法解析'})
        if name.endswith(('.json','.toml','.ini','.yaml','.yml')):
            result['config_files'].append(name)
        try:
            source = data.decode('utf-8-sig')
        except UnicodeDecodeError:
            continue
        result['secret_findings'] += redact(source)[1]
        if not name.endswith('.py'):
            continue
        result['python_count'] += 1
        try:
            tree = ast.parse(source, filename=name)
        except (SyntaxError, RecursionError) as exc:
            result['errors'].append({"file":name,"error":f"语法无法分析: {type(exc).__name__}"})
            continue
        imports, score = [], 0
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imports.extend(a.name for a in node.names)
            elif isinstance(node, ast.ImportFrom):
                imports.append(node.module or '')
            elif isinstance(node, ast.If) and '__main__' in ast.unparse(node.test):
                score += 100
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in {'main','run','checkin'}:
                score += 10
            elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == 'input':
                result['interactive'].append({"file":name,"line":node.lineno})
            elif isinstance(node,ast.Call) and isinstance(node.func,ast.Attribute):
                if node.func.attr=='add_argument':
                    result['input_arguments'].extend(a.value for a in node.args if isinstance(a,ast.Constant) and isinstance(a.value,str))
                if node.func.attr in {'mkdir','makedirs'}:
                    result['persistent_dirs'].append({'file':name,'line':node.lineno,'expression':ast.unparse(node)})
            if isinstance(node,(ast.FunctionDef,ast.AsyncFunctionDef)) and re.search(r'load.*(?:wallet|account)|read.*(?:wallet|account)',node.name,re.I):
                result['wallet_loaders'].append({'file':name,'line':node.lineno,'function':node.name,'automatic_adapter':'manual-required'})
            if isinstance(node, ast.Constant) and isinstance(node.value,str):
                if PATH_RE.match(node.value):
                    result['wallet_paths'].append({"file":name,"line":node.lineno,"path":node.value,"absolute":bool(re.match(r'^(?:[A-Za-z]:[\\/]|/)',node.value))})
                if node.value in WALLET_FIELDS:
                    fields.add(node.value)
        if score:
            result['entrypoints'].append({"path":name,"score":score})
        result['imports'][name] = sorted(set(imports))
        result['network'].extend(i for i in imports if i.split('.')[0] in {'requests','httpx','aiohttp','urllib','socket'})
        result['web3'].extend(i for i in imports if i.split('.')[0] in {'web3','eth_account','solders','solana'})
        if any(i in {'win32api','win32com','msvcrt'} for i in imports):
            result['platform'] = 'manual-required-windows'
    result['entrypoints'].sort(key=lambda v:-v['score'])
    if not result['entrypoints'] and result['python_count']==1:
        result['entrypoints'] = [{"path":next(n for n in files if n.endswith('.py')),"score":1}]
    result['wallet_fields'] = sorted(fields)
    result['network'] = sorted(set(result['network']))
    result['web3'] = sorted(set(result['web3']))
    return result
