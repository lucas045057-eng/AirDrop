import ast
import difflib
import io
import tokenize
from backend.uploads import analyze,redact

TRANSACTION_TOKENS=('sign_transaction','send_raw_transaction','send_transaction','approve(','transfer(','sign_message','sign_and_send','sendTransaction')


def replace_paths(source,mappings):
    tokens=[]
    for token in tokenize.generate_tokens(io.StringIO(source).readline):
        if token.type==tokenize.STRING:
            try:value=ast.literal_eval(token.string)
            except (ValueError,SyntaxError):value=None
            if isinstance(value,str) and value in mappings:
                token=token._replace(string=repr(mappings[value]))
        tokens.append(token)
    output=tokenize.untokenize(tokens)
    ast.parse(output)
    return output


def apply_replacements(files,replacements):
    adapted=dict(files);diffs=[]
    for name,source in replacements.items():
        if name not in files or not name.endswith('.py'):raise ValueError('只允许修改已有 Python 文件')
        before=files[name].decode('utf-8-sig')
        ast.parse(source)
        if before!=source and any(t in before or t in source for t in TRANSACTION_TOKENS):
            raise ValueError('该文件含签名或交易逻辑，自动修改被禁止，需专门人工处理')
        adapted[name]=source.encode()
        diff=''.join(difflib.unified_diff(before.splitlines(True),source.splitlines(True),fromfile=f'original/{name}',tofile=f'adapted/{name}'))
        diffs.append(redact(diff)[0])
    analysis=analyze(adapted)
    if analysis['errors']:raise ValueError('适配代码未通过静态语法检查')
    return adapted,'\n'.join(diffs),analysis


def rules(files,mappings):
    replacements={name:replace_paths(value.decode('utf-8-sig'),mappings) for name,value in files.items() if name.endswith('.py')}
    return apply_replacements(files,replacements)
