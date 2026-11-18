ALTER TABLE mnemotrace_next.fact_salience
    ADD CONSTRAINT fact_salience_last_access_required
    CHECK (salience <= 1.0 OR last_access IS NOT NULL);
