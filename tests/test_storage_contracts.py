"""Executable target contracts, deliberately independent of the current runtime."""
from dataclasses import FrozenInstanceError, asdict, replace
import json

import pytest

from mnemotrace.store.contracts import (
    Candidate, CandidateBatch, ChangeSet, CommitConditions, CommitReceipt, EmbeddingWrite,
    ExtractionProduct, FactInvalidation, FactQuery, FactState, Lease, MessageInput,
    OperationKind, OperationResult, OperationSpec, OperationStats, ProcessingPhase,
    ProcessingState, ReadSnapshot, RecordKind, RecordRef, RecordWrite, RetrievalQuery,
    SourceLink, SourceVersion, SpaceContext, SpaceSettings, StoredRecord, RecordVersion,
)
from mnemotrace.types import Fact, Episode

SPACE = SpaceContext('owner', 'space-1')
SNAPSHOT = ReadSnapshot(SPACE, 7, 2, 'thread')
SOURCES = (SourceVersion('source-1', 1),)
CONDITIONS = CommitConditions(SNAPSHOT, SOURCES, Lease('operation-1', 'lease-1'), 'config-1')


def ref(kind=RecordKind.FACT, key='fact-1', context=SPACE):
    return RecordRef(context, kind, key)


def fact_record(**kwargs):
    return StoredRecord.from_record(SPACE, Fact('Ana', 'works_at', 'Orbit', user_id='owner',
                                              id='fact-1', created_at=100, valid_at=90,
                                              last_access=100, text='Ana works at Orbit'), **kwargs)


def product():
    text = 'I work at Orbit.'
    data = dict(operation='extract', source_kind='dialogue', extract=True, context=[], parts=[], candidates=[],
                pending=[dict(id='source-1', role='user', content=text, needs_digest=False,
                              message_id='message-1', sequence=1, event_time=100, representation='raw',
                              representation_source=None, source='a' * 64, source_length=len(text),
                              ranges=[[0, len(text)]], partial=False)])
    result = dict(facts=[dict(categories=[], object_kind='unknown', sensitive=False, time_kind='current', effective_date=None, cardinality='unknown', retention='unknown', subject='Ana', predicate='works_at', object='Orbit', text='Ana works at Orbit',
                             source_ids=['source-1'])], digests={})
    return ExtractionProduct(CONDITIONS, json.dumps(data), json.dumps(result),
                             OperationStats(0, 0, 0, 1, 0), 800, 7)


def test_context_is_immutable_and_preserves_identity():
    context = SpaceContext('Owner', 'Space-A')
    assert context.user_id == 'Owner' and context.space_id == 'Space-A'
    with pytest.raises(FrozenInstanceError):
        context.space_id = 'other'
    with pytest.raises(TypeError):
        SpaceContext('owner')


@pytest.mark.parametrize('kind', ['facts'])
def test_dialogue_output_requires_a_pending_user_source(kind):
    from mnemotrace.config import Config
    from mnemotrace.consolidate.planning import validate_response
    from mnemotrace.extraction import ExtractionResponseError

    checkpoint = product()
    data = json.loads(checkpoint.input_json)
    old_user, = data['pending']
    data.update(context=[old_user], pending=[dict(old_user, id='assistant', role='assistant', sequence=2)])
    output = dict(facts=[], digests=[])
    refs = ['source-1', 'assistant']
    if kind == 'facts':
        fact, = json.loads(checkpoint.result_json)['facts']
        output[kind] = [dict(fact, source_ids=refs)]
    else:
        output[kind] = [dict(content='Synthetic task', kind='task', source_ids=refs)]
    with pytest.raises(ExtractionResponseError):
        validate_response(json.dumps(output), data, Config())


@pytest.mark.parametrize('value', ['', ' ', None, 1, 'x' * 513])
def test_invalid_identity(value):
    with pytest.raises((ValueError, TypeError)):
        SpaceContext('owner', value)


@pytest.mark.parametrize('kwargs', [dict(core_revision=True), dict(core_revision=-1),
    dict(space_generation=-1), dict(session_id='')])
def test_snapshot_rejects_missing_or_invalid_versions(kwargs):
    with pytest.raises(ValueError):
        replace(SNAPSHOT, **kwargs)


