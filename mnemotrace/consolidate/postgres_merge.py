"""Build an explicit commit from a validated product and a scoped fact workset."""
from dataclasses import replace
from itertools import chain

from .adjudication import plan_merge
from .pipeline import content_hash
from ..store._validation import validate_embedding
from ..store.contracts import EmbeddingWrite, RecordRef, RecordWrite, SourceLink, StoredRecord
from ..store.postgres.records import FACT, SOURCE, domain
from ..types import EntityAlias


def write_record(context, value, previous=None, *, digest_base=1):
    if previous is None:
        return RecordWrite(StoredRecord.from_record(context, value), None)
    old = domain(previous)
    version = previous.digest_version
    if hasattr(value, 'digest') and value.digest != old.digest:
        version = (version + 1 if version is not None else digest_base) if value.digest else None
    record = StoredRecord.from_record(context, value, content_version=previous.content_version,
                                      record_version=previous.record_version + 1, digest_version=version)
    return RecordWrite(replace(record, agent_id=previous.agent_id), previous.record_version)


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


def merge_records(context, config, embedder, model, data, now,
                  facts, previous, vectors, llm, existing_pairs, *, verdicts=None, ontology=None):
    old_records = {record.ref.record_id: record for record in previous}
    old_facts = [domain(record) for record in previous]
    semantic_subjects = {fact.subject_key for fact in facts}
    missing = [fact for fact in old_facts if ontology is None and embedder is not None
               and fact.subject_key in semantic_subjects
               and not fact.review_pending and fact.id not in vectors]
    fresh = embed_texts(embedder, chain((fact.text for fact in facts),
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
    duplicate_ids = {}
    changed, conflicts, _ = plan_merge(facts, old_facts, llm, config, embedder, now, existing_pairs,
                                       verdicts=verdicts, semantic=ontology is not None,
                                       symmetric=ontology.symmetric if ontology else (), duplicate_ids=duplicate_ids)
    writes = [write_record(context, item) for item in ontology.records] if ontology else []
    embeddings, links = extra_vectors, []
    for value in changed:
        previous = old_records.get(value.id)
        vector = value.embedding
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
    versions = {record.ref.record_id: record.record_version for record in old_records.values()}
    versions.update((item.ref.record_id, item.record.record_version) for item in writes)
    for conflict in conflicts:
        conflict.older_version = versions[conflict.older]
        conflict.newer_version = versions[conflict.newer]
        writes.append(write_record(context, conflict))
    if ontology:
        aliases = {}
        for incoming_id, names in ontology.aliases.items():
            fact_id = duplicate_ids.get(incoming_id, incoming_id)
            for entity_id, source_id, name, sensitive in sorted(names):
                key = 'alias_' + content_hash([entity_id, fact_id, source_id, name])[:32]
                # The fact controls its own aliases. Later sensitive corroboration cannot expose a new alias.
                restricted = fact_id != incoming_id and sensitive
                aliases[key] = EntityAlias(key, entity_id, fact_id, source_id, name, restricted, context.user_id)
        writes.extend(write_record(context, value) for value in aliases.values())
    return writes, embeddings, links
