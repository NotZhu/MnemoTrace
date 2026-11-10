CREATE TABLE mnemotrace.record_commit_receipts (
    user_id mnemotrace.identifier NOT NULL,
    space_id mnemotrace.identifier NOT NULL,
    operation_id mnemotrace.identifier NOT NULL,
    core_revision mnemotrace.counter NOT NULL,
    fact_ids text[] NOT NULL,
    source_ids text[] NOT NULL,
    affected_count mnemotrace.counter NOT NULL,
    PRIMARY KEY (user_id, space_id, operation_id),
    FOREIGN KEY (user_id, space_id) REFERENCES mnemotrace.memory_spaces(user_id, space_id) ON DELETE CASCADE
);
ALTER TABLE mnemotrace.record_commit_receipts ENABLE ROW LEVEL SECURITY;
ALTER TABLE mnemotrace.record_commit_receipts FORCE ROW LEVEL SECURITY;
GRANT SELECT, INSERT, DELETE ON mnemotrace.record_commit_receipts TO {runtime};
GRANT SELECT, DELETE ON mnemotrace.record_commit_receipts TO {admin};
CREATE POLICY migration ON mnemotrace.record_commit_receipts TO {migration}
    USING (true) WITH CHECK (true);
CREATE POLICY account_read ON mnemotrace.record_commit_receipts FOR SELECT TO {admin} USING (true);
CREATE POLICY account_delete ON mnemotrace.record_commit_receipts FOR DELETE TO {admin} USING (true);
CREATE POLICY user_scope ON mnemotrace.record_commit_receipts TO {runtime}
    USING (user_id::text = nullif(current_setting('mnemotrace.user_id', true), ''))
    WITH CHECK (user_id::text = nullif(current_setting('mnemotrace.user_id', true), ''));

ALTER TABLE mnemotrace.facts ADD COLUMN subject_key text;
ALTER TABLE mnemotrace.facts ADD COLUMN predicate_key text;
ALTER TABLE mnemotrace.facts ADD COLUMN subject_match_key text;
CREATE INDEX facts_subject_normalized ON mnemotrace.facts
    (user_id, space_id, coalesce(subject_match_key, lower(btrim(subject))), id);
CREATE INDEX facts_slot_normalized ON mnemotrace.facts
    (user_id, space_id, coalesce(subject_key, lower(subject)), coalesce(predicate_key, lower(predicate)), id);
ALTER TABLE mnemotrace.fact_sources ADD COLUMN source_order mnemotrace.counter;
CREATE INDEX facts_predecessor ON mnemotrace.facts(user_id, space_id, supersedes);
CREATE INDEX operation_sources_source ON mnemotrace.operation_sources(user_id, space_id, source_id);
