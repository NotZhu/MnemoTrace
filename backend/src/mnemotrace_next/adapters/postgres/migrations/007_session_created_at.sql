ALTER TABLE mnemotrace_next.sessions ADD COLUMN created_at timestamptz;
ALTER TABLE mnemotrace_next.sessions ALTER COLUMN created_at SET DEFAULT statement_timestamp();

CREATE INDEX sessions_by_creation ON mnemotrace_next.sessions
    (space_id, created_at DESC NULLS LAST, session_id);
