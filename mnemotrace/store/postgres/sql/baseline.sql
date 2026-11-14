CREATE SCHEMA mnemotrace AUTHORIZATION {migration};
GRANT USAGE ON SCHEMA mnemotrace TO {runtime}, {admin}, {auth};

CREATE DOMAIN mnemotrace.identifier AS text
    CHECK (length(VALUE) >= 1 AND length(VALUE) <= 512 AND VALUE ~ '[^[:space:]]');
CREATE DOMAIN mnemotrace.positive_version AS bigint CHECK (VALUE >= 1);
CREATE DOMAIN mnemotrace.counter AS bigint CHECK (VALUE >= 0);

CREATE TABLE mnemotrace.schema_baseline (
    baseline text PRIMARY KEY,
    singleton boolean UNIQUE NOT NULL DEFAULT true CHECK (singleton),
    checksum text NOT NULL,
    role_prefix text NOT NULL,
    structure_hash text NOT NULL
);
GRANT SELECT ON mnemotrace.schema_baseline TO {runtime}, {admin};

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
    name text NOT NULL CHECK (length(name) >= 1 AND length(name) <= 128 AND name ~ '[^[:space:]]'),
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
    source_kind text NOT NULL CHECK (source_kind IN ('note','dialogue')),
    extract boolean NOT NULL,
    message_id mnemotrace.identifier,
    sequence mnemotrace.counter,
    agent_id text CHECK (agent_id ~ '^[A-Za-z0-9_.:@/\-]{1,128}$'),
    message_fingerprint text CHECK (message_fingerprint ~ '^[0-9a-f]{64}$'),
    coverage jsonb,
    consolidated boolean NOT NULL DEFAULT false,
    record_version mnemotrace.positive_version NOT NULL,
    content_version mnemotrace.positive_version NOT NULL,
    PRIMARY KEY(user_id, space_id, id),
    UNIQUE(user_id, space_id, session_id, message_id),
    UNIQUE(user_id, space_id, session_id, sequence),
    CHECK (message_id IS NULL OR (message_fingerprint IS NOT NULL AND role IN ('user','assistant')
           AND role IS NOT NULL AND sequence IS NOT NULL
           AND length(content) BETWEEN 1 AND 1000000 AND content ~ '[^[:space:]]')),
    FOREIGN KEY(user_id, space_id, session_id) REFERENCES mnemotrace.sessions ON DELETE CASCADE
);
CREATE INDEX sources_session_time ON mnemotrace.sources(user_id, space_id, session_id, event_time, id);
CREATE TABLE mnemotrace.source_digests (
    user_id mnemotrace.identifier NOT NULL,
    space_id mnemotrace.identifier NOT NULL,
    source_id mnemotrace.identifier NOT NULL,
    digest text NOT NULL,
    digest_written_at timestamptz,
    CHECK ((digest <> '') = (digest_written_at IS NOT NULL)),
    digest_coverage jsonb,
    content_version mnemotrace.positive_version NOT NULL,
    digest_version mnemotrace.positive_version NOT NULL,
    PRIMARY KEY(user_id, space_id, source_id),
    FOREIGN KEY(user_id, space_id, source_id) REFERENCES mnemotrace.sources ON DELETE CASCADE
);
CREATE TABLE mnemotrace.entities (
    user_id mnemotrace.identifier NOT NULL,
    space_id mnemotrace.identifier NOT NULL,
    id mnemotrace.identifier NOT NULL,
    name mnemotrace.identifier NOT NULL,
    type mnemotrace.identifier,
    normalized_name mnemotrace.identifier NOT NULL,
    anchor_terms text[] NOT NULL,
    record_version mnemotrace.positive_version NOT NULL,
    content_version mnemotrace.positive_version NOT NULL,
    PRIMARY KEY(user_id, space_id, id),
    FOREIGN KEY(user_id, space_id) REFERENCES mnemotrace.memory_spaces(user_id, space_id) ON DELETE CASCADE
);
CREATE UNIQUE INDEX entities_name ON mnemotrace.entities(user_id, space_id, normalized_name);

