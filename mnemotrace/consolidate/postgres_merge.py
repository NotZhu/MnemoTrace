"""Build an explicit commit from a validated product and a scoped fact workset."""
from dataclasses import replace
from itertools import chain

from .planning import fact_valid_at, source_anchor
from .adjudication import plan_merge
from .fact_rules import _NAME_PREDICATES, _SELF_REFS, _norm_predicate
from .facts import FactWriter
from .pipeline import digest
from .topic_rules import resolve_topic
from ..store._validation import validate_embedding
from ..store.contracts import EmbeddingWrite, RecordRef, RecordWrite, SourceLink, StoredRecord
from ..store.postgres.records import FACT, SOURCE, domain
from ..types import Fact, WorkingMemory


def write_record(context, value, previous=None, *, summary_base=1):
    if previous is None:
        return RecordWrite(StoredRecord.from_record(context, value), None)
    old = domain(previous)
    version = previous.summary_version
    if hasattr(value, 'summary') and value.summary != old.summary:
        version = (version + 1 if version is not None else summary_base) if value.summary else None
    record = StoredRecord.from_record(context, value, content_version=previous.content_version,
                                      record_version=previous.record_version + 1, summary_version=version)
    return RecordWrite(replace(record, agent_id=previous.agent_id), previous.record_version)


def materialize(context, data, result, settings, config, now):
    views = {s['id']: s for s in (*data['context'], *data['pending'])}
    pending = {s['id']: s for s in data['pending']}
    content = '\n'.join(s['content'] for s in data['pending'])
    dialogue = data['source_kind'] == 'dialogue'

    def anchor(item):
        return source_anchor(item['source_ids'], pending, data['source_kind'])

    owner = {*_SELF_REFS, *(value.lower() for value in settings.subject_aliases)}
    if settings.subject_name:
        owner.add(settings.subject_name.lower())
    owner_name = settings.subject_name
    aliases = list(settings.subject_aliases)
    policy = FactWriter(None, None, None, config)
    facts = []
    for item in sorted(result['facts'], key=lambda item: anchor(item)['sequence']):
        origin = anchor(item)
        subject = item['subject'].strip()
        predicate = _norm_predicate(item['predicate'])
        is_owner = subject.lower() in owner
        if is_owner and predicate in _NAME_PREDICATES:
            owner_name = owner_name or item['object']
            if item['object'].lower() not in owner:
                aliases.append(item['object'])
                owner.add(item['object'].lower())
            if not dialogue:
                continue
        if is_owner:
            subject = owner_name or 'user'
        if data['scope'] == 'working':
            continue
        topic, topic_source = resolve_topic(predicate, item['object'], item.get('topic') if config.llm_topics else None)
        fact = Fact(subject, predicate, item['object'], text=item['text'] if dialogue else '',
                    display=item['text'], user_id=context.user_id, valid_at=fact_valid_at(item, origin['event_time']),
                    category=item['category'], sensitive=item['sensitive'],
                    created_at=now, last_access=now, provenance=item['source_ids'],
                    source_roles=list(dict.fromkeys(views[key]['role'] for key in item['source_ids']
                                                    if views[key]['role'])),
                    topic=topic, topic_source=topic_source,
                    id='ft_' + digest([data, item])[:24])
        if policy._accepted(fact, content):
            facts.append(fact)

    working = []
    for item in result['working']:
        origin = anchor(item)
        ttl = config.working_memory_ttl_seconds
        working.append((item, origin, now + ttl if ttl else None))
    new_settings = replace(settings, subject_name=owner_name, subject_aliases=tuple(aliases))
    return facts, working, new_settings


def embed_texts(embedder, texts):
    texts = tuple(dict.fromkeys(texts))
    if embedder is None or not texts:
        return {}
    vectors = embedder.embed_batch(list(texts))
    if len(vectors) != len(texts):
        raise ValueError('Embedding batch length mismatch')
    for vector in vectors:
        validate_embedding(vector, 'operation embedding', embedder.dim)
    return dict(zip(texts, vectors, strict=True))


def merge_records(context, config, embedder, model, data, session_id, now,
                  facts, working, previous, vectors, llm, existing_pairs):
    old_records = {record.ref.record_id: record for record in previous}
    old_facts = [domain(record) for record in previous]
    work = [WorkingMemory(id='wm_' + digest([data, item])[:24], content=item['content'],
                          kind=item['kind'], user_id=context.user_id, session_id=session_id,
                          event_time=origin['event_time'], created_at=now, expires_at=expires,
                          metadata=dict(provenance=item['source_ids'],
                                        episode_id=origin['id'])) for item, origin, expires in working]
    semantic_subjects = {fact.subject.strip().lower() for fact in facts}
    missing = [fact for fact in old_facts if embedder is not None
               and fact.subject.strip().lower() in semantic_subjects
               and not fact.review_pending and fact.id not in vectors]
    fresh = embed_texts(embedder, chain((fact.text for fact in facts), (item.content for item in work),
                                       (fact.text for fact in missing)))
    for fact in old_facts:
        fact.embedding = vectors.get(fact.id)
    extra_vectors = []
    for fact in missing:
        fact.embedding = fresh.get(fact.text)
        record = old_records[fact.id]
        extra_vectors.append(EmbeddingWrite(record.ref, record.content_version, model, tuple(fact.embedding)))

    for fact in facts:
        fact.embedding = fresh.get(fact.text)
    changed, conflicts, _ = plan_merge(facts, old_facts, llm, config, embedder, now, existing_pairs)
    writes, embeddings, links = [], extra_vectors, []
    for value in chain(changed, work):
        previous = old_records.get(value.id)
        vector = value.embedding if isinstance(value, Fact) else fresh.get(value.content)
        value.embedding = None
        if previous is not None and value == domain(previous):
            continue
        write = write_record(context, value, previous)
        writes.append(write)
        if previous is None and vector is not None:
            embeddings.append(EmbeddingWrite(write.ref, write.record.content_version, model, tuple(vector)))
        if write.ref.kind == FACT:
            links.extend(SourceLink(write.ref, RecordRef(context, SOURCE, source_id))
                         for source_id in value.provenance)
    writes.extend(write_record(context, conflict) for conflict in conflicts)
    return writes, embeddings, links
