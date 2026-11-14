ALTER TABLE mnemotrace.lexical_documents
    ADD COLUMN field text NOT NULL DEFAULT 'text' CHECK (field IN ('text', 'digest')),
    ADD CHECK ((field = 'digest') = (source_id IS NOT NULL));
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

ALTER TABLE mnemotrace.entities ADD COLUMN anchor_terms text[] NOT NULL;
CREATE INDEX entity_anchor_terms ON mnemotrace.entities USING gin(anchor_terms);

ALTER TABLE mnemotrace.source_digests ADD COLUMN digest_written_at timestamptz;
