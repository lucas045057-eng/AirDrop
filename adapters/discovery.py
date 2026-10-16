"""Bounded AST discovery. Uploads are parsed as data; never imported or executed."""
import ast
import hashlib
import re
from pathlib import PurePosixPath
from backend.uploads import SECRET_NAME, redact
from packages.feature_protocol import CAPABILITY_REASONS, Evidence, FeatureDefinition, Parameter, WalletInput, parameter_schema

MAX_ANALYSIS_CHARS = 2_000_000
MAX_NODES = 150_000
MAX_FUNCTIONS = 1500
MAX_FINDINGS = 2000
INTERACTION_CATEGORIES = ('MENU', 'PARAMETER', 'FILE_PATH', 'WALLET_ACCOUNT', 'CONFIRMATION', 'CAPTCHA_OTP', 'FUNDS_CONFIRMATION', 'UNKNOWN')
RISK = re.compile(r'private.?key|mnemonic|getpass|secret|\bseed\b|\bsign(?:_|\b)|send.?transaction|transfer|approve|\bgas\b|nonce|rpc|contract|auth|permission|credential|session|login|password|proxy|cookie|token|captcha|verify|security|私钥|助记词|签名|交易|转账|资金|授权|认证|安全验证|验证码|密码', re.I)
NETWORK = {'requests', 'httpx', 'aiohttp', 'urllib', 'socket'}
WINDOWS = {'win32api', 'win32com', 'msvcrt', 'ctypes.windll'}
FIELD_ROLES = {'address': 'identity', 'wallet_id': 'identity', 'name': 'identity', 'chain': 'identity',
               'private_key': 'credential', 'mnemonic': 'credential', 'token': 'credential', 'cookie': 'credential',
               'proxy': 'account_config', 'invite_code': 'account_config', 'referral_code': 'account_config'}


def dotted(node):
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return dotted(node.value) + '.' + node.attr
    if isinstance(node, ast.Call):
        return dotted(node.func)
    return '<dynamic>'


def bounded_nodes(node):
    for count, child in enumerate(ast.walk(node)):
        if count >= MAX_NODES:
            raise ValueError('AST 节点数超过分析预算，请缩小源码范围')
        yield child


