"""Build an explicit commit from a validated product and a scoped fact workset."""
from dataclasses import replace

from .adjudication import plan_merge
from ..store._validation import validate_embedding
from ..store.contracts import EmbeddingWrite, RecordWrite, StoredRecord
from ..store.postgres.records import domain


def build_record_write(context, value, previous=None, *, digest_base=1):
    """构造带版本前置条件的记录写入项。

    Args:
        context: 目标记忆空间。
        value: 要写入的领域记录。
        previous: 对应的旧记录。新增时为 None。
        digest_base: 首次生成摘要时使用的版本号。

    Returns:
        供 PostgreSQL 提交的记录写入项。
    """
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
    """批量编码去重后的文本并校验向量。

    Args:
        embedder: 可选的向量编码器。
        texts: 要编码的文本。

    Returns:
        文本到向量的映射。未配置编码器时返回空字典。

    Raises:
        ValueError: 编码器返回的向量数量或格式无效。
    """
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
    """合并新旧事实并生成记录与向量变更。

    Args:
        context: 目标记忆空间。
        config: 事实处理配置。
        embedder: 可选的向量编码器。
        model: 向量模型标识。
        now: 事实合并时间戳。
        facts: 待合并的新事实。
        previous: 相关旧事实记录。
        vectors: 按旧事实 ID 索引的向量。
        llm: 用于事实判定的模型。
        ontology: 本体解析结果。

    Returns:
        记录写入项和向量写入项。
    """
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
    writes = [build_record_write(context, value, previous) for value, previous in ontology.records(accepted)]
    embeddings = []
    for value in changed:
        previous = old_records.get(value.id)
        vector = value.embedding
        value.embedding = None
        if previous is not None and value == domain(previous):
            continue
        write = build_record_write(context, value, previous)
        writes.append(write)
        if previous is None and vector is not None:
            embeddings.append(EmbeddingWrite(write.ref, write.record.content_version, model, tuple(vector)))
    return writes, embeddings
