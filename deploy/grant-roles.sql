BEGIN;
GRANT USAGE ON SCHEMA mnemotrace TO :"runtime_role", :"admin_role";

GRANT SELECT (key_id, owner_id, created_at, revoked_at),
    INSERT (key_id, owner_id, secret_hash), UPDATE (revoked_at)
    ON mnemotrace.api_keys TO :"admin_role";

GRANT SELECT ON mnemotrace.schema_migrations TO :"runtime_role";
GRANT SELECT (owner_id, secret_hash, revoked_at) ON mnemotrace.api_keys
    TO :"runtime_role";
GRANT SELECT, INSERT, UPDATE (purge_generation, name, memory_decay, profile_auto_refresh,
    profile_next_transition_at, session_summary_enabled, fact_revision)
    ON mnemotrace.spaces
    TO :"runtime_role";
GRANT DELETE ON mnemotrace.spaces, mnemotrace.embedding_spaces
    TO :"runtime_role";
GRANT SELECT, INSERT ON mnemotrace.space_purge_keys
    TO :"runtime_role";
GRANT SELECT, INSERT, UPDATE ON mnemotrace.remember_operations
    TO :"runtime_role";
GRANT SELECT, INSERT, DELETE ON
    mnemotrace.messages, mnemotrace.fact_provenance,
    mnemotrace.sessions, mnemotrace.fact_changes TO :"runtime_role";
GRANT UPDATE (operation_id) ON mnemotrace.fact_changes TO :"runtime_role";
GRANT SELECT, INSERT ON
    mnemotrace.embedding_spaces, mnemotrace.fact_change_triggers
    TO :"runtime_role";
GRANT SELECT, INSERT, UPDATE, DELETE ON
    mnemotrace.entities, mnemotrace.facts,
    mnemotrace.profiles, mnemotrace.profile_refreshes, mnemotrace.profile_group_refreshes, mnemotrace.fact_salience,
    mnemotrace.fact_vectors TO :"runtime_role";
GRANT SELECT, INSERT, UPDATE ON mnemotrace.session_summaries TO :"runtime_role";
GRANT USAGE ON ALL SEQUENCES IN SCHEMA mnemotrace TO :"runtime_role";
COMMIT;
