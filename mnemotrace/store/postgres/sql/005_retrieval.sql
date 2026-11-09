ALTER TABLE mnemotrace.lexical_documents
    ADD COLUMN field text NOT NULL DEFAULT 'text' CHECK (field IN ('text', 'digest')),
    ADD CHECK (field <> 'digest' OR source_id IS NOT NULL);
DO $$
DECLARE old_unique name;
BEGIN
    SELECT conname INTO STRICT old_unique FROM pg_constraint
    WHERE conrelid = 'mnemotrace.lexical_documents'::regclass AND contype = 'u';
    EXECUTE format('ALTER TABLE mnemotrace.lexical_documents DROP CONSTRAINT %I', old_unique);
END $$;
ALTER TABLE mnemotrace.lexical_documents
    ADD UNIQUE NULLS NOT DISTINCT(user_id, space_id, source_id, fact_id, entity_id, field);

CREATE INDEX embeddings_retrieval
    ON mnemotrace.embeddings(user_id, space_id, target_kind, field, model, dimension);
CREATE INDEX lexical_fact_version
    ON mnemotrace.lexical_documents(user_id, space_id, fact_id, field, content_version) WHERE fact_id IS NOT NULL;
CREATE INDEX lexical_source_version
    ON mnemotrace.lexical_documents(user_id, space_id, source_id, field, content_version) WHERE source_id IS NOT NULL;
CREATE INDEX operation_targets_source_version
    ON mnemotrace.operation_targets(user_id, space_id, source_id, content_version, operation_id) WHERE source_id IS NOT NULL;

ALTER TABLE mnemotrace.entities ADD COLUMN anchor_terms text[];
CREATE INDEX entity_anchor_terms ON mnemotrace.entities USING gin(anchor_terms);

ALTER TABLE mnemotrace.source_digests ADD COLUMN digest_written_at timestamptz;

ALTER TABLE mnemotrace.operations
    ADD COLUMN embedding_model mnemotrace.identifier,
    ADD COLUMN embedding_dimension integer CHECK (embedding_dimension > 0),
    ADD CHECK ((embedding_model IS NULL) = (embedding_dimension IS NULL)),
    ADD CHECK (embedding_model IS NULL OR kind = 'reembed');

DROP FUNCTION mnemotrace.discover_processing(integer);
CREATE FUNCTION mnemotrace.discover_processing(batch_size integer, model text DEFAULT NULL,
    dimension integer DEFAULT NULL, include_extraction boolean DEFAULT true)
RETURNS TABLE(user_id text, space_id text, operation_id text)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog AS $$
    SELECT o.user_id::text, o.space_id::text, o.operation_id::text
    FROM mnemotrace.operations o
    JOIN mnemotrace.users u ON u.user_id = o.user_id AND u.status = 'active'
    JOIN mnemotrace.memory_spaces s ON (s.user_id,s.space_id) = (o.user_id,o.space_id)
        AND s.status = 'active'
    WHERE ((include_extraction AND o.kind IN ('extract', 'digest'))
        OR (o.kind = 'reembed' AND o.embedding_model = model AND o.embedding_dimension = dimension)) AND (
        (o.state = 'pending' AND (o.phase <> 'queued' OR o.ready_at <= statement_timestamp()))
        OR (o.state = 'retry_wait' AND o.next_attempt_at <= statement_timestamp())
        OR (o.state = 'running' AND o.lease_until <= statement_timestamp()))
    AND (o.kind <> 'extract' OR NOT EXISTS (
        SELECT 1 FROM mnemotrace.operations earlier
        WHERE (earlier.user_id,earlier.space_id,earlier.session_id) =
            (o.user_id,o.space_id,o.session_id)
        AND earlier.kind = 'extract' AND earlier.sequence < o.sequence
        AND earlier.state IN ('pending','retry_wait','running')))
    ORDER BY (o.kind = 'reembed'), coalesce(o.next_attempt_at,o.ready_at,o.lease_until), o.operation_id
    LIMIT least(greatest(batch_size, 0), 100)
$$;
GRANT SELECT (embedding_model, embedding_dimension) ON mnemotrace.operations TO {discovery_owner};
GRANT CREATE ON SCHEMA mnemotrace TO {discovery_owner};
ALTER FUNCTION mnemotrace.discover_processing(integer, text, integer, boolean) OWNER TO {discovery_owner};
REVOKE CREATE ON SCHEMA mnemotrace FROM {discovery_owner};
REVOKE ALL ON FUNCTION mnemotrace.discover_processing(integer, text, integer, boolean) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION mnemotrace.discover_processing(integer, text, integer, boolean) TO {discovery};