CREATE TABLE mnemotrace.entity_types (
    user_id mnemotrace.identifier NOT NULL,
    space_id mnemotrace.identifier NOT NULL,
    id mnemotrace.identifier NOT NULL,
    meaning text NOT NULL CHECK (meaning ~ '[^[:space:]]'),
    record_version mnemotrace.positive_version NOT NULL,
    content_version mnemotrace.positive_version NOT NULL,
    PRIMARY KEY(user_id, space_id, id),
    FOREIGN KEY(user_id, space_id) REFERENCES mnemotrace.memory_spaces(user_id, space_id) ON DELETE CASCADE
);

CREATE TABLE mnemotrace.relation_types (
    user_id mnemotrace.identifier NOT NULL,
    space_id mnemotrace.identifier NOT NULL,
    id mnemotrace.identifier NOT NULL,
    predicate mnemotrace.identifier NOT NULL,
    meaning text NOT NULL CHECK (meaning ~ '[^[:space:]]'),
    direction text NOT NULL CHECK (direction IN ('forward', 'symmetric')),
    endpoint_types jsonb NOT NULL CHECK (jsonb_typeof(endpoint_types) = 'array'),
    record_version mnemotrace.positive_version NOT NULL,
    content_version mnemotrace.positive_version NOT NULL,
    PRIMARY KEY(user_id, space_id, id),
    UNIQUE(user_id, space_id, id, predicate),
    UNIQUE(user_id, space_id, predicate),
    FOREIGN KEY(user_id, space_id) REFERENCES mnemotrace.memory_spaces(user_id, space_id) ON DELETE CASCADE
);