def test_task_commit_requires_versions_config_and_lease_identity():
    for kwargs in (dict(sources=()), dict(extraction_config=None), dict(sources=SOURCES * 2)):
        with pytest.raises(ValueError):
            replace(CONDITIONS, **kwargs)
    with pytest.raises(ValueError, match='Lease'):
        ChangeSet(CONDITIONS, 'another-operation')


@pytest.mark.parametrize('kwargs', [dict(state='current'), dict(limit=0), dict(limit=True),
    dict(limit=1001), dict(valid_at=float('nan')), dict(known_at=float('inf')), dict(source_ids=['s'])])
def test_structured_queries_reject_callbacks_invalid_time_and_unbounded_pages(kwargs):
    with pytest.raises(ValueError):
        FactQuery(**kwargs)
    with pytest.raises(TypeError):
        FactQuery(where=lambda f: True)


def test_query_time_axes_and_history_are_explicit():
    query = FactQuery(FactState.HISTORY, valid_at=10, known_at=20, subject='Ana', predicate='works_at')
    assert query.valid_at != query.known_at
    assert 'space_id' not in asdict(query)  # Bound by the DAO, never overridden per query.


@pytest.mark.parametrize('vector', [(), [], (True,), (None,), (float('nan'),)])
def test_embedding_input_rejects_invalid_coordinates(vector):
    with pytest.raises(ValueError):
        RetrievalQuery(FactQuery(), 'query', vector, 'model-64')


def test_message_fingerprint_normalizes_only_event_time():
    message = MessageInput('message', 'user', '  Orbit  ', 1, 'agent-A')
    assert message.fingerprint == replace(message, event_time=1.0).fingerprint
    assert message.fingerprint == replace(message, message_id='another-id').fingerprint
    for kwargs in (dict(role='assistant'), dict(content='Orbit'), dict(agent_id='agent-B'),
                   dict(event_time=None), dict(extract=False)):
        assert message.fingerprint != replace(message, **kwargs).fingerprint
    with pytest.raises(TypeError):
        MessageInput('message', 'user', 'text', ingested_at=10)


@pytest.mark.parametrize('kwargs', [dict(role='system'), dict(agent_id=''), dict(agent_id='bad space'),
    dict(agent_id='助手'), dict(content=' '), dict(event_time=True), dict(event_time=float('inf'))])
def test_messages_reject_invalid_source_fields(kwargs):
    with pytest.raises(ValueError):
        replace(MessageInput('message', 'user', 'text'), **kwargs)


def test_domain_record_is_detached_and_rejects_wrong_owner_or_unknown_columns():
    value = Fact('Ana', 'works_at', 'Orbit', user_id='owner', text='Ana works at Orbit')
    record = StoredRecord.from_record(SPACE, value)
    value.object = 'Changed'
    assert json.loads(record.payload_json)['object'] == 'Orbit'
    data = json.loads(record.payload_json)
    for altered in ({**data, 'user_id': 'attacker'}, {**data, 'arbitrary_sql_column': 'value'},
                    {k: v for k, v in data.items() if k != 'created_at'}):
        with pytest.raises(ValueError):
            replace(record, payload_json=json.dumps(altered))


def test_record_updates_advance_exactly_one_version():
    RecordWrite(fact_record())
    RecordWrite(fact_record(record_version=3), expected_version=2)
    with pytest.raises(ValueError):
        RecordWrite(fact_record(record_version=3))


def test_candidates_distinguish_empty_success_from_degradation_and_wrong_scope():
    assert CandidateBatch(SNAPSHOT, (), True).complete
    candidate = Candidate(fact_record(), 0.5, 1.0, 0.0, 0.5, 1.0)
    limited = CandidateBatch(SNAPSHOT, (candidate,), False, ('vector_index_not_ready',))
    assert limited.limitations
    with pytest.raises(ValueError):
        replace(limited, limitations=())
    with pytest.raises(ValueError):
        replace(limited, snapshot=replace(SNAPSHOT, context=SpaceContext('other', 'space-1')))


