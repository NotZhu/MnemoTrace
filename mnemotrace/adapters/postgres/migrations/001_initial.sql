CREATE DOMAIN mnemotrace.epoch_seconds AS double precision
    CHECK (VALUE >= -62135596800 AND VALUE < 253402300800);

CREATE TABLE mnemotrace.spaces (
    space_id text COLLATE "C" PRIMARY KEY,
    owner_id text COLLATE "C" NOT NULL,
    name text NOT NULL,
    created_at mnemotrace.epoch_seconds NOT NULL,
    purge_generation bigint NOT NULL DEFAULT 0,
    fact_revision bigint NOT NULL DEFAULT 0,
    memory_decay boolean NOT NULL DEFAULT false,
    profile_auto_refresh boolean NOT NULL DEFAULT false,
    profile_evidence_revision bigint NOT NULL DEFAULT 0,
    session_summary_enabled boolean NOT NULL DEFAULT false
);

CREATE INDEX spaces_by_owner ON mnemotrace.spaces (owner_id, space_id);

CREATE TABLE mnemotrace.api_keys (
    key_id text COLLATE "C" PRIMARY KEY,
    owner_id text COLLATE "C" NOT NULL,
    secret_hash bytea NOT NULL CHECK (octet_length(secret_hash) = 32),
    created_at timestamptz NOT NULL DEFAULT statement_timestamp(),
    revoked_at timestamptz
);

CREATE INDEX api_keys_by_owner ON mnemotrace.api_keys (owner_id, key_id);

CREATE TABLE mnemotrace.sessions (
    space_id text COLLATE "C" REFERENCES mnemotrace.spaces NOT NULL,
    session_id text COLLATE "C" NOT NULL,
    created_at timestamptz NOT NULL DEFAULT statement_timestamp(),
    PRIMARY KEY (space_id, session_id)
);

CREATE INDEX sessions_by_creation ON mnemotrace.sessions
    (space_id, created_at DESC, session_id);

CREATE TABLE mnemotrace.messages (
    space_id text COLLATE "C" REFERENCES mnemotrace.spaces NOT NULL,
    message_id text COLLATE "C" NOT NULL,
    session_id text COLLATE "C" NOT NULL,
    append_seq bigint GENERATED ALWAYS AS IDENTITY,
    role text NOT NULL,
    content text NOT NULL,
    event_time mnemotrace.epoch_seconds,
    recorded_at mnemotrace.epoch_seconds NOT NULL,
    PRIMARY KEY (space_id, message_id),
    FOREIGN KEY (space_id, session_id) REFERENCES mnemotrace.sessions
);

CREATE INDEX messages_session_tail
    ON mnemotrace.messages (space_id, session_id, append_seq DESC);

CREATE TABLE mnemotrace.entities (
    space_id text COLLATE "C" REFERENCES mnemotrace.spaces NOT NULL,
    entity_id text COLLATE "C" NOT NULL,
    name_key text COLLATE "C" NOT NULL,
    name text NOT NULL,
    type text,
    kind text NOT NULL DEFAULT 'named',
    PRIMARY KEY (space_id, entity_id),
    CONSTRAINT entities_space_kind_name_key UNIQUE (space_id, kind, name_key),
    CONSTRAINT entities_kind_check CHECK (kind IN ('named', 'space_user')),
    CONSTRAINT entities_space_user_name_check
        CHECK (kind <> 'space_user' OR (name = '用户' AND name_key = '用户'))
);

CREATE UNIQUE INDEX entities_space_user_unique
    ON mnemotrace.entities (space_id) WHERE kind = 'space_user';

CREATE TABLE mnemotrace.facts (
    space_id text COLLATE "C" REFERENCES mnemotrace.spaces NOT NULL,
    fact_id text COLLATE "C" NOT NULL,
    subject_id text COLLATE "C" NOT NULL,
    predicate text COLLATE "C" NOT NULL,
    object_entity_id text COLLATE "C",
    object_value text,
    object_kind text,
    text text NOT NULL,
    valid_at mnemotrace.epoch_seconds,
    invalid_at mnemotrace.epoch_seconds,
    recorded_at mnemotrace.epoch_seconds NOT NULL,
    expired_at mnemotrace.epoch_seconds,
    labels text[] NOT NULL,
    sensitive boolean NOT NULL,
    source text NOT NULL,
    lexemes tsvector NOT NULL,
    PRIMARY KEY (space_id, fact_id),
    FOREIGN KEY (space_id, subject_id) REFERENCES mnemotrace.entities,
    FOREIGN KEY (space_id, object_entity_id) REFERENCES mnemotrace.entities,
    CHECK (num_nonnulls(object_entity_id, object_value) = 1),
    CHECK (object_value IS NOT NULL OR object_kind IS NULL),
    CHECK ((invalid_at IS NULL) = (expired_at IS NULL)),
    CHECK (valid_at < invalid_at),
    CHECK (recorded_at <= expired_at)
);

