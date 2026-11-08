ALTER TABLE mnemotrace.facts
    ADD COLUMN sensitive_override boolean NOT NULL DEFAULT false,
    ADD COLUMN review_pending boolean NOT NULL DEFAULT false,
    ADD COLUMN approved_at timestamptz,
    ADD CHECK (NOT review_pending OR approved_at IS NULL);

-- Old extraction checkpoints have no sensitivity or time verdicts.
UPDATE mnemotrace.operations o
SET state = 'pending', phase = 'generate', extraction_saved = false,
    extraction_config = NULL, context_revision = NULL,
    lease_token = NULL, lease_until = NULL, next_attempt_at = NULL,
    error_code = NULL, generation_attempts = 0, embed_attempts = 0,
    ready_at = statement_timestamp()
WHERE o.kind = 'extract' AND NOT o.core_committed AND o.extraction_saved
  AND o.state IN ('pending','retry_wait','running','failed');
DELETE FROM mnemotrace.operation_products p USING mnemotrace.operations o
WHERE (p.user_id,p.space_id,p.operation_id) = (o.user_id,o.space_id,o.operation_id)
  AND o.kind = 'extract' AND NOT o.extraction_saved;
