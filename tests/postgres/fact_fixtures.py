"""Persist synthetic notes and assertions for PostgreSQL tests."""
from mnemotrace.consolidate.postgres_merge import write_record
from mnemotrace.store.contracts import RecordKind
from mnemotrace.types import Entity, Fact, RelationType, relation_identifier


def seed_note(memory, content, session_id, *, event_time=None):
    source_id, = memory.remember(content, session_id, extract=False, event_time=event_time)['source_ids']
    return memory.get_episode(source_id)


def relation_writes(view, records):
    context = view.snapshot.context
    definitions = {}
    for fact in records:
        if isinstance(fact, Fact) and fact.relation_id == relation_identifier(fact.predicate):
            definitions[fact.relation_id] = fact.predicate
    return tuple(
        write_record(context, RelationType(predicate, predicate, context.user_id, id=key))
        for key, predicate in definitions.items()
        if view.get(RecordKind.RELATION_TYPE, key) is None
    )


def seed_fact(memory, subject, predicate, object_, *, text, categories=(), linked=False, **fields):
    entities = {}
    if linked:
        for name in (subject, object_):
            if name not in entities:
                old = memory.entity(name)
                if old is None:
                    entities[name] = Entity(name, 'person' if name == '我' else None,
                                            user_id=memory.context.user_id)
                fields['subject_id' if name == subject else 'object_id'] = (old or entities[name]).id
        if subject == object_:
            fields['object_id'] = fields['subject_id']
    fact = Fact(subject, predicate, object_, text, user_id=memory.context.user_id,
                categories=list(categories), **fields)
    with memory.dao.read_snapshot() as view:
        snapshot = view.snapshot
        relations = relation_writes(view, (fact,))
    write = memory._write(fact)
    memory._commit(snapshot, writes=(*relations, *map(memory._write, entities.values()), write), embeddings=memory._embeddings(write, text))
    return fact
