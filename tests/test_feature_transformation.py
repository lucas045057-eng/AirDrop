import copy
import json
import pytest
from adapters.discovery import scan
from adapters.planning import context
from adapters.transformation import rules_plan, transform, wrapper
from packages.feature_protocol import FeatureDefinition, ModelPlan, Parameter
from tests.test_feature_discovery import MENU


def setup(source=MENU):
    files = {'main.py': source.encode()}
    report = scan(files)
    ids = [f['feature_id'] for f in report['features']]
    return files, report, ids


class FakeProvider:
    """Test injection only. No production API or settings expose this provider."""
    def __init__(self, proposal):
        self.proposal, self.calls = proposal, 0
    async def plan(self, payload):
        self.calls += 1
        return copy.deepcopy(self.proposal)


async def test_fake_ai_splits_three_features_without_copying_business():
    files, report, ids = setup()
    provider = FakeProvider(rules_plan(files, report, ids).model_dump())
    payload, digest = context(files, report, ids)
    proposal = await provider.plan(payload)
    adapted, result = transform(files, report, ids, proposal)
    assert provider.calls == 1 and len(digest) == 64
    assert len(result['features']) == 3 and len(result['new_files']) == 3
    assert adapted['main.py'] == files['main.py']
    assert not result['modified_files']
    assert all(f['automation_status'] == 'REVIEW_REQUIRED' for f in result['features'])
    assert sum(len(adapted[name]) for name in result['new_files']) < 1600


def test_explicit_parameter_extraction_in_limited_module_patch():
    source = 'def run(count:int): return count\ndef main():\n count=int(input("数量"))\n return run(count)\nif __name__=="__main__": main()\n'
    files, report, ids = setup(source)
    feature = FeatureDefinition.model_validate(report['features'][0])
    feature.parameters = [Parameter(name='count', type='integer', source='function')]
    generated = wrapper(feature, files)
    replacement = 'def run(count:int): return count\ndef main(count:int):\n return run(count)\nif __name__=="__main__": main()\n'
    proposal = ModelPlan(features=[feature], generated_files={feature.entrypoint: generated}, replacements={'main.py': replacement})
    adapted, result = transform(files, report, ids, proposal)
    assert adapted['main.py'] != files['main.py']
    assert result['features'][0]['input_schema']['required'] == ['count']
    assert result['features'][0]['automation_status'] == 'REVIEW_REQUIRED'
    assert result['modified_files'] == ['main.py']


@pytest.mark.parametrize('mutation, match', [
    (lambda p: p['features'][0].update(source_functions=['main.py:nonexistent']), '实际函数'),
    (lambda p: p['generated_files'].update({'../evil.py': 'pass'}), '路径'),
    (lambda p: p['generated_files'].update({'airdrop_generated/x.py': 'invalid python('}), None),
    (lambda p: p['features'][0]['evidence'][0].update(line=999, end_line=999), '行号'),
    (lambda p: p['features'][0].update(permissions=['sign']), None),
    (lambda p: p.update(execution_enabled=True), None),
    (lambda p: p['generated_files'].update({'airdrop_generated/x.py': 'import os\nos.environ["AIRDROP_EXECUTION_ENABLED"]="true"'}), '环境开关'),
    (lambda p: p['generated_files'].update({'main.py': 'pass'}), '覆盖'),
])
def test_untrusted_model_outputs_fail_closed(mutation, match):
    files, report, ids = setup()
    proposal = rules_plan(files, report, ids).model_dump()
    mutation(proposal)
    with pytest.raises((ValueError, SyntaxError), match=match):
        transform(files, report, ids, proposal)
    assert files['main.py'] == MENU.encode()


def test_sensitive_module_cannot_be_rewritten_or_risk_downgraded():
    files, report, ids = setup('def main():\n return sign_transaction(payload)\nif __name__=="__main__": main()\n')
    proposal = rules_plan(files, report, ids).model_dump()
    proposal['replacements'] = {'main.py': 'def main():\n return True\nif __name__=="__main__": main()\n'}
    with pytest.raises(ValueError, match='禁止自动修改'):
        transform(files, report, ids, proposal)
    proposal['replacements'] = {}
    proposal['features'][0]['risk_level'] = 'low'
    _, result = transform(files, report, ids, proposal)
    assert result['features'][0]['risk_level'] == 'high'
    assert result['features'][0]['automation_status'] == 'MANUAL_REQUIRED'


def test_unresolved_interaction_and_business_logic_removal():
    files, report, ids = setup('def main(): return input("数量")\nif __name__=="__main__": main()\n')
    proposal = rules_plan(files, report, ids)
    _, result = transform(files, report, ids, proposal)
    assert result['features'][0]['automation_status'] == 'MANUAL_REQUIRED'
    files, report, ids = setup()
    proposal = rules_plan(files, report, ids)
    proposal.replacements['main.py'] = MENU.replace('return count', 'return 0')
    with pytest.raises(ValueError, match='非交互业务'):
        transform(files, report, ids, proposal)


def test_context_is_bounded_redacted_and_bound_to_selected_features():
    files, report, ids = setup()
    files['accounts.json'] = b'{"private_key":"very-real-secret"}'
    payload, digest = context(files, report, ids)
    assert 'very-real-secret' not in json.dumps(payload)
    assert 'accounts.json' not in json.dumps(payload)
    assert context(files, report, ids[:1])[1] != digest
    with pytest.raises(ValueError): context(files, report, ['made_up'])


