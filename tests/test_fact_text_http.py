"""HTTP rejects incomplete fact content before reaching the service."""
import pytest


@pytest.mark.parametrize('text', [None, '', ' \n\t', 42])
def test_manual_fact_request_requires_nonblank_sentence(text):
    pytest.importorskip('fastapi')
    from pydantic import ValidationError
    from mnemotrace.server.app import FactReq

    with pytest.raises(ValidationError):
        FactReq(space_id='synthetic', predicate='likes', object='茶', text=text)


@pytest.mark.parametrize('patch', [
    {'text': '用户喜欢茶。'}, {'object': '茶'}, {'subject': None},
    {'subject': None, 'predicate': None, 'object': None, 'text': None},
    {'subject': 'user', 'predicate': 'likes', 'object': '茶'},
    {'subject': 'user', 'predicate': 'likes', 'object': '茶', 'text': ' '},
    {'display': '旧字段'}, {'statement': '旧字段'},
])
def test_manual_patch_requires_complete_content_and_rejects_retired_fields(patch):
    pytest.importorskip('fastapi')
    from pydantic import ValidationError
    from mnemotrace.server.app import FactEdit

    with pytest.raises(ValidationError):
        FactEdit(space_id='synthetic', **patch)


def test_metadata_only_patch_and_complete_content_are_valid():
    pytest.importorskip('fastapi')
    from mnemotrace.server.app import FactEdit, FactReq

    fact = FactReq(space_id='synthetic', predicate='likes', object='茶', text='用户喜欢茶。')
    assert fact.subject == 'user'
    assert FactEdit(space_id='synthetic', sensitive=False).text is None
    patch = FactEdit(space_id='synthetic', subject='user', predicate='likes', object='咖啡', text='用户喜欢咖啡。')
    assert patch.text == '用户喜欢咖啡。'
