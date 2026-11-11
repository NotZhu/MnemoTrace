"""Operation contracts for authorized Postgres memory spaces.

The DAO applies SQL authorization and cross-record existence checks. See the
storage design for transaction boundaries and operation acceptance and completion.
"""
from __future__ import annotations

from contextlib import AbstractContextManager
from dataclasses import asdict, dataclass
from enum import Enum
import hashlib
import json
import math
import re
from typing import Protocol
from types import SimpleNamespace

from ..categories import Category, normalize_categories
from ..types import Conflict, Entity, Episode, Fact, Relation
from ..extraction import ExtractionResponseError
from ._validation import validate_record


def _id(value: str) -> None:
    if not isinstance(value, str) or not value.strip() or len(value) > 512:
        raise ValueError('Expected a non-empty identifier of at most 512 characters')
    value.encode('utf-8')


def _count(value: int, minimum: int = 0) -> None:
    if type(value) is not int or value < minimum:
        raise ValueError(f'Expected an integer >= {minimum}')


def _time(value: float | None) -> None:
    if value is not None and (type(value) not in (int, float) or not math.isfinite(value)):
        raise ValueError('Expected a finite timestamp or None')


def _enum(value: Enum, kind: type[Enum]) -> None:
    if not isinstance(value, kind):
        raise ValueError(f'Expected {kind.__name__}')


def _agent(value: str | None) -> None:
    if value is not None and (not isinstance(value, str) or
            re.fullmatch(r'[A-Za-z0-9_.:@/\-]{1,128}', value) is None):
        raise ValueError('Invalid agent ID')


def _tuple(value: tuple, kind: type) -> None:
    if not isinstance(value, tuple) or any(not isinstance(item, kind) for item in value):
        raise ValueError(f'Expected a tuple of {kind.__name__}')


def _json(value: object) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'),
                         allow_nan=False)
    encoded.encode('utf-8')
    return encoded


class ProcessingState(str, Enum):
    PENDING = 'pending'
    RUNNING = 'running'
    RETRY_WAIT = 'retry_wait'
    FAILED = 'failed'
    COMPLETED = 'completed'
    CANCELLED = 'cancelled'


class ProcessingPhase(str, Enum):
    QUEUED = 'queued'
    GENERATE = 'generate'
    EMBED = 'embed'
    COMMIT = 'commit'
    COMPENSATE = 'compensate'
    DONE = 'done'


class OperationKind(str, Enum):
    EXTRACT = 'extract'
    DIGEST = 'digest'
    CONFLICTS = 'conflicts'
    GRAPH = 'graph'
    PROFILE = 'profile'


class RecordKind(str, Enum):
    SOURCE = 'Episode'
    FACT = 'Fact'
    ENTITY = 'Entity'
    RELATION = 'Relation'
    CONFLICT = 'Conflict'


class FactState(str, Enum):
    CURRENT = 'current'
    HISTORY = 'history'
    ALL = 'all'


@dataclass(frozen=True)
class SpaceContext:
    """Established by trusted authentication/ownership checks, never by parsing alone."""

    user_id: str
    space_id: str

    def __post_init__(self):
        _id(self.user_id)
        _id(self.space_id)


@dataclass(frozen=True)
class ReadSnapshot:
    context: SpaceContext
    core_revision: int
    space_generation: int
    session_id: str | None = None

    def __post_init__(self):
        if not isinstance(self.context, SpaceContext):
            raise ValueError('A space context is required')
        _count(self.core_revision)
        _count(self.space_generation)
        if self.session_id is not None:
            _id(self.session_id)


@dataclass(frozen=True)
class SourceVersion:
    source_id: str
    content_version: int

    def __post_init__(self):
        _id(self.source_id)
        _count(self.content_version, 1)


@dataclass(frozen=True)
class Lease:
    operation_id: str
    token: str

    def __post_init__(self):
        _id(self.operation_id)
        _id(self.token)