CREATE INDEX facts_timeline
    ON mnemotrace.facts (space_id, recorded_at DESC, fact_id);
CREATE INDEX facts_slot
    ON mnemotrace.facts (space_id, subject_id, predicate, recorded_at DESC, fact_id);
CREATE INDEX facts_cutoff
    ON mnemotrace.facts (space_id, invalid_at) WHERE invalid_at IS NOT NULL;
CREATE INDEX facts_edges_out
    ON mnemotrace.facts (space_id, subject_id, valid_at DESC NULLS LAST, fact_id)
    WHERE object_entity_id IS NOT NULL;
CREATE INDEX facts_edges_in
    ON mnemotrace.facts (space_id, object_entity_id, valid_at DESC NULLS LAST, fact_id)
    WHERE object_entity_id IS NOT NULL;
CREATE INDEX facts_labels ON mnemotrace.facts USING gin (labels);
CREATE INDEX facts_lexemes ON mnemotrace.facts USING gin (lexemes);
CREATE INDEX facts_profile_activation
    ON mnemotrace.facts (space_id, subject_id, valid_at)
    WHERE source = 'extracted' AND NOT sensitive AND valid_at IS NOT NULL;

CREATE TABLE mnemotrace.fact_provenance (
    space_id text COLLATE "C" NOT NULL,
    fact_id text COLLATE "C" NOT NULL,
    position integer NOT NULL,
    message_id text COLLATE "C" NOT NULL,
    support_group integer NOT NULL CHECK (support_group >= 0),
    PRIMARY KEY (space_id, fact_id, position),
    FOREIGN KEY (space_id, fact_id) REFERENCES mnemotrace.facts ON DELETE CASCADE,
    FOREIGN KEY (space_id, message_id) REFERENCES mnemotrace.messages
);

CREATE INDEX fact_provenance_message
    ON mnemotrace.fact_provenance (space_id, message_id, fact_id);

CREATE TABLE mnemotrace.fact_salience (
    space_id text COLLATE "C" NOT NULL,
    fact_id text COLLATE "C" NOT NULL,
    salience double precision NOT NULL CHECK (salience BETWEEN 1.0 AND 3.0),
    last_access mnemotrace.epoch_seconds,
    PRIMARY KEY (space_id, fact_id),
    FOREIGN KEY (space_id, fact_id) REFERENCES mnemotrace.facts ON DELETE CASCADE,
    CONSTRAINT fact_salience_last_access_required CHECK (salience <= 1.0 OR last_access IS NOT NULL)
);

CREATE TABLE mnemotrace.embedding_spaces (
    space_id text COLLATE "C" REFERENCES mnemotrace.spaces NOT NULL,
    embedding_space text COLLATE "C" NOT NULL,
    dim integer NOT NULL CHECK (dim > 0),
    PRIMARY KEY (space_id, embedding_space),
    UNIQUE (space_id, embedding_space, dim)
);

CREATE TABLE mnemotrace.fact_vectors (
    space_id text COLLATE "C" NOT NULL,
    embedding_space text COLLATE "C" NOT NULL,
    fact_id text COLLATE "C" NOT NULL,
    dim integer NOT NULL,
    embedding public.vector NOT NULL,
    PRIMARY KEY (space_id, embedding_space, fact_id),
    FOREIGN KEY (space_id, fact_id) REFERENCES mnemotrace.facts ON DELETE CASCADE,
    FOREIGN KEY (space_id, embedding_space, dim)
        REFERENCES mnemotrace.embedding_spaces (space_id, embedding_space, dim),
    CHECK (public.vector_dims(embedding) = dim),
    CHECK (public.vector_norm(embedding) > 0)
);

CREATE INDEX fact_vectors_fact ON mnemotrace.fact_vectors (space_id, fact_id);

