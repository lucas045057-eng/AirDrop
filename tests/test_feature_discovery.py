import pytest
from adapters.discovery import scan


MENU = '''
def register(count: int = 1):
    return count
def checkin():
    return True
async def points():
    return 5
def main():
    print("1 注册 2 签到 3 积分")
    choice = input("请选择功能")
    if choice == "1": register()
    elif choice == "2": checkin()
    elif choice == "3": points()
if __name__ == "__main__": main()
'''


def analyze(source, extra=None):
    return scan({'main.py': source.encode(), **(extra or {})})


def test_native_menu_evidence_and_async():
    result = analyze(MENU)
    assert len(result['features']) == 3
    assert {f['source_functions'][0] for f in result['features']} == {'main.py:register', 'main.py:checkin', 'main.py:points'}
    assert all(f['automation_status'] == 'REVIEW_REQUIRED' for f in result['features'])
    assert any(f['async'] for f in result['functions'])
    for feature in result['features']:
        for evidence in feature['evidence']:
            assert 1 <= evidence['line'] <= evidence['end_line'] <= len(MENU.splitlines())
            assert evidence['excerpt'] in MENU
    assert any(item['type'] == 'MENU' for item in result['findings'])


def test_multifile_menu_and_shared_state():
    source = 'from modules.business import run\ndef main():\n c=input("menu")\n if c=="1": run()\nif __name__=="__main__": main()\n'
    result = analyze(source, {'modules/business.py': b'STATE=0\ndef run():\n global STATE\n STATE+=1\n return STATE\n'})
    feature = result['features'][0]
    assert feature['source_functions'] == ['modules/business.py:run']
    assert feature['shared_state'] == ['STATE']


@pytest.mark.parametrize('imports, expression', [
    ('from rich.console import Console as C\nconsole=C()', 'console.input("数量")'),
    ('from rich.prompt import Prompt as P', 'P.ask("数量")'),
    ('from rich.prompt import Confirm', 'Confirm.ask("是否继续")'),
    ('import click as c', 'c.prompt("数量")'),
    ('from typer import prompt as ask', 'ask("数量")'),
    ('import questionary', 'questionary.text("数量").ask()'),
    ('import inquirer', 'inquirer.prompt([])'),
    ('from getpass import getpass as gp', 'gp("密码")'),
    ('import sys as system', 'system.stdin.readline()'),
])
def test_interaction_aliases(imports, expression):
    result = analyze(imports + '\ndef main():\n value=' + expression + '\n return value\nif __name__=="__main__": main()\n')
    assert result['features'][0]['interaction_points']
    assert result['features'][0]['automation_status'] == 'MANUAL_REQUIRED'


@pytest.mark.parametrize('prompt, expected', [('请输入数量', 'PARAMETER'), ('文件路径', 'FILE_PATH'), ('选择钱包', 'WALLET_ACCOUNT'),
    ('是否确认', 'CONFIRMATION'), ('输入验证码 OTP', 'CAPTCHA_OTP'), ('确认转账', 'FUNDS_CONFIRMATION'), ('', 'UNKNOWN')])
def test_classification(prompt, expected):
    result = analyze(f'def main():\n return input({prompt!r})\nif __name__=="__main__": main()\n')
    assert result['features'][0]['interaction_points'][0]['type'] == expected


def test_prompt_wrapper_not_all_helper_functions():
    source = 'def ask():\n return input("请选择")\ndef helper():\n return 1\ndef run():\n return helper()\ndef main():\n c=ask()\n if c=="1": run()\nif __name__=="__main__": main()\n'
    result = analyze(source)
    assert [f['source_functions'] for f in result['features']] == [['main.py:run']]
    assert result['features'][0]['dependencies'] == ['main.py:helper']


@pytest.mark.parametrize('source', [
    'import click\n@click.group()\ndef cli(): pass\n@cli.command()\ndef checkin(): return 1\n',
    'import typer\napp=typer.Typer()\n@app.command()\ndef checkin(count:int=1): return count\n',
    'import argparse\ndef checkin(): return 1\ndef main():\n parser=argparse.ArgumentParser()\n sub=parser.add_subparsers()\n run=sub.add_parser("checkin")\n run.set_defaults(func=checkin)\nif __name__=="__main__": main()\n',
])
def test_cli_features(source):
    assert any(f['source_functions'] == ['main.py:checkin'] for f in analyze(source)['features'])


def test_multi_entry_and_non_interactive():
    source = 'def main(count:int): return count\nif __name__=="__main__": main(1)\n'
    result = analyze(source, {'second.py': source.encode()})
    assert len(result['entrypoints']) == 2 and len(result['features']) == 2
    assert not result['findings']


def test_dynamic_menu_is_not_invented():
    result = analyze('def main():\n dispatch={"x":lambda:None}\n return dispatch[input()]()\nif __name__=="__main__": main()\n')
    assert len(result['features']) == 1
    assert result['features'][0]['automation_status'] == 'MANUAL_REQUIRED'
    assert result['features'][0]['blocking_reasons']


def test_data_isolation_sensitive_defaults_and_no_import_execution(tmp_path):
    marker = tmp_path / 'must-not-exist'
    source = f'open({str(marker)!r},"w").write("executed")\ndef main(private_key="hidden-secret"):\n return private_key\nif __name__=="__main__": main()\n'
    result = analyze(source, {'accounts.csv': b'private_key,password\nREAL_SECRET,REAL_PASSWORD'})
    assert not marker.exists()
    assert 'hidden-secret' not in str(result)
    assert 'REAL_SECRET' not in str(result)
    assert result['excluded_data_files'][0]['file'] == 'accounts.csv'
    feature = result['features'][0]
    assert feature['risk_level'] == 'high'
    assert feature['parameters'][0]['sensitive'] and not feature['parameters'][0]['has_default']


