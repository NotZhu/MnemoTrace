"""Persist synthetic assertions without exposing a product write endpoint."""
from mnemotrace.types import Entity, Fact


def seed_fact(memory, subject, predicate, object_, *, text, categories=(), linked=False, **fields):
    entities = {}
    if linked:
        for name in (subject, object_):
            if name not in entities:
                old = memory.entity(name)
                if old is None:
                    entities[name] = Entity(name, 'person' if name == 'user' else None,
                                            user_id=memory.context.user_id)
                fields['subject_id' if name == subject else 'object_id'] = (old or entities[name]).id
        if subject == object_:
            fields['object_id'] = fields['subject_id']
    fact = Fact(subject, predicate, object_, text, user_id=memory.context.user_id,
                categories=list(categories), categories_source='llm' if categories else 'unset', **fields)
    with memory.dao.read_snapshot() as view:
        snapshot = view.snapshot
    write = memory._write(fact)
    memory._commit(snapshot, writes=(*map(memory._write, entities.values()), write), embeddings=memory._embeddings(write, text))
    return fact