CREATE TABLE mnemotrace.facts (
    user_id mnemotrace.identifier NOT NULL,
    space_id mnemotrace.identifier NOT NULL,
    id mnemotrace.identifier NOT NULL,
    subject_key text NOT NULL,
    predicate_key text NOT NULL,
    object_key text NOT NULL,
    subject text NOT NULL CHECK (subject ~ '[^[:space:]]'),
    predicate text NOT NULL CHECK (predicate ~ '[^[:space:]]'),
    object text NOT NULL CHECK (object ~ '[^[:space:]]'),
    text text NOT NULL CHECK (text ~ '[^[:space:]]'),
    salience double precision NOT NULL CHECK (salience > '-Infinity'::float8 AND salience < 'Infinity'::float8),
    categories text[] NOT NULL CHECK (array_position(categories, NULL) IS NULL AND categories <@ ARRAY[
        'personal_details','family','professional_details','sports','travel','food','music','health',
        'technology','hobbies','fashion','entertainment','milestones','user_preferences','misc'
    ]::text[]),
    relation_id mnemotrace.identifier NOT NULL,
    subject_id mnemotrace.identifier,
    object_id mnemotrace.identifier,
    sensitive boolean NOT NULL,
    retention text NOT NULL CHECK (retention IN ('durable','normal','unknown')),
    valid_at timestamptz,
    invalid_at timestamptz,
    created_at timestamptz NOT NULL,
    expired_at timestamptz,
    record_version mnemotrace.positive_version NOT NULL,
    content_version mnemotrace.positive_version NOT NULL,
    PRIMARY KEY(user_id, space_id, id),
    CHECK (valid_at IS NULL OR invalid_at IS NULL OR valid_at < invalid_at),
    FOREIGN KEY(user_id, space_id) REFERENCES mnemotrace.memory_spaces(user_id, space_id) ON DELETE CASCADE,
    FOREIGN KEY(user_id, space_id, relation_id, predicate)
        REFERENCES mnemotrace.relation_types(user_id, space_id, id, predicate),
    FOREIGN KEY(user_id, space_id, subject_id) REFERENCES mnemotrace.entities,
    FOREIGN KEY(user_id, space_id, object_id) REFERENCES mnemotrace.entities
);
CREATE INDEX facts_slot ON mnemotrace.facts(user_id, space_id, subject_id, relation_id, valid_at, id);
CREATE INDEX facts_slot_normalized ON mnemotrace.facts(user_id, space_id, subject_key, predicate_key, id);
CREATE INDEX facts_time ON mnemotrace.facts(user_id, space_id, valid_at, invalid_at);
CREATE TABLE mnemotrace.fact_sources (
    user_id mnemotrace.identifier NOT NULL,
    space_id mnemotrace.identifier NOT NULL,
    fact_id mnemotrace.identifier NOT NULL,
    source_id mnemotrace.identifier NOT NULL,
    source_order mnemotrace.counter,
    PRIMARY KEY(user_id, space_id, fact_id, source_id),
    FOREIGN KEY(user_id, space_id, fact_id) REFERENCES mnemotrace.facts ON DELETE CASCADE,
    FOREIGN KEY(user_id, space_id, source_id) REFERENCES mnemotrace.sources ON DELETE CASCADE
);
CREATE INDEX fact_sources_source ON mnemotrace.fact_sources(user_id, space_id, source_id);
CREATE TABLE mnemotrace.fact_supersedes (
    user_id mnemotrace.identifier NOT NULL,
    space_id mnemotrace.identifier NOT NULL,
    fact_id mnemotrace.identifier NOT NULL,
    predecessor_id mnemotrace.identifier NOT NULL,
    PRIMARY KEY(user_id, space_id, fact_id, predecessor_id),
    CHECK (fact_id <> predecessor_id),
    FOREIGN KEY(user_id, space_id, fact_id) REFERENCES mnemotrace.facts ON DELETE CASCADE,
    FOREIGN KEY(user_id, space_id, predecessor_id) REFERENCES mnemotrace.facts
        ON DELETE CASCADE DEFERRABLE INITIALLY DEFERRED
);
CREATE INDEX fact_supersedes_predecessor
    ON mnemotrace.fact_supersedes(user_id, space_id, predecessor_id);

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
CREATE UNIQUE INDEX graph_fact ON mnemotrace.graph_relations(user_id, space_id, fact_id);

