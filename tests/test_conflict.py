from mnemotrace import MemoryEngine
from mnemotrace.store.contracts import FactQuery, FactState


def test_only_explicit_single_value_assertions_replace_prior_manual_values():
    memory = MemoryEngine()
    first = memory.add_fact('Ana', 'arbitrary_attribute', 'One', valid_at=1, text='Ana arbitrary attribute One')
    second = memory.add_fact('Ana', 'arbitrary_attribute', 'Two', valid_at=2, text='Ana arbitrary attribute Two')
    assert first.is_live() and second.is_live()
    third = memory.add_fact('Ana', 'arbitrary_attribute', 'Three', valid_at=3, cardinality='single', text='Ana arbitrary attribute Three')
    assert first.invalid_at == second.invalid_at == 3
    assert third.supersedes == second.id
    history = memory.aggregate_facts('arbitrary_attribute', facts=FactQuery(state=FactState.HISTORY))
    assert {fact.id for fact in history.facts} == {first.id, second.id}


def test_lifecycle_metadata_edits_and_claim_edits_have_distinct_effects():
    memory = MemoryEngine()
    fact = memory.add_fact('Ana', 'visited', 'Orbit', cardinality='multi', retention='durable', text='Ana visited Orbit')
    valid_at = fact.valid_at
    assert memory.update_fact(fact.id, retention='normal').cardinality == 'multi'
    assert fact.valid_at == valid_at and fact.text == 'Ana visited Orbit'
    revised = memory.update_fact(fact.id, object='Nadir', retention='durable', subject=fact.subject, predicate=fact.predicate, text=' '.join((fact.subject, fact.predicate.replace('_', ' '), 'Nadir')))
    assert revised.cardinality == 'unknown' and revised.retention == 'durable'