CREATE TABLE mnemotrace.remember_operations (
    operation_id text COLLATE "C" PRIMARY KEY,
    space_id text COLLATE "C" NOT NULL,
    session_id text COLLATE "C" NOT NULL,
    enqueue_order bigint GENERATED ALWAYS AS IDENTITY,
    key_hash bytea NOT NULL CHECK (octet_length(key_hash) = 32),
    request_hash bytea NOT NULL CHECK (octet_length(request_hash) = 32),
    messages jsonb,
    status text NOT NULL DEFAULT 'queued'
        CHECK (status IN ('queued', 'running', 'succeeded', 'failed')),
    attempts integer NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    created_at timestamptz NOT NULL DEFAULT statement_timestamp(),
    updated_at timestamptz NOT NULL DEFAULT statement_timestamp(),
    next_retry_at timestamptz,
    lease_token text,
    lease_until timestamptz,
    message_ids text[],
    fact_change_count integer CHECK (fact_change_count >= 0),
    error_code text,
    error_stage text CHECK (error_stage IN ('extraction', 'embedding', 'adjudication', 'publish')),
    UNIQUE (space_id, key_hash),
    CONSTRAINT remember_operations_space_operation_key UNIQUE (space_id, operation_id),
    FOREIGN KEY (space_id, session_id)
        REFERENCES mnemotrace.sessions (space_id, session_id) ON DELETE CASCADE,
    CHECK ((status IN ('queued', 'running')) = (messages IS NOT NULL)),
    CHECK ((status = 'running') = (lease_token IS NOT NULL)),
    CHECK ((status = 'running') = (lease_until IS NOT NULL)),
    CHECK (status = 'queued' OR next_retry_at IS NULL),
    CHECK ((status = 'succeeded') = (message_ids IS NOT NULL)),
    CHECK ((status = 'succeeded') = (fact_change_count IS NOT NULL)),
    CHECK ((status = 'failed') = (error_code IS NOT NULL)),
    CHECK (status = 'failed' OR error_stage IS NULL)
);

CREATE INDEX remember_operations_by_session ON mnemotrace.remember_operations
    (space_id, session_id, enqueue_order DESC);
CREATE INDEX remember_operations_pending_by_session ON mnemotrace.remember_operations
    (space_id, session_id, enqueue_order) WHERE status IN ('queued', 'running');
CREATE INDEX remember_operations_pending ON mnemotrace.remember_operations
    (enqueue_order) WHERE status IN ('queued', 'running');
CREATE INDEX remember_operations_by_space_order
    ON mnemotrace.remember_operations (space_id, enqueue_order DESC);
CREATE INDEX remember_operations_by_status_order
    ON mnemotrace.remember_operations (space_id, status, enqueue_order DESC);

CREATE TABLE mnemotrace.fact_changes (
    space_id text COLLATE "C" NOT NULL,
    fact_id text COLLATE "C" NOT NULL,
    cursor bigint GENERATED ALWAYS AS IDENTITY,
    changed_at mnemotrace.epoch_seconds NOT NULL,
    before jsonb,
    after jsonb NOT NULL,
    source_message_ids text[] NOT NULL,
    operation_id text COLLATE "C",
    PRIMARY KEY (space_id, fact_id, cursor),
    FOREIGN KEY (space_id, fact_id) REFERENCES mnemotrace.facts ON DELETE CASCADE,
    CONSTRAINT fact_changes_operation_in_space
        FOREIGN KEY (space_id, operation_id)
        REFERENCES mnemotrace.remember_operations (space_id, operation_id)
        ON DELETE SET NULL (operation_id)
);

CREATE INDEX fact_changes_sources
    ON mnemotrace.fact_changes USING gin (source_message_ids);
CREATE INDEX fact_changes_by_space_cursor ON mnemotrace.fact_changes (space_id, cursor);
CREATE INDEX fact_changes_by_operation
    ON mnemotrace.fact_changes (operation_id, space_id, cursor DESC)
    WHERE operation_id IS NOT NULL;

CREATE TABLE mnemotrace.fact_change_triggers (
    space_id text COLLATE "C" NOT NULL,
    fact_id text COLLATE "C" NOT NULL,
    cursor bigint NOT NULL,
    position integer NOT NULL CHECK (position >= 0),
    trigger_fact_id text COLLATE "C" NOT NULL,
    valid_end mnemotrace.epoch_seconds NOT NULL,
    source_message_ids text[] NOT NULL,
    PRIMARY KEY (space_id, fact_id, cursor, position),
    FOREIGN KEY (space_id, fact_id, cursor)
        REFERENCES mnemotrace.fact_changes ON DELETE CASCADE,
    FOREIGN KEY (space_id, trigger_fact_id) REFERENCES mnemotrace.facts
);

