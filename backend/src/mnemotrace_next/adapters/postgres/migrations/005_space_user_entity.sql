ALTER TABLE mnemotrace_next.entities
    ADD COLUMN kind text NOT NULL DEFAULT 'named',
    ADD CONSTRAINT entities_kind_check CHECK (kind IN ('named', 'space_user')),
    ADD CONSTRAINT entities_space_user_name_check
        CHECK (kind <> 'space_user' OR (name = '用户' AND name_key = '用户')),
    DROP CONSTRAINT entities_space_id_name_key_key,
    ADD CONSTRAINT entities_space_kind_name_key UNIQUE (space_id, kind, name_key);

CREATE UNIQUE INDEX entities_space_user_unique
    ON mnemotrace_next.entities (space_id) WHERE kind = 'space_user';