@dataclass(frozen=True)
class CommitConditions:
    snapshot: ReadSnapshot
    sources: tuple[SourceVersion, ...] = ()
    lease: Lease | None = None
    extraction_config: str | None = None
    targets: tuple[RecordVersion, ...] = ()

    def __post_init__(self):
        if not isinstance(self.snapshot, ReadSnapshot):
            raise ValueError('A read snapshot is required')
        _tuple(self.sources, SourceVersion)
        _tuple(self.targets, RecordVersion)
        if any(target.record.context != self.snapshot.context for target in self.targets):
            raise ValueError('Maintenance conditions cannot cross the read scope')
        if len({s.source_id for s in self.sources}) != len(self.sources):
            raise ValueError('Duplicate source preconditions')
        if self.lease is not None:
            if (not isinstance(self.lease, Lease) or not (self.sources or self.targets)
                    or self.sources and self.extraction_config is None):
                raise ValueError('Task commits require versioned inputs and model configuration when applicable')
        if self.extraction_config is not None:
            _id(self.extraction_config)


@dataclass(frozen=True)
class FactQuery:
    """Times are independent. CURRENT filters both axes; HISTORY includes withdrawn facts."""

    state: FactState = FactState.CURRENT
    valid_at: float | None = None
    known_at: float | None = None
    categories: tuple[Category, ...] = ()
    subject: str | None = None
    predicate: str | None = None
    source_ids: tuple[str, ...] = ()
    limit: int = 100
    after_id: str | None = None
    exclude_sensitive: bool = False

    @property
    def is_current_view(self) -> bool:
        return self.state == FactState.CURRENT and self.valid_at is None and self.known_at is None

    def __post_init__(self):
        _enum(self.state, FactState)
        _time(self.valid_at)
        _time(self.known_at)
        _count(self.limit, 1)
        if self.limit > 1000:
            raise ValueError('Page/candidate limit exceeds 1000')
        _tuple(self.source_ids, str)
        _tuple(self.categories, str)
        normalize_categories(self.categories)
        if type(self.exclude_sensitive) is not bool:
            raise ValueError('Expected a boolean sensitive-fact filter')
        for value in (*self.source_ids, self.subject, self.predicate, self.after_id):
            if value is not None:
                _id(value)


@dataclass(frozen=True)
class RetrievalQuery:
    facts: FactQuery
    text: str
    vector: tuple[float, ...] | None = None
    embedding_model: str | None = None
    exact: bool = True

    def __post_init__(self):
        if not isinstance(self.facts, FactQuery) or not isinstance(self.text, str):
            raise ValueError('Expected fact conditions and query text')
        if type(self.exact) is not bool or (self.vector is None) != (self.embedding_model is None):
            raise ValueError('Expected an exact flag and a vector paired with its model')
        if self.vector is None:
            return
        _id(self.embedding_model)
        if not isinstance(self.vector, tuple) or not self.vector:
            raise ValueError('Expected a non-empty vector tuple')
        for number in self.vector:
            if number is None:
                raise ValueError('Null vector coordinate')
            _time(number)


@dataclass(frozen=True)
class MessageInput:
    message_id: str
    role: str
    content: str
    event_time: float | None = None
    agent_id: str | None = None
    extract: bool = True

    def __post_init__(self):
        _id(self.message_id)
        if self.role not in {'user', 'assistant'}:
            raise ValueError('Invalid message role')
        if type(self.extract) is not bool:
            raise ValueError('extract must be a boolean')
        if (not isinstance(self.content, str) or not self.content.strip()
                or '\x00' in self.content or len(self.content) > 1_000_000):
            raise ValueError('Expected non-empty message content, at most 1000000 codepoints')
        self.content.encode('utf-8')
        _time(self.event_time)
        _agent(self.agent_id)

    @property
    def fingerprint(self) -> str:
        # 1 and 1.0 are the same event time; content/IDs are never trimmed or case-folded.
        stamp = float(self.event_time) if self.event_time is not None else None
        value = [self.role, self.content, stamp, self.agent_id, self.extract]
        return hashlib.sha256(_json(value).encode('utf-8')).hexdigest()


@dataclass(frozen=True)
class RecordRef:
    context: SpaceContext
    kind: RecordKind
    record_id: str

    def __post_init__(self):
        if not isinstance(self.context, SpaceContext):
            raise ValueError('A space context is required')
        _enum(self.kind, RecordKind)
        _id(self.record_id)


