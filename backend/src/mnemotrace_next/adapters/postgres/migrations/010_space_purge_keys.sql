CREATE TABLE mnemotrace_next.space_purge_keys (
    space_id text COLLATE "C" REFERENCES mnemotrace_next.spaces ON DELETE CASCADE,
    key_hash bytea NOT NULL CHECK (octet_length(key_hash) = 32),
    PRIMARY KEY (space_id, key_hash)
);