def test_digest_vector_uses_digest_version_not_immutable_raw_version():
    source = Episode('Synthetic source', user_id='owner', digest='Digest', digest_embedding=[1.0])
    stored = StoredRecord.from_record(SPACE, source, content_version=1, record_version=4, digest_version=3)
    changes = ChangeSet(CommitConditions(SNAPSHOT), 'digest-commit', writes=(RecordWrite(stored, 3),),
                        embeddings=(EmbeddingWrite(stored.ref, 3, 'model', (1.0,), 'digest'),))
    with pytest.raises(ValueError, match='version'):
        replace(changes, embeddings=(EmbeddingWrite(stored.ref, 1, 'model', (1.0,), 'digest'),))


def test_maintenance_can_target_source_free_manual_fact_and_cannot_cross_space():
    operation = OperationSpec('reembed-1', OperationKind.REEMBED, targets=(RecordVersion(ref(), 1),))
    changes = ChangeSet(CommitConditions(SNAPSHOT), 'schedule-1', operations=(operation,))
    assert changes.operations[0].sources == ()
    guarded = CommitConditions(SNAPSHOT, lease=Lease('reembed-1', 'lease'), targets=operation.targets)
    assert ChangeSet(guarded, 'reembed-1').conditions.targets
    other = RecordVersion(ref(context=SpaceContext('owner', 'other-space')), 1)
    with pytest.raises(ValueError):
        replace(changes, operations=(replace(operation, targets=(other,)),))


def test_stale_vectors_and_new_references_to_erased_records_are_rejected():
    write = RecordWrite(fact_record())
    with pytest.raises(ValueError, match='text version'):
        ChangeSet(CommitConditions(SNAPSHOT), 'op', writes=(write,),
                  embeddings=(EmbeddingWrite(write.ref, 2, 'model', (1.0,)),))
    with pytest.raises(ValueError, match='erased'):
        ChangeSet(CommitConditions(SNAPSHOT), 'op', erasures=(write.ref,),
                  embeddings=(EmbeddingWrite(write.ref, 1, 'model', (1.0,)),))


def test_changeset_checks_scope_provenance_duplicate_mutations_and_receipt():
    record = fact_record()
    payload = {**json.loads(record.payload_json), 'provenance': ['source-1']}
    write = RecordWrite(replace(record, payload_json=json.dumps(payload)))
    source = ref(RecordKind.SOURCE, 'source-1')
    changes = ChangeSet(CONDITIONS, 'operation-1', writes=(write,),
        source_links=(SourceLink(write.ref, source),), embeddings=(EmbeddingWrite(write.ref, 1, 'model', (1.0, 0.0)),),
        operations=(OperationSpec('digest-1', OperationKind.DIGEST, 'thread', SOURCES),),
        receipt=CommitReceipt('operation-1', 8, ('fact-1',)))
    assert changes.receipt.core_revision == 8
    for kwargs in (dict(erasures=(write.ref,)), dict(writes=(write, write)),
                   dict(erasures=(ref(context=SpaceContext('owner', 'space-2')),)),
                   dict(receipt=CommitReceipt('operation-1', 9)), dict(settings={})):
        with pytest.raises(ValueError):
            replace(changes, **kwargs)
    with pytest.raises(ValueError, match='provenance'):
        replace(changes, source_links=())
    with pytest.raises(ValueError):
        SourceLink(write.ref, replace(source, context=SpaceContext('other', 'space-1')))


def test_invalidation_entities_and_settings_are_narrow_types():
    FactInvalidation(ref(), 1, 10, 20)
    with pytest.raises(ValueError):
        FactInvalidation(ref(RecordKind.SOURCE), 1, 10, 20)
    assert SpaceSettings(track=('work',)).track == ('work',)


def test_extraction_product_checks_sources_schema_stats_and_format():
    saved = product()
    assert saved.reusable(SNAPSHOT, SOURCES, 'config-1')
    for kwargs in (dict(format_version=True), dict(format_version=3), dict(format_version=4), dict(context_revision=6),
                   dict(result_json='{}'), dict(input_json='{}'), dict(stats={})):
        with pytest.raises(ValueError):
            replace(saved, **kwargs)
    with pytest.raises(TypeError):
        OperationStats(**{'facts_added': 0})
    with pytest.raises(ValueError):
        OperationStats(0, 0, 0, True, 0)


