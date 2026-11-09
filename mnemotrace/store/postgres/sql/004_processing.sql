ALTER TABLE mnemotrace.operations
    ADD COLUMN ready_at timestamptz,
    ADD COLUMN frozen boolean NOT NULL DEFAULT false,
    ADD COLUMN generation_attempts mnemotrace.counter NOT NULL DEFAULT 0,
    ADD COLUMN parent_id mnemotrace.identifier,
    ADD FOREIGN KEY (user_id, space_id, parent_id)
        REFERENCES mnemotrace.operations ON DELETE CASCADE;
UPDATE mnemotrace.operations SET ready_at = statement_timestamp()
    WHERE kind IN ('extract', 'digest') AND phase = 'queued';
ALTER TABLE mnemotrace.operations ALTER COLUMN ready_at SET DEFAULT statement_timestamp();
ALTER TABLE mnemotrace.operations ADD CHECK (
    kind NOT IN ('extract', 'digest') OR phase <> 'queued' OR state <> 'pending' OR ready_at IS NOT NULL
);
ALTER TABLE mnemotrace.operation_sources
    ADD COLUMN is_context boolean NOT NULL DEFAULT false;
ALTER TABLE mnemotrace.operation_products
    ADD COLUMN input_hash text CHECK (input_hash ~ '^[0-9a-f]{64}$');
CREATE INDEX operations_parent ON mnemotrace.operations(user_id, space_id, parent_id);
CREATE INDEX operations_ready ON mnemotrace.operations(ready_at)
    WHERE state = 'pending' AND kind IN ('extract', 'digest');

ALTER TABLE mnemotrace.record_commit_receipts
    ADD COLUMN segment_of mnemotrace.identifier,
    ADD FOREIGN KEY (user_id, space_id, segment_of)
        REFERENCES mnemotrace.operations ON DELETE CASCADE;
CREATE INDEX record_commit_receipts_segment ON mnemotrace.record_commit_receipts(user_id, space_id, segment_of)
    WHERE segment_of IS NOT NULL;
GRANT UPDATE (fact_ids) ON mnemotrace.record_commit_receipts TO {runtime};

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
ALTER TABLE mnemotrace.operation_digest_parts ENABLE ROW LEVEL SECURITY;
ALTER TABLE mnemotrace.operation_digest_parts FORCE ROW LEVEL SECURITY;
GRANT SELECT, INSERT, UPDATE, DELETE ON mnemotrace.operation_digest_parts TO {runtime};
GRANT SELECT, DELETE ON mnemotrace.operation_digest_parts TO {admin};
CREATE POLICY migration ON mnemotrace.operation_digest_parts TO {migration} USING (true) WITH CHECK (true);
CREATE POLICY account_read ON mnemotrace.operation_digest_parts FOR SELECT TO {admin} USING (true);
CREATE POLICY account_delete ON mnemotrace.operation_digest_parts FOR DELETE TO {admin} USING (true);
CREATE POLICY user_scope ON mnemotrace.operation_digest_parts TO {runtime}
    USING (user_id::text = nullif(current_setting('mnemotrace.user_id', true), ''))
    WITH CHECK (user_id::text = nullif(current_setting('mnemotrace.user_id', true), ''));

GRANT USAGE ON SCHEMA mnemotrace TO {discovery}, {discovery_owner};
GRANT SELECT ON mnemotrace.schema_migrations TO {discovery};
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
    WHERE o.kind IN ('extract', 'digest') AND (
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