def own_nodes(node):
    """Skip nested definitions when attributing evidence to a function."""
    stack = list(reversed(getattr(node, 'body', [])))
    while stack:
        current = stack.pop()
        yield current
        if not isinstance(current, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            stack.extend(reversed(list(ast.iter_child_nodes(current))))


def category(prompt, qualified):
    if re.search(r'captcha|验证码|otp|2fa|人机|助记词|mnemonic', prompt, re.I):
        return 'CAPTCHA_OTP'
    if re.search(r'交易|转账|授权|资金|签名|approve|transfer|sign|transaction', prompt, re.I):
        return 'FUNDS_CONFIRMATION'
    if re.search(r'confirm|Confirm', qualified) or re.search(r'确认|同意|继续|是否', prompt):
        return 'CONFIRMATION'
    if re.search(r'文件|路径|path|file', prompt, re.I):
        return 'FILE_PATH'
    if re.search(r'钱包|账户|账号|wallet|account', prompt, re.I):
        return 'WALLET_ACCOUNT'
    if re.search(r'选择.*功能|菜单|menu|choice|请选择', prompt, re.I):
        return 'MENU'
    return 'PARAMETER' if prompt else 'UNKNOWN'


class Module:
    def __init__(self, name, data):
        self.name = name
        self.source = data.decode('utf-8-sig')
        self.lines = self.source.splitlines()
        self.tree = ast.parse(self.source, filename=name)
        self.nodes = list(bounded_nodes(self.tree))
        self.aliases, self.functions, self.symbols = {}, {}, {}
        self.ambiguous_aliases=set()
        self.module_name = name[:-3].replace('/', '.')
        if self.module_name.endswith('.__init__'):
            self.module_name = self.module_name[:-9]
        self.imports = []
        for node in self.nodes:
            if isinstance(node, ast.Import):
                for alias in node.names:
                    self.bind_alias(alias.asname or alias.name.split('.')[0], alias.name if alias.asname else alias.name.split('.')[0])
                    self.imports.append(alias.name)
            if isinstance(node, ast.ImportFrom):
                base = node.module or ''
                if node.level:
                    package_parts = self.module_name.split('.') if name.endswith('/__init__.py') else self.module_name.split('.')[:-1]
                    prefix = package_parts[:len(package_parts)-node.level+1]
                    base = '.'.join(prefix + ([base] if base else []))
                for alias in node.names:
                    self.bind_alias(alias.asname or alias.name, base + '.' + alias.name)
                    self.imports.append(base + '.' + alias.name)
                self.imports.append(base)
            if isinstance(node, (ast.Assign, ast.AnnAssign)):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                if isinstance(node.value, ast.Call):
                    for target in targets:
                        if isinstance(target, ast.Name):
                            self.bind_alias(target.id,self.resolve(node.value.func))
                elif isinstance(node.value, (ast.Name, ast.Attribute)):
                    for target in targets:
                        if isinstance(target, ast.Name):
                            self.bind_alias(target.id,self.resolve(node.value))
        self._definitions(self.tree.body, '')
        for name, definition in self.functions.items():
            if any(self.resolve(d.func if isinstance(d, ast.Call) else d) in {'click.group', 'click.Group'} for d in definition.decorator_list):
                self.aliases[name] = 'click.Group'
        self.top_effects = [node for node in self.tree.body if not self._safe_top(node)]

    def _definitions(self, body, prefix):
        for node in body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                name = prefix + node.name
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    self.functions[name] = node
                    self.symbols[self.module_name + '.' + name] = name
                self._definitions(node.body, name + '.')

    def resolve(self, node):
        name = dotted(node)
        first, *rest = name.split('.')
        if first in self.ambiguous_aliases:
            return '.'.join(['<ambiguous>',*rest])
        if first in self.functions and first not in self.aliases:
            return '.'.join([self.module_name,first,*rest])
        return '.'.join([self.aliases.get(first, first), *rest])

    def bind_alias(self,name,value):
        if name in self.aliases and self.aliases[name]!=value:
            self.ambiguous_aliases.add(name)
        self.aliases[name]=value

    def _safe_top(self, node):
        if isinstance(node, (ast.Import, ast.ImportFrom, ast.Pass)):
            return True
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name in self.aliases and self.aliases[node.name]!='click.Group':
                return False
            allowed = {'click.command', 'click.group', 'click.option', 'click.argument', 'click.Group.command', 'typer.Typer.command'}
            if any(self.resolve(d.func if isinstance(d, ast.Call) else d) not in allowed for d in node.decorator_list):
                return False
            if any(isinstance(d,ast.Call) and any(keyword.arg=='cls' for keyword in d.keywords) for d in node.decorator_list):
                return False
            expressions = [*node.args.defaults, *filter(None, node.args.kw_defaults), *[a.annotation for a in (*node.args.args, *node.args.kwonlyargs) if a.annotation], node.returns]
            return not any(isinstance(child, ast.Call) for expression in filter(None, expressions) for child in ast.walk(expression))
        if isinstance(node, ast.ClassDef):
            return not node.bases and not node.decorator_list and all(self._safe_top(child) for child in node.body)
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant):
            return True
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            if isinstance(node.value, ast.Name) and self.resolve(node.value) in {'input','builtins.input'}:
                return True
            if isinstance(node.value,ast.Name) and node.value.id in self.functions and self.functions[node.value.id].lineno<node.lineno:
                return True
            try:
                ast.literal_eval(node.value)
                return True
            except (ValueError, TypeError):
                # A command registration is detectable; arbitrary factories are not.
                return isinstance(node.value, ast.Call) and self.resolve(node.value.func) in {'typer.Typer', 'argparse.ArgumentParser', 'rich.console.Console'}
        return isinstance(node, ast.If) and self.is_main(node)

    @staticmethod
    def is_main(node):
        if not isinstance(node, ast.If) or not isinstance(node.test, ast.Compare) or len(node.test.ops) != 1 or not isinstance(node.test.ops[0], ast.Eq):
            return False
        values = [node.test.left, *node.test.comparators]
        return any(isinstance(value, ast.Name) and value.id == '__name__' for value in values) and any(isinstance(value, ast.Constant) and value.value == '__main__' for value in values)

    def evidence(self, node, kind, function='', feasibility='REVIEW_REQUIRED', confidence=.95):
        segment = ast.get_source_segment(self.source, node) or ''
        excerpt = redact(segment)[0][:500]
        return Evidence(file=self.name, line=node.lineno, end_line=node.end_lineno,
                        type=kind, function=function, excerpt=excerpt, confidence=confidence,
                        feasibility=feasibility, manual_required=feasibility in {'MANUAL_REQUIRED', 'UNKNOWN'}).model_dump()