def test_wallet_mapping_does_not_add_business_fields_to_wallet_identity():
    source = 'import csv\ndef main():\n data=csv.DictReader(open("wallets.csv"))\n return [(row["address"],row["invite_code"],row["proxy"]) for row in data]\nif __name__=="__main__": main()\n'
    feature = analyze(source)['features'][0]
    assert feature['wallet_input']['mode'] == 'CSV'
    assert feature['wallet_input']['fields']['address'] == 'identity'
    assert feature['wallet_input']['fields']['invite_code'] == 'account_config'
    assert feature['wallet_input']['fields']['proxy'] == 'account_config'


def test_budget_syntax_and_windows_fail_closed():
    assert scan({'huge.py': b'#' * 2_000_001})['status'] == 'MANUAL_REQUIRED'
    assert analyze('def broken(')['errors']
    result = analyze('import win32api\ndef main(): return win32api.GetUserName()\nif __name__=="__main__": main()\n')
    assert result['features'][0]['blocking_reasons']


@pytest.mark.parametrize('source',[
    'def main(x=print("side effect")): return 1\nif __name__=="__main__": main()\n',
    '@unexpected_factory()\ndef main(): return 1\nif __name__=="__main__": main()\n',
    'class Factory:\n print("side effect")\ndef main(): return 1\nif __name__=="__main__": main()\n',
])
def test_definition_time_effects_are_manual(source):
    feature=analyze(source)['features'][0]
    assert any('导入' in reason for reason in feature['blocking_reasons'])


def test_modular_roots_are_unknown_not_invented_business_features():
    result=analyze('def load_config(): return 1\ndef operation(): return load_config()\n')
    assert len(result['features'])==1
    assert result['features'][0]['source_functions']==['main.py:operation']
    assert result['features'][0]['automation_status']=='MANUAL_REQUIRED'
    assert '不能静态确认' in str(result['features'][0]['blocking_reasons'])


def test_security_alias_cannot_lower_risk():
    feature=analyze('from security import verify_identity as benign\ndef main(): return benign()\nif __name__=="__main__": main()\n')['features'][0]
    assert feature['risk_level']=='high'


def test_assignment_alias_and_transitive_menu_wrapper():
    source='from builtins import input as ask\nreader=ask\ndef layer(): return reader("请选择功能")\ndef outer(): return layer()\ndef operation(): return 1\ndef main():\n choice=outer()\n if choice=="1": operation()\nif __name__=="__main__": main()\n'
    result=analyze(source)
    assert len(result['features'])==1 and result['features'][0]['source_functions']==['main.py:operation']
    assert any(item['type']=='MENU' for item in result['findings'])


def test_local_function_assignment_alias_preserves_interaction_call_graph():
    result=analyze('def reader(): return input("数量")\nask=reader\ndef main(): return ask()\nif __name__=="__main__": main()\n')
    assert result['call_graph']['main.py:main']==['main.py:reader']
    assert result['features'][0]['automation_status']=='MANUAL_REQUIRED'


def test_local_function_named_input_is_not_reported_as_builtin_stdin():
    result=analyze('def input(): return 1\ndef main(): return input()\nif __name__=="__main__": main()\n')
    assert not result['features'][0]['interaction_points']
    assert result['call_graph']['main.py:main']==['main.py:input']


def test_conflicting_scope_alias_is_unknown_instead_of_hiding_prompt():
    source='import click as c\ndef main(): return c.prompt("数量")\ndef helper():\n import unrelated as c\n return c.value\nif __name__=="__main__": main()\n'
    result=analyze(source)
    assert result['features'][0]['automation_status']=='MANUAL_REQUIRED'
    assert any(f['type']=='DYNAMIC_CALL' and f['feasibility']=='UNKNOWN' for f in result['findings'])


def test_structure_contains_environment_config_persistence_and_classes():
    source='import os, sqlite3\nclass Config: pass\nSTATE=1\ndef main():\n file=os.getenv("WALLET_FILE")\n db=sqlite3.connect("config.json")\n db.execute("SELECT 1")\n return STATE\nif __name__=="__main__": main()\n'
    result=analyze(source)
    assert result['modules'][0]['classes'][0]['name']=='Config'
    assert {'ENVIRONMENT_INPUT','CONFIG_FILE_REFERENCE','PERSISTENCE'} <= {f['type'] for f in result['findings']}
    assert result['features'][0]['shared_state']==['STATE']


@pytest.mark.parametrize('extension',['txt','json','xlsx'])
def test_wallet_formats_keep_source_reference_without_reading_data(extension):
    feature=analyze(f'def main(): return open("wallets.{extension}")\nif __name__=="__main__": main()\n')['features'][0]
    assert feature['wallet_input']['mode']==extension.upper()
    assert feature['wallet_input']['source']==f'wallets.{extension}'


def test_business_fields_are_classified_and_missing_mapping_is_explicit():
    source='def main(row,config):\n return row["address"],row["campaign_name"],config["project_code"]\nif __name__=="__main__": main({}, {})\n'
    mapping=analyze(source)['features'][0]['wallet_input']
    assert mapping['fields']['campaign_name']=='feature_parameter'
    assert mapping['fields']['project_code']=='project_config'
    assert set(mapping['missing_fields'])=={'campaign_name','project_code'}