@dataclass(frozen=True)
class StoredRecord:
    """Detached domain fields, not arbitrary SQL columns or a snapshot of a space.

    Existing domain record validators remain authoritative for their fields.
    The DAO splits domain embeddings into versioned embedding rows on submission.
    """

    ref: RecordRef
    content_version: int
    payload_json: str
    agent_id: str | None = None
    record_version: int = 1
    digest_version: int | None = None

    def __post_init__(self):
        if not isinstance(self.ref, RecordRef):
            raise ValueError('A scoped record reference is required')
        _count(self.content_version, 1)
        _count(self.record_version, 1)
        if self.agent_id is not None:
            if self.ref.kind != RecordKind.SOURCE:
                raise ValueError('Only sources carry agent provenance')
            _agent(self.agent_id)
        value = json.loads(self.payload_json)
        checked = validate_record(self.ref.kind.value, value, 'record', detached=True)
        if set(value) != set(checked):
            raise ValueError('Unknown domain record fields')
        if checked['id'] != self.ref.record_id or checked.get('user_id', self.ref.context.user_id) != self.ref.context.user_id:
            raise ValueError('Record identity/owner differs from its scoped reference')
        if self.digest_version is not None:
            _count(self.digest_version, 1)
            if self.ref.kind != RecordKind.SOURCE or not checked['digest']:
                raise ValueError('Digest versions only belong to non-empty source digests')
        elif self.ref.kind == RecordKind.SOURCE and checked['digest']:
            raise ValueError('A source digest requires its independent text version')
        object.__setattr__(self, 'payload_json', _json(checked))

    @classmethod
    def from_record(cls, context: SpaceContext,
                    record: Episode | Fact | Entity | Relation | Conflict,
                    *, content_version: int = 1, record_version: int = 1,
                    digest_version: int | None = None) -> StoredRecord:
        kind = RecordKind(type(record).__name__)
        return cls(RecordRef(context, kind, record.id), content_version, _json(asdict(record)),
                   record_version=record_version, digest_version=digest_version)


@dataclass(frozen=True)
class RecordWrite:
    record: StoredRecord
    expected_version: int | None = None

    def __post_init__(self):
        if not isinstance(self.record, StoredRecord):
            raise ValueError('A domain record is required')
        if self.expected_version is not None:
            _count(self.expected_version, 1)
        if self.record.record_version != (1 if self.expected_version is None else self.expected_version + 1):
            raise ValueError('Record version must advance by one; inserts start at one')

    @property
    def ref(self) -> RecordRef:
        return self.record.ref


@dataclass(frozen=True)
class Candidate:
    record: StoredRecord
    semantic: float
    lexical: float
    graph: float
    recency: float
    salience: float

    def __post_init__(self):
        if not isinstance(self.record, StoredRecord):
            raise ValueError('A candidate requires a scoped record')
        for score in (self.semantic, self.lexical, self.graph, self.recency, self.salience):
            _time(score)
            if score is None:
                raise ValueError('Missing candidate score')


@dataclass(frozen=True)
class CandidateBatch:
    snapshot: ReadSnapshot
    candidates: tuple[Candidate, ...]
    complete: bool
    limitations: tuple[str, ...] = ()

    def __post_init__(self):
        if not isinstance(self.snapshot, ReadSnapshot):
            raise ValueError('A candidate batch requires its read snapshot')
        _tuple(self.candidates, Candidate)
        _tuple(self.limitations, str)
        for limitation in self.limitations:
            _id(limitation)
        if type(self.complete) is not bool or self.complete == bool(self.limitations):
            raise ValueError('Incomplete coverage must declare limitations')
        if any(c.record.ref.context != self.snapshot.context for c in self.candidates):
            raise ValueError('Candidates cannot cross the read scope')


@dataclass(frozen=True)
class FactInvalidation:
    ref: RecordRef
    expected_version: int
    invalid_at: float | None
    expired_at: float

    def __post_init__(self):
        if not isinstance(self.ref, RecordRef) or self.ref.kind != RecordKind.FACT:
            raise ValueError('Only a fact can be invalidated')
        _count(self.expected_version, 1)
        _time(self.invalid_at)
        _time(self.expired_at)
        if self.expired_at is None:
            raise ValueError('Transaction withdrawal time is required')


@dataclass(frozen=True)
class SourceLink:
    fact: RecordRef
    source: RecordRef

    def __post_init__(self):
        if (not isinstance(self.fact, RecordRef) or not isinstance(self.source, RecordRef)
                or self.fact.kind != RecordKind.FACT or self.source.kind != RecordKind.SOURCE
                or self.fact.context != self.source.context):
            raise ValueError('Fact provenance must reference a source in the same space')