def interaction_kind(qualified):
    if qualified in {'input', 'builtins.input', 'getpass.getpass', 'getpass'}:
        return True
    if qualified.startswith('sys.stdin'):
        return True
    if qualified in {'argparse.ArgumentParser.parse_args','argparse.ArgumentParser.parse_known_args','click.get_current_context'}:
        return True
    if qualified in {'click.prompt', 'click.confirm', 'typer.prompt', 'typer.confirm', 'rich.prompt.Prompt.ask', 'rich.prompt.Confirm.ask', 'rich.console.Console.input'}:
        return True
    return qualified.split('.')[0] in {'questionary', 'inquirer', 'InquirerPy'}


def parameters(node):
    results = []
    args = list(node.args.posonlyargs) + list(node.args.args)
    defaults = [None] * (len(args) - len(node.args.defaults)) + list(node.args.defaults)
    for arg, default in [*zip(args, defaults), *zip(node.args.kwonlyargs, node.args.kw_defaults)]:
        if arg.arg in {'self', 'cls'}:
            continue
        annotation = dotted(arg.annotation.value if isinstance(arg.annotation, ast.Subscript) else arg.annotation) if arg.annotation else ''
        kind = {'str': 'string', 'int': 'integer', 'float': 'number', 'bool': 'boolean', 'list': 'array', 'dict': 'object'}.get(annotation, 'unknown')
        sensitive = bool(SECRET_NAME.search(arg.arg) or re.search('proxy', arg.arg, re.I))
        if default is not None and redact(ast.unparse(default))[1]:
            sensitive = True
        literal, has_default = None, False
        if default is not None and not sensitive:
            try:
                literal = ast.literal_eval(default)
                if isinstance(literal, (str, int, float, bool, type(None), list, dict)) and len(str(literal)) <= 200:
                    has_default = True
                    if kind == 'unknown':
                        kind = {str: 'string', bool: 'boolean', int: 'integer', float: 'number', list: 'array', dict: 'object'}.get(type(literal), 'unknown')
            except (ValueError, TypeError):
                pass
        results.append(Parameter(name=arg.arg, type=kind, required=default is None or sensitive,
                                 default=literal if has_default else None, has_default=has_default,
                                 sensitive=sensitive, source='function'))
    return results


def module_dependencies(modules, roots):
    """Include local imports and package initializers, without importing them."""
    lookup={module.module_name:module for module in modules.values()}
    seen,pending=set(),list(roots)
    while pending:
        module=pending.pop()
        if module in seen:
            continue
        seen.add(module)
        if len(seen)>100:
            raise ValueError('本地模块导入链超过 100 项预算，请人工拆分包')
        own_parts=module.module_name.split('.')
        for count in range(1,len(own_parts)):
            parent=lookup.get('.'.join(own_parts[:count]))
            if parent and parent not in seen:
                pending.append(parent)
        for imported in module.imports:
            parts=imported.split('.')
            for count in range(1,len(parts)+1):
                dependency=lookup.get('.'.join(parts[:count]))
                if dependency and dependency not in seen:
                    pending.append(dependency)
    return seen


