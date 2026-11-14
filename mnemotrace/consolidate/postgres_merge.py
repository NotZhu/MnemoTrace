"""Build an explicit commit from a validated product and a scoped fact workset."""
from dataclasses import replace

from .adjudication import plan_merge
from ..store._validation import validate_embedding
from ..store.contracts import EmbeddingWrite, RecordRef, RecordWrite, SourceLink, StoredRecord
from ..store.postgres.records import SOURCE, domain


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


def merge_records(context, config, embedder, model, now,
                  facts, previous, vectors, llm, *, ontology):
    old_records = {record.ref.record_id: record for record in previous}
    old_facts = [domain(record) for record in previous]
    fresh = embed_texts(embedder, (fact.text for fact in facts))
    for fact in old_facts:
        fact.embedding = vectors.get(fact.id)
    for fact in facts:
        fact.embedding = fresh.get(fact.text)
    duplicate_ids = {}
    changed, _ = plan_merge(facts, old_facts, llm, config, embedder, now,
                            semantic=True, symmetric=ontology.symmetric, duplicate_ids=duplicate_ids)
    accepted = duplicate_ids.keys() | {value.id for value in changed if value.id not in old_records}
    writes = [write_record(context, value, previous) for value, previous in ontology.records(accepted)]
    embeddings, links = [], []
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
        links.extend(SourceLink(write.ref, RecordRef(context, SOURCE, source_id))
                     for source_id in value.provenance)
    return writes, embeddings, links