@dataclass(frozen=True)
class EmbeddingWrite:
    record: RecordRef
    content_version: int
    model: str
    vector: tuple[float, ...]
    field: str = 'text'

    def __post_init__(self):
        if not isinstance(self.record, RecordRef):
            raise ValueError('A scoped embedding target is required')
        _count(self.content_version, 1)
        allowed = {RecordKind.SOURCE: {'digest'}, RecordKind.FACT: {'text'},
                   RecordKind.ENTITY: {'text'}}
        if self.field not in allowed.get(self.record.kind, set()):
            raise ValueError('Invalid embedding field for this record kind')
        RetrievalQuery(FactQuery(), '', self.vector, self.model)


@dataclass(frozen=True)
class SpaceSettings:
    track: tuple[str, ...] = ()
    mute: tuple[str, ...] = ()
    extract_instruction: str = ''
    extract_system: str = ''
    digest_system: str = ''
    profile_system: str = ''

    def __post_init__(self):
        _tuple(self.track, str)
        _tuple(self.mute, str)
        from ..focus import selectors
        selectors((*self.track, *self.mute))
        if any(not isinstance(value, str) for value in (
                self.extract_instruction, self.extract_system, self.digest_system, self.profile_system)):
            raise ValueError('Policy fields must be strings')


@dataclass(frozen=True)
class RecordVersion:
    record: RecordRef
    content_version: int

    def __post_init__(self):
        if not isinstance(self.record, RecordRef):
            raise ValueError('A scoped target is required')
        _count(self.content_version, 1)


@dataclass(frozen=True)
class OperationSpec:
    operation_id: str
    kind: OperationKind
    session_id: str | None = None
    sources: tuple[SourceVersion, ...] = ()
    targets: tuple[RecordVersion, ...] = ()

    def __post_init__(self):
        _id(self.operation_id)
        _enum(self.kind, OperationKind)
        if self.session_id is not None:
            _id(self.session_id)
        _tuple(self.sources, SourceVersion)
        _tuple(self.targets, RecordVersion)
        if len({s.source_id for s in self.sources}) != len(self.sources):
            raise ValueError('Duplicate operation sources')
        if len({t.record for t in self.targets}) != len(self.targets):
            raise ValueError('Duplicate maintenance targets')
        if self.kind is OperationKind.PROFILE:
            if self.session_id is not None or self.sources or self.targets:
                raise ValueError('Profile tasks are space-scoped fact snapshots')
        elif self.kind == OperationKind.GRAPH:
            if not self.targets or self.sources:
                raise ValueError('Maintenance requires record targets, not fabricated sources')
        elif not self.sources or self.session_id is None or self.targets:
            raise ValueError('Model operations require a session and unique source versions')
        if self.kind is OperationKind.DIGEST and len(self.sources) != 1:
            raise ValueError('Digest operations require exactly one source')


@dataclass(frozen=True)
class CommitReceipt:
    operation_id: str
    core_revision: int
    fact_ids: tuple[str, ...] = ()
    source_ids: tuple[str, ...] = ()
    affected_count: int = 0

    def __post_init__(self):
        _id(self.operation_id)
        _count(self.core_revision)
        _count(self.affected_count)
        for values in (self.fact_ids, self.source_ids):
            _tuple(values, str)
            for value in values:
                _id(value)


@dataclass(frozen=True)
class OperationStats:
    """All fields are required when restoring JSONB; defaults must not repair corruption."""

    facts_added: int
    duplicates: int
    invalidated: int
    model_calls: int
    windows_completed: int

    def __post_init__(self):
        for value in asdict(self).values():
            _count(value)