CREATE TABLE mnemotrace.embeddings (
    user_id mnemotrace.identifier NOT NULL,
    space_id mnemotrace.identifier NOT NULL,
    source_id mnemotrace.identifier,
    fact_id mnemotrace.identifier,
    target_kind text NOT NULL,
    field text NOT NULL CHECK (field IN ('text','digest')),
    content_version mnemotrace.positive_version NOT NULL,
    model mnemotrace.identifier NOT NULL,
    dimension integer NOT NULL CHECK (dimension > 0),
    vector public.vector NOT NULL,
    CHECK (public.vector_dims(vector) = dimension),
    CHECK (num_nonnulls(source_id, fact_id) = 1),
    CHECK ((target_kind = 'Episode' AND source_id IS NOT NULL) OR (target_kind = 'Fact' AND fact_id IS NOT NULL)),
    CHECK ((field = 'digest') = (source_id IS NOT NULL)),
    UNIQUE NULLS NOT DISTINCT(user_id, space_id, source_id, fact_id, field, model, dimension, content_version),
    FOREIGN KEY(user_id, space_id, source_id) REFERENCES mnemotrace.sources ON DELETE CASCADE,
    FOREIGN KEY(user_id, space_id, fact_id) REFERENCES mnemotrace.facts ON DELETE CASCADE
);
CREATE TABLE mnemotrace.lexical_documents (
    user_id mnemotrace.identifier NOT NULL,
    space_id mnemotrace.identifier NOT NULL,
    id mnemotrace.identifier NOT NULL,
    source_id mnemotrace.identifier,
    fact_id mnemotrace.identifier,
    target_kind text NOT NULL,
    content_version mnemotrace.positive_version NOT NULL,
    analyzer_version mnemotrace.identifier NOT NULL,
    field text NOT NULL CHECK (field IN ('text', 'digest')),
    document_length mnemotrace.counter NOT NULL,
    PRIMARY KEY(user_id, space_id, id),
    CHECK (num_nonnulls(source_id, fact_id) = 1),
    CHECK ((target_kind = 'Episode' AND source_id IS NOT NULL) OR (target_kind = 'Fact' AND fact_id IS NOT NULL)),
    CHECK ((field = 'digest') = (source_id IS NOT NULL)),
    UNIQUE NULLS NOT DISTINCT(user_id, space_id, source_id, fact_id, field),
    FOREIGN KEY(user_id, space_id, source_id) REFERENCES mnemotrace.sources ON DELETE CASCADE,
    FOREIGN KEY(user_id, space_id, fact_id) REFERENCES mnemotrace.facts ON DELETE CASCADE
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
    kind text NOT NULL CHECK (kind IN ('extract','digest','profile')),
    session_id mnemotrace.identifier,
    sequence mnemotrace.counter,
    state text NOT NULL CHECK (state IN ('pending','running','retry_wait','failed','completed','cancelled')),
    phase text NOT NULL CHECK (phase IN ('queued','generate','embed','commit','done')),
    attempts mnemotrace.counter NOT NULL,
    embed_attempts mnemotrace.counter NOT NULL,
    next_attempt_at timestamptz,
    error_code mnemotrace.identifier,
    lease_token mnemotrace.identifier,
    lease_until timestamptz,
    space_generation mnemotrace.counter NOT NULL,
    extraction_config text,
    context_revision mnemotrace.counter,
    ready_at timestamptz DEFAULT statement_timestamp(),
    frozen boolean NOT NULL DEFAULT false,
    generation_attempts mnemotrace.counter NOT NULL DEFAULT 0,
    parent_id mnemotrace.identifier,
    extraction_saved boolean NOT NULL,
    core_committed boolean NOT NULL,
    PRIMARY KEY(user_id, space_id, operation_id),
    CHECK ((lease_token IS NULL) = (lease_until IS NULL)),
    CHECK (state <> 'running' OR lease_token IS NOT NULL),
    CHECK (state <> 'retry_wait' OR next_attempt_at IS NOT NULL),
    CHECK (state NOT IN ('failed','cancelled') OR error_code IS NOT NULL),
    CHECK (NOT core_committed OR kind <> 'extract' OR extraction_saved),
    CHECK ((state = 'completed') = (phase = 'done')),
    CHECK (state <> 'completed' OR core_committed),
    CHECK (kind NOT IN ('extract', 'digest') OR phase <> 'queued' OR state <> 'pending' OR ready_at IS NOT NULL),
    FOREIGN KEY (user_id, space_id, parent_id) REFERENCES mnemotrace.operations ON DELETE CASCADE,
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
    input_order mnemotrace.counter NOT NULL,
    is_context boolean NOT NULL DEFAULT false,
    PRIMARY KEY(user_id, space_id, operation_id, source_id),
    FOREIGN KEY(user_id, space_id, operation_id) REFERENCES mnemotrace.operations ON DELETE CASCADE,
    FOREIGN KEY(user_id, space_id, source_id) REFERENCES mnemotrace.sources ON DELETE CASCADE
);
CREATE TABLE mnemotrace.operation_products (
    user_id mnemotrace.identifier NOT NULL,
    space_id mnemotrace.identifier NOT NULL,
    operation_id mnemotrace.identifier NOT NULL,
    input jsonb NOT NULL CHECK (jsonb_typeof(input) = 'object'),
    result jsonb NOT NULL CHECK (jsonb_typeof(result) = 'object'),
    format_version integer NOT NULL CHECK (format_version = 8),
    extraction_config text NOT NULL,
    context_revision mnemotrace.counter,
    source_versions jsonb NOT NULL CHECK (jsonb_typeof(source_versions) = 'array'),
    digest_max_chars integer NOT NULL CHECK (digest_max_chars >= 0),
    input_hash text NOT NULL CHECK (input_hash ~ '^[0-9a-f]{64}$'),
    result_hash text NOT NULL CHECK (result_hash ~ '^[0-9a-f]{64}$'),
    PRIMARY KEY(user_id, space_id, operation_id),
    FOREIGN KEY(user_id, space_id, operation_id) REFERENCES mnemotrace.operations ON DELETE CASCADE
);
CREATE TABLE mnemotrace.commit_receipts (
    user_id mnemotrace.identifier NOT NULL,
    space_id mnemotrace.identifier NOT NULL,
    operation_id mnemotrace.identifier NOT NULL,
    core_revision mnemotrace.counter NOT NULL,
    fact_ids text[] NOT NULL,
    source_ids text[] NOT NULL,
    affected_count mnemotrace.counter NOT NULL,
    segment_of mnemotrace.identifier,
    FOREIGN KEY (user_id, space_id, segment_of) REFERENCES mnemotrace.operations ON DELETE CASCADE,
    PRIMARY KEY (user_id, space_id, operation_id),
    FOREIGN KEY (user_id, space_id) REFERENCES mnemotrace.memory_spaces(user_id, space_id) ON DELETE CASCADE
);
-- Authorization helpers have no caller-controlled search path or table names.
CREATE FUNCTION mnemotrace.authenticate(content_hash text)
RETURNS TABLE(user_id text, key_id text)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog AS $$
    SELECT u.user_id::text, k.key_id::text
    FROM mnemotrace.api_keys k JOIN mnemotrace.users u USING(user_id)
    WHERE k.key_hash = content_hash AND k.revoked_at IS NULL AND u.status = 'active'