def test_generation_reuse_depends_on_actual_context_and_not_embedder():
    saved = product()
    for snapshot, sources, config in (
        (replace(SNAPSHOT, core_revision=8), SOURCES, 'config-1'),
        (replace(SNAPSHOT, session_id='other'), SOURCES, 'config-1'),
        (replace(SNAPSHOT, space_generation=3), SOURCES, 'config-1'),
        (replace(SNAPSHOT, context=SpaceContext('owner', 'space-2')), SOURCES, 'config-1'),
        (SNAPSHOT, (SourceVersion('source-1', 2),), 'config-1'),
        (SNAPSHOT, SOURCES, 'config-2'),
    ):
        assert not saved.reusable(snapshot, sources, config)
    assert replace(saved, context_revision=None).reusable(replace(SNAPSHOT, core_revision=8), SOURCES, 'config-1')
    # Embedding model/version are bound separately, never part of the generation reuse key.
    assert EmbeddingWrite(ref(), 1, 'embedder-A', (1.0,)).model != EmbeddingWrite(ref(), 1, 'embedder-B', (1.0,)).model


@pytest.mark.parametrize('field', ['operation', 'source_kind', 'extract', 'pending', 'parts', 'candidates'])
def test_empty_model_result_cannot_hide_missing_prepared_input_fields(field):
    saved = product()
    data = json.loads(saved.input_json)
    del data[field]
    with pytest.raises(ValueError):
        replace(saved, input_json=json.dumps(data), result_json='{"facts":[],"digests":{}}')


@pytest.mark.parametrize('field', ['role', 'content', 'ranges', 'representation', 'source_length'])
def test_empty_model_result_cannot_hide_corrupt_source_views(field):
    saved = product()
    data = json.loads(saved.input_json)
    del data['pending'][0][field]
    with pytest.raises(ValueError):
        replace(saved, input_json=json.dumps(data), result_json='{"facts":[],"digests":{}}')


def test_digest_context_requires_business_version_and_representation_fingerprint():
    saved = product()
    data = json.loads(saved.input_json)
    data['pending'][0]['sequence'] = 2
    context = {**data['pending'][0], 'id': 'context-source', 'sequence': 1,
               'representation': 'digest', 'representation_source': 'b' * 64}
    data['context'] = [context]
    conditions = replace(CONDITIONS, sources=(*SOURCES, SourceVersion('context-source', 1)))
    guarded = replace(saved, conditions=conditions, input_json=json.dumps(data))
    assert not guarded.reusable(replace(SNAPSHOT, core_revision=8), conditions.sources, 'config-1')
    with pytest.raises(ValueError):
        replace(guarded, context_revision=None)
    data['context'][0]['representation_source'] = None
    with pytest.raises(ValueError):
        replace(guarded, input_json=json.dumps(data))


def test_completion_serialization_and_independent_auxiliary_operations():
    result = OperationResult('operation-1', OperationKind.EXTRACT, ProcessingState.COMPLETED,
                             ProcessingPhase.DONE, True, True, True, auxiliary_operation_ids=('digest-1',))
    wire = json.loads(json.dumps(result.to_dict(), allow_nan=False))
    assert wire['state'] == 'completed' and wire['auxiliary_operation_ids'] == ['digest-1']
    child = OperationResult('digest-1', OperationKind.DIGEST, ProcessingState.FAILED,
                           ProcessingPhase.GENERATE, True, False, False, error_code='model_unavailable')
    assert child.to_dict()['state'] == 'failed' and result.core_committed


@pytest.mark.parametrize('kwargs', [dict(state='completed'), dict(core_committed=False),
    dict(extraction_saved=False), dict(stored_raw=False), dict(phase=ProcessingPhase.EMBED),
    dict(attempts=True), dict(state=ProcessingState.RETRY_WAIT), dict(state=ProcessingState.FAILED)])
def test_impossible_or_malformed_operation_results_are_rejected(kwargs):
    result = OperationResult('operation-1', OperationKind.EXTRACT, ProcessingState.COMPLETED,
                             ProcessingPhase.DONE, True, True, True)
    with pytest.raises(ValueError):
        replace(result, **kwargs)