@dataclass(frozen=True)
class ExtractionProduct:
    conditions: CommitConditions
    input_json: str
    result_json: str
    stats: OperationStats
    digest_max_chars: int
    context_revision: int | None
    format_version: int = 6

    def __post_init__(self):
        if not isinstance(self.conditions, CommitConditions) or self.conditions.lease is None:
            raise ValueError('Saving a model product requires task commit conditions')
        if not isinstance(self.stats, OperationStats):
            raise ValueError('Validated operation statistics are required')
        _count(self.digest_max_chars, 1)
        if type(self.format_version) is not int or self.format_version != 6:
            raise ValueError('Unsupported model product format')
        if self.context_revision is not None:
            _count(self.context_revision)
            if self.context_revision != self.conditions.snapshot.core_revision:
                raise ValueError('Model context must describe the read snapshot')
        data, result = json.loads(self.input_json), json.loads(self.result_json)
        try:
            _validate_product_input(data)
            if self.context_revision is None and (data['operation'] == 'conflicts'
                    or any(view['representation'] == 'digest' for view in (*data['context'], *data['pending']))):
                raise ValueError('Digest/fact inputs require a business context revision')
            ids = [s['id'] for s in (*data['pending'], *data['context'])]
            if not data['pending'] or len(set(ids)) != len(ids) or set(ids) != {s.source_id for s in self.conditions.sources}:
                raise ValueError('Product source references differ from input versions')
            from ..consolidate.planning import validate_result
            validate_result(result, data, SimpleNamespace(digest_max_chars=self.digest_max_chars))
        except (KeyError, TypeError, ValueError, ExtractionResponseError) as exc:
            raise ValueError('Invalid model product or source references') from exc
        object.__setattr__(self, 'input_json', _json(data))
        object.__setattr__(self, 'result_json', _json(result))

    def reusable(self, snapshot: ReadSnapshot, sources: tuple[SourceVersion, ...],
                 extraction_config: str) -> bool:
        """Reusing generation never authorizes reusing a merge against old facts."""
        _tuple(sources, SourceVersion)
        before = self.conditions.snapshot
        return (snapshot.context == before.context
                and snapshot.space_generation == before.space_generation
                and snapshot.session_id == before.session_id
                and set(sources) == set(self.conditions.sources)
                and extraction_config == self.conditions.extraction_config
                and (self.context_revision is None or snapshot.core_revision == self.context_revision))


def _validate_product_input(data: dict) -> None:
    """Validate prepared input even when an empty model result touches no fields."""
    from ..consolidate.planning import validate_view
    required = {'operation', 'source_kind', 'extract', 'pending', 'context', 'parts', 'candidates'}
    if not isinstance(data, dict) or set(data) != required:
        raise ValueError('Missing prepared input fields')
    if data['operation'] not in {'extract', 'digest', 'conflicts'}:
        raise ValueError('Invalid model operation')
    if data['source_kind'] not in {'dialogue', 'note'} or type(data['extract']) is not bool:
        raise ValueError('Invalid source routing')
    dialogue = data['source_kind'] == 'dialogue'
    if any(not isinstance(data[key], list) for key in ('pending', 'context', 'parts', 'candidates')) or not data['pending']:
        raise ValueError('Invalid input lists')
    views = [*data['context'], *data['pending']]
    for view in views:
        validate_view(view)
        _id(view['id'])
        if (dialogue and (view['role'] not in {'user', 'assistant'} or view['message_id'] is None)
                or not dialogue and (view['role'] is not None or view['message_id'] is not None)):
            raise ValueError('Source attribution does not match its kind')
        for key in ('source', 'representation_source'):
            if view[key] is not None and re.fullmatch('[0-9a-f]{64}', view[key]) is None:
                raise ValueError('Invalid source fingerprint')
        composing = data['operation'] == 'digest' and data['parts'] and view in data['pending']
        if ((view['representation'] == 'raw' and view['representation_source'] is not None)
                or view['representation'] == 'digest' and view['representation_source'] is None and not composing):
            raise ValueError('Representation and its source fingerprint disagree')
    if any(a['sequence'] >= b['sequence'] for a, b in zip(views, views[1:])):
        raise ValueError('Input views must follow source sequence')
    if data['operation'] == 'digest':
        if len(data['pending']) != 1 or data['context'] or data['candidates']:
            raise ValueError('Invalid digest input')
        if data['parts'] and len(data['parts']) < 2:
            raise ValueError('Digest composition requires at least two parts')
    elif data['parts']:
        raise ValueError('Only digests compose parts')
    for part in data['parts']:
        _id(part)
    if len(set(data['parts'])) != len(data['parts']):
        raise ValueError('Duplicate digest parts')
    if data['operation'] == 'conflicts':
        if data['context'] or not data['candidates']:
            raise ValueError('Invalid conflict candidates')
        for pair in data['candidates']:
            if not isinstance(pair, dict) or set(pair) != {'older', 'newer'}:
                raise ValueError('Invalid conflict pair')
            for candidate in pair.values():
                fields = {'id', 'subject', 'predicate', 'object', 'text', 'valid_at', 'source', 'source_roles'}
                if not isinstance(candidate, dict) or set(candidate) != fields:
                    raise ValueError('Invalid conflict fact view')
                for key in ('id', 'subject', 'predicate', 'object'):
                    _id(candidate[key])
                if not isinstance(candidate['text'], str) or candidate['source'] not in {'user', 'extracted'}:
                    raise ValueError('Invalid conflict fact content')
                _time(candidate['valid_at'])
                if not isinstance(candidate['source_roles'], list) or any(r not in {'user', 'assistant'} for r in candidate['source_roles']):
                    raise ValueError('Invalid conflict source roles')
            if (pair['older']['id'] == pair['newer']['id']
                    or pair['older']['subject'].lower() != pair['newer']['subject'].lower()):
                raise ValueError('Inconsistent conflict pair')
    elif data['candidates']:
        raise ValueError('Unexpected candidates')


