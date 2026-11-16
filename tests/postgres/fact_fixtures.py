"""Persist synthetic notes and assertions for PostgreSQL tests."""
from mnemotrace.consolidate.postgres_merge import build_record_write
from mnemotrace.store.contracts import EmbeddingWrite, RecordKind
from mnemotrace.types import Entity, Fact, RelationType, relation_identifier


def seed_note(memory, content, session_id, *, event_time=None):
    source_id, = memory._store_originals(content, session_id, event_time=event_time)
    return memory.get_episode(source_id)


def relation_writes(view, records):
    context = view.snapshot.context
    definitions = {}
    for fact in records:
        if isinstance(fact, Fact) and fact.relation_id == relation_identifier(fact.predicate):
            definitions[fact.relation_id] = fact.predicate
    return tuple(
        build_record_write(context, RelationType(predicate, predicate, context.user_id, id=key))
        for key, predicate in definitions.items()
        if view.get(RecordKind.RELATION_TYPE, key) is None
    )


def fact_embeddings(memory, write, text):
    if memory._embedder is None:
        return ()
    vector = tuple(memory._embedder.embed(text))
    return (EmbeddingWrite(write.ref, write.record.content_version, memory._model, vector),)


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
    memory._commit(snapshot, writes=(*relations, *map(memory._write, entities.values()), write), embeddings=fact_embeddings(memory, write, text))
    return fact


def remember_and_publish(memory, value, session_id, **options):
    accepted = memory.remember(value, session_id, **options)
    task_id = accepted['operation_id']
    while memory._processing.run_next_stage(task_id):
        pass
    # Reuse the accepted ID for the completion response of a string shorthand.
    if isinstance(value, str):
        options['message_id'] = accepted['messages'][0]['message_id']
    return memory.remember(value, session_id, **options)


def checkpoint_before_publication(memory, task_id):
    """Simulate a transient commit failure after the validated bundle has been saved."""
    from unittest.mock import patch

    from mnemotrace.store.postgres.errors import DatabaseUnavailable

    with patch.object(type(memory._processing), '_commit_core',
                      side_effect=DatabaseUnavailable('Synthetic commit interruption')):
        assert memory._processing.run_next_stage(task_id)
    task = memory.operation(task_id)
    assert task.extraction_saved and task.state.value == 'retry_wait'
