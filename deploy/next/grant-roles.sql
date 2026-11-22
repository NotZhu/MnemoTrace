BEGIN;
GRANT USAGE ON SCHEMA mnemotrace_next TO :"runtime_role", :"admin_role";

GRANT SELECT (key_id, owner_id, created_at, revoked_at),
    INSERT (key_id, owner_id, secret_hash), UPDATE (revoked_at)
    ON mnemotrace_next.api_keys TO :"admin_role";

GRANT SELECT ON mnemotrace_next.schema_migrations TO :"runtime_role";
GRANT SELECT (key_id, owner_id, secret_hash, revoked_at) ON mnemotrace_next.api_keys
    TO :"runtime_role";
GRANT SELECT, INSERT, UPDATE (purge_generation, name, memory_decay) ON mnemotrace_next.spaces
    TO :"runtime_role";
GRANT DELETE ON mnemotrace_next.spaces, mnemotrace_next.embedding_spaces
    TO :"runtime_role";
GRANT SELECT, INSERT ON mnemotrace_next.space_purge_keys
    TO :"runtime_role";
GRANT SELECT, INSERT, UPDATE ON mnemotrace_next.remember_operations
    TO :"runtime_role";
GRANT SELECT, INSERT, DELETE ON
    mnemotrace_next.messages, mnemotrace_next.fact_provenance,
    mnemotrace_next.sessions, mnemotrace_next.fact_changes TO :"runtime_role";
GRANT UPDATE (operation_id) ON mnemotrace_next.fact_changes TO :"runtime_role";
GRANT SELECT, INSERT ON
    mnemotrace_next.embedding_spaces, mnemotrace_next.fact_change_triggers
    TO :"runtime_role";
GRANT SELECT, INSERT, UPDATE, DELETE ON
    mnemotrace_next.entities, mnemotrace_next.facts, mnemotrace_next.digests,
    mnemotrace_next.profiles, mnemotrace_next.fact_salience,
    mnemotrace_next.fact_vectors TO :"runtime_role";
GRANT USAGE ON ALL SEQUENCES IN SCHEMA mnemotrace_next TO :"runtime_role";
COMMIT;
