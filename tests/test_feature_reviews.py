from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from backend import feature_reviews


def test_task_snapshot_rejects_runtime_sensitive_schema_mismatch(monkeypatch):
    definition = {
        'feature_id': 'feature', 'name': 'Feature', 'description': '',
        'entrypoint_candidate': 'main.py', 'source_functions': ['run'],
        'parameters': [{'name': 'api_key', 'type': 'string', 'required': True,
                        'sensitive': False, 'source': 'manual'}],
        'evidence': [{'file': 'main.py', 'line': 1, 'end_line': 1, 'type': 'entrypoint',
                      'confidence': 1, 'feasibility': 'POSSIBLE', 'manual_required': False}],
        'confidence': 0.9, 'entrypoint': 'main.py',
        'input_schema': {'type': 'object', 'properties': {'api_key': {'type': 'string'}}, 'required': ['api_key']},
    }
    version = SimpleNamespace(id='version', source_artifact_id='artifact', manifest={'entrypoint': 'main.py'})
    feature = SimpleNamespace(version_id=version.id, feature_key='feature', status='APPROVED', approved_by='admin',
                              definition=definition, code_sha256='code-sha256', cipher_sha256='cipher-sha256',
                              version_manifest_sha256=feature_reviews.digest(version.manifest),
                              runtime_spec={'protocol': 'airdrop-native-runtime-v1', 'entrypoint': 'main.py',
                                            'parameters': [{'name': 'api_key', 'type': 'string',
                                                            'source': 'manual', 'sensitive': True}]})
    feature.approval_sha256 = feature_reviews.approval_digest(feature)
    source = SimpleNamespace(sha256=feature.code_sha256)

    class Database:
        def get(self, model, identity):
            return source

    monkeypatch.setattr(feature_reviews, 'artifact_fingerprint', lambda _: feature.cipher_sha256)
    with pytest.raises(HTTPException) as error:
        feature_reviews.task_snapshot(Database(), feature, version, {'api_key': 'raw-secret-value'}, None)

    assert error.value.status_code == 409
    assert '敏感属性' in error.value.detail
