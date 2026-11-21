CREATE TABLE mnemotrace_next.remember_operations (
    operation_id text COLLATE "C" PRIMARY KEY,
    space_id text COLLATE "C" NOT NULL,
    session_id text COLLATE "C" NOT NULL,
    enqueue_order bigint GENERATED ALWAYS AS IDENTITY,
    key_hash bytea NOT NULL CHECK (octet_length(key_hash) = 32),
    request_hash bytea NOT NULL CHECK (octet_length(request_hash) = 32),
    messages jsonb,
    erasure_token text NOT NULL,
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
    FOREIGN KEY (space_id, session_id)
        REFERENCES mnemotrace_next.sessions (space_id, session_id) ON DELETE CASCADE,
    CHECK ((status IN ('queued', 'running')) = (messages IS NOT NULL)),
    CHECK ((status = 'running') = (lease_token IS NOT NULL)),
    CHECK ((status = 'running') = (lease_until IS NOT NULL)),
    CHECK (status = 'queued' OR next_retry_at IS NULL),
    CHECK ((status = 'succeeded') = (message_ids IS NOT NULL)),
    CHECK ((status = 'succeeded') = (fact_change_count IS NOT NULL)),
    CHECK ((status = 'failed') = (error_code IS NOT NULL)),
    CHECK (status = 'failed' OR error_stage IS NULL)
);

CREATE INDEX remember_operations_by_session ON mnemotrace_next.remember_operations
    (space_id, session_id);
CREATE INDEX remember_operations_pending_by_session ON mnemotrace_next.remember_operations
    (space_id, session_id, enqueue_order) WHERE status IN ('queued', 'running');
CREATE INDEX remember_operations_pending ON mnemotrace_next.remember_operations
    (enqueue_order) WHERE status IN ('queued', 'running');