CREATE INDEX fact_change_triggers_cause
    ON mnemotrace.fact_change_triggers (space_id, trigger_fact_id);

CREATE TABLE mnemotrace.space_purge_keys (
    space_id text COLLATE "C" REFERENCES mnemotrace.spaces ON DELETE CASCADE,
    key_hash bytea NOT NULL CHECK (octet_length(key_hash) = 32),
    PRIMARY KEY (space_id, key_hash)
);

CREATE TABLE mnemotrace.profiles (
    space_id text COLLATE "C" PRIMARY KEY REFERENCES mnemotrace.spaces,
    data jsonb NOT NULL,
    updated_at mnemotrace.epoch_seconds NOT NULL
);

CREATE TABLE mnemotrace.profile_refreshes (
    space_id text COLLATE "C" PRIMARY KEY REFERENCES mnemotrace.spaces ON DELETE CASCADE,
    request_version bigint NOT NULL DEFAULT 1 CHECK (request_version > 0),
    status text NOT NULL DEFAULT 'queued'
        CHECK (status IN ('idle', 'queued', 'running', 'succeeded', 'failed')),
    available_at timestamptz DEFAULT statement_timestamp(),
    attempts integer NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    lease_token text,
    lease_until timestamptz,
    error_code text,
    last_success_fact_cursor bigint CHECK (last_success_fact_cursor >= 0),
    trigger_kind text NOT NULL DEFAULT 'manual' CHECK (trigger_kind IN ('manual', 'auto')),
    auto_started boolean NOT NULL DEFAULT false,
    followup_at timestamptz,
    CHECK ((status = 'queued') = (available_at IS NOT NULL)),
    CHECK ((status = 'running') = (lease_token IS NOT NULL)),
    CHECK ((status = 'running') = (lease_until IS NOT NULL)),
    CHECK ((status = 'failed') = (error_code IS NOT NULL)),
    CHECK (followup_at IS NULL OR status IN ('queued', 'running'))
);

CREATE INDEX profile_refreshes_pending ON mnemotrace.profile_refreshes
    ((COALESCE(available_at, lease_until)), space_id)
    WHERE status IN ('queued', 'running');

CREATE TABLE mnemotrace.session_summaries (
    space_id text COLLATE "C" NOT NULL,
    session_id text COLLATE "C" NOT NULL,
    summary_text text CHECK (char_length(summary_text) <= 2000),
    covered_through_seq bigint CHECK (covered_through_seq > 0),
    updated_at timestamptz,
    eligible_through_seq bigint NOT NULL CHECK (eligible_through_seq > 0),
    target_seq bigint,
    status text NOT NULL CHECK (status IN ('idle', 'queued', 'running', 'failed')),
    available_at timestamptz,
    followup_at timestamptz,
    attempts integer NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    lease_token text,
    lease_until timestamptz,
    error_code text,
    PRIMARY KEY (space_id, session_id),
    FOREIGN KEY (space_id, session_id) REFERENCES mnemotrace.sessions ON DELETE CASCADE,
    CHECK ((summary_text IS NULL) = (covered_through_seq IS NULL)),
    CHECK ((updated_at IS NULL) = (covered_through_seq IS NULL)),
    CHECK (covered_through_seq <= eligible_through_seq),
    CHECK (target_seq > COALESCE(covered_through_seq, 0) AND target_seq <= eligible_through_seq),
    CHECK (target_seq IS NULL OR status IN ('queued', 'running')),
    CHECK (followup_at IS NULL OR target_seq IS NOT NULL),
    CHECK ((status = 'queued') = (available_at IS NOT NULL)),
    CHECK ((status = 'running') = (lease_token IS NOT NULL)),
    CHECK ((status = 'running') = (lease_until IS NOT NULL)),
    CHECK (status <> 'running' OR target_seq IS NOT NULL),
    CHECK ((status = 'failed') = (error_code IS NOT NULL))
);

CREATE INDEX session_summaries_pending ON mnemotrace.session_summaries
    ((COALESCE(available_at, lease_until)), space_id, session_id)
    WHERE status IN ('queued', 'running');
