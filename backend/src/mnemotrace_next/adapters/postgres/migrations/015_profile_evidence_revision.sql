ALTER TABLE mnemotrace_next.spaces
    ADD COLUMN profile_evidence_revision bigint NOT NULL DEFAULT 0;

CREATE INDEX facts_profile_activation
    ON mnemotrace_next.facts (space_id, subject_id, valid_at)
    WHERE source = 'extracted' AND NOT sensitive AND valid_at IS NOT NULL;
