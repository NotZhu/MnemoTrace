CREATE TABLE mnemotrace_next.fact_change_triggers (
    space_id text COLLATE "C" NOT NULL,
    fact_id text COLLATE "C" NOT NULL,
    cursor bigint NOT NULL,
    position integer NOT NULL CHECK (position >= 0),
    trigger_fact_id text COLLATE "C" NOT NULL,
    valid_end double precision NOT NULL,
    source_message_ids text[],
    PRIMARY KEY (space_id, fact_id, cursor, position),
    FOREIGN KEY (space_id, fact_id, cursor)
        REFERENCES mnemotrace_next.fact_changes ON DELETE CASCADE,
    FOREIGN KEY (space_id, trigger_fact_id) REFERENCES mnemotrace_next.facts
);

CREATE INDEX fact_change_triggers_cause
    ON mnemotrace_next.fact_change_triggers (space_id, trigger_fact_id);

INSERT INTO mnemotrace_next.fact_change_triggers
    (space_id, fact_id, cursor, position, trigger_fact_id, valid_end, source_message_ids)
SELECT space_id, fact_id, cursor, 0, trigger_fact_id, (after ->> 'invalid_at')::double precision, NULL
FROM mnemotrace_next.fact_changes
WHERE trigger_fact_id IS NOT NULL;

ALTER TABLE mnemotrace_next.fact_changes DROP COLUMN trigger_fact_id;