def validate_erasures(records: tuple[RecordRef, ...]) -> None:
    _tuple(records, RecordRef)
    if any(record.kind != RecordKind.FACT for record in records):
        raise ValueError('Record erasure only supports facts')


@dataclass(frozen=True)
class ChangeSet:
    conditions: CommitConditions
    operation_id: str
    writes: tuple[RecordWrite, ...] = ()
    invalidations: tuple[FactInvalidation, ...] = ()
    erasures: tuple[RecordRef, ...] = ()
    source_links: tuple[SourceLink, ...] = ()
    embeddings: tuple[EmbeddingWrite, ...] = ()
    operations: tuple[OperationSpec, ...] = ()
    settings: SpaceSettings | None = None
    receipt: CommitReceipt | None = None

    def __post_init__(self):
        if not isinstance(self.conditions, CommitConditions):
            raise ValueError('Commit conditions are required')
        _id(self.operation_id)
        context = self.conditions.snapshot.context
        refs = []
        for values, kind in ((self.writes, RecordWrite), (self.invalidations, FactInvalidation),
                             (self.source_links, SourceLink),
                             (self.embeddings, EmbeddingWrite),
                             (self.operations, OperationSpec)):
            _tuple(values, kind)
        validate_erasures(self.erasures)
        mutations = [w.ref for w in self.writes] + [i.ref for i in self.invalidations] + list(self.erasures)
        if len(set(mutations)) != len(mutations):
            raise ValueError('Multiple mutations of one record in a commit')
        refs.extend(mutations)
        refs.extend(e.record for e in self.embeddings)
        refs.extend(target.record for target in self.conditions.targets)
        for link in self.source_links:
            refs.extend((link.fact, link.source))
        for operation in self.operations:
            refs.extend(target.record for target in operation.targets)
        if len({operation.operation_id for operation in self.operations}) != len(self.operations):
            raise ValueError('Duplicate follow-up operations')
        if any(ref.context != context for ref in refs):
            raise ValueError('A change set cannot cross authorized spaces')
        derived_refs = [e.record for e in self.embeddings]
        derived_refs.extend(link.source for link in self.source_links)
        derived_refs.extend(link.fact for link in self.source_links)
        if any(ref in self.erasures for ref in derived_refs):
            raise ValueError('Cannot add derived data for records erased by this commit')
        keys = [(e.record, e.field, e.model, e.content_version) for e in self.embeddings]
        if len(keys) != len(set(keys)):
            raise ValueError('Duplicate embedding writes')
        for write in self.writes:
            if write.ref.kind in (RecordKind.ENTITY, RecordKind.RELATION):
                raise ValueError('Graph records are derived from facts')
            fields = json.loads(write.record.payload_json)
            if write.ref.kind == RecordKind.FACT and set(fields['provenance']) != {
                    link.source.record_id for link in self.source_links if link.fact == write.ref}:
                raise ValueError('Fact provenance requires matching explicit source links')
            for key, field in (('embedding', 'text'), ('digest_embedding', 'digest')):
                version = write.record.digest_version if field == 'digest' else write.record.content_version
                if any(e.record == write.ref and e.field == field and e.content_version != version
                       for e in self.embeddings):
                    raise ValueError('Embedding text version differs from its record')
                if fields.get(key) is not None and not any(
                        e.record == write.ref and e.content_version == version
                        and e.field == field and list(e.vector) == fields[key] for e in self.embeddings):
                    raise ValueError('Inline domain vectors require matching versioned embedding writes')
        if self.settings is not None and not isinstance(self.settings, SpaceSettings):
            raise ValueError('Expected space settings')
        if self.conditions.lease is not None and self.conditions.lease.operation_id != self.operation_id:
            raise ValueError('Lease belongs to another operation')
        revision = self.conditions.snapshot.core_revision
        if self.receipt is not None and (not isinstance(self.receipt, CommitReceipt)
                or self.receipt.operation_id != self.operation_id
                or self.receipt.core_revision not in (revision, revision + 1)):
            raise ValueError('Receipt must describe this commit at its current or next business revision')


