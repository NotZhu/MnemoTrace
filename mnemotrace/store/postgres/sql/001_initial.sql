CREATE SCHEMA mnemotrace AUTHORIZATION {migration};
GRANT USAGE ON SCHEMA mnemotrace TO {runtime}, {admin}, {auth};

CREATE DOMAIN mnemotrace.identifier AS text
    CHECK (length(VALUE) BETWEEN 1 AND 512 AND VALUE ~ '[^[:space:]]');
CREATE DOMAIN mnemotrace.positive_version AS bigint CHECK (VALUE >= 1);
CREATE DOMAIN mnemotrace.counter AS bigint CHECK (VALUE >= 0);

CREATE TABLE mnemotrace.schema_migrations (
    version integer PRIMARY KEY,
    checksum text NOT NULL,
    role_prefix text NOT NULL,
    structure_hash text NOT NULL,
    applied_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
GRANT SELECT ON mnemotrace.schema_migrations TO {runtime}, {admin};

CREATE TABLE mnemotrace.users (
    user_id mnemotrace.identifier PRIMARY KEY,
    status text NOT NULL CHECK (status IN ('active', 'deleting')),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE TABLE mnemotrace.api_keys (
    key_id mnemotrace.identifier PRIMARY KEY,
    user_id mnemotrace.identifier NOT NULL REFERENCES mnemotrace.users ON DELETE CASCADE,
    key_hash text NOT NULL UNIQUE CHECK (key_hash ~ '^[0-9a-f]{64}$'),
    name text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    revoked_at timestamptz
);
CREATE INDEX api_keys_user ON mnemotrace.api_keys(user_id, key_id);

CREATE TABLE mnemotrace.memory_spaces (
    space_id mnemotrace.identifier PRIMARY KEY,
    user_id mnemotrace.identifier NOT NULL REFERENCES mnemotrace.users ON DELETE CASCADE,
    name text NOT NULL CHECK (length(name) BETWEEN 1 AND 128 AND name ~ '[^[:space:]]'),
    status text NOT NULL CHECK (status IN ('active', 'deleting')),
    core_revision mnemotrace.counter NOT NULL DEFAULT 0,
    space_generation mnemotrace.counter NOT NULL DEFAULT 0,
    record_version mnemotrace.positive_version NOT NULL DEFAULT 1,
    settings jsonb NOT NULL CHECK (jsonb_typeof(settings) = 'object'),
    UNIQUE(user_id, space_id)
);
CREATE INDEX memory_spaces_user ON mnemotrace.memory_spaces(user_id, status, space_id);
CREATE TABLE mnemotrace.sessions (
    user_id mnemotrace.identifier NOT NULL,
    space_id mnemotrace.identifier NOT NULL,
    session_id mnemotrace.identifier NOT NULL,
    session_generation mnemotrace.counter NOT NULL DEFAULT 0,
    next_sequence mnemotrace.counter NOT NULL DEFAULT 0,
    PRIMARY KEY(user_id, space_id, session_id),
    FOREIGN KEY(user_id, space_id) REFERENCES mnemotrace.memory_spaces(user_id, space_id) ON DELETE CASCADE
);

CREATE TABLE mnemotrace.sources (
    user_id mnemotrace.identifier NOT NULL,
    space_id mnemotrace.identifier NOT NULL,
    id mnemotrace.identifier NOT NULL,
    content text NOT NULL,
    session_id mnemotrace.identifier NOT NULL,
    role text,
    event_time timestamptz,
    ingested_at timestamptz NOT NULL,
    source_kind text NOT NULL CHECK (source_kind IN ('raw','dialogue','document')),
    scope text NOT NULL CHECK (scope IN ('auto','long','raw','working')),
    message_id mnemotrace.identifier,
    sequence mnemotrace.counter,
    mode text CHECK (mode IN ('live','historical')),
    agent_id text CHECK (agent_id ~ '^[A-Za-z0-9_.:@/\-]{1,128}$'),
    message_fingerprint text CHECK (message_fingerprint ~ '^[0-9a-f]{64}$'),
    metadata jsonb NOT NULL CHECK (jsonb_typeof(metadata) = 'object'),
    record_version mnemotrace.positive_version NOT NULL,
    content_version mnemotrace.positive_version NOT NULL,
    PRIMARY KEY(user_id, space_id, id),
    UNIQUE(user_id, space_id, session_id, message_id),
    UNIQUE(user_id, space_id, session_id, sequence),
    CHECK (message_id IS NULL OR (message_fingerprint IS NOT NULL AND role IN ('user','assistant')
           AND role IS NOT NULL AND sequence IS NOT NULL AND mode IS NOT NULL
           AND length(content) BETWEEN 1 AND 1000000 AND content ~ '[^[:space:]]')),
    FOREIGN KEY(user_id, space_id, session_id) REFERENCES mnemotrace.sessions ON DELETE CASCADE
);
CREATE INDEX sources_session_time ON mnemotrace.sources(user_id, space_id, session_id, event_time, id);
CREATE TABLE mnemotrace.source_summaries (
    user_id mnemotrace.identifier NOT NULL,
    space_id mnemotrace.identifier NOT NULL,
    source_id mnemotrace.identifier NOT NULL,
    summary text NOT NULL,
    coverage jsonb,
    summary_coverage jsonb,
    content_version mnemotrace.positive_version NOT NULL,
    summary_version mnemotrace.positive_version NOT NULL,
    record_version mnemotrace.positive_version NOT NULL,
    processing_state text NOT NULL CHECK (processing_state IN ('pending','running','retry_wait','failed','completed','cancelled')),
    PRIMARY KEY(user_id, space_id, source_id),
    FOREIGN KEY(user_id, space_id, source_id) REFERENCES mnemotrace.sources ON DELETE CASCADE
);
CREATE TABLE mnemotrace.facts (
    user_id mnemotrace.identifier NOT NULL,
    space_id mnemotrace.identifier NOT NULL,
    id mnemotrace.identifier NOT NULL,
    subject text NOT NULL CHECK (subject ~ '[^[:space:]]'),
    predicate text NOT NULL CHECK (predicate ~ '[^[:space:]]'),
    object text NOT NULL CHECK (object ~ '[^[:space:]]'),
    text text NOT NULL,
    display text NOT NULL,
    salience double precision NOT NULL CHECK (salience > '-Infinity'::float8 AND salience < 'Infinity'::float8),
    confidence double precision NOT NULL CHECK (confidence > '-Infinity'::float8 AND confidence < 'Infinity'::float8),
    source text NOT NULL CHECK (source IN ('user','extracted')),
    category text NOT NULL,
    sensitive boolean NOT NULL,
    topic text NOT NULL CHECK (topic IN ('food','travel','music','film_tv','sports','reading','hobbies','environment','health','work','learning','technology','finance','relationships','unknown')),
    cardinality text NOT NULL CHECK (cardinality IN ('single','multi','unknown')),
    retention text NOT NULL CHECK (retention IN ('durable','normal','unknown')),
    topic_source text NOT NULL CHECK (topic_source IN ('unset','llm','user')),
    source_roles text[] NOT NULL CHECK (source_roles <@ ARRAY['user','assistant']::text[]),
    valid_at timestamptz,
    invalid_at timestamptz,
    created_at timestamptz NOT NULL,
    expired_at timestamptz,
    validity_revisions jsonb NOT NULL DEFAULT '[]' CHECK (jsonb_typeof(validity_revisions) = 'array'),
    supersedes mnemotrace.identifier,
    last_access timestamptz NOT NULL,
    access_count mnemotrace.counter NOT NULL,
    record_version mnemotrace.positive_version NOT NULL,
    content_version mnemotrace.positive_version NOT NULL,
    PRIMARY KEY(user_id, space_id, id),
    CHECK ((topic_source <> 'unset' OR topic = 'unknown') AND (topic_source <> 'llm' OR topic <> 'unknown')),
    CHECK (supersedes IS NULL OR supersedes <> id),
    FOREIGN KEY(user_id, space_id) REFERENCES mnemotrace.memory_spaces(user_id, space_id) ON DELETE CASCADE,
    FOREIGN KEY(user_id, space_id, supersedes) REFERENCES mnemotrace.facts DEFERRABLE INITIALLY DEFERRED
);
CREATE INDEX facts_slot ON mnemotrace.facts(user_id, space_id, lower(subject), lower(predicate), valid_at, id);
CREATE INDEX facts_time ON mnemotrace.facts(user_id, space_id, valid_at, invalid_at, expired_at);
CREATE TABLE mnemotrace.fact_sources (
    user_id mnemotrace.identifier NOT NULL,
    space_id mnemotrace.identifier NOT NULL,
    fact_id mnemotrace.identifier NOT NULL,
    source_id mnemotrace.identifier NOT NULL,
    PRIMARY KEY(user_id, space_id, fact_id, source_id),
    FOREIGN KEY(user_id, space_id, fact_id) REFERENCES mnemotrace.facts ON DELETE CASCADE,
    FOREIGN KEY(user_id, space_id, source_id) REFERENCES mnemotrace.sources ON DELETE CASCADE
);
CREATE INDEX fact_sources_source ON mnemotrace.fact_sources(user_id, space_id, source_id);
CREATE TABLE mnemotrace.working_memories (
    user_id mnemotrace.identifier NOT NULL,
    space_id mnemotrace.identifier NOT NULL,
    id mnemotrace.identifier NOT NULL,
    content text NOT NULL,
    session_id mnemotrace.identifier NOT NULL,
    kind mnemotrace.identifier NOT NULL,
    event_time timestamptz,
    created_at timestamptz NOT NULL,
    expires_at timestamptz,
    consumed boolean NOT NULL,
    metadata jsonb NOT NULL CHECK (jsonb_typeof(metadata) = 'object'),
    record_version mnemotrace.positive_version NOT NULL,
    content_version mnemotrace.positive_version NOT NULL,
    PRIMARY KEY(user_id, space_id, id),
    FOREIGN KEY(user_id, space_id, session_id) REFERENCES mnemotrace.sessions ON DELETE CASCADE
);
CREATE INDEX working_session ON mnemotrace.working_memories(user_id, space_id, session_id, consumed, expires_at);
CREATE TABLE mnemotrace.working_sources (
    user_id mnemotrace.identifier NOT NULL,
    space_id mnemotrace.identifier NOT NULL,
    working_id mnemotrace.identifier NOT NULL,
    source_id mnemotrace.identifier NOT NULL,
    PRIMARY KEY(user_id, space_id, working_id, source_id),
    FOREIGN KEY(user_id, space_id, working_id) REFERENCES mnemotrace.working_memories ON DELETE CASCADE,
    FOREIGN KEY(user_id, space_id, source_id) REFERENCES mnemotrace.sources ON DELETE CASCADE
);
CREATE TABLE mnemotrace.conflicts (
    user_id mnemotrace.identifier NOT NULL,
    space_id mnemotrace.identifier NOT NULL,
    id mnemotrace.identifier NOT NULL,
    older mnemotrace.identifier NOT NULL,
    newer mnemotrace.identifier NOT NULL,
    text_older text NOT NULL,
    text_newer text NOT NULL,
    reason text NOT NULL,
    status text NOT NULL CHECK (status IN ('pending','resolved','dismissed')),
    detected_at timestamptz NOT NULL,
    record_version mnemotrace.positive_version NOT NULL,
    PRIMARY KEY(user_id, space_id, id),
    CHECK (older <> newer),
    FOREIGN KEY(user_id, space_id, older) REFERENCES mnemotrace.facts ON DELETE CASCADE,
    FOREIGN KEY(user_id, space_id, newer) REFERENCES mnemotrace.facts ON DELETE CASCADE
);
CREATE UNIQUE INDEX conflicts_pair ON mnemotrace.conflicts(user_id, space_id, least(older,newer), greatest(older,newer));
CREATE INDEX conflicts_status ON mnemotrace.conflicts(user_id, space_id, status, id);
CREATE TABLE mnemotrace.import_receipts (
    user_id mnemotrace.identifier NOT NULL,
    space_id mnemotrace.identifier NOT NULL,
    source_kind text NOT NULL,
    digest text NOT NULL CHECK (digest ~ '^[0-9a-f]{64}$'),
    source_id mnemotrace.identifier,
    receipt jsonb NOT NULL CHECK (jsonb_typeof(receipt) = 'object'),
    PRIMARY KEY(user_id, space_id, source_kind, digest),
    FOREIGN KEY(user_id, space_id) REFERENCES mnemotrace.memory_spaces(user_id, space_id) ON DELETE CASCADE,
    FOREIGN KEY(user_id, space_id, source_id) REFERENCES mnemotrace.sources ON DELETE CASCADE
);

CREATE TABLE mnemotrace.entities (
    user_id mnemotrace.identifier NOT NULL,
    space_id mnemotrace.identifier NOT NULL,
    id mnemotrace.identifier NOT NULL,
    name mnemotrace.identifier NOT NULL,
    type mnemotrace.identifier NOT NULL,
    normalized_name mnemotrace.identifier NOT NULL,
    aliases text[] NOT NULL,
    record_version mnemotrace.positive_version NOT NULL,
    content_version mnemotrace.positive_version NOT NULL,
    PRIMARY KEY(user_id, space_id, id),
    UNIQUE(user_id, space_id, normalized_name),
    FOREIGN KEY(user_id, space_id) REFERENCES mnemotrace.memory_spaces(user_id, space_id) ON DELETE CASCADE
);
CREATE TABLE mnemotrace.entity_merges (
    user_id mnemotrace.identifier NOT NULL,
    space_id mnemotrace.identifier NOT NULL,
    merged_id mnemotrace.identifier NOT NULL,
    survivor_id mnemotrace.identifier NOT NULL,
    decision_source text NOT NULL,
    decided_at timestamptz NOT NULL,
    record_version mnemotrace.positive_version NOT NULL,
    PRIMARY KEY(user_id, space_id, merged_id),
    CHECK (merged_id <> survivor_id),
    FOREIGN KEY(user_id, space_id, merged_id) REFERENCES mnemotrace.entities ON DELETE CASCADE,
    FOREIGN KEY(user_id, space_id, survivor_id) REFERENCES mnemotrace.entities ON DELETE CASCADE
);
CREATE TABLE mnemotrace.graph_relations (
    user_id mnemotrace.identifier NOT NULL,
    space_id mnemotrace.identifier NOT NULL,
    id mnemotrace.identifier NOT NULL,
    subject_id mnemotrace.identifier NOT NULL,
    predicate mnemotrace.identifier NOT NULL,
    object_id mnemotrace.identifier NOT NULL,
    fact_id mnemotrace.identifier NOT NULL,
    valid_at timestamptz,
    invalid_at timestamptz,
    projection_version mnemotrace.positive_version NOT NULL,
    PRIMARY KEY(user_id, space_id, id),
    FOREIGN KEY(user_id, space_id, subject_id) REFERENCES mnemotrace.entities ON DELETE CASCADE,
    FOREIGN KEY(user_id, space_id, object_id) REFERENCES mnemotrace.entities ON DELETE CASCADE,
    FOREIGN KEY(user_id, space_id, fact_id) REFERENCES mnemotrace.facts ON DELETE CASCADE
);
CREATE INDEX graph_forward ON mnemotrace.graph_relations(user_id, space_id, subject_id, predicate);
CREATE INDEX graph_reverse ON mnemotrace.graph_relations(user_id, space_id, object_id, predicate);
CREATE INDEX graph_fact ON mnemotrace.graph_relations(user_id, space_id, fact_id);

CREATE TABLE mnemotrace.embeddings (
    user_id mnemotrace.identifier NOT NULL,
    space_id mnemotrace.identifier NOT NULL,
    source_id mnemotrace.identifier,
    fact_id mnemotrace.identifier,
    working_id mnemotrace.identifier,
    entity_id mnemotrace.identifier,
    target_kind text NOT NULL,
    field text NOT NULL CHECK (field IN ('text','summary')),
    content_version mnemotrace.positive_version NOT NULL,
    model mnemotrace.identifier NOT NULL,
    dimension integer NOT NULL CHECK (dimension > 0),
    vector public.vector NOT NULL,
    CHECK (public.vector_dims(vector) = dimension),
    CHECK (num_nonnulls(source_id, fact_id, working_id, entity_id) = 1),
    CHECK ((target_kind = 'Episode' AND source_id IS NOT NULL) OR (target_kind = 'Fact' AND fact_id IS NOT NULL)
        OR (target_kind = 'WorkingMemory' AND working_id IS NOT NULL) OR (target_kind = 'Entity' AND entity_id IS NOT NULL)),
    CHECK (field <> 'summary' OR source_id IS NOT NULL),
    UNIQUE NULLS NOT DISTINCT(user_id, space_id, source_id, fact_id, working_id, entity_id, field, model, dimension, content_version),
    FOREIGN KEY(user_id, space_id, source_id) REFERENCES mnemotrace.sources ON DELETE CASCADE,
    FOREIGN KEY(user_id, space_id, fact_id) REFERENCES mnemotrace.facts ON DELETE CASCADE,
    FOREIGN KEY(user_id, space_id, working_id) REFERENCES mnemotrace.working_memories ON DELETE CASCADE,
    FOREIGN KEY(user_id, space_id, entity_id) REFERENCES mnemotrace.entities ON DELETE CASCADE
);
CREATE TABLE mnemotrace.lexical_documents (
    user_id mnemotrace.identifier NOT NULL,
    space_id mnemotrace.identifier NOT NULL,
    id mnemotrace.identifier NOT NULL,
    source_id mnemotrace.identifier,
    fact_id mnemotrace.identifier,
    working_id mnemotrace.identifier,
    entity_id mnemotrace.identifier,
    target_kind text NOT NULL,
    content_version mnemotrace.positive_version NOT NULL,
    analyzer_version mnemotrace.identifier NOT NULL,
    document_length mnemotrace.counter NOT NULL,
    PRIMARY KEY(user_id, space_id, id),
    CHECK (num_nonnulls(source_id, fact_id, working_id, entity_id) = 1),
    CHECK ((target_kind = 'Episode' AND source_id IS NOT NULL) OR (target_kind = 'Fact' AND fact_id IS NOT NULL)
        OR (target_kind = 'WorkingMemory' AND working_id IS NOT NULL) OR (target_kind = 'Entity' AND entity_id IS NOT NULL)),
    UNIQUE NULLS NOT DISTINCT(user_id, space_id, source_id, fact_id, working_id, entity_id),
    FOREIGN KEY(user_id, space_id, source_id) REFERENCES mnemotrace.sources ON DELETE CASCADE,
    FOREIGN KEY(user_id, space_id, fact_id) REFERENCES mnemotrace.facts ON DELETE CASCADE,
    FOREIGN KEY(user_id, space_id, working_id) REFERENCES mnemotrace.working_memories ON DELETE CASCADE,
    FOREIGN KEY(user_id, space_id, entity_id) REFERENCES mnemotrace.entities ON DELETE CASCADE
);
CREATE TABLE mnemotrace.lexical_terms (
    user_id mnemotrace.identifier NOT NULL,
    space_id mnemotrace.identifier NOT NULL,
    document_id mnemotrace.identifier NOT NULL,
    term text NOT NULL,
    tf integer NOT NULL CHECK (tf > 0),
    PRIMARY KEY(user_id, space_id, document_id, term),
    FOREIGN KEY(user_id, space_id, document_id) REFERENCES mnemotrace.lexical_documents ON DELETE CASCADE
);
CREATE INDEX lexical_term ON mnemotrace.lexical_terms(user_id, space_id, term, document_id);

CREATE TABLE mnemotrace.operations (
    user_id mnemotrace.identifier NOT NULL,
    space_id mnemotrace.identifier NOT NULL,
    operation_id mnemotrace.identifier NOT NULL,
    kind text NOT NULL CHECK (kind IN ('extract','summary','outcomes','conflicts','reembed','graph')),
    session_id mnemotrace.identifier,
    sequence mnemotrace.counter,
    state text NOT NULL CHECK (state IN ('pending','running','retry_wait','failed','completed','cancelled')),
    phase text NOT NULL CHECK (phase IN ('queued','generate','embed','commit','compensate','done')),
    attempts mnemotrace.counter NOT NULL,
    embed_attempts mnemotrace.counter NOT NULL,
    next_attempt_at timestamptz,
    error_code mnemotrace.identifier,
    lease_token mnemotrace.identifier,
    lease_until timestamptz,
    space_generation mnemotrace.counter NOT NULL,
    session_generation mnemotrace.counter,
    extraction_config text,
    context_revision mnemotrace.counter,
    stats jsonb NOT NULL CHECK (jsonb_typeof(stats) = 'object'),
    stored_raw boolean NOT NULL,
    extraction_saved boolean NOT NULL,
    core_committed boolean NOT NULL,
    PRIMARY KEY(user_id, space_id, operation_id),
    CHECK ((session_id IS NULL) = (session_generation IS NULL)),
    CHECK ((lease_token IS NULL) = (lease_until IS NULL)),
    CHECK (state <> 'running' OR lease_token IS NOT NULL),
    CHECK (state <> 'retry_wait' OR next_attempt_at IS NOT NULL),
    CHECK (state NOT IN ('failed','cancelled') OR error_code IS NOT NULL),
    CHECK (NOT extraction_saved OR stored_raw),
    CHECK (NOT core_committed OR (stored_raw AND (kind <> 'extract' OR extraction_saved))),
    CHECK ((state = 'completed') = (phase = 'done')),
    CHECK (state <> 'completed' OR core_committed),
    CHECK (kind <> 'extract' OR phase NOT IN ('embed','commit') OR extraction_saved),
    FOREIGN KEY(user_id, space_id) REFERENCES mnemotrace.memory_spaces(user_id, space_id) ON DELETE CASCADE,
    FOREIGN KEY(user_id, space_id, session_id) REFERENCES mnemotrace.sessions ON DELETE CASCADE
);
CREATE INDEX operations_claim ON mnemotrace.operations(state, next_attempt_at, lease_until);
CREATE INDEX operations_session ON mnemotrace.operations(user_id, space_id, session_id, sequence, operation_id);
CREATE TABLE mnemotrace.operation_sources (
    user_id mnemotrace.identifier NOT NULL,
    space_id mnemotrace.identifier NOT NULL,
    operation_id mnemotrace.identifier NOT NULL,
    source_id mnemotrace.identifier NOT NULL,
    content_version mnemotrace.positive_version NOT NULL,
    input_start mnemotrace.counter NOT NULL,
    input_end mnemotrace.counter NOT NULL,
    input_order mnemotrace.counter NOT NULL,
    role text,
    representation_source text,
    PRIMARY KEY(user_id, space_id, operation_id, source_id, input_start),
    CHECK (input_end > input_start),
    FOREIGN KEY(user_id, space_id, operation_id) REFERENCES mnemotrace.operations ON DELETE CASCADE,
    FOREIGN KEY(user_id, space_id, source_id) REFERENCES mnemotrace.sources ON DELETE CASCADE
);
CREATE TABLE mnemotrace.operation_targets (
    user_id mnemotrace.identifier NOT NULL,
    space_id mnemotrace.identifier NOT NULL,
    operation_id mnemotrace.identifier NOT NULL,
    source_id mnemotrace.identifier,
    fact_id mnemotrace.identifier,
    working_id mnemotrace.identifier,
    entity_id mnemotrace.identifier,
    target_kind text NOT NULL,
    content_version mnemotrace.positive_version NOT NULL,
    CHECK (num_nonnulls(source_id, fact_id, working_id, entity_id) = 1),
    CHECK ((target_kind = 'Episode' AND source_id IS NOT NULL) OR (target_kind = 'Fact' AND fact_id IS NOT NULL)
        OR (target_kind = 'WorkingMemory' AND working_id IS NOT NULL) OR (target_kind = 'Entity' AND entity_id IS NOT NULL)),
    UNIQUE NULLS NOT DISTINCT(user_id, space_id, operation_id, source_id, fact_id, working_id, entity_id),
    FOREIGN KEY(user_id, space_id, operation_id) REFERENCES mnemotrace.operations ON DELETE CASCADE,
    FOREIGN KEY(user_id, space_id, source_id) REFERENCES mnemotrace.sources ON DELETE CASCADE,
    FOREIGN KEY(user_id, space_id, fact_id) REFERENCES mnemotrace.facts ON DELETE CASCADE,
    FOREIGN KEY(user_id, space_id, working_id) REFERENCES mnemotrace.working_memories ON DELETE CASCADE,
    FOREIGN KEY(user_id, space_id, entity_id) REFERENCES mnemotrace.entities ON DELETE CASCADE
);
CREATE TABLE mnemotrace.operation_products (
    user_id mnemotrace.identifier NOT NULL,
    space_id mnemotrace.identifier NOT NULL,
    operation_id mnemotrace.identifier NOT NULL,
    input jsonb NOT NULL CHECK (jsonb_typeof(input) = 'object'),
    result jsonb NOT NULL CHECK (jsonb_typeof(result) = 'object'),
    format_version integer NOT NULL CHECK (format_version = 2),
    extraction_config text NOT NULL,
    context_revision mnemotrace.counter,
    source_versions jsonb NOT NULL CHECK (jsonb_typeof(source_versions) = 'array'),
    stats jsonb NOT NULL CHECK (jsonb_typeof(stats) = 'object'),
    summary_max_chars integer NOT NULL CHECK (summary_max_chars >= 0),
    result_hash text NOT NULL CHECK (result_hash ~ '^[0-9a-f]{64}$'),
    PRIMARY KEY(user_id, space_id, operation_id),
    FOREIGN KEY(user_id, space_id, operation_id) REFERENCES mnemotrace.operations ON DELETE CASCADE
);
CREATE TABLE mnemotrace.operation_receipts (
    user_id mnemotrace.identifier NOT NULL,
    space_id mnemotrace.identifier NOT NULL,
    operation_id mnemotrace.identifier NOT NULL,
    core_revision mnemotrace.counter NOT NULL,
    fact_ids text[] NOT NULL,
    working_ids text[] NOT NULL,
    completed_at timestamptz NOT NULL,
    PRIMARY KEY(user_id, space_id, operation_id),
    FOREIGN KEY(user_id, space_id, operation_id) REFERENCES mnemotrace.operations ON DELETE CASCADE
);

-- Required stats are never silently reconstructed from defaults when restoring JSONB.
CREATE FUNCTION mnemotrace.valid_stats(value jsonb) RETURNS boolean
LANGUAGE sql IMMUTABLE SET search_path = pg_catalog AS $$
    SELECT jsonb_typeof(value) = 'object'
       AND value ?& ARRAY['facts_added','duplicates','invalidated','working_added','model_calls','windows_completed']
       AND NOT EXISTS (
          SELECT 1 FROM unnest(ARRAY['facts_added','duplicates','invalidated','working_added','model_calls','windows_completed']) AS key
          WHERE jsonb_typeof(value -> key) <> 'number' OR (value ->> key) !~ '^[0-9]+$')
$$;
ALTER TABLE mnemotrace.operations ADD CHECK (mnemotrace.valid_stats(stats));
ALTER TABLE mnemotrace.operation_products ADD CHECK (mnemotrace.valid_stats(stats));

-- Authorization helpers have no caller-controlled search path or table names.
CREATE FUNCTION mnemotrace.authenticate(digest text)
RETURNS TABLE(user_id text, key_id text)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog AS $$
    SELECT u.user_id::text, k.key_id::text
    FROM mnemotrace.api_keys k JOIN mnemotrace.users u USING(user_id)
    WHERE k.key_hash = digest AND k.revoked_at IS NULL AND u.status = 'active'
$$;
GRANT CREATE ON SCHEMA mnemotrace TO {auth};
ALTER FUNCTION mnemotrace.authenticate(text) OWNER TO {auth};
REVOKE CREATE ON SCHEMA mnemotrace FROM {auth};
REVOKE ALL ON FUNCTION mnemotrace.authenticate(text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION mnemotrace.authenticate(text) TO {runtime};
REVOKE ALL ON FUNCTION mnemotrace.valid_stats(jsonb) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION mnemotrace.valid_stats(jsonb) TO {runtime}, {admin};

GRANT SELECT ON mnemotrace.users TO {runtime}, {auth};
-- SELECT FOR SHARE needs UPDATE privilege; the RLS lock policy rejects changes.
GRANT UPDATE(status) ON mnemotrace.users TO {runtime};
GRANT SELECT ON mnemotrace.api_keys TO {auth};
GRANT SELECT, INSERT, UPDATE, DELETE ON mnemotrace.users, mnemotrace.api_keys TO {admin};
GRANT SELECT, DELETE ON ALL TABLES IN SCHEMA mnemotrace TO {admin};
REVOKE DELETE ON mnemotrace.schema_migrations FROM {admin};
-- Runtime business grants and per-table policies are generated below by the migration runner.