$$;
GRANT CREATE ON SCHEMA mnemotrace TO {auth};
ALTER FUNCTION mnemotrace.authenticate(text) OWNER TO {auth};
REVOKE CREATE ON SCHEMA mnemotrace FROM {auth};
REVOKE ALL ON FUNCTION mnemotrace.authenticate(text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION mnemotrace.authenticate(text) TO {runtime};

GRANT SELECT ON mnemotrace.users TO {runtime}, {auth};
-- SELECT FOR SHARE needs UPDATE privilege; the RLS lock policy rejects changes.
GRANT UPDATE(status) ON mnemotrace.users TO {runtime};
GRANT SELECT ON mnemotrace.api_keys TO {auth};
GRANT SELECT, INSERT, UPDATE, DELETE ON mnemotrace.users, mnemotrace.api_keys TO {admin};

CREATE TABLE mnemotrace.profile_states (
    user_id mnemotrace.identifier NOT NULL,
    space_id mnemotrace.identifier NOT NULL,
    profile_data jsonb NOT NULL CHECK (jsonb_typeof(profile_data) = 'object'),
    PRIMARY KEY (user_id, space_id),
    FOREIGN KEY (user_id,space_id) REFERENCES mnemotrace.memory_spaces(user_id,space_id) ON DELETE CASCADE
);
CREATE UNIQUE INDEX one_active_profile_refresh ON mnemotrace.operations(user_id,space_id)
    WHERE kind = 'profile' AND state IN ('pending','retry_wait','running');

CREATE TABLE mnemotrace.operation_digest_parts (
    user_id mnemotrace.identifier NOT NULL,
    space_id mnemotrace.identifier NOT NULL,
    operation_id mnemotrace.identifier NOT NULL,
    part_id mnemotrace.identifier NOT NULL,
    source_id mnemotrace.identifier NOT NULL,
    input_start mnemotrace.counter NOT NULL,
    input_end mnemotrace.counter NOT NULL CHECK (input_end > input_start),
    text text NOT NULL CHECK (length(text) > 0),
    extraction_config text NOT NULL CHECK (extraction_config ~ '^[0-9a-f]{64}$'),
    PRIMARY KEY (user_id,space_id,operation_id,part_id),
    FOREIGN KEY (user_id,space_id,operation_id) REFERENCES mnemotrace.operations ON DELETE CASCADE,
    FOREIGN KEY (user_id,space_id,source_id) REFERENCES mnemotrace.sources ON DELETE CASCADE
);
CREATE INDEX operations_parent ON mnemotrace.operations(user_id, space_id, parent_id);
CREATE INDEX operations_ready ON mnemotrace.operations(ready_at)
    WHERE state = 'pending' AND kind IN ('extract', 'digest');

CREATE INDEX commit_receipts_segment ON mnemotrace.commit_receipts(user_id, space_id, segment_of)
    WHERE segment_of IS NOT NULL;
CREATE INDEX operation_sources_source ON mnemotrace.operation_sources(user_id, space_id, source_id);
CREATE INDEX embeddings_retrieval
    ON mnemotrace.embeddings(user_id, space_id, target_kind, field, model, dimension);
CREATE INDEX lexical_fact_version
    ON mnemotrace.lexical_documents(user_id, space_id, fact_id, field, content_version) WHERE fact_id IS NOT NULL;
CREATE INDEX lexical_source_version
    ON mnemotrace.lexical_documents(user_id, space_id, source_id, field, content_version) WHERE source_id IS NOT NULL;
CREATE INDEX entity_anchor_terms ON mnemotrace.entities USING gin(anchor_terms);

DO $policies$
DECLARE relation record;
BEGIN
    FOR relation IN SELECT c.relname FROM pg_catalog.pg_class c
        JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname = 'mnemotrace' AND c.relkind = 'r' AND c.relname <> 'schema_baseline'
    LOOP
        EXECUTE format('ALTER TABLE mnemotrace.%I ENABLE ROW LEVEL SECURITY', relation.relname);
        EXECUTE format('ALTER TABLE mnemotrace.%I FORCE ROW LEVEL SECURITY', relation.relname);
        EXECUTE format('CREATE POLICY migration ON mnemotrace.%I TO {migration} USING (true) WITH CHECK (true)', relation.relname);
        EXECUTE format('CREATE POLICY account_read ON mnemotrace.%I FOR SELECT TO {admin} USING (true)', relation.relname);
        EXECUTE format('CREATE POLICY account_delete ON mnemotrace.%I FOR DELETE TO {admin} USING (true)', relation.relname);
        EXECUTE format('GRANT SELECT, DELETE ON mnemotrace.%I TO {admin}', relation.relname);
        IF relation.relname NOT IN ('users', 'api_keys') THEN
            IF relation.relname = 'commit_receipts' THEN
                GRANT SELECT, INSERT, DELETE ON mnemotrace.commit_receipts TO {runtime};
                GRANT UPDATE (fact_ids) ON mnemotrace.commit_receipts TO {runtime};
            ELSE
                EXECUTE format('GRANT SELECT, INSERT, UPDATE, DELETE ON mnemotrace.%I TO {runtime}', relation.relname);
            END IF;
            EXECUTE format('CREATE POLICY user_scope ON mnemotrace.%I TO {runtime}
                USING (user_id::text = nullif(current_setting(''mnemotrace.user_id'', true), ''''))
                WITH CHECK (user_id::text = nullif(current_setting(''mnemotrace.user_id'', true), ''''))', relation.relname);
        END IF;
    END LOOP;
END
$policies$;
CREATE POLICY own_user ON mnemotrace.users FOR SELECT TO {runtime}
    USING (user_id::text = nullif(current_setting('mnemotrace.user_id', true), ''));
-- Row locking also applies UPDATE USING. WITH CHECK false allows locking the
-- owner's row without allowing the runtime to change account status.
CREATE POLICY own_user_lock ON mnemotrace.users FOR UPDATE TO {runtime}
    USING (user_id::text = nullif(current_setting('mnemotrace.user_id', true), ''))
    WITH CHECK (false);
CREATE POLICY account_insert_user ON mnemotrace.users FOR INSERT TO {admin} WITH CHECK (true);
CREATE POLICY account_update_user ON mnemotrace.users FOR UPDATE TO {admin} USING (true) WITH CHECK (true);
CREATE POLICY account_insert_key ON mnemotrace.api_keys FOR INSERT TO {admin} WITH CHECK (true);
CREATE POLICY account_update_key ON mnemotrace.api_keys FOR UPDATE TO {admin} USING (true) WITH CHECK (true);
CREATE POLICY auth_users ON mnemotrace.users FOR SELECT TO {auth} USING (true);
CREATE POLICY auth_keys ON mnemotrace.api_keys FOR SELECT TO {auth} USING (true);

GRANT USAGE ON SCHEMA mnemotrace TO {discovery}, {discovery_owner};
GRANT SELECT ON mnemotrace.schema_baseline TO {discovery};
GRANT SELECT (user_id, status) ON mnemotrace.users TO {discovery_owner};
GRANT SELECT (user_id, space_id, status) ON mnemotrace.memory_spaces TO {discovery_owner};
GRANT SELECT (user_id, space_id, operation_id, state, phase, kind, session_id, sequence,
    ready_at, next_attempt_at, lease_until) ON mnemotrace.operations TO {discovery_owner};
CREATE POLICY discovery_accounts ON mnemotrace.users FOR SELECT TO {discovery_owner} USING (true);
CREATE POLICY discovery_spaces ON mnemotrace.memory_spaces FOR SELECT TO {discovery_owner} USING (true);
CREATE POLICY discovery_operations ON mnemotrace.operations FOR SELECT TO {discovery_owner} USING (true);

CREATE FUNCTION mnemotrace.discover_processing(batch_size integer)
RETURNS TABLE(user_id text, space_id text, operation_id text)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog AS $$
    SELECT o.user_id::text, o.space_id::text, o.operation_id::text
    FROM mnemotrace.operations o
    JOIN mnemotrace.users u ON u.user_id = o.user_id AND u.status = 'active'
    JOIN mnemotrace.memory_spaces s ON (s.user_id,s.space_id) = (o.user_id,o.space_id)
        AND s.status = 'active'
    WHERE o.kind IN ('extract', 'digest', 'profile') AND (
        (o.state = 'pending' AND (o.phase <> 'queued' OR o.ready_at <= statement_timestamp()))
        OR (o.state = 'retry_wait' AND o.next_attempt_at <= statement_timestamp())
        OR (o.state = 'running' AND o.lease_until <= statement_timestamp()))
    AND (o.kind <> 'extract' OR NOT EXISTS (
        SELECT 1 FROM mnemotrace.operations earlier
        WHERE (earlier.user_id,earlier.space_id,earlier.session_id) =
            (o.user_id,o.space_id,o.session_id)
        AND earlier.kind = 'extract' AND earlier.sequence < o.sequence
        AND earlier.state IN ('pending','retry_wait','running')))
    ORDER BY coalesce(o.next_attempt_at,o.ready_at,o.lease_until), o.operation_id
    LIMIT least(greatest(batch_size, 0), 100)
$$;
GRANT CREATE ON SCHEMA mnemotrace TO {discovery_owner};
ALTER FUNCTION mnemotrace.discover_processing(integer) OWNER TO {discovery_owner};
REVOKE CREATE ON SCHEMA mnemotrace FROM {discovery_owner};
REVOKE ALL ON FUNCTION mnemotrace.discover_processing(integer) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION mnemotrace.discover_processing(integer) TO {discovery};
