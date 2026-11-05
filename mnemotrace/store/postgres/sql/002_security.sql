DO $policies$
DECLARE relation record;
BEGIN
    FOR relation IN SELECT c.relname FROM pg_catalog.pg_class c
        JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname = 'mnemotrace' AND c.relkind = 'r' AND c.relname <> 'schema_migrations'
    LOOP
        EXECUTE format('ALTER TABLE mnemotrace.%I ENABLE ROW LEVEL SECURITY', relation.relname);
        EXECUTE format('ALTER TABLE mnemotrace.%I FORCE ROW LEVEL SECURITY', relation.relname);
        EXECUTE format('CREATE POLICY migration ON mnemotrace.%I TO {migration} USING (true) WITH CHECK (true)', relation.relname);
        EXECUTE format('CREATE POLICY account_read ON mnemotrace.%I FOR SELECT TO {admin} USING (true)', relation.relname);
        EXECUTE format('CREATE POLICY account_delete ON mnemotrace.%I FOR DELETE TO {admin} USING (true)', relation.relname);
        IF relation.relname NOT IN ('users', 'api_keys') THEN
            EXECUTE format('GRANT SELECT, INSERT, UPDATE, DELETE ON mnemotrace.%I TO {runtime}', relation.relname);
            EXECUTE format('CREATE POLICY user_scope ON mnemotrace.%I TO {runtime}
                USING (user_id::text = nullif(current_setting(''mnemotrace.user_id'', true), ''''))
                WITH CHECK (user_id::text = nullif(current_setting(''mnemotrace.user_id'', true), ''''))', relation.relname);
        END IF;
    END LOOP;
END
$policies$;
CREATE POLICY own_user ON mnemotrace.users FOR SELECT TO {runtime}
    USING (user_id::text = nullif(current_setting('mnemotrace.user_id', true), ''));
-- Row locking also applies UPDATE USING. WITH CHECK false allows locking the
-- owner's row without allowing the runtime to change account status.
CREATE POLICY own_user_lock ON mnemotrace.users FOR UPDATE TO {runtime}
    USING (user_id::text = nullif(current_setting('mnemotrace.user_id', true), ''))
    WITH CHECK (false);
CREATE POLICY account_insert_user ON mnemotrace.users FOR INSERT TO {admin} WITH CHECK (true);
CREATE POLICY account_update_user ON mnemotrace.users FOR UPDATE TO {admin} USING (true) WITH CHECK (true);
CREATE POLICY account_insert_key ON mnemotrace.api_keys FOR INSERT TO {admin} WITH CHECK (true);
CREATE POLICY account_update_key ON mnemotrace.api_keys FOR UPDATE TO {admin} USING (true) WITH CHECK (true);
CREATE POLICY auth_users ON mnemotrace.users FOR SELECT TO {auth} USING (true);
CREATE POLICY auth_keys ON mnemotrace.api_keys FOR SELECT TO {auth} USING (true);
