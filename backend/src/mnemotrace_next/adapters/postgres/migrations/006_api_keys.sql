CREATE TABLE mnemotrace_next.api_keys (
    key_id text COLLATE "C" PRIMARY KEY,
    owner_id text COLLATE "C" NOT NULL,
    secret_hash bytea NOT NULL CHECK (octet_length(secret_hash) = 32),
    created_at timestamptz NOT NULL DEFAULT statement_timestamp(),
    revoked_at timestamptz
);

CREATE INDEX api_keys_by_owner ON mnemotrace_next.api_keys (owner_id, key_id);
CREATE INDEX spaces_by_owner ON mnemotrace_next.spaces (owner_id, space_id);