@dataclass(frozen=True)
class OperationResult:
    operation_id: str
    kind: OperationKind
    state: ProcessingState
    phase: ProcessingPhase
    stored_raw: bool
    extraction_saved: bool
    core_committed: bool
    attempts: int = 0
    next_attempt_at: float | None = None
    error_code: str | None = None
    auxiliary_operation_ids: tuple[str, ...] = ()

    def __post_init__(self):
        _id(self.operation_id)
        _enum(self.kind, OperationKind)
        _enum(self.state, ProcessingState)
        _enum(self.phase, ProcessingPhase)
        _count(self.attempts)
        _time(self.next_attempt_at)
        if any(type(value) is not bool for value in (
                self.stored_raw, self.extraction_saved, self.core_committed)):
            raise ValueError('Persistence progress must use booleans')
        if self.extraction_saved and not self.stored_raw or self.core_committed and not self.stored_raw:
            raise ValueError('A result cannot precede its durable input')
        if self.kind == OperationKind.EXTRACT and self.core_committed and not self.extraction_saved:
            raise ValueError('Extraction completion requires a saved model product')
        if self.state == ProcessingState.COMPLETED and (not self.core_committed or self.phase != ProcessingPhase.DONE):
            raise ValueError('Completion requires a committed result and done phase')
        if self.phase == ProcessingPhase.DONE and self.state != ProcessingState.COMPLETED:
            raise ValueError('Done phase requires completed state')
        if (self.kind == OperationKind.EXTRACT and self.phase in {ProcessingPhase.EMBED, ProcessingPhase.COMMIT}
                and not self.extraction_saved):
            raise ValueError('Embedding/commit requires a durable model product')
        if self.state == ProcessingState.RETRY_WAIT and self.next_attempt_at is None:
            raise ValueError('Retry waiting requires a next attempt time')
        if self.state in {ProcessingState.FAILED, ProcessingState.CANCELLED} and self.error_code is None:
            raise ValueError('Failure/cancellation requires an observable reason')
        if self.error_code is not None:
            _id(self.error_code)
        _tuple(self.auxiliary_operation_ids, str)
        for value in self.auxiliary_operation_ids:
            _id(value)

    def to_dict(self) -> dict:
        return json.loads(_json(asdict(self)))


class ReadView(Protocol):
    """Only valid inside one REPEATABLE READ transaction on a scoped connection."""

    snapshot: ReadSnapshot

    def sources(self, source_ids: tuple[str, ...], *, known_at: float | None = None) -> tuple[StoredRecord, ...]: ...
    def facts(self, query: FactQuery) -> tuple[StoredRecord, ...]: ...
    def history(self, subject: str, predicate: str | None = None) -> tuple[StoredRecord, ...]: ...
    def candidates(self, query: RetrievalQuery) -> CandidateBatch: ...


class SpaceDAO(Protocol):
    """Ordinary business access. Administration/discovery use separate restricted paths."""

    context: SpaceContext

    def read_snapshot(self, session_id: str | None = None) -> AbstractContextManager[ReadView]: ...
    def commit(self, changes: ChangeSet) -> CommitReceipt: ...
    def operation(self, operation_id: str) -> OperationResult: ...
    def claim(self, operation_id: str) -> Lease | None: ...
    def renew(self, lease: Lease) -> bool: ...
    def save_product(self, product: ExtractionProduct) -> None: ...
    def product(self, operation_id: str) -> ExtractionProduct | None: ...
