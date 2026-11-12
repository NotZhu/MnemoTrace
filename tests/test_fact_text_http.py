"""HTTP rejects generic fact edits and accepts only a boolean sensitivity flag."""
import pytest


@pytest.mark.parametrize('patch', [
    {}, {'sensitive': None}, {'sensitive': 'false'}, {'sensitive': 1},
    {'sensitive': True, 'text': 'Changed sentence'},
    {'sensitive': True, 'object_id': 'linked'},
    {'sensitive': True, 'categories': ['food']},
])
def test_sensitivity_request_rejects_other_fact_edits(patch):
    pytest.importorskip('fastapi')
    from pydantic import ValidationError
    from mnemotrace.server.app import FactSensitivity

    with pytest.raises(ValidationError):
        FactSensitivity(space_id='synthetic', **patch)


@pytest.mark.parametrize('sensitive', [True, False])
def test_sensitivity_request_accepts_boolean(sensitive):
    pytest.importorskip('fastapi')
    from mnemotrace.server.app import FactSensitivity

    assert FactSensitivity(space_id='synthetic', sensitive=sensitive).sensitive is sensitive