def scan(files: dict[str, bytes]) -> dict:
    modules, errors, findings = {}, [], []
    total = sum(len(data) for name, data in files.items() if name.endswith('.py'))
    if total > MAX_ANALYSIS_CHARS:
        return {'protocol': 'airdrop-discovery-v1', 'features': [], 'findings': [], 'functions': [], 'call_graph': {},
                'errors': ['Python 源码超过 2 MiB 深度分析预算；请缩小包或人工拆分'], 'status': 'MANUAL_REQUIRED',
                'excluded_data_files': isolated_data(files), 'file_tree': list(sorted(files)), 'entrypoints': []}
    for name, data in sorted(files.items()):
        if not name.endswith('.py'):
            continue
        try:
            modules[name] = Module(name, data)
        except (SyntaxError, UnicodeError, ValueError, RecursionError, MemoryError) as exc:
            errors.append(f'{name}: 无法静态解析（{type(exc).__name__}）')
    lookup = {symbol: (module.name, local) for module in modules.values() for symbol, local in module.symbols.items()}
    functions, graph, interactions, entrypoints = {}, {}, {}, []
    for module in modules.values():
        functions.update({module.name + ':' + name: (module, node) for name, node in module.functions.items()})
    if len(functions) > MAX_FUNCTIONS:
        return {'protocol': 'airdrop-discovery-v1', 'features': [], 'findings': [], 'functions': [], 'call_graph': {},
                'errors': ['函数数量超过分析预算'], 'status': 'MANUAL_REQUIRED', 'excluded_data_files': isolated_data(files),
                'file_tree': sorted(files), 'entrypoints': []}
    seeds = {}

    def target(module, call):
        raw = dotted(call.func)
        if raw in module.functions:
            return module.name + ':' + raw
        resolved=module.resolve(call.func)
        if resolved in module.functions:
            return module.name+':'+resolved
        found = lookup.get(resolved)
        return ':'.join(found) if found else None

    def contains_interaction(ref, visited=None):
        visited = visited or set()
        if ref not in functions or ref in visited or len(visited) >= 100:
            return False
        visited.add(ref)
        module, node = functions[ref]
        for child in own_nodes(node):
            if isinstance(child, ast.Attribute) and module.resolve(child).startswith('sys.stdin'):
                return True
            if isinstance(child, ast.Call) and (interaction_kind(module.resolve(child.func)) or contains_interaction(target(module, child), visited)):
                return True
        return False

    for module in modules.values():
        for node in module.tree.body:
            if module.is_main(node):
                entrypoints.append(module.evidence(node, 'MAIN_GUARD'))
                for call in (n for n in ast.walk(node) if isinstance(n, ast.Call)):
                    if called := target(module, call):
                        seeds.setdefault(called, ('命令行入口', module.evidence(call, 'ENTRY_CALL')))
        for name, node in module.functions.items():
            ref = module.name + ':' + name
            functions[ref] = (module, node)
            calls = [child for child in own_nodes(node) if isinstance(child, ast.Call)]
            graph[ref] = sorted({called for call in calls if (called := target(module, call))})
            interactions[ref] = []
            for call in calls:
                qualified = module.resolve(call.func)
                if interaction_kind(qualified):
                    prompt = ' '.join(str(arg.value) for arg in call.args if isinstance(arg, ast.Constant) and isinstance(arg.value, str))
                    kind = category(prompt, qualified)
                    evidence = module.evidence(call, kind, name, 'MANUAL_REQUIRED' if kind in {'CAPTCHA_OTP', 'FUNDS_CONFIRMATION', 'CONFIRMATION', 'UNKNOWN'} else 'REVIEW_REQUIRED')
                    evidence['qualified_call'] = qualified
                    interactions[ref].append(evidence)
                    findings.append(evidence)
                if qualified in {'eval', 'exec', '__import__', 'getattr', 'importlib.import_module'} or qualified.startswith('<ambiguous>'):
                    findings.append({**module.evidence(call, 'DYNAMIC_CALL', name, 'UNKNOWN', .4), 'qualified_call': qualified})
                if qualified.split('.')[0] in NETWORK:
                    findings.append({**module.evidence(call, 'NETWORK', name), 'qualified_call': qualified})
                if qualified in {'open', 'pathlib.Path.open', 'pathlib.Path.read_text', 'pathlib.Path.write_text'}:
                    findings.append({**module.evidence(call, 'FILE_IO', name), 'qualified_call': qualified})
                if qualified in {'os.getenv', 'os.environ.get'}:
                    findings.append({**module.evidence(call, 'ENVIRONMENT_INPUT', name), 'qualified_call': qualified})
                if qualified.split('.')[0] in {'sqlite3', 'sqlalchemy', 'shelve', 'pickle'} or qualified.endswith(('.commit', '.execute', '.save')):
                    findings.append({**module.evidence(call, 'PERSISTENCE', name), 'qualified_call': qualified})
            for child in own_nodes(node):
                if isinstance(child, ast.If):
                    findings.append(module.evidence(child.test, 'CONDITIONAL_BRANCH', name))
                if isinstance(child, ast.Constant) and isinstance(child.value, str) and re.search(r'\.(json|toml|ini|yaml|yml|csv|xlsx|txt)$', child.value, re.I):
                    findings.append(module.evidence(child, 'CONFIG_FILE_REFERENCE', name))
                if isinstance(child, ast.Subscript) and module.resolve(child.value) == 'os.environ':
                    findings.append(module.evidence(child, 'ENVIRONMENT_INPUT', name))
                if isinstance(child, ast.Attribute) and module.resolve(child).startswith(('sys.stdin','sys.argv')):
                    if not any(item['line'] == child.lineno for item in interactions[ref]):
                        item = {**module.evidence(child, 'UNKNOWN', name, 'UNKNOWN', .5), 'qualified_call': module.resolve(child)}
                        interactions[ref].append(item); findings.append(item)
                if isinstance(child, ast.Global):
                    findings.append({**module.evidence(child, 'SHARED_STATE', name), 'names': child.names})
            for decorator in node.decorator_list:
                resolved = module.resolve(decorator.func if isinstance(decorator, ast.Call) else decorator)
                if isinstance(decorator,ast.Call) and resolved in {'click.option','click.argument'} and any(keyword.arg in {'callback','confirmation_prompt'} or keyword.arg=='type' and isinstance(keyword.value,(ast.Call,ast.Attribute)) for keyword in decorator.keywords):
                    item={**module.evidence(decorator,'CLI_VALIDATION',name,'MANUAL_REQUIRED'), 'qualified_call':resolved}
                    findings.append(item);interactions[ref].append(item)
                if resolved=='click.Group.command':
                    parent=dotted(decorator.func if isinstance(decorator,ast.Call) else decorator).split('.')[0]
                    group=module.functions.get(parent)
                    if group is not None and any(not isinstance(statement,ast.Pass) and not (isinstance(statement,ast.Expr) and isinstance(statement.value,ast.Constant)) for statement in group.body):
                        item={**module.evidence(group,'CLI_PREREQUISITE',name,'MANUAL_REQUIRED'), 'qualified_call':resolved}
                        findings.append(item);interactions[ref].append(item)
                if resolved.endswith(('.command', '.callback')) and resolved.split('.')[0] in {'click', 'typer'}:
                    seeds[ref] = (name, module.evidence(decorator, 'CLI_COMMAND', name))
            # A branch driven by a prompt (including a prompt wrapper) supplies candidates.
            input_names = set()
            for child in own_nodes(node):
                if isinstance(child, (ast.Assign, ast.AnnAssign)) and child.value is not None:
                    interactive_value = False
                    for value_call in (item for item in ast.walk(child.value) if isinstance(item, ast.Call)):
                        called = target(module, value_call)
                        interactive_value |= interaction_kind(module.resolve(value_call.func))
                        if called in functions:
                            interactive_value |= contains_interaction(called)
                    if interactive_value:
                        assigned = child.targets if isinstance(child, ast.Assign) else [child.target]
                        input_names.update(target.id for target in assigned if isinstance(target, ast.Name))
            for branch in (child for child in own_nodes(node) if isinstance(child, ast.If)):
                compare_names = {child.id for child in ast.walk(branch.test) if isinstance(child, ast.Name)}
                if not compare_names & input_names:
                    continue
                branch_calls = [call for item in branch.body for call in ast.walk(item) if isinstance(call, ast.Call)]
                label = next((str(c.value) for c in ast.walk(branch.test) if isinstance(c, ast.Constant)), name)
                for call in branch_calls:
                    if called := target(module, call):
                        seeds[called] = (dotted(call.func), module.evidence(branch.test, 'MENU_BRANCH', name))
                for item in interactions[ref]:
                    if item['type'] in {'PARAMETER', 'UNKNOWN'}:
                        item['type'] = 'MENU'; item['feasibility'] = 'REVIEW_REQUIRED'; item['manual_required'] = False
            # argparse routes via set_defaults(func=...), never by feeding stdin.
            for call in calls:
                if dotted(call.func).endswith('.set_defaults'):
                    for keyword in call.keywords:
                        if keyword.arg in {'func', 'handler', 'callback'}:
                            fake = ast.Call(func=keyword.value, args=[], keywords=[])
                            if called := target(module, fake):
                                seeds[called] = (dotted(keyword.value), module.evidence(call, 'ARGPARSE_COMMAND', name))
        for call in (n for n in module.nodes if isinstance(n, ast.Call)):
            if dotted(call.func).endswith(('.add_parser', '.add_argument', '.add_subparsers')):
                findings.append(module.evidence(call, 'CLI_INPUT'))
        for node in module.top_effects:
            findings.append(module.evidence(node, 'IMPORT_SIDE_EFFECT', feasibility='MANUAL_REQUIRED'))
        for node in module.nodes:
            if isinstance(node, ast.Name) and node.id in module.aliases and module.aliases[node.id].split('.')[0] in WINDOWS:
                findings.append(module.evidence(node, 'WINDOWS_DEPENDENCY', feasibility='MANUAL_REQUIRED'))

    # Propagate wrapper interactions along the actual call graph, with a bounded closure.
    def reachable(ref):
        visited, queue = set(), [ref]
        while queue and len(visited) < 100:
            current = queue.pop()
            if current in visited:
                continue
            visited.add(current); queue.extend(graph.get(current, []))
        return visited

    # A menu router itself is not an independent business feature when child branches exist.
    menu_refs = {ref for ref, (_, evidence) in seeds.items() if evidence['type'] == 'MENU_BRANCH'}
    seeds = {ref: value for ref, value in seeds.items() if ref in menu_refs or not (reachable(ref) & menu_refs)}
    if any(evidence['type']=='CLI_COMMAND' for _,evidence in seeds.values()):
        seeds={ref:value for ref,value in seeds.items() if not any(functions[ref][0].resolve(d.func if isinstance(d,ast.Call) else d)=='click.group' for d in functions[ref][1].decorator_list)}
    if not seeds:
        for ref, (module, node) in functions.items():
            if node.name in {'main', 'run'} and '.' not in ref.split(':', 1)[1]:
                seeds[ref] = (node.name, module.evidence(node, 'PUBLIC_ENTRY_CANDIDATE', node.name, 'UNKNOWN', .45))
        if not seeds:
            exports = {ref for ref, (module, node) in functions.items()
                       if any(isinstance(statement, ast.Assign) and any(isinstance(t, ast.Name) and t.id == '__all__' for t in statement.targets)
                              and any(isinstance(c, ast.Constant) and c.value == node.name for c in ast.walk(statement.value)) for statement in module.tree.body)}
            for ref in exports:
                module, node = functions[ref]
                seeds[ref] = (node.name, module.evidence(node, 'EXPORTED_FUNCTION', node.name))
            if not exports:
                incoming = {dependency for dependencies in graph.values() for dependency in dependencies}
                for ref, (module, node) in functions.items():
                    # Unreferenced public roots are suggestions, never confirmed business features.
                    if ref not in incoming and '.' not in ref.split(':', 1)[1] and not node.name.startswith('_'):
                        seeds[ref] = (node.name, module.evidence(node, 'PUBLIC_ENTRY_CANDIDATE', node.name, 'UNKNOWN', .35))

    features = []
    for ref, (label, evidence) in sorted(seeds.items())[:20]:
        if ref not in functions:
            continue
        module, node = functions[ref]
        closure = reachable(ref)
        points = [Evidence.model_validate({k: v for k, v in item.items() if k != 'qualified_call'}) for called in sorted(closure) for item in interactions.get(called, [])][:50]
        reasons = []
        if points:
            reasons.append('调用链仍包含交互；需改成显式参数并审核，不能自动填写或默认确认')
        if '.' in ref.split(':', 1)[1] or node.args.posonlyargs or node.args.vararg or node.args.kwarg:
            reasons.append('实例/嵌套函数或动态参数调用需要人工确认绑定')
        if evidence['feasibility'] == 'UNKNOWN':
            reasons.append('只能确定公开入口，不能静态确认它是否为独立业务功能')
        selected_modules = module_dependencies(modules, {functions[r][0] for r in closure if r in functions})
        if any(m.top_effects for m in selected_modules):
            reasons.append('依赖模块存在导入时副作用，需拆出安全模块后审核')
        if any(m.module_name in {'click','typer','argparse','rich','questionary','inquirer','getpass','sys'} for m in selected_modules):
            reasons.append('上传包包含与 CLI/交互库同名的本地模块，实际 API 语义未知，需人工确认')
        risk_nodes = [item for item in findings if item.get('function') in {functions[r][1].name for r in closure if r in functions}
                      and item['type'] in {'DYNAMIC_CALL', 'WINDOWS_DEPENDENCY','CLI_VALIDATION','CLI_PREREQUISITE'}]
        if any(item['type'] in {'DYNAMIC_CALL','WINDOWS_DEPENDENCY'} for item in risk_nodes) or any(imported.split('.')[0] in WINDOWS for m in selected_modules for imported in m.imports):
            reasons.append('存在动态调用或 Windows 平台依赖，无法保证 Linux 调用语义')
        if any(item['type'] in {'CLI_VALIDATION','CLI_PREREQUISITE'} for item in risk_nodes):
            reasons.append('CLI 包含参数验证、确认、复杂类型或上级分组前置逻辑，不能直接绕过；需人工保留其验证与依赖')
        protected = any(RISK.search(ast.get_source_segment(functions[r][0].source, functions[r][1]) or '') or redact(ast.get_source_segment(functions[r][0].source, functions[r][1]) or '')[1] or any(
            RISK.search(functions[r][0].resolve(child.func)) for child in own_nodes(functions[r][1]) if isinstance(child, ast.Call)
        ) for r in closure if r in functions)
        if protected:
            reasons.append(CAPABILITY_REASONS[0])
        params = parameters(node)
        fields = {c.value: FIELD_ROLES.get(c.value, 'feature_parameter') for r in closure if r in functions
                  for c in ast.walk(functions[r][1]) if isinstance(c, ast.Constant) and isinstance(c.value, str) and c.value in FIELD_ROLES}
        for r in closure:
            if r not in functions:
                continue
            for child in own_nodes(functions[r][1]):
                if isinstance(child,ast.Subscript) and isinstance(child.slice,ast.Constant) and isinstance(child.slice.value,str):
                    root=dotted(child.value).split('.')[0]
                    key=child.slice.value
                    if re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]{0,63}',key) and root in {'row','wallet','account','record','config','settings'}:
                        fields[key]=FIELD_ROLES.get(key,'project_config' if root in {'config','settings'} else 'feature_parameter')
        wallet_paths = [(c.value, r) for r in closure if r in functions for c in ast.walk(functions[r][1])
                        if isinstance(c, ast.Constant) and isinstance(c.value, str) and re.search(r'\.(csv|txt|xlsx|json)$', c.value, re.I)]
        path = wallet_paths[0][0] if wallet_paths else ''
        mode = PurePosixPath(path.replace('\\', '/')).suffix[1:].upper() if path else 'unknown'
        state = set()
        for r in closure:
            if r not in functions:
                continue
            m, n = functions[r]
            globals_in_module={t.id for statement in m.tree.body if isinstance(statement, (ast.Assign, ast.AnnAssign)) for t in (statement.targets if isinstance(statement,ast.Assign) else [statement.target]) if isinstance(t,ast.Name)}
            state.update(child.id for child in own_nodes(n) if isinstance(child,ast.Name) and isinstance(child.ctx,ast.Load) and child.id in globals_in_module)
            state.update(name for child in own_nodes(n) if isinstance(child,ast.Global) for name in child.names)
        state=sorted(state)
        if not path and any(point['type']=='ENVIRONMENT_INPUT' and point['file'] in {functions[r][0].name for r in closure if r in functions} for point in findings):
            mode='environment'
        elif not path and evidence['type'] in {'CLI_COMMAND','ARGPARSE_COMMAND'}:
            mode='cli'
        definition = FeatureDefinition(feature_id='f_' + hashlib.sha256(ref.encode()).hexdigest()[:16], name=str(label)[:100],
            description='根据实际入口和调用关系发现的功能；独立性与业务前置条件需审核。', entrypoint_candidate=module.name,
            source_functions=[ref], dependencies=sorted(closure - {ref})[:40], shared_state=state[:40], parameters=params,
            interaction_points=points, evidence=[Evidence.model_validate(evidence)],
            wallet_input=WalletInput(mode=mode if mode in {'CSV', 'TXT', 'JSON', 'XLSX','environment','cli'} else 'unknown', source=redact(path)[0][:300], fields=dict(list(fields.items())[:30]),
                                    missing_fields=[key for key,role in fields.items() if role != 'identity'][:30]),
            risk_level='high' if protected else ('medium' if points or reasons else 'low'), confidence=evidence['confidence'],
            automation_status='MANUAL_REQUIRED' if reasons else 'REVIEW_REQUIRED', blocking_reasons=reasons,
            input_schema=parameter_schema(params))
        features.append(definition.model_dump())
    excluded = isolated_data(files)
    return {'protocol': 'airdrop-discovery-v1', 'status': 'MANUAL_REQUIRED' if errors or not features else 'REVIEW_REQUIRED',
            'features': features, 'findings': findings[:MAX_FINDINGS], 'functions': [
                {'ref': ref, 'file': m.name, 'name': n.name, 'line': n.lineno, 'end_line': n.end_lineno,
                 'async': isinstance(n, ast.AsyncFunctionDef), 'parameters': [p.model_dump() for p in parameters(n)]}
                for ref, (m, n) in functions.items()], 'call_graph': graph,
            'entrypoints': entrypoints, 'file_tree': sorted(files), 'excluded_data_files': excluded, 'errors': errors,
            'modules': [{'file':m.name,'imports':sorted(set(m.imports)),'classes':[{'name':node.name,'line':node.lineno,'end_line':node.end_lineno} for node in m.nodes if isinstance(node,ast.ClassDef)]} for m in modules.values()],
            'limits': {'source_chars': MAX_ANALYSIS_CHARS, 'nodes_per_file': MAX_NODES, 'max_features': 20},
            'dependency_inference': '仅表示实际调用和共享状态；注册、登录与业务顺序不能凭函数名确定'}


def isolated_data(files):
    """All non-source data stays encrypted; names/reasons only, no values in analysis."""
    return [{'file': name, 'reason': '数据文件隔离：不展示值且不发送外部模型'}
            for name in sorted(files) if not name.endswith('.py') and not name.endswith('requirements.txt')]
