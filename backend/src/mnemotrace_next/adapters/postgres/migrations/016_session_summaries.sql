ALTER TABLE mnemotrace_next.spaces
    ADD COLUMN session_summary_enabled boolean NOT NULL DEFAULT false;

DROP TABLE mnemotrace_next.digests;

CREATE TABLE mnemotrace_next.session_summaries (
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
    FOREIGN KEY (space_id, session_id) REFERENCES mnemotrace_next.sessions ON DELETE CASCADE,
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

CREATE INDEX session_summaries_pending ON mnemotrace_next.session_summaries
    ((COALESCE(available_at, lease_until)), space_id, session_id)
    WHERE status IN ('queued', 'running');