def test_missing_config_and_shared_state_are_not_guessed():
    files, report, ids = setup('STATE=0\ndef main(count:int):\n global STATE\n STATE+=count\n return STATE\nif __name__=="__main__": main(1)\n')
    proposal = rules_plan(files, report, ids)
    _, result = transform(files, report, ids, proposal)
    feature = result['features'][0]
    assert feature['shared_state'] == ['STATE']
    assert feature['parameters'][0]['required'] and not feature['parameters'][0]['has_default']


def test_model_cannot_return_noop_stub_or_guess_default():
    files, report, ids = setup()
    proposal = rules_plan(files, report, ids)
    feature = proposal.features[0]
    proposal.generated_files[feature.entrypoint] = 'def run_feature(**parameters): return True\n'
    with pytest.raises(ValueError, match='实际调用'):
        transform(files, report, ids, proposal)


def test_input_extraction_cannot_delete_business_logic():
    files, report, ids = setup('def main():\n count=int(input("数量"))\n print(count)\n return count\nif __name__=="__main__": main()\n')
    feature=FeatureDefinition.model_validate(report['features'][0])
    feature.parameters=[Parameter(name='count',type='integer',source='function')]
    generated=wrapper(feature,files)
    proposal=ModelPlan(features=[feature],generated_files={feature.entrypoint:generated},replacements={'main.py':'def main(count:int):\n return 0\nif __name__=="__main__": main()\n'})
    with pytest.raises(ValueError,match='业务逻辑'): transform(files,report,ids,proposal)


def test_generated_wrapper_cannot_call_extra_business_function():
    files, report, ids=setup()
    proposal=rules_plan(files,report,ids)
    feature=proposal.features[0]
    proposal.generated_files[feature.entrypoint] += '\ndef unrelated(): return print("side effect")\n'
    with pytest.raises(ValueError,match='轻量调用'): transform(files,report,ids,proposal)
    proposal = rules_plan(files, report, ids)
    feature = next(f for f in proposal.features if f.parameters)
    feature.parameters[0].default = 99
    with pytest.raises(ValueError, match='默认参数'):
        transform(files, report, ids, proposal)


def test_click_entry_invokes_callback_without_replaying_cli():
    files,report,ids=setup('import click\n@click.command()\ndef query(count:int=1): return count\nif __name__=="__main__": query()\n')
    proposal=rules_plan(files,report,ids)
    adapted,result=transform(files,report,ids,proposal)
    source=adapted[result['features'][0]['entrypoint']].decode()
    assert '_business.callback(**parameters)' in source
    assert result['features'][0]['automation_status']=='REVIEW_REQUIRED'


@pytest.mark.parametrize('source',[
    'import click\n@click.command()\n@click.option("--count",type=click.IntRange(1,5))\ndef query(count:int): return count\nif __name__=="__main__": query()\n',
    'import click\n@click.group()\ndef cli(): initialize_state()\n@cli.command()\ndef query(): return 1\nif __name__=="__main__": cli()\n',
    'import click\n@click.command()\ndef query(): return click.get_current_context()\nif __name__=="__main__": query()\n',
    'import argparse\ndef main():\n parser=argparse.ArgumentParser()\n return parser.parse_args()\nif __name__=="__main__": main()\n',
    'import sys\ndef main(): return sys.argv\nif __name__=="__main__": main()\n',
])
def test_cli_validation_context_and_runtime_arguments_are_manual(source):
    files,report,ids=setup(source)
    assert len(ids)==1
    _,result=transform(files,report,ids,rules_plan(files,report,ids))
    assert result['features'][0]['automation_status']=='MANUAL_REQUIRED'
    assert result['features'][0]['blocking_reasons']


@pytest.mark.parametrize('extra',[
    {'pkg/__init__.py':b'print("import side effect")\n','pkg/core.py':b'def operation(): return 1\n'},
    {'pkg/__init__.py':b'from .initialization import STATE\n','pkg/initialization.py':b'STATE=compute_state()\n','pkg/core.py':b'def operation(): return 1\n'},
])
def test_package_and_transitive_import_effects_cannot_be_claimed_safe(extra):
    files={'main.py':b'from pkg.core import operation\nif __name__=="__main__": operation()\n',**extra}
    report=scan(files);ids=[f['feature_id'] for f in report['features']]
    assert report['features'][0]['automation_status']=='MANUAL_REQUIRED'
    proposal=rules_plan(files,report,ids)
    proposal.features[0].blocking_reasons=[]
    proposal.generated_files[proposal.features[0].feature_id]=wrapper(proposal.features[0],files)
    proposal.generated_files={proposal.features[0].entrypoint:next(iter(proposal.generated_files.values()))}
    _,result=transform(files,report,ids,proposal)
    assert result['features'][0]['automation_status']=='MANUAL_REQUIRED'


def test_local_click_shadow_is_unknown_without_import_execution():
    files={'main.py':b'import click\n@click.command()\ndef operation(): return 1\nif __name__=="__main__": operation()\n','click.py':b'def command(): return None\n'}
    feature=scan(files)['features'][0]
    assert any('同名' in reason for reason in feature['blocking_reasons'])
