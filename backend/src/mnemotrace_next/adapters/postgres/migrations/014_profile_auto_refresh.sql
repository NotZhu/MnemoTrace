ALTER TABLE mnemotrace_next.spaces
    ADD COLUMN profile_auto_refresh boolean NOT NULL DEFAULT false;

ALTER TABLE mnemotrace_next.profile_refreshes
    DROP CONSTRAINT profile_refreshes_status_check,
    ADD CHECK (status IN ('idle', 'queued', 'running', 'succeeded', 'failed')),
    ADD COLUMN trigger_kind text NOT NULL DEFAULT 'manual'
        CHECK (trigger_kind IN ('manual', 'auto')),
    ADD COLUMN auto_started boolean NOT NULL DEFAULT false,
    ADD COLUMN followup_at timestamptz,
    ADD CHECK (followup_at IS NULL OR status IN ('queued', 'running'));
