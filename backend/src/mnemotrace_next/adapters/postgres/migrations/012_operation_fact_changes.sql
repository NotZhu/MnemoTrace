ALTER TABLE mnemotrace_next.remember_operations
    ADD CONSTRAINT remember_operations_space_operation_key UNIQUE (space_id, operation_id);

ALTER TABLE mnemotrace_next.fact_changes
    ADD COLUMN operation_id text COLLATE "C",
    ADD CONSTRAINT fact_changes_operation_in_space
        FOREIGN KEY (space_id, operation_id)
        REFERENCES mnemotrace_next.remember_operations (space_id, operation_id)
        ON DELETE SET NULL (operation_id);

CREATE INDEX fact_changes_by_operation
    ON mnemotrace_next.fact_changes (operation_id, space_id, cursor DESC)
    WHERE operation_id IS NOT NULL;

CREATE INDEX remember_operations_by_space_order
    ON mnemotrace_next.remember_operations (space_id, enqueue_order DESC);

CREATE INDEX remember_operations_by_status_order
    ON mnemotrace_next.remember_operations (space_id, status, enqueue_order DESC);

DROP INDEX mnemotrace_next.remember_operations_by_session;
CREATE INDEX remember_operations_by_session
    ON mnemotrace_next.remember_operations (space_id, session_id, enqueue_order DESC);
