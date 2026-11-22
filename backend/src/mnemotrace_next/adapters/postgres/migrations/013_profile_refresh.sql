CREATE TABLE mnemotrace_next.profile_refreshes (
    space_id text COLLATE "C" PRIMARY KEY REFERENCES mnemotrace_next.spaces ON DELETE CASCADE,
    request_version bigint NOT NULL DEFAULT 1 CHECK (request_version > 0),
    status text NOT NULL DEFAULT 'queued'
        CHECK (status IN ('queued', 'running', 'succeeded', 'failed')),
    available_at timestamptz DEFAULT statement_timestamp(),
    attempts integer NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    lease_token text,
    lease_until timestamptz,
    error_code text,
    last_success_fact_cursor bigint CHECK (last_success_fact_cursor >= 0),
    CHECK ((status = 'queued') = (available_at IS NOT NULL)),
    CHECK ((status = 'running') = (lease_token IS NOT NULL)),
    CHECK ((status = 'running') = (lease_until IS NOT NULL)),
    CHECK ((status = 'failed') = (error_code IS NOT NULL))
);

CREATE INDEX profile_refreshes_pending ON mnemotrace_next.profile_refreshes
    ((COALESCE(available_at, lease_until)), space_id)
    WHERE status IN ('queued', 'running');

CREATE INDEX fact_changes_by_space_cursor ON mnemotrace_next.fact_changes (space_id, cursor);
