CREATE TABLE mnemotrace_next.sessions (
    space_id text COLLATE "C" REFERENCES mnemotrace_next.spaces NOT NULL,
    session_id text COLLATE "C" NOT NULL,
    PRIMARY KEY (space_id, session_id)
);

INSERT INTO mnemotrace_next.sessions (space_id, session_id)
SELECT space_id, session_id FROM mnemotrace_next.messages
UNION
SELECT space_id, session_id FROM mnemotrace_next.digests;

ALTER TABLE mnemotrace_next.messages
    ADD FOREIGN KEY (space_id, session_id) REFERENCES mnemotrace_next.sessions;
ALTER TABLE mnemotrace_next.digests
    ADD FOREIGN KEY (space_id, session_id) REFERENCES mnemotrace_next.sessions;

ALTER TABLE mnemotrace_next.fact_provenance
    ADD COLUMN support_group integer NOT NULL DEFAULT 0 CHECK (support_group >= 0);
ALTER TABLE mnemotrace_next.fact_provenance
    ALTER COLUMN support_group DROP DEFAULT;

ALTER TABLE mnemotrace_next.fact_changes
    ADD COLUMN trigger_fact_id text COLLATE "C",
    ADD FOREIGN KEY (space_id, trigger_fact_id)
        REFERENCES mnemotrace_next.facts ON DELETE CASCADE;
CREATE INDEX fact_changes_trigger ON mnemotrace_next.fact_changes (space_id, trigger_fact_id)
    WHERE trigger_fact_id IS NOT NULL;

UPDATE mnemotrace_next.fact_changes
SET before = before - 'supersedes', after = after - 'supersedes'
WHERE before ? 'supersedes' OR after ? 'supersedes';

DROP TABLE mnemotrace_next.fact_supersedes;
