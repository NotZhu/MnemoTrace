from mnemotrace import MemoryEngine
from tests.model_fixtures import seed_timeline
from mnemotrace.store.contracts import FactQuery, FactState


def test_only_confirmed_contradictions_replace_prior_values():
    memory = MemoryEngine()
    first = memory.add_fact('Ana', 'arbitrary_attribute', 'One', valid_at=1, text='Ana arbitrary attribute One')
    second = memory.add_fact('Ana', 'arbitrary_attribute', 'Two', valid_at=2, text='Ana arbitrary attribute Two')
    assert first.is_live() and second.is_live()
    third = seed_timeline(memory, 'Ana', 'arbitrary_attribute', 'Three', valid_at=3, text='Ana arbitrary attribute Three')
    assert first.invalid_at == second.invalid_at == 3
    assert set(third.supersedes) == {first.id, second.id}
    history = memory.aggregate_facts('arbitrary_attribute', facts=FactQuery(state=FactState.HISTORY))
    assert {fact.id for fact in history.facts} == {first.id, second.id}
